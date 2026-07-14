from __future__ import annotations

import sqlite3
import json
import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .order_state import (
    ACTIVE_RESERVATION_STATES,
    BROKER_RELEVANT_STATES,
    TERMINAL_STATES,
    InvalidOrderTransition,
    OrderState,
    logical_action_key,
    stable_client_order_id,
    validate_transition,
)
from .risk_engine import RiskEngine
from .capabilities import require_autonomous_entry_support, require_autonomous_exit_support, require_protective_paper_exit_support
from .utils import iso_now, json_dumps
from .formula_versions import ACCOUNTING_VERSION, EVIDENCE_VERSION
from .quotes import implementation_shortfall_bps, validate_quote_payload


def _value(obj: Any, name: str, default: Any = None) -> Any:
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


def _winner_add_reservation_risk(
    proposal: dict[str, Any], quantity: float, reference: float, stop_price: float | None
) -> tuple[float, float]:
    incremental_risk = proposal.get("incremental_risk")
    if incremental_risk is None:
        raise ValueError("winner ADD requires canonical incremental-risk provenance")
    incremental_risk = float(incremental_risk)
    if not math.isfinite(incremental_risk):
        raise ValueError("winner ADD incremental risk must be finite")
    canonical_add_leg_risk = quantity * max(reference - float(stop_price or reference), 0.0)
    stated_add_leg_risk = proposal.get("pending_add_stop_risk")
    if stated_add_leg_risk is not None:
        stated_add_leg_risk = float(stated_add_leg_risk)
        if not math.isfinite(stated_add_leg_risk) or stated_add_leg_risk < 0:
            raise ValueError("winner ADD pending leg risk must be finite and nonnegative")
        if abs(stated_add_leg_risk - canonical_add_leg_risk) > 1e-6:
            raise ValueError("winner ADD pending leg risk does not match final quantity, price, and stop")
    return incremental_risk, canonical_add_leg_risk


@dataclass(frozen=True)
class ExecutionResult:
    submitted: bool
    status: str
    client_order_id: str | None
    broker_response: Any = None
    reason: str = ""
    intent_id: str | None = None


@dataclass(frozen=True)
class RecoveryResult:
    approvals_without_intents: int = 0
    intents_awaiting_submission: int = 0
    intents_awaiting_reconciliation: int = 0
    stale_submitted: int = 0
    terminal_with_reservations: int = 0


class DurableExecutionStore:
    """Transactional order-intent, event and reservation persistence.

    Network calls are deliberately absent from this class. Every mutating method
    commits before returning, so callers cannot accidentally hold SQLite locks
    while waiting for the broker.
    """

    def __init__(self, storage: Any) -> None:
        self.storage = storage
        try:
            rows = storage.fetch_all(
                "SELECT 1 AS present FROM schema_migrations WHERE version='phase0_execution_integrity_v1' LIMIT 1"
            )
        except sqlite3.Error as exc:
            raise RuntimeError("Phase 0 execution schema is unavailable; run Storage.initialize() before execution") from exc
        if not rows:
            raise RuntimeError("Phase 0 execution migration has not completed; broker submission is disabled")

    @staticmethod
    def _quantity_and_reference(proposal: dict[str, Any]) -> tuple[float, float, float | None, str, float | None]:
        prices = [proposal.get("latest_price"), proposal.get("reference_price"), proposal.get("limit_price")]
        valid_prices = [float(value) for value in prices if value is not None and float(value) > 0]
        if not valid_prices:
            raise ValueError("a positive conservative reference price is required")
        # For entries, the highest locally approved/observed price is the conservative
        # exposure reference. This avoids understating reserved notional.
        reference = max(valid_prices)
        quantity = proposal.get("qty")
        requested_notional = float(proposal.get("notional") or 0) or None
        request_basis = "quantity" if quantity is not None else "notional"
        if quantity is None:
            quantity = float(requested_notional or 0) / reference if reference > 0 else 0
        quantity = float(quantity)
        if quantity <= 0:
            raise ValueError("a positive final requested quantity is required")
        stop = proposal.get("stop_price", proposal.get("intended_stop_price"))
        stop_price = float(stop) if stop is not None and float(stop) > 0 else None
        return quantity, reference, stop_price, request_basis, requested_notional

    def create_or_get_intent(
        self,
        proposal: dict[str, Any],
        *,
        run_id: str | None,
        source_type: str,
        approval_id: str | None = None,
        sequence: int = 0,
    ) -> dict[str, Any]:
        if proposal.get("shadow_only") or proposal.get("observation_only") or proposal.get("research_only"):
            raise ValueError("shadow, observation-only and research-only records cannot create order intents")
        if str(proposal.get("trading_mode") or proposal.get("mode") or "paper") != "paper":
            raise PermissionError("durable execution supports paper mode only")
        expires_at = proposal.get("expires_at")
        if expires_at:
            expiry = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=UTC)
            if expiry.astimezone(UTC) <= datetime.now(UTC):
                raise ValueError("expired approval cannot create an order intent")
        symbol = str(proposal.get("symbol") or "").upper()
        side = str(proposal.get("side") or "").lower()
        if not symbol or side not in {"buy", "sell"}:
            raise ValueError("intent requires a symbol and buy/sell side")
        quantity, reference, stop_price, request_basis, requested_notional = self._quantity_and_reference(proposal)
        source_id = str(proposal.get("source_id") or proposal.get("proposal_id") or proposal.get("id") or "")
        if not source_id:
            raise ValueError("intent requires a stable proposal or emergency-action source ID")
        action = str(proposal.get("action") or proposal.get("intended_action") or ("exit" if side == "sell" else "entry"))
        keyed = {**proposal, "source_id": source_id, "symbol": symbol, "side": side, "action": action}
        action_key = logical_action_key(keyed, source_type, sequence)
        client_order_id = stable_client_order_id(action_key)
        reserved_notional = quantity * reference if side == "buy" else 0.0
        reserved_stop_risk = quantity * max(reference - stop_price, 0.0) if side == "buy" and stop_price else 0.0
        if side == "buy" and proposal.get("winner_expansion_decision_id"):
            incremental_risk, reserved_stop_risk = _winner_add_reservation_risk(
                proposal, quantity, reference, stop_price
            )
            if not proposal.get("pyramiding_milestone_id") or not proposal.get("pyramiding_milestone_key"):
                raise ValueError("winner ADD requires a durable pyramiding milestone")
            # The held-risk snapshot already reflects the final authoritative
            # stop. Reserve the ADD leg's full post-stop risk; the net
            # position delta remains separate audit provenance and may be
            # negative for a genuinely risk-neutral ADD.
        else:
            incremental_risk = reserved_stop_risk
        if side == "buy":
            ceiling_checks = (
                ("approved quantity", quantity, proposal.get("approved_quantity_ceiling")),
                ("approved notional", reserved_notional, proposal.get("approved_notional_ceiling")),
                ("approved stop-risk", reserved_stop_risk, proposal.get("approved_stop_risk_ceiling")),
            )
            for label, actual, raw_ceiling in ceiling_checks:
                if raw_ceiling is None:
                    continue
                try:
                    ceiling = float(raw_ceiling)
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"{label} ceiling must be numeric") from exc
                if not math.isfinite(ceiling) or ceiling < 0:
                    raise ValueError(f"{label} ceiling must be finite and nonnegative")
                if actual > ceiling + 1e-9:
                    raise RuntimeError(f"atomic reservation exceeds {label} ceiling")
        now = iso_now()
        intent_id = str(uuid.uuid4())
        event_id = str(uuid.uuid4())
        reservation_id = str(uuid.uuid4())
        with self.storage.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT * FROM order_intents WHERE logical_action_key=?", (action_key,)).fetchone()
            if existing:
                return dict(existing)
            if approval_id:
                workflow = conn.execute(
                    "SELECT state,intent_id FROM approval_workflows WHERE approval_id=?",
                    (approval_id,),
                ).fetchone()
                if not workflow:
                    raise RuntimeError("durable approval workflow is required before intent creation")
                if workflow["intent_id"]:
                    raise RuntimeError("approval workflow references an unavailable existing intent")
                if workflow["state"] != "approved_pending_intent":
                    raise RuntimeError("approval workflow is not eligible for intent creation")
            conflict = conn.execute(
                """SELECT id,state FROM order_intents
                   WHERE symbol=? AND side=? AND state IN (?,?,?,?,?,?,?,?) LIMIT 1""",
                (
                    symbol,
                    side,
                    OrderState.RESERVED.value,
                    OrderState.SUBMITTING.value,
                    OrderState.SUBMITTED.value,
                    OrderState.PARTIALLY_FILLED.value,
                    OrderState.CANCEL_PENDING.value,
                    OrderState.UNKNOWN.value,
                    OrderState.RECONCILIATION_REQUIRED.value,
                    OrderState.CREATED.value,
                ),
            ).fetchone()
            if conflict:
                raise RuntimeError(f"conflicting active order intent exists: {conflict['state']}")
            if side == "buy" and proposal.get("winner_expansion_decision_id"):
                winner_authority = conn.execute(
                    """SELECT 1 FROM add_risk_decisions
                       WHERE id=? AND proposal_id=? AND decision_stage='final_revalidation'
                         AND eligible=1 AND milestone_id=? AND milestone_key=?
                         AND ABS(incremental_risk-?)<=0.000000001 LIMIT 1""",
                    (
                        proposal["winner_expansion_decision_id"],
                        proposal.get("proposal_id") or proposal.get("id"),
                        proposal["pyramiding_milestone_id"],
                        proposal["pyramiding_milestone_key"],
                        incremental_risk,
                    ),
                ).fetchone()
                milestone_authority = conn.execute(
                    """SELECT 1 FROM pyramiding_milestones
                       WHERE id=? AND milestone_key=? AND active_proposal_id=?
                         AND status='APPROVED' LIMIT 1""",
                    (
                        proposal["pyramiding_milestone_id"],
                        proposal["pyramiding_milestone_key"],
                        proposal.get("proposal_id") or proposal.get("id"),
                    ),
                ).fetchone()
                if winner_authority is None or milestone_authority is None:
                    raise RuntimeError("winner ADD lacks final canonical risk and milestone authority")
            limits = proposal.get("_reservation_limits") or {}
            if side == "buy" and limits:
                totals = conn.execute(
                    """SELECT COALESCE(SUM(active_notional),0) total,
                              COALESCE(SUM(active_stop_risk),0) stop_risk
                       FROM risk_reservations WHERE state='active'"""
                ).fetchone()
                symbol_total = conn.execute(
                    "SELECT COALESCE(SUM(active_notional),0) n FROM risk_reservations WHERE state='active' AND symbol=?",
                    (symbol,),
                ).fetchone()["n"]
                cluster_total = 0.0
                if proposal.get("cluster_name"):
                    cluster_total = conn.execute(
                        "SELECT COALESCE(SUM(active_notional),0) n FROM risk_reservations WHERE state='active' AND cluster_name=?",
                        (proposal["cluster_name"],),
                    ).fetchone()["n"]

                def enforce(name: str, projected: float, ceiling_key: str) -> None:
                    ceiling = limits.get(ceiling_key)
                    if ceiling is not None and projected > float(ceiling) + 1e-9:
                        raise RuntimeError(f"atomic reservation blocked by {name}")

                enforce(
                    "total exposure ceiling",
                    float(limits.get("base_total_notional") or 0) + float(totals["total"]) + reserved_notional,
                    "total_notional_ceiling",
                )
                enforce(
                    "symbol exposure ceiling",
                    float(limits.get("base_symbol_notional") or 0) + float(symbol_total) + reserved_notional,
                    "symbol_notional_ceiling",
                )
                enforce(
                    "cluster exposure ceiling",
                    float(limits.get("base_cluster_notional") or 0) + float(cluster_total) + reserved_notional,
                    "cluster_notional_ceiling",
                )
                enforce(
                    "open risk ceiling",
                    float(limits.get("base_open_risk") or 0) + float(totals["stop_risk"]) + reserved_stop_risk,
                    "open_risk_ceiling",
                )
                enforce("paper buying power", float(totals["total"]) + reserved_notional, "buying_power_ceiling")
                if proposal.get("phase4_mode") == "probe":
                    probe_slots = conn.execute(
                        """SELECT COUNT(DISTINCT proposal_id) n FROM (
                               SELECT i.proposal_id proposal_id
                               FROM risk_reservations rr JOIN order_intents i ON i.id=rr.intent_id
                               JOIN trade_proposals p ON p.id=i.proposal_id
                               WHERE rr.state='active' AND p.strategy_state='PROBE'
                               UNION ALL
                               SELECT pl.entry_proposal_id proposal_id
                               FROM position_lots pl JOIN trade_proposals p ON p.id=pl.entry_proposal_id
                               WHERE pl.remaining_quantity>0 AND p.strategy_state='PROBE'
                           )"""
                    ).fetchone()["n"]
                    maximum = int(limits.get("probe_max_active_count", 1))
                    if int(probe_slots or 0) >= maximum:
                        raise RuntimeError("atomic reservation blocked by PROBE active-count ceiling")
                    probe_totals = conn.execute(
                        """SELECT
                               COALESCE((SELECT SUM(rr.active_notional) FROM risk_reservations rr
                                 JOIN order_intents i ON i.id=rr.intent_id JOIN trade_proposals p ON p.id=i.proposal_id
                                 WHERE rr.state='active' AND p.strategy_state='PROBE'),0)
                               + COALESCE((SELECT SUM(pl.remaining_quantity*pl.unit_cost) FROM position_lots pl
                                 JOIN trade_proposals p ON p.id=pl.entry_proposal_id
                                 WHERE pl.remaining_quantity>0 AND p.strategy_state='PROBE'),0) gross,
                               COALESCE((SELECT SUM(rr.active_stop_risk) FROM risk_reservations rr
                                 JOIN order_intents i ON i.id=rr.intent_id JOIN trade_proposals p ON p.id=i.proposal_id
                                 WHERE rr.state='active' AND p.strategy_state='PROBE'),0)
                               + COALESCE((SELECT SUM(pl.initial_risk_dollars*pl.remaining_quantity/pl.original_quantity) FROM position_lots pl
                                 JOIN trade_proposals p ON p.id=pl.entry_proposal_id
                                 WHERE pl.remaining_quantity>0 AND pl.initial_risk_dollars IS NOT NULL AND p.strategy_state='PROBE'),0) heat"""
                    ).fetchone()
                    enforce("PROBE gross-exposure ceiling", float(probe_totals["gross"] or 0) + reserved_notional, "probe_gross_notional_ceiling")
                    enforce("PROBE portfolio-heat ceiling", float(probe_totals["heat"] or 0) + reserved_stop_risk, "probe_stop_risk_ceiling")
            sleeve_fields_present = any(
                proposal.get(name) is not None
                for name in (
                    "strategy_registry_snapshot_id", "strategy_sleeve", "sleeve_allocation_id",
                    "sleeve_notional_ceiling", "sleeve_stop_risk_ceiling",
                )
            )
            if side == "buy" and (sleeve_fields_present or limits.get("require_strategy_sleeve") is True):
                required = {
                    "strategy_registry_snapshot_id": proposal.get("strategy_registry_snapshot_id"),
                    "strategy_sleeve": proposal.get("strategy_sleeve"),
                    "sleeve_allocation_id": proposal.get("sleeve_allocation_id"),
                    "sleeve_notional_ceiling": proposal.get("sleeve_notional_ceiling"),
                    "sleeve_stop_risk_ceiling": proposal.get("sleeve_stop_risk_ceiling"),
                    "strategy_version": proposal.get("strategy_version"),
                }
                missing = [name for name, value in required.items() if value in (None, "")]
                if missing:
                    raise RuntimeError("atomic strategy sleeve reservation missing " + ", ".join(sorted(missing)))
                registry_authority = conn.execute(
                    """SELECT 1 FROM strategy_registry_decisions
                       WHERE snapshot_id=? AND strategy_version=? AND authorized=1 AND run_id=? LIMIT 1""",
                    (proposal["strategy_registry_snapshot_id"], proposal["strategy_version"], run_id),
                ).fetchone()
                if registry_authority is None:
                    raise RuntimeError("atomic strategy sleeve reservation lacks registry authority")
                allocation = conn.execute(
                    """SELECT run_id,decided_at,payload FROM phase4_allocation_decisions
                       WHERE id=? AND run_id=? LIMIT 1""",
                    (proposal["sleeve_allocation_id"], run_id),
                ).fetchone()
                if allocation is None:
                    raise RuntimeError("atomic strategy sleeve reservation references an unknown current-run allocation")
                try:
                    allocation_payload = json.loads(allocation["payload"] or "{}")
                    canonical_sleeves = allocation_payload["strategy_sleeves"]
                    canonical_sleeve = canonical_sleeves[proposal["strategy_version"]]
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise RuntimeError("atomic strategy sleeve allocation payload is invalid") from exc
                if proposal["strategy_sleeve"] != proposal["strategy_version"]:
                    raise RuntimeError("atomic strategy sleeve identity does not match the strategy")
                if canonical_sleeve.get("strategy_version") != proposal["strategy_version"]:
                    raise RuntimeError("canonical allocation does not contain the requested strategy sleeve")
                if proposal["strategy_version"] not in set(allocation_payload.get("authorized_strategies") or []):
                    raise RuntimeError("canonical allocation does not authorize the requested strategy")
                if allocation_payload.get("registry_snapshot_id") != proposal["strategy_registry_snapshot_id"]:
                    raise RuntimeError("canonical allocation is not bound to the supplied registry snapshot")
                risk_unit = str(canonical_sleeve.get("risk_unit") or "")
                canonical_risk = float(canonical_sleeve.get("remaining_risk"))
                if risk_unit == "pct_equity":
                    replay = allocation_payload.get("raw_replay_inputs") or {}
                    portfolio_snapshot = replay.get("portfolio_snapshot") or {}
                    equity = float(portfolio_snapshot.get("portfolio_equity") or 0.0)
                    if not math.isfinite(equity) or equity <= 0:
                        raise RuntimeError("canonical sleeve equity conversion is unavailable")
                    canonical_risk = equity * canonical_risk / 100.0
                elif risk_unit != "stop_risk_dollars":
                    raise RuntimeError("canonical sleeve risk unit is unsupported")
                canonical_notional = float(canonical_sleeve.get("remaining_notional"))
                supplied_notional = float(proposal["sleeve_notional_ceiling"])
                supplied_risk = float(proposal["sleeve_stop_risk_ceiling"])
                if any(
                    not math.isfinite(value) or value < 0
                    for value in (canonical_risk, canonical_notional, supplied_notional, supplied_risk)
                ):
                    raise RuntimeError("canonical strategy sleeve ceilings must be finite and nonnegative")
                if supplied_notional > canonical_notional + 1e-9 or supplied_risk > canonical_risk + 1e-9:
                    raise RuntimeError("proposal-carried sleeve ceiling exceeds canonical persisted allocation")
                effective_notional_ceiling = min(canonical_notional, supplied_notional)
                effective_risk_ceiling = min(canonical_risk, supplied_risk)
                # The allocation snapshot persists the exact active reservation
                # IDs already deducted from canonical remaining capacity. Sum
                # every currently active strategy reservation *not* in that
                # immutable set. This coordinates overlapping allocations and
                # closes the read-to-persist race without timestamp ordering or
                # double-counting claims already present in the baseline.
                try:
                    snapshot = allocation_payload["raw_replay_inputs"]["portfolio_snapshot"]
                    by_strategy = snapshot["active_reservation_ids_by_strategy"]
                    pending_by_strategy = snapshot["pending_proposal_claims_by_strategy"]
                    if not isinstance(by_strategy, dict) or not isinstance(pending_by_strategy, dict):
                        raise TypeError("reservation and pending snapshots must be mappings")
                    included_ids = by_strategy.get(proposal["strategy_version"], [])
                    pending_claims = pending_by_strategy.get(proposal["strategy_version"], [])
                    if not isinstance(included_ids, list) or not isinstance(pending_claims, list):
                        raise TypeError("reservation snapshot IDs must be a mapping of lists")
                    included_ids = [str(identifier) for identifier in included_ids]
                    if any(not identifier for identifier in included_ids) or len(included_ids) != len(set(included_ids)):
                        raise ValueError("reservation snapshot IDs must be unique and nonempty")
                    pending_claim_map: dict[str, tuple[float, float]] = {}
                    for claim in pending_claims:
                        if not isinstance(claim, dict):
                            raise TypeError("pending claim snapshot rows must be mappings")
                        proposal_id = str(claim.get("proposal_id") or "")
                        claim_notional = float(claim.get("notional"))
                        claim_risk = float(claim.get("stop_risk"))
                        if (
                            not proposal_id or proposal_id in pending_claim_map
                            or not math.isfinite(claim_notional) or claim_notional < 0
                            or not math.isfinite(claim_risk) or claim_risk < 0
                        ):
                            raise ValueError("pending claim snapshot identity or amount is invalid")
                        pending_claim_map[proposal_id] = (claim_notional, claim_risk)
                except (KeyError, TypeError, ValueError, OverflowError) as exc:
                    raise RuntimeError("canonical strategy sleeve reservation snapshot is unavailable") from exc
                current_rows = conn.execute(
                    """SELECT rr.id,rr.active_notional,rr.active_stop_risk,i.proposal_id
                       FROM risk_reservations rr
                       LEFT JOIN order_intents i ON i.id=rr.intent_id
                       WHERE rr.state='active' AND rr.strategy_version=?""",
                    (proposal["strategy_version"],),
                ).fetchall()
                incremental_notional = 0.0
                incremental_risk = 0.0
                included_id_set = set(included_ids)
                for current_row in current_rows:
                    if str(current_row["id"]) in included_id_set:
                        continue
                    pending_notional, pending_risk = pending_claim_map.get(
                        str(current_row["proposal_id"] or ""), (0.0, 0.0)
                    )
                    incremental_notional += max(
                        0.0, float(current_row["active_notional"] or 0.0) - pending_notional
                    )
                    incremental_risk += max(
                        0.0, float(current_row["active_stop_risk"] or 0.0) - pending_risk
                    )
                candidate_pending_notional, candidate_pending_risk = pending_claim_map.get(
                    str(proposal.get("proposal_id") or proposal.get("id") or ""), (0.0, 0.0)
                )
                candidate_incremental_notional = max(0.0, reserved_notional - candidate_pending_notional)
                candidate_incremental_risk = max(0.0, reserved_stop_risk - candidate_pending_risk)
                if incremental_notional + candidate_incremental_notional > effective_notional_ceiling + 1e-9:
                    raise RuntimeError("atomic reservation blocked by strategy sleeve notional ceiling")
                if incremental_risk + candidate_incremental_risk > effective_risk_ceiling + 1e-9:
                    raise RuntimeError("atomic reservation blocked by strategy sleeve stop-risk ceiling")
            conn.execute(
                f"""INSERT INTO order_intents(
                       id,run_id,proposal_id,approval_id,source_id,source_type,logical_action_key,candidate_id,
                       position_lifecycle_id,symbol,side,intended_action,request_basis,approved_quantity_ceiling,
                       approved_notional_ceiling,requested_quantity,requested_notional,filled_quantity,reference_price,intended_stop_price,reserved_notional,
                       reserved_stop_risk,quote_bid,quote_ask,quote_timestamp,quote_spread_bps,limit_price,implementation_shortfall_bps,
                       client_order_id,trading_mode,state,created_at,updated_at,replacement_enabled,
                       parent_intent_id,relationship_group_id,relationship_type,order_role,protection_confirmed,
                       strategy_version,entry_regime,entry_score,initial_risk_dollars,config_hash,evidence_version,formula_version,
                       strategy_registry_snapshot_id,strategy_sleeve,sleeve_allocation_id,sleeve_notional_ceiling,
                       sleeve_stop_risk_ceiling,winner_expansion_decision_id,pyramiding_milestone_id,
                       pyramiding_milestone_key,management_mode,pre_add_open_risk,post_add_open_risk,
                       incremental_risk,rotation_step_id)
                   VALUES({','.join('?' for _ in range(59))})""",
                (
                    intent_id,
                    run_id,
                    proposal.get("proposal_id") or proposal.get("id"),
                    approval_id,
                    source_id,
                    source_type,
                    action_key,
                    proposal.get("candidate_id"),
                    proposal.get("position_lifecycle_id"),
                    symbol,
                    side,
                    action,
                    request_basis,
                    proposal.get("approved_quantity_ceiling", quantity),
                    proposal.get("approved_notional_ceiling", proposal.get("approved_notional", requested_notional)),
                    quantity,
                    requested_notional,
                    0.0,
                    reference,
                    stop_price,
                    reserved_notional,
                    reserved_stop_risk,
                    proposal.get("quote_bid"),
                    proposal.get("quote_ask"),
                    proposal.get("quote_timestamp"),
                    proposal.get("quote_spread_bps"),
                    proposal.get("limit_price"),
                    proposal.get("implementation_shortfall_bps"),
                    client_order_id,
                    "paper",
                    OrderState.RESERVED.value,
                    now,
                    now,
                    int(bool(proposal.get("replacement_enabled", False))),
                    proposal.get("parent_intent_id"),
                    proposal.get("relationship_group_id"),
                    proposal.get("relationship_type"),
                    proposal.get("order_role", "primary"),
                    int(bool(proposal.get("protection_confirmed", False))),
                    proposal.get("strategy_version"),
                    proposal.get("entry_regime", proposal.get("volatility_regime")),
                    proposal.get("entry_score", proposal.get("score")),
                    proposal.get("initial_risk_dollars"),
                    proposal.get("config_hash"),
                    proposal.get("evidence_version", EVIDENCE_VERSION),
                    proposal.get("formula_version", ACCOUNTING_VERSION),
                    proposal.get("strategy_registry_snapshot_id"),
                    proposal.get("strategy_sleeve"),
                    proposal.get("sleeve_allocation_id"),
                    proposal.get("sleeve_notional_ceiling"),
                    proposal.get("sleeve_stop_risk_ceiling"),
                    proposal.get("winner_expansion_decision_id"),
                    proposal.get("pyramiding_milestone_id"),
                    proposal.get("pyramiding_milestone_key"),
                    proposal.get("management_mode"),
                    proposal.get("pre_add_open_risk"),
                    proposal.get("post_add_open_risk"),
                    incremental_risk,
                    proposal.get("rotation_step_id"),
                ),
            )
            conn.execute(
                """INSERT INTO risk_reservations(
                       id,intent_id,symbol,cluster_name,initial_notional,active_notional,initial_stop_risk,
                       active_stop_risk,state,created_at,updated_at,strategy_version,strategy_sleeve,
                       sleeve_allocation_id,sleeve_notional_ceiling,sleeve_stop_risk_ceiling,incremental_risk,
                       risk_value,risk_unit,conversion_equity,conversion_equity_as_of,risk_formula_version)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    reservation_id,
                    intent_id,
                    symbol,
                    proposal.get("cluster_name"),
                    reserved_notional,
                    reserved_notional,
                    reserved_stop_risk,
                    reserved_stop_risk,
                    "active",
                    now,
                    now,
                    proposal.get("strategy_version"),
                    proposal.get("strategy_sleeve"),
                    proposal.get("sleeve_allocation_id"),
                    proposal.get("sleeve_notional_ceiling"),
                    proposal.get("sleeve_stop_risk_ceiling"),
                    incremental_risk,
                    reserved_stop_risk,
                    "stop_risk_dollars",
                    proposal.get("conversion_equity"),
                    proposal.get("conversion_equity_as_of") or now,
                    proposal.get("risk_formula_version") or "risk_unit_to_stop_risk_dollars_v1",
                ),
            )
            conn.execute(
                """INSERT INTO order_events(
                       id,intent_id,event_key,from_state,to_state,event_type,safe_detail,created_at,transition_counter)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    event_id,
                    intent_id,
                    f"{intent_id}:reserved:0",
                    None,
                    OrderState.RESERVED.value,
                    "intent_created_and_reserved",
                    json_dumps({"source_type": source_type, "reservation_committed_before_broker": True}),
                    now,
                    0,
                ),
            )
            conn.execute(
                """INSERT INTO orders(id,run_id,proposal_id,client_order_id,symbol,side,notional,qty,status,payload,
                       quote_bid,quote_ask,quote_timestamp,quote_spread_bps,limit_price,implementation_shortfall_bps,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(client_order_id) DO NOTHING""",
                (
                    intent_id,
                    run_id,
                    proposal.get("proposal_id") or proposal.get("id"),
                    client_order_id,
                    symbol,
                    side,
                    reserved_notional if side == "buy" else proposal.get("notional"),
                    quantity,
                    OrderState.RESERVED.value,
                    json_dumps({"intent_id": intent_id, "source_type": source_type,
                                "quote_bid": proposal.get("quote_bid"), "quote_ask": proposal.get("quote_ask"),
                                "quote_timestamp": proposal.get("quote_timestamp"), "quote_spread_bps": proposal.get("quote_spread_bps"),
                                "limit_price": proposal.get("limit_price")}),
                    proposal.get("quote_bid"), proposal.get("quote_ask"), proposal.get("quote_timestamp"),
                    proposal.get("quote_spread_bps"), proposal.get("limit_price"), proposal.get("implementation_shortfall_bps"),
                    now,
                    now,
                ),
            )
            from .profit_milestones import bind_take_profit_intent_in_transaction

            bound_intent = dict(
                conn.execute("SELECT * FROM order_intents WHERE id=?", (intent_id,)).fetchone()
            )
            bind_take_profit_intent_in_transaction(
                conn,
                intent=bound_intent,
                proposal=proposal,
                now=now,
            )
            if approval_id:
                changed = conn.execute(
                    """UPDATE approval_workflows SET intent_id=?,state='intent_created',updated_at=?,version=version+1
                       WHERE approval_id=? AND state='approved_pending_intent' AND intent_id IS NULL""",
                    (intent_id, now, approval_id),
                ).rowcount
                if changed != 1:
                    raise RuntimeError("approval workflow compare-and-swap lost during intent creation")
        return self.get_intent(intent_id)

    def get_intent(self, intent_id: str) -> dict[str, Any]:
        rows = self.storage.fetch_all("SELECT * FROM order_intents WHERE id=?", (intent_id,))
        if not rows:
            raise LookupError(f"order intent not found: {intent_id}")
        return rows[0]

    def transition(
        self,
        intent_id: str,
        target: OrderState,
        *,
        event_type: str,
        broker_order_id: str | None = None,
        error_category: str | None = None,
        safe_summary: str | None = None,
        expected_state: OrderState | None = None,
    ) -> dict[str, Any]:
        now = iso_now()
        with self.storage.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM order_intents WHERE id=?", (intent_id,)).fetchone()
            if not row:
                raise LookupError(f"order intent not found: {intent_id}")
            current = OrderState(row["state"])
            if expected_state is not None and current != expected_state:
                raise InvalidOrderTransition(f"expected {expected_state.value}, found {current.value}")
            source, destination = validate_transition(current, target)
            if source == destination:
                return dict(row)
            counter = int(row["transition_counter"] or 0) + 1
            first_submission = row["first_submission_at"]
            attempts = int(row["submission_attempt_count"] or 0)
            if destination == OrderState.SUBMITTING:
                attempts += 1
                first_submission = first_submission or now
            terminal_at = now if destination in TERMINAL_STATES else None
            conn.execute(
                """UPDATE order_intents SET state=?,broker_order_id=COALESCE(?,broker_order_id),updated_at=?,
                       first_submission_at=?,terminal_at=COALESCE(?,terminal_at),last_error_category=?,safe_error_summary=?,
                       submission_attempt_count=?,transition_counter=? WHERE id=?""",
                (
                    destination.value,
                    broker_order_id,
                    now,
                    first_submission,
                    terminal_at,
                    error_category,
                    safe_summary,
                    attempts,
                    counter,
                    intent_id,
                ),
            )
            conn.execute(
                """INSERT INTO order_events(id,intent_id,event_key,from_state,to_state,event_type,safe_detail,created_at,transition_counter)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    str(uuid.uuid4()),
                    intent_id,
                    f"{intent_id}:{destination.value}:{counter}",
                    source.value,
                    destination.value,
                    event_type,
                    json_dumps({"error_category": error_category, "summary": safe_summary}),
                    now,
                    counter,
                ),
            )
            conn.execute(
                "UPDATE orders SET broker_order_id=COALESCE(?,broker_order_id),status=?,updated_at=? WHERE id=?",
                (broker_order_id, destination.value, now, intent_id),
            )
            if destination in TERMINAL_STATES:
                conn.execute(
                    """UPDATE risk_reservations SET active_notional=0,active_stop_risk=0,state='released',
                       released_at=COALESCE(released_at,?),release_reason=COALESCE(release_reason,?),updated_at=?,version=version+1
                       WHERE intent_id=? AND state='active'""",
                    (now, destination.value, now, intent_id),
                )
                from .profit_milestones import apply_take_profit_terminal_state_in_transaction

                apply_take_profit_terminal_state_in_transaction(
                    conn,
                    order_intent_id=intent_id,
                    terminal_state=destination.value,
                    now=now,
                )
        return self.get_intent(intent_id)

    def record_fill(
        self,
        intent_id: str,
        *,
        cumulative_quantity: float,
        fill_price: float,
        broker_event_key: str,
        broker_order_id: str | None = None,
        occurred_at: str | None = None,
        fees: float = 0.0,
        adjustments: float = 0.0,
        source: str = "broker_fill",
        price_is_cumulative_average: bool = False,
    ) -> dict[str, Any]:
        cumulative_quantity = float(cumulative_quantity)
        fill_price = float(fill_price)
        if cumulative_quantity < 0 or fill_price < 0:
            raise ValueError("fill quantity and price cannot be negative")
        now = iso_now()
        with self.storage.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            intent = conn.execute("SELECT * FROM order_intents WHERE id=?", (intent_id,)).fetchone()
            if not intent:
                raise LookupError(f"order intent not found: {intent_id}")
            prior = conn.execute("SELECT 1 FROM broker_fill_events WHERE broker_event_key=?", (broker_event_key,)).fetchone()
            if prior:
                return dict(intent)
            requested = float(intent["requested_quantity"])
            previous = float(intent["filled_quantity"] or 0)
            if cumulative_quantity + 1e-9 < previous:
                # Retain/dedupe the stale broker event but never reduce quantity.
                counter = int(intent["transition_counter"] or 0) + 1
                conn.execute(
                    """INSERT INTO broker_fill_events(id,intent_id,broker_event_key,broker_order_id,cumulative_filled_quantity,
                           delta_quantity,fill_price,occurred_at,received_at,payload) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (str(uuid.uuid4()), intent_id, broker_event_key, broker_order_id, cumulative_quantity, 0.0, fill_price, occurred_at or now, now, json_dumps({"out_of_order": True, "retained_cumulative": previous})),
                )
                conn.execute(
                    """UPDATE pnl_ledger_status SET confidence='partially_reconstructed',
                           provenance='late lower cumulative broker event; authoritative history required',
                           last_event_at=?,updated_at=? WHERE scope='prospective'""",
                    (occurred_at or now, now),
                )
                conn.execute(
                    "INSERT INTO order_events(id,intent_id,event_key,from_state,to_state,event_type,safe_detail,created_at,transition_counter) VALUES(?,?,?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()), intent_id, f"{intent_id}:out_of_order_fill:{broker_event_key}", intent["state"], intent["state"], "out_of_order_fill_ignored", json_dumps({"reported": cumulative_quantity, "retained": previous}), now, counter),
                )
                conn.execute("UPDATE order_intents SET transition_counter=?,updated_at=? WHERE id=?", (counter, now, intent_id))
                return dict(intent)
            cumulative = min(cumulative_quantity, requested)
            delta = max(0.0, cumulative - previous)
            prior_avg = float(intent["average_fill_price"] or 0)
            delta_fill_price = fill_price
            if price_is_cumulative_average and delta > 0:
                delta_fill_price = max(0.0, ((cumulative * fill_price) - (previous * prior_avg)) / delta)
                average = fill_price
            else:
                average = ((previous * prior_avg) + (delta * fill_price)) / cumulative if cumulative > 0 else None
            quote = {
                "bid": intent["quote_bid"], "ask": intent["quote_ask"],
                "midpoint": ((float(intent["quote_bid"]) + float(intent["quote_ask"])) / 2.0)
                if intent["quote_bid"] is not None and intent["quote_ask"] is not None else None,
            }
            shortfall = implementation_shortfall_bps(quote, intent["side"], float(average or fill_price))
            current = OrderState(intent["state"])
            late_after_cancel = current == OrderState.CANCELLED and cumulative > previous
            target = current if late_after_cancel else (OrderState.FILLED if cumulative >= requested - 1e-9 else OrderState.PARTIALLY_FILLED)
            if current != target:
                validate_transition(current, target)
            counter = int(intent["transition_counter"] or 0) + 1
            fill_event_id = str(uuid.uuid4())
            conn.execute(
                """INSERT INTO broker_fill_events(id,intent_id,broker_event_key,broker_order_id,cumulative_filled_quantity,
                       delta_quantity,fill_price,occurred_at,received_at,payload) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (fill_event_id, intent_id, broker_event_key, broker_order_id, cumulative, delta, delta_fill_price, occurred_at or now, now, json_dumps({"aggregate": True, "reported_price": fill_price, "price_semantics": "cumulative_average" if price_is_cumulative_average else "delta_execution"})),
            )
            # Lot/P&L accounting shares the fill transaction: a crash cannot
            # commit quantity while omitting its prospective accounting event.
            from .lot_ledger import LotLedger

            LotLedger.apply_fill_in_transaction(
                conn,
                intent=intent,
                broker_event_key=broker_event_key,
                delta_quantity=delta,
                fill_price=delta_fill_price,
                occurred_at=occurred_at or now,
                fees=fees,
                adjustments=adjustments,
                source=source,
            )
            from .profit_milestones import apply_take_profit_fill_in_transaction

            apply_take_profit_fill_in_transaction(
                conn,
                intent=dict(intent),
                fill_event_id=fill_event_id,
                broker_event_key=broker_event_key,
                cumulative_quantity=cumulative,
                delta_quantity=delta,
                fill_price=delta_fill_price,
                occurred_at=occurred_at or now,
                now=now,
            )
            conn.execute(
                """UPDATE order_intents SET filled_quantity=?,average_fill_price=?,state=?,broker_order_id=COALESCE(?,broker_order_id),
                       implementation_shortfall_bps=?,
                       updated_at=?,terminal_at=?,transition_counter=? WHERE id=?""",
                (cumulative, average, target.value, broker_order_id, shortfall, now, now if target == OrderState.FILLED else None, counter, intent_id),
            )
            remaining_ratio = max(0.0, requested - cumulative) / requested
            reservation_state = "released" if target == OrderState.FILLED or late_after_cancel else "active"
            if late_after_cancel:
                remaining_ratio = 0.0
            conn.execute(
                """UPDATE risk_reservations SET active_notional=initial_notional*?,active_stop_risk=initial_stop_risk*?,
                       state=?,released_at=CASE WHEN ?='released' THEN COALESCE(released_at,?) ELSE released_at END,
                       release_reason=CASE WHEN ?='released' THEN COALESCE(release_reason,'filled') ELSE release_reason END,
                       updated_at=?,version=version+1 WHERE intent_id=?""",
                (remaining_ratio, remaining_ratio, reservation_state, reservation_state, now, reservation_state, now, intent_id),
            )
            conn.execute(
                "INSERT INTO order_events(id,intent_id,event_key,from_state,to_state,event_type,broker_event_id,filled_quantity,fill_price,safe_detail,created_at,transition_counter) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), intent_id, f"{intent_id}:fill:{broker_event_key}", current.value, target.value, "late_fill_after_cancelled" if late_after_cancel else ("final_fill" if target == OrderState.FILLED else "partial_fill"), broker_event_key, cumulative, delta_fill_price, json_dumps({"delta_quantity": delta}), now, counter),
            )
            conn.execute(
                "UPDATE orders SET broker_order_id=COALESCE(?,broker_order_id),status=?,implementation_shortfall_bps=?,updated_at=? WHERE id=?",
                (broker_order_id, target.value, shortfall, now, intent_id),
            )
            existing_fill = conn.execute("SELECT id FROM fills WHERE order_id=?", (intent_id,)).fetchone()
            if existing_fill:
                conn.execute(
                    """UPDATE fills SET qty=?,price=?,filled_at=?,payload=?,implementation_shortfall_bps=?,
                       fill_notified_at=CASE WHEN ?='filled' THEN NULL ELSE fill_notified_at END,
                       fill_notification_status=CASE WHEN ?='filled' THEN 'pending' ELSE fill_notification_status END,
                       fill_notification_error=CASE WHEN ?='filled' THEN NULL ELSE fill_notification_error END
                       WHERE order_id=?""",
                    (cumulative, average, occurred_at or now, json_dumps({"aggregate": True, "intent_id": intent_id}), shortfall, target.value, target.value, target.value, intent_id),
                )
            else:
                conn.execute(
                    "INSERT INTO fills(run_id,order_id,qty,price,filled_at,payload,implementation_shortfall_bps,fill_notification_status) VALUES(?,?,?,?,?,?,?,?)",
                    (intent["run_id"], intent_id, cumulative, average, occurred_at or now, json_dumps({"aggregate": True, "intent_id": intent_id}), shortfall, "pending"),
                )
        return self.get_intent(intent_id)

    def active_reservations(self) -> dict[str, Any]:
        rows = self.storage.fetch_all(
            "SELECT symbol,cluster_name,active_notional,active_stop_risk FROM risk_reservations WHERE state='active'"
        )
        by_symbol: dict[str, float] = {}
        by_cluster: dict[str, float] = {}
        for row in rows:
            by_symbol[row["symbol"]] = by_symbol.get(row["symbol"], 0.0) + float(row["active_notional"] or 0)
            if row.get("cluster_name"):
                by_cluster[row["cluster_name"]] = by_cluster.get(row["cluster_name"], 0.0) + float(row["active_notional"] or 0)
        return {
            "active_reserved_notional": sum(float(row["active_notional"] or 0) for row in rows),
            "active_reserved_stop_risk": sum(float(row["active_stop_risk"] or 0) for row in rows),
            "symbol_reserved_notional": by_symbol,
            "cluster_reserved_notional": by_cluster,
            "count": len(rows),
        }

    def recovery_sweep(self, stale_after_seconds: int = 300) -> RecoveryResult:
        # Read/diagnostic recovery is idempotent. It never submits or cancels.
        approvals = self.storage.fetch_all(
            """SELECT COUNT(*) AS n FROM approvals a
               LEFT JOIN order_intents i ON i.approval_id=a.id
               LEFT JOIN approval_workflows w ON w.approval_id=a.id
               WHERE a.consumed_at IS NOT NULL AND i.id IS NULL AND w.id IS NULL"""
        )[0]["n"]
        awaiting = self.storage.fetch_all(
            "SELECT COUNT(*) AS n FROM order_intents WHERE state IN ('created','reserved')"
        )[0]["n"]
        reconcile = self.storage.fetch_all(
            "SELECT COUNT(*) AS n FROM order_intents WHERE state IN ('unknown','reconciliation_required')"
        )[0]["n"]
        stale_submitted = self.storage.fetch_all(
            """SELECT COUNT(*) AS n FROM order_intents WHERE state IN ('submitting','submitted','partially_filled')
               AND (julianday('now')-julianday(updated_at))*86400 > ?""",
            (stale_after_seconds,),
        )[0]["n"]
        terminal_reserved = self.storage.fetch_all(
            """SELECT COUNT(*) AS n FROM order_intents i JOIN risk_reservations r ON r.intent_id=i.id
               WHERE i.state IN ('filled','cancelled','rejected','expired') AND r.state='active'"""
        )[0]["n"]
        return RecoveryResult(int(approvals), int(awaiting), int(reconcile), int(stale_submitted), int(terminal_reserved))

    def integrity_report(self) -> dict[str, int]:
        checks = {
            "orphaned_approvals": "SELECT COUNT(*) n FROM approvals a LEFT JOIN trade_proposals p ON p.id=a.proposal_id WHERE a.proposal_id IS NOT NULL AND p.id IS NULL",
            "approvals_without_intents": "SELECT COUNT(*) n FROM approvals a LEFT JOIN order_intents i ON i.approval_id=a.id LEFT JOIN approval_workflows w ON w.approval_id=a.id WHERE a.consumed_at IS NOT NULL AND i.id IS NULL AND w.id IS NULL",
            "intents_without_reservations": "SELECT COUNT(*) n FROM order_intents i LEFT JOIN risk_reservations r ON r.intent_id=i.id WHERE r.id IS NULL",
            "terminal_intents_with_active_reservations": "SELECT COUNT(*) n FROM order_intents i JOIN risk_reservations r ON r.intent_id=i.id WHERE i.state IN ('filled','cancelled','rejected','expired') AND r.state='active'",
            "active_intents_missing_reservations": "SELECT COUNT(*) n FROM order_intents i LEFT JOIN risk_reservations r ON r.intent_id=i.id WHERE i.state IN ('created','reserved','submitting','submitted','partially_filled','cancel_pending','unknown','reconciliation_required') AND r.id IS NULL",
            "duplicate_client_order_ids": "SELECT COUNT(*) n FROM (SELECT client_order_id FROM order_intents GROUP BY client_order_id HAVING COUNT(*)>1)",
            "fills_exceeding_quantity": "SELECT COUNT(*) n FROM order_intents WHERE filled_quantity>requested_quantity+0.000000001",
            "stale_unknown_intents": "SELECT COUNT(*) n FROM order_intents WHERE state='unknown' AND (julianday('now')-julianday(updated_at))*86400>300",
            "stale_partial_fills": "SELECT COUNT(*) n FROM order_intents WHERE state='partially_filled' AND (julianday('now')-julianday(updated_at))*86400>300",
            "position_state_without_active_lifecycle": "SELECT COUNT(*) n FROM position_management_state s LEFT JOIN position_lifecycles l ON l.id=s.position_lifecycle_id AND l.state='active' WHERE s.position_lifecycle_id IS NOT NULL AND l.id IS NULL",
            "state_latest_event_mismatch": """SELECT COUNT(*) n FROM order_intents i
                WHERE COALESCE((SELECT e.to_state FROM order_events e WHERE e.intent_id=i.id
                                ORDER BY e.transition_counter DESC,e.created_at DESC LIMIT 1),'') <> i.state""",
            "transition_counter_mismatch": """SELECT COUNT(*) n FROM order_intents i
                WHERE COALESCE((SELECT MAX(e.transition_counter) FROM order_events e WHERE e.intent_id=i.id),-1)
                      <> i.transition_counter""",
            "fill_ledger_mismatch": """SELECT COUNT(*) n FROM order_intents i
                WHERE ABS(COALESCE((SELECT MAX(f.cumulative_filled_quantity) FROM broker_fill_events f
                                    WHERE f.intent_id=i.id),0)-COALESCE(i.filled_quantity,0))>0.000000001""",
            "broker_relevant_missing_identity": """SELECT COUNT(*) n FROM order_intents
                WHERE state IN ('submitting','submitted','partially_filled','cancel_pending','unknown','reconciliation_required')
                  AND COALESCE(client_order_id,'')='' AND COALESCE(broker_order_id,'')=''""",
        }
        return {name: int(self.storage.fetch_all(sql)[0]["n"]) for name, sql in checks.items()}


class Executor:
    def __init__(
        self,
        broker: Any,
        risk_engine: RiskEngine,
        storage: Any | None = None,
        run_id: str | None = None,
        fault_hook: Any | None = None,
        recovery_proven_no_submit: bool = False,
    ) -> None:
        self.broker = broker
        self.risk_engine = risk_engine
        self.storage = storage
        self.run_id = run_id
        self.fault_hook = fault_hook
        self.recovery_proven_no_submit = recovery_proven_no_submit

    def _fault(self, boundary: str, **detail: Any) -> None:
        if self.fault_hook is not None:
            self.fault_hook(boundary, detail)

    def execute(
        self,
        proposal: dict[str, Any],
        context: dict[str, Any],
        *,
        source_type: str = "proposal",
        approval_id: str | None = None,
    ) -> ExecutionResult:
        if str(proposal.get("trading_mode") or proposal.get("mode") or "paper") != "paper":
            return ExecutionResult(False, "blocked", None, reason="paper execution is the only supported execution mode")
        if source_type == "emergency":
            try:
                require_protective_paper_exit_support()
            except PermissionError as exc:
                return ExecutionResult(False, "blocked", None, reason=str(exc))
            if not approval_id:
                return ExecutionResult(False, "blocked", None, reason="manual approval is required for protective paper exits")
            if (
                str(proposal.get("side", "")).lower() != "sell"
                or int(proposal.get("emergency_exit_triggered") or 0) != 1
                or not proposal.get("emergency_exit_trigger_reason")
            ):
                return ExecutionResult(False, "blocked", None, reason="ordinary workflows cannot use the protective paper-exit path")
        elif proposal.get("autonomous_entry_requested") is True:
            try:
                require_autonomous_entry_support()
            except PermissionError as exc:
                return ExecutionResult(False, "blocked", None, reason=str(exc))
        elif proposal.get("autonomous_exit_requested") is True:
            try:
                require_autonomous_exit_support()
            except PermissionError as exc:
                return ExecutionResult(False, "blocked", None, reason=str(exc))
        else:
            quote_fields = ("quote_bid", "quote_ask", "quote_timestamp", "quote_spread_bps", "limit_price")
            if any(proposal.get(field) is None for field in quote_fields) or proposal.get("order_type") != "limit":
                return ExecutionResult(False, "blocked", None, reason="fresh validated quote and bounded limit price are required for normal orders")
            try:
                validate_quote_payload(
                    proposal,
                    str(proposal.get("side") or ""),
                    getattr(self.risk_engine, "config", {}) or {},
                    now=datetime.now(UTC),
                )
            except (TypeError, ValueError) as exc:
                return ExecutionResult(False, "blocked", None, reason=f"quote validation blocked: {exc}")
        if proposal.get("status") != "approved" or context.get("approval_valid") is not True:
            return ExecutionResult(False, "blocked", None, reason="validated approval required")
        if self.storage is None:
            return ExecutionResult(False, "blocked", None, reason="durable storage is required before broker submission")
        if self.broker is None:
            return ExecutionResult(False, "blocked", None, reason="broker client unavailable")

        action_key = logical_action_key(proposal, source_type)
        client_order_id = stable_client_order_id(action_key)
        candidate = {**proposal, "client_order_id": client_order_id, "trading_mode": "paper"}
        final_context = {**context, "final_revalidation": True}
        decision = self.risk_engine.evaluate(candidate, final_context, final=True)
        if not decision.passed:
            return ExecutionResult(False, "blocked", client_order_id, reason="; ".join(decision.reasons))

        workflow_store = None
        workflow = None
        if approval_id:
            # Persist the final local validation decision before intent creation.
            # create_or_get_intent then links this workflow in the same SQLite
            # transaction as the intent and reservation.
            from .approval_workflow import ApprovalWorkflowState, ApprovalWorkflowStore

            workflow_store = ApprovalWorkflowStore(self.storage)
            workflow = workflow_store.get_by_approval(approval_id)
            if workflow is None:
                return ExecutionResult(False, "blocked", client_order_id, reason="durable approval workflow is required")
            if workflow["state"] == ApprovalWorkflowState.VALIDATING.value:
                workflow_store.transition(
                    workflow["id"],
                    ApprovalWorkflowState.APPROVED_PENDING_INTENT,
                    expected_state=ApprovalWorkflowState.VALIDATING,
                    validation_status="passed",
                    safe_detail="final local validation passed",
                )
            elif not workflow.get("intent_id") and workflow["state"] != ApprovalWorkflowState.APPROVED_PENDING_INTENT.value:
                return ExecutionResult(False, "blocked", client_order_id, reason="approval workflow is not executable")

        # Risk evaluation uses a coherent snapshot, while this final reservation
        # check runs under BEGIN IMMEDIATE. The optional absolute ceilings close
        # the scanner/listener race between snapshot evaluation and persistence.
        if str(candidate.get("side", "")).lower() == "buy":
            equity = float(context.get("portfolio_equity") or 0)
            config = getattr(self.risk_engine, "config", {}) or {}
            portfolio = config.get("portfolio_behavior", {})
            optimizer = config.get("portfolio_optimizer", {})
            risk_budget = config.get("risk_budget", {})
            active = DurableExecutionStore(self.storage).active_reservations()
            requested = float(candidate.get("notional") or 0)
            if requested <= 0 and candidate.get("qty") is not None:
                requested = float(candidate["qty"]) * float(candidate.get("latest_price") or 0)
            if equity > 0:
                projected_total = float(context.get("proposed_total_exposure_pct") or 0) * equity / 100
                projected_symbol = float(context.get("proposed_symbol_exposure_pct") or 0) * equity / 100
                projected_cluster = float(context.get("proposed_cluster_exposure_pct") or 0) * equity / 100
                current_active = float(active["active_reserved_notional"])
                candidate["_reservation_limits"] = {
                    "base_total_notional": max(0.0, projected_total - requested - current_active),
                    "base_symbol_notional": max(0.0, projected_symbol - requested - float(active["symbol_reserved_notional"].get(candidate.get("symbol"), 0))),
                    "base_cluster_notional": max(0.0, projected_cluster - requested - float(active["cluster_reserved_notional"].get(candidate.get("cluster_name"), 0))),
                    "total_notional_ceiling": equity * float(portfolio.get("max_total_portfolio_exposure_pct", 6.0)) / 100,
                    "symbol_notional_ceiling": equity * float(portfolio.get("max_single_symbol_exposure_pct", 2.5)) / 100,
                    "cluster_notional_ceiling": equity * float(optimizer.get("max_same_cluster_exposure_pct", 5.0)) / 100,
                    "base_open_risk": (
                        float(context["held_open_stop_risk"])
                        if isinstance(context.get("held_open_stop_risk"), (int, float))
                        else equity * float(risk_budget.get("max_open_risk_pct", 0.30)) / 100 + 1.0
                    ),
                    "open_risk_ceiling": equity * float(risk_budget.get("max_open_risk_pct", 0.30)) / 100,
                    "buying_power_ceiling": float(context["buying_power"]) + current_active if context.get("buying_power") is not None else None,
                }
                if candidate.get("phase4_mode") == "probe":
                    phase4 = config.get("phase4", {}) or {}
                    candidate["_reservation_limits"].update({
                        "probe_max_active_count": int(phase4.get("probe_max_active_count", 1)),
                        "probe_gross_notional_ceiling": equity * float(phase4.get("probe_gross_exposure_pct", 2.5)) / 100.0,
                        "probe_stop_risk_ceiling": equity * float(phase4.get("probe_portfolio_heat_pct", 0.10)) / 100.0,
                    })

        store = DurableExecutionStore(self.storage)
        try:
            self._fault("before_intent_persistence", client_order_id=client_order_id)
            intent = store.create_or_get_intent(
                candidate,
                run_id=self.run_id or proposal.get("run_id"),
                source_type=source_type,
                approval_id=approval_id,
            )
        except (ValueError, PermissionError, RuntimeError, sqlite3.Error) as exc:
            return ExecutionResult(False, "blocked", client_order_id, reason=f"intent persistence blocked: {type(exc).__name__}")

        intent_id = str(intent["id"])
        self._fault("after_intent_and_reservation_commit", intent_id=intent_id)
        state = OrderState(intent["state"])
        exact_pre_broker_recovery = state == OrderState.SUBMITTING and self.recovery_proven_no_submit
        if (state in BROKER_RELEVANT_STATES or state in TERMINAL_STATES) and not exact_pre_broker_recovery:
            # An existing logical action is never automatically submitted again.
            return ExecutionResult(
                state in {OrderState.SUBMITTED, OrderState.PARTIALLY_FILLED, OrderState.FILLED},
                state.value,
                intent["client_order_id"],
                reason="existing durable intent reused; no duplicate broker call",
                intent_id=intent_id,
            )
        if workflow_store is not None and workflow is not None:
            current_workflow = workflow_store.get(workflow["id"])
            if current_workflow["state"] == ApprovalWorkflowState.INTENT_CREATED.value:
                workflow_store.transition(current_workflow["id"], ApprovalWorkflowState.SUBMISSION_PENDING)
        try:
            self._fault("immediately_before_broker_invocation", intent_id=intent_id)
            if not exact_pre_broker_recovery:
                intent = store.transition(
                    intent_id,
                    OrderState.SUBMITTING,
                    event_type="broker_submission_started",
                    expected_state=OrderState.RESERVED,
                )
            if workflow_store is not None and workflow is not None:
                current_workflow = workflow_store.get(workflow["id"])
                if current_workflow["state"] == ApprovalWorkflowState.SUBMISSION_PENDING.value:
                    workflow_store.transition(current_workflow["id"], ApprovalWorkflowState.SUBMISSION_STARTED)
        except InvalidOrderTransition:
            current = store.get_intent(intent_id)
            return ExecutionResult(False, current["state"], current["client_order_id"], reason="another worker owns submission", intent_id=intent_id)

        try:
            order_args: dict[str, float]
            if intent["request_basis"] == "notional" and intent.get("requested_notional") is not None:
                order_args = {"notional": float(intent["requested_notional"])}
            else:
                order_args = {"qty": float(intent["requested_quantity"])}
            # This is deliberately the final instruction before adapter I/O. The
            # production hook is a no-op; deterministic tests may fail here.
            self._fault("immediately_before_broker_submit", intent_id=intent_id)
            response = self.broker.submit_order(
                intent["symbol"],
                intent["side"],
                order_args,
                candidate.get("order_type", "market"),
                candidate.get("limit_price"),
                intent["client_order_id"],
            )
            self._fault("after_broker_success_before_local_update", intent_id=intent_id)
            broker_order_id = str(_value(response, "id", "") or "") or None
            remote_status = str(_value(response, "status", "submitted") or "submitted").lower()
            target = OrderState.SUBMITTED
            if remote_status == "filled":
                target = OrderState.FILLED
            elif remote_status == "partially_filled":
                target = OrderState.PARTIALLY_FILLED
            if target in {OrderState.PARTIALLY_FILLED, OrderState.FILLED}:
                filled_quantity = float(_value(response, "filled_qty", intent["requested_quantity"] if target == OrderState.FILLED else 0) or 0)
                fill_price = float(_value(response, "filled_avg_price", candidate.get("latest_price")) or 0)
                if filled_quantity <= 0 or fill_price <= 0:
                    store.transition(
                        intent_id,
                        OrderState.UNKNOWN,
                        event_type="broker_fill_response_incomplete",
                        broker_order_id=broker_order_id,
                        safe_summary="filled response omitted reliable quantity or price; reconciliation required",
                    )
                    target = OrderState.UNKNOWN
                else:
                    event_key = str(_value(response, "execution_id", "") or f"{broker_order_id or intent['client_order_id']}:{filled_quantity:.12g}:{fill_price:.12g}")
                    store.record_fill(
                        intent_id,
                        cumulative_quantity=filled_quantity,
                        fill_price=fill_price,
                        broker_event_key=event_key,
                        broker_order_id=broker_order_id,
                        occurred_at=str(_value(response, "filled_at", iso_now())),
                        price_is_cumulative_average=True,
                    )
            else:
                store.transition(intent_id, target, event_type="broker_submission_acknowledged", broker_order_id=broker_order_id)
            if workflow_store is not None and workflow is not None:
                current_workflow = workflow_store.get(workflow["id"])
                workflow_target = ApprovalWorkflowState.UNKNOWN if target == OrderState.UNKNOWN else ApprovalWorkflowState.SUBMITTED
                if current_workflow["state"] == ApprovalWorkflowState.SUBMISSION_STARTED.value:
                    workflow_store.transition(current_workflow["id"], workflow_target)
            return ExecutionResult(target != OrderState.UNKNOWN, target.value, intent["client_order_id"], response, intent_id=intent_id)
        except Exception as exc:
            # The request may have reached the broker. Preserve the exact client ID,
            # retain all reservations, and require lookup proof before any retry.
            store.transition(
                intent_id,
                OrderState.UNKNOWN,
                event_type="broker_submission_ambiguous",
                error_category=type(exc).__name__,
                safe_summary="broker submission outcome is unknown; reconciliation required",
            )
            if workflow_store is not None and workflow is not None:
                current_workflow = workflow_store.get(workflow["id"])
                if current_workflow["state"] == ApprovalWorkflowState.SUBMISSION_STARTED.value:
                    workflow_store.transition(
                        current_workflow["id"],
                        ApprovalWorkflowState.UNKNOWN,
                        safe_detail="broker submission outcome is unknown; reconciliation required",
                    )
            return ExecutionResult(
                False,
                OrderState.UNKNOWN.value,
                intent["client_order_id"],
                reason=f"manual review required: {type(exc).__name__}",
                intent_id=intent_id,
            )


def execute_proposal(
    broker: Any,
    risk_engine: RiskEngine,
    proposal: dict[str, Any],
    context: dict[str, Any],
    *,
    storage: Any | None = None,
    run_id: str | None = None,
) -> ExecutionResult:
    return Executor(broker, risk_engine, storage, run_id).execute(proposal, context)
