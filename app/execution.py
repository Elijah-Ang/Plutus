from __future__ import annotations

import sqlite3
import json
import hashlib
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Callable, Mapping

from .order_state import (
    BROKER_RELEVANT_STATES,
    TERMINAL_STATES,
    InvalidOrderTransition,
    OrderState,
    logical_action_key,
    stable_client_order_id,
    validate_transition,
)
from .risk_engine import RiskEngine
from .broker_interface import BrokerSubmissionNotAttempted
from .capabilities import require_autonomous_entry_support, require_autonomous_exit_support, require_protective_paper_exit_support
from .utils import iso_now, json_dumps
from .formula_versions import ACCOUNTING_VERSION, EVIDENCE_VERSION
from .fixed_point_accounting import (
    EXACT_DECIMAL_PROVENANCE,
    ZERO,
    decimal_text,
    decimal_value,
    legacy_float,
    require_exact_decimal,
)
from .formula_versions import FIXED_POINT_ACCOUNTING_VERSION
from .quotes import implementation_shortfall_bps, validate_quote_payload
from .canonical_sizing import canonical_sizing, enforce_ceilings
from .approval_authority import authority_fingerprint
from .execution_risk_snapshot import (
    execution_candidate_evidence,
    snapshot_body_from_row,
    verify_execution_risk_snapshot,
    verify_snapshot_immediately_before_broker,
)


def _value(obj: Any, name: str, default: Any = None) -> Any:
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


def _broker_evidence_safe(value: Any) -> Any:
    """Normalize a broker response without retaining SDK objects or secrets."""

    if isinstance(value, Decimal):
        return decimal_text(value)
    if isinstance(value, datetime):
        return value.isoformat()
    enum_value = getattr(value, "value", None)
    if enum_value is not None and not callable(enum_value):
        return _broker_evidence_safe(enum_value)
    if isinstance(value, Mapping):
        return {
            str(key): _broker_evidence_safe(item)
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
            if "secret" not in str(key).lower()
            and "token" not in str(key).lower()
            and "key" not in str(key).lower()
        }
    if isinstance(value, (list, tuple)):
        return [_broker_evidence_safe(item) for item in value]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    return str(value)


def _broker_evidence_payload(evidence: Mapping[str, Any]) -> dict[str, Any]:
    raw = dict(evidence)
    payload = _broker_evidence_safe(raw)
    if not isinstance(payload, dict):
        raise ValueError("broker evidence payload must be a mapping")
    return payload


def _broker_evidence_fingerprint(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _winner_add_reservation_risk(
    proposal: dict[str, Any], quantity: Decimal, reference: Decimal, stop_price: Decimal | None
) -> tuple[Decimal, Decimal]:
    quantity_decimal = decimal_value(quantity, "winner ADD quantity", minimum=ZERO)
    reference_decimal = decimal_value(reference, "winner ADD reference price", minimum=ZERO)
    stop_decimal = (
        decimal_value(stop_price, "winner ADD stop price", minimum=ZERO)
        if stop_price is not None
        else None
    )
    if quantity_decimal is None or reference_decimal is None:
        raise ValueError("winner ADD canonical quantity or reference is unavailable")
    incremental_risk = proposal.get("incremental_risk")
    if incremental_risk is None:
        raise ValueError("winner ADD requires canonical incremental-risk provenance")
    incremental_risk_decimal = decimal_value(
        incremental_risk, "winner ADD incremental risk"
    )
    assert incremental_risk_decimal is not None
    canonical_add_leg_risk = quantity_decimal * max(
        reference_decimal - (stop_decimal or reference_decimal), ZERO
    )
    stated_add_leg_risk = proposal.get("pending_add_stop_risk")
    if stated_add_leg_risk is not None:
        stated_add_leg_risk_decimal = decimal_value(
            stated_add_leg_risk, "winner ADD pending leg risk", minimum=ZERO
        )
        assert stated_add_leg_risk_decimal is not None
        if stated_add_leg_risk_decimal != canonical_add_leg_risk:
            raise ValueError("winner ADD pending leg risk does not match final quantity, price, and stop")
    # The second return value is a long-standing private helper compatibility
    # projection used by callers/tests that compare it as a native number.
    # Durable execution immediately reparses it into Decimal below.
    return incremental_risk_decimal, legacy_float(canonical_add_leg_risk)


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
        sized = canonical_sizing(proposal)
        return (
            sized.quantity,
            sized.reference_price,
            sized.stop_price,
            sized.request_basis,
            sized.notional if sized.request_basis == "notional" else None,
        )

    def create_or_get_intent(
        self,
        proposal: dict[str, Any],
        *,
        run_id: str | None,
        source_type: str,
        approval_id: str | None = None,
        sequence: int = 0,
    ) -> dict[str, Any]:
        proposal = dict(proposal)
        if proposal.get("shadow_only") or proposal.get("observation_only") or proposal.get("research_only"):
            raise ValueError("shadow, observation-only and research-only records cannot create order intents")
        if str(proposal.get("trading_mode") or proposal.get("mode") or "paper") != "paper":
            raise PermissionError("durable execution supports paper mode only")
        if not approval_id and os.getenv("TRADING_AGENT_TESTING") != "1":
            raise PermissionError("manual approval is required before intent creation")
        synthetic_test_snapshot = False
        if not proposal.get("risk_snapshot_id") and os.getenv("TRADING_AGENT_TESTING") == "1":
            from .execution_risk_snapshot import capture_execution_risk_snapshot

            proposal_identity = str(proposal.get("proposal_id") or proposal.get("id") or "")
            synthetic_approval_id = approval_id or f"test-unapproved:{proposal_identity}"
            synthetic_config: dict[str, Any] = {}
            if proposal.get("config_hash"):
                synthetic_config["effective_config_hash"] = proposal["config_hash"]
            if isinstance(proposal.get("formula_versions"), Mapping):
                synthetic_config["formula_versions"] = dict(
                    proposal["formula_versions"]
                )
            synthetic_snapshot = capture_execution_risk_snapshot(
                self.storage, None,
                proposal_id=proposal_identity,
                approval_id=synthetic_approval_id,
                run_id=run_id or "isolated-test-run",
                context={},
                config=synthetic_config,
                candidate=proposal,
            )
            proposal["risk_snapshot_id"] = synthetic_snapshot["id"]
            synthetic_test_snapshot = True
        now = iso_now()
        intent_id = str(uuid.uuid4())
        event_id = str(uuid.uuid4())
        reservation_id = str(uuid.uuid4())
        with self.storage.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            durable_envelope: dict[str, Any] | None = None
            stored_proposal: dict[str, Any] | None = None
            stored_approval: dict[str, Any] | None = None
            isolated_caller_reservation_limits = proposal.get("_reservation_limits")
            proposal_identity = str(proposal.get("proposal_id") or proposal.get("id") or "")
            if approval_id:
                from .approval_display import validate_consumed_display_authority

                stored_proposal, stored_approval, durable_envelope = validate_consumed_display_authority(
                    conn,
                    approval_id=approval_id,
                    proposal_id=proposal_identity,
                    source_type=source_type,
                )
            snapshot_id = str(proposal.get("risk_snapshot_id") or "")
            if not snapshot_id:
                raise RuntimeError("authoritative risk snapshot identity is required")
            raw_snapshot = conn.execute(
                "SELECT config_hash,formula_versions_json,approval_id,run_id FROM execution_risk_snapshots WHERE id=?",
                (snapshot_id,),
            ).fetchone()
            if raw_snapshot is None:
                raise RuntimeError("authoritative risk snapshot is missing")
            latest_snapshot = conn.execute(
                """SELECT id FROM execution_risk_snapshots
                   WHERE proposal_id=? AND approval_id=? AND run_id=?
                   ORDER BY captured_at DESC,id DESC LIMIT 1""",
                (proposal_identity, approval_id or raw_snapshot["approval_id"], run_id or raw_snapshot["run_id"]),
            ).fetchone()
            # Production candidates must bind the newest snapshot. Isolated legacy
            # concurrency fixtures synthesize one snapshot per racing worker and
            # have no approval/display authority; their protection is the unique
            # logical-action constraint exercised by those tests.
            if (not synthetic_test_snapshot) and (
                latest_snapshot is None or str(latest_snapshot["id"]) != snapshot_id
            ):
                raise RuntimeError("intent creation is not bound to the latest authoritative risk snapshot")
            expected_config_hash = str(
                (durable_envelope or {}).get("config_hash") or raw_snapshot["config_hash"] or ""
            )
            try:
                expected_formulas = (
                    dict(durable_envelope["formula_versions"])
                    if durable_envelope is not None
                    else json.loads(raw_snapshot["formula_versions_json"] or "{}")
                )
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise RuntimeError("authoritative formula versions are invalid") from exc
            snapshot_approval_id = approval_id or str(raw_snapshot["approval_id"] or "")
            effective_run_id = run_id or ("isolated-test-run" if os.getenv("TRADING_AGENT_TESTING") == "1" else None)
            snapshot_row, snapshot_body = verify_execution_risk_snapshot(
                conn,
                snapshot_id,
                proposal_id=proposal_identity,
                approval_id=snapshot_approval_id,
                run_id=effective_run_id,
                config_hash=expected_config_hash,
                formula_versions=expected_formulas,
            )
            candidate_evidence = dict(snapshot_body["execution_candidate"])
            caller_evidence = execution_candidate_evidence(proposal)
            exact_snapshot_fields = (
                "proposal_id", "symbol", "side", "action", "approval_source_type",
                "execution_path", "request_basis", "position_lifecycle_id", "strategy_version",
                "relationship_type", "relationship_group_id", "rotation_group_id",
                "rotation_step_id", "emergency_exit_triggered", "emergency_exit_hard_trigger",
                "emergency_exit_trigger_reason", "emergency_exit_mode", "config_hash",
                "formula_versions", "proposal_version", "display_envelope_id",
                "display_context_type", "display_context_id",
                "approved_quantity_ceiling", "approved_notional_ceiling",
                "approved_stop_risk_ceiling",
            )
            for field in exact_snapshot_fields:
                if field in proposal and caller_evidence.get(field) != candidate_evidence.get(field):
                    raise RuntimeError(f"caller {field} does not match the authoritative risk snapshot")
            if durable_envelope is not None:
                envelope_candidate_fields = {
                    "proposal_id": "proposal_id", "symbol": "symbol", "side": "side", "action": "action",
                    "approval_source_type": "approval_source_type", "execution_path": "execution_path",
                    "request_basis": "request_basis", "position_lifecycle_id": "position_lifecycle_id",
                    "strategy_version": "strategy_version", "relationship_type": "relationship_type",
                    "relationship_group_id": "relationship_group_id", "rotation_group_id": "rotation_group_id",
                    "rotation_step_id": "rotation_step_id", "emergency_triggered": "emergency_exit_triggered",
                    "emergency_trigger_identity": "emergency_exit_hard_trigger",
                    "emergency_trigger_reason": "emergency_exit_trigger_reason",
                    "emergency_trigger_mode": "emergency_exit_mode", "config_hash": "config_hash",
                    "formula_versions": "formula_versions", "proposal_version": "proposal_version",
                    "display_context_type": "display_context_type", "display_context_id": "display_context_id",
                }
                for envelope_field, candidate_field in envelope_candidate_fields.items():
                    expected = durable_envelope.get(envelope_field)
                    actual = candidate_evidence.get(candidate_field)
                    if expected != actual:
                        if not (os.getenv("TRADING_AGENT_TESTING") == "1" and actual in (None, "")):
                            raise RuntimeError(f"risk snapshot {candidate_field} does not match immutable display authority")
                if (
                    str(durable_envelope.get("approval_source_type") or "") != str(source_type)
                    and not (
                        os.getenv("TRADING_AGENT_TESTING") == "1"
                        and str(durable_envelope.get("display_context_type") or "") == "test_fixture"
                    )
                ):
                    raise RuntimeError("execution source type changed after display")
                if str(stored_approval.get("display_envelope_id") or "") != str(candidate_evidence.get("display_envelope_id") or stored_approval.get("display_envelope_id") or ""):
                    raise RuntimeError("risk snapshot display identity is invalid")

            # All execution terms below originate from the verified snapshot,
            # display and stored proposal while this transaction owns the write lock.
            proposal = {
                **candidate_evidence,
                "risk_snapshot_id": snapshot_id,
                "_reservation_limits": dict(snapshot_body["risk_context"].get("reservation_limits") or {}),
            }
            if durable_envelope is not None:
                proposal.update(
                    approved_quantity_ceiling=durable_envelope.get("max_quantity"),
                    approved_notional_ceiling=durable_envelope.get("max_notional"),
                    approved_stop_risk_ceiling=durable_envelope.get("max_stop_risk"),
                    approval_authority_fingerprint=authority_fingerprint(durable_envelope),
                    displayed_fingerprint=authority_fingerprint(durable_envelope),
                    execution_path=durable_envelope.get("execution_path"),
                )
            isolated_test_fixture = os.getenv("TRADING_AGENT_TESTING") == "1" and (
                durable_envelope is None
                or str((durable_envelope or {}).get("display_context_type") or "") == "test_fixture"
            )
            if isolated_test_fixture and isolated_caller_reservation_limits:
                proposal["_reservation_limits"] = dict(isolated_caller_reservation_limits)
            elif isolated_test_fixture and durable_envelope is None:
                proposal["_reservation_limits"] = {}
            expires_at = proposal.get("expires_at") or (durable_envelope or {}).get("expires_at")
            if not expires_at and isolated_test_fixture:
                expires_at = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
                proposal["expires_at"] = expires_at
            if not expires_at:
                raise ValueError("authoritative proposal expiry is required")
            expiry = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=UTC)
            if expiry.astimezone(UTC) <= datetime.now(UTC):
                raise ValueError("expired approval cannot create an order intent")
            symbol = str(proposal.get("symbol") or "").upper()
            side = str(proposal.get("side") or "").lower()
            if not symbol or side not in {"buy", "sell"}:
                raise ValueError("intent requires a symbol and buy/sell side")
            sizing = canonical_sizing(proposal)
            if isolated_test_fixture and durable_envelope is not None:
                proposal["approved_quantity_ceiling"] = sizing.quantity
                proposal["approved_notional_ceiling"] = sizing.notional
                proposal["approved_stop_risk_ceiling"] = sizing.stop_risk
            elif isolated_test_fixture:
                if proposal.get("approved_quantity_ceiling") in (None, ""):
                    proposal["approved_quantity_ceiling"] = sizing.quantity
                if proposal.get("approved_notional_ceiling") in (None, ""):
                    proposal["approved_notional_ceiling"] = sizing.notional
                if proposal.get("approved_stop_risk_ceiling") in (None, ""):
                    proposal["approved_stop_risk_ceiling"] = sizing.stop_risk
            request_basis = sizing.request_basis
            quantity, reference, stop_price = sizing.quantity, sizing.reference_price, sizing.stop_price
            requested_notional = sizing.notional
            applicable = {
                "quantity": proposal.get("approved_quantity_ceiling"),
                "notional": proposal.get("approved_notional_ceiling"),
            }
            required_basis = "quantity" if request_basis == "quantity" else "notional"
            if applicable[required_basis] in (None, ""):
                raise RuntimeError(f"immutable displayed {required_basis} ceiling is required")
            if side == "buy" and str(proposal.get("action") or "entry") in {"entry", "add"} and proposal.get("approved_stop_risk_ceiling") in (None, ""):
                raise RuntimeError("immutable displayed stop-risk ceiling is required for a risk-increasing order")
            enforce_ceilings(sizing, proposal)
            source_id = str(proposal.get("source_id") or proposal_identity)
            if not source_id:
                raise ValueError("intent requires a stable proposal or emergency-action source ID")
            action = str(proposal.get("action") or ("exit" if side == "sell" else "entry")).lower()
            keyed = {**proposal, "source_id": source_id, "symbol": symbol, "side": side, "action": action}
            action_key = logical_action_key(keyed, source_type, sequence)
            client_order_id = stable_client_order_id(action_key)
            if candidate_evidence.get("client_order_id") not in (None, client_order_id):
                raise RuntimeError("risk snapshot logical action key does not match the final candidate")
            reserved_notional = sizing.notional if side == "buy" else ZERO
            reserved_stop_risk = sizing.stop_risk if side == "buy" else ZERO
            if side == "buy" and proposal.get("winner_expansion_decision_id"):
                incremental_risk, reserved_stop_risk_projection = _winner_add_reservation_risk(
                    proposal, quantity, reference, stop_price
                )
                reserved_stop_risk_decimal = decimal_value(
                    reserved_stop_risk_projection,
                    "winner ADD reserved stop risk",
                    minimum=ZERO,
                )
                if reserved_stop_risk_decimal is None:
                    raise ValueError("winner ADD reserved stop risk is unavailable")
                reserved_stop_risk = reserved_stop_risk_decimal
                if not proposal.get("pyramiding_milestone_id") or not proposal.get("pyramiding_milestone_key"):
                    raise ValueError("winner ADD requires a durable pyramiding milestone")
            else:
                incremental_risk = reserved_stop_risk
            approved_quantity_decimal = decimal_value(
                proposal.get("approved_quantity_ceiling", quantity),
                "approved quantity ceiling",
                minimum=ZERO,
            )
            approved_notional_decimal = decimal_value(
                proposal.get("approved_notional_ceiling", proposal.get("approved_notional", requested_notional)),
                "approved notional ceiling",
                minimum=ZERO,
                allow_none=True,
            )
            initial_risk_decimal = decimal_value(
                proposal.get("initial_risk_dollars"),
                "initial risk dollars",
                minimum=ZERO,
                allow_none=True,
            )
            # SQLite REAL fields are retained only as compatibility
            # projections.  Convert exact Decimal sizing at the persistence
            # boundary; all identity and risk arithmetic above remains exact.
            legacy_quantity = legacy_float(quantity)
            legacy_requested_notional = legacy_float(requested_notional) if requested_notional is not None else None
            legacy_reference = legacy_float(reference)
            legacy_stop_price = legacy_float(stop_price) if stop_price is not None else None
            legacy_reserved_notional = legacy_float(reserved_notional)
            legacy_reserved_stop_risk = legacy_float(reserved_stop_risk)
            legacy_incremental_risk = legacy_float(
                incremental_risk if hasattr(incremental_risk, "as_tuple")
                else decimal_value(incremental_risk, "incremental risk")
            )
            existing = conn.execute("SELECT * FROM order_intents WHERE logical_action_key=?", (action_key,)).fetchone()
            if existing:
                if str(existing["approval_id"] or "") != str(approval_id or ""):
                    raise RuntimeError("logical action is already bound to another approval")
                if str(existing["state"]) in {"created", "reserved", "retryable_pre_submission"} and int(existing["broker_invocation_occurred"] or 0) == 0:
                    conn.execute(
                        "UPDATE order_intents SET risk_snapshot_id=?,updated_at=? WHERE id=?",
                        (snapshot_id, iso_now(), existing["id"]),
                    )
                    return dict(conn.execute("SELECT * FROM order_intents WHERE id=?", (existing["id"],)).fetchone())
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
                approval_row = conn.execute(
                    """SELECT authority_fingerprint,displayed_fingerprint FROM approvals
                       WHERE id=? AND proposal_id=? AND authorized=1
                         AND status='consumed' AND consumed_at IS NOT NULL""",
                    (approval_id, proposal.get("proposal_id") or proposal.get("id")),
                ).fetchone()
                stored_fingerprint = str(approval_row["authority_fingerprint"] or "") if approval_row else ""
                candidate_fingerprint = str(proposal.get("approval_authority_fingerprint") or "")
                if not stored_fingerprint:
                    raise RuntimeError("approval authority envelope is missing")
                if not candidate_fingerprint:
                    candidate_fingerprint = stored_fingerprint
                    proposal["approval_authority_fingerprint"] = stored_fingerprint
                if candidate_fingerprint != stored_fingerprint:
                    raise RuntimeError("approval authority fingerprint does not match the durable approval")
                if str(approval_row["displayed_fingerprint"] or "") != stored_fingerprint:
                    raise RuntimeError("approval is not bound to the displayed authority")
            conflict = conn.execute(
                """SELECT id,state FROM order_intents
                   WHERE symbol=? AND side=? AND state IN (?,?,?,?,?,?,?,?,?) LIMIT 1""",
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
                    OrderState.RETRYABLE_PRE_SUBMISSION.value,
                ),
            ).fetchone()
            if conflict:
                raise RuntimeError(f"conflicting active order intent exists: {conflict['state']}")
            if side == "buy" and proposal.get("winner_expansion_decision_id"):
                winner_rows = conn.execute(
                    """SELECT * FROM add_risk_decisions
                       WHERE id=? AND proposal_id=? AND decision_stage='final_revalidation'
                         AND eligible=1 AND milestone_id=? AND milestone_key=?""",
                    (
                        proposal["winner_expansion_decision_id"],
                        proposal.get("proposal_id") or proposal.get("id"),
                        proposal["pyramiding_milestone_id"],
                        proposal["pyramiding_milestone_key"],
                    ),
                ).fetchall()
                winner_authority = None
                for winner_row in winner_rows:
                    # A winner decision is authoritative only when its exact
                    # Decimal risk column is present and equal.  Legacy REAL
                    # rows are deliberately not revived by an epsilon match.
                    raw_incremental = winner_row["incremental_risk_decimal"]
                    if raw_incremental in (None, ""):
                        continue
                    try:
                        exact_incremental = decimal_value(
                            raw_incremental, "winner authority incremental risk"
                        )
                    except ValueError:
                        continue
                    if exact_incremental == incremental_risk:
                        winner_authority = winner_row
                        break
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
                # SQLite REAL aggregation is not authority at this boundary:
                # summing binary projections can cross a displayed ceiling by
                # one cent or one ULP.  Read the canonical Decimal text for
                # every active reservation and aggregate in Python.
                active_rows = conn.execute(
                    "SELECT * FROM risk_reservations WHERE state='active' ORDER BY created_at,id"
                ).fetchall()

                def exact_reservation(row: Mapping[str, Any], field: str) -> Decimal:
                    raw = row[f"{field}_decimal"] if f"{field}_decimal" in row.keys() else None
                    if raw in (None, ""):
                        raw = row[field]
                    value = decimal_value(raw, f"active reservation {field}", minimum=ZERO)
                    if value is None:
                        raise RuntimeError(f"active reservation {field} is unavailable")
                    return value

                total_notional = ZERO
                total_stop_risk = ZERO
                symbol_total = ZERO
                cluster_total = ZERO
                for active_row in active_rows:
                    row_notional = exact_reservation(active_row, "active_notional")
                    row_stop_risk = exact_reservation(active_row, "active_stop_risk")
                    total_notional += row_notional
                    total_stop_risk += row_stop_risk
                    if str(active_row["symbol"] or "").upper() == symbol:
                        symbol_total += row_notional
                    if proposal.get("cluster_name") and str(active_row["cluster_name"] or "") == str(proposal["cluster_name"]):
                        cluster_total += row_notional

                def exact_limit(value: Any, label: str) -> Decimal:
                    parsed = decimal_value(value if value not in (None, "") else ZERO, label, minimum=ZERO)
                    if parsed is None:
                        raise RuntimeError(f"atomic reservation {label} is unavailable")
                    return parsed

                def enforce(name: str, projected: Decimal, ceiling_key: str) -> None:
                    ceiling = limits.get(ceiling_key)
                    if ceiling in (None, ""):
                        return
                    ceiling_value = exact_limit(ceiling, f"atomic reservation {ceiling_key}")
                    if projected > ceiling_value:
                        raise RuntimeError(f"atomic reservation blocked by {name}")

                enforce(
                    "total exposure ceiling",
                    exact_limit(limits.get("base_total_notional"), "base total notional")
                    + total_notional + reserved_notional,
                    "total_notional_ceiling",
                )
                enforce(
                    "symbol exposure ceiling",
                    exact_limit(limits.get("base_symbol_notional"), "base symbol notional")
                    + symbol_total + reserved_notional,
                    "symbol_notional_ceiling",
                )
                enforce(
                    "cluster exposure ceiling",
                    exact_limit(limits.get("base_cluster_notional"), "base cluster notional")
                    + cluster_total + reserved_notional,
                    "cluster_notional_ceiling",
                )
                enforce(
                    "open risk ceiling",
                    exact_limit(limits.get("base_open_risk"), "base open risk")
                    + total_stop_risk + reserved_stop_risk,
                    "open_risk_ceiling",
                )
                enforce(
                    "paper buying power",
                    total_notional + reserved_notional,
                    "buying_power_ceiling",
                )
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
                               WHERE COALESCE(pl.remaining_quantity_decimal,'0')<>'0' AND p.strategy_state='PROBE'
                           )"""
                    ).fetchone()["n"]
                    maximum = int(limits.get("probe_max_active_count", 1))
                    if int(probe_slots or 0) >= maximum:
                        raise RuntimeError("atomic reservation blocked by PROBE active-count ceiling")
                    probe_gross = ZERO
                    probe_heat = ZERO
                    probe_reservations = conn.execute(
                        """SELECT rr.* FROM risk_reservations rr
                           JOIN order_intents i ON i.id=rr.intent_id
                           JOIN trade_proposals p ON p.id=i.proposal_id
                           WHERE rr.state='active' AND p.strategy_state='PROBE'"""
                    ).fetchall()
                    for probe_row in probe_reservations:
                        probe_gross += exact_reservation(probe_row, "active_notional")
                        probe_heat += exact_reservation(probe_row, "active_stop_risk")
                    probe_lots = conn.execute(
                        """SELECT pl.* FROM position_lots pl
                           JOIN trade_proposals p ON p.id=pl.entry_proposal_id
                           WHERE COALESCE(pl.remaining_quantity_decimal,'0')<>'0'
                             AND p.strategy_state='PROBE'"""
                    ).fetchall()
                    for probe_lot in probe_lots:
                        remaining = decimal_value(
                            probe_lot["remaining_quantity_decimal"],
                            "PROBE lot remaining quantity",
                            minimum=ZERO,
                        )
                        unit_cost = decimal_value(
                            probe_lot["unit_cost_decimal"],
                            "PROBE lot unit cost",
                            minimum=ZERO,
                        )
                        initial_risk = decimal_value(
                            probe_lot["initial_risk_dollars_decimal"],
                            "PROBE lot initial risk",
                            minimum=ZERO,
                        )
                        if remaining is None or unit_cost is None or initial_risk is None:
                            raise RuntimeError("PROBE lot exact accounting evidence is unavailable")
                        probe_gross += remaining * unit_cost
                        original = decimal_value(
                            probe_lot["original_quantity_decimal"],
                            "PROBE lot original quantity",
                            minimum=ZERO,
                        )
                        if original is None or original <= ZERO:
                            raise RuntimeError("PROBE lot original quantity is invalid")
                        probe_heat += initial_risk * remaining / original
                    enforce(
                        "PROBE gross-exposure ceiling",
                        probe_gross + reserved_notional,
                        "probe_gross_notional_ceiling",
                    )
                    enforce(
                        "PROBE portfolio-heat ceiling",
                        probe_heat + reserved_stop_risk,
                        "probe_stop_risk_ceiling",
                    )
            sleeve_fields_present = any(
                proposal.get(name) is not None
                for name in (
                    "strategy_registry_snapshot_id", "strategy_sleeve", "sleeve_allocation_id",
                    "sleeve_notional_ceiling", "sleeve_stop_risk_ceiling",
                )
            )
            if side == "buy" and (sleeve_fields_present or limits.get("require_strategy_sleeve") is True):
                from .allocation_authority import (
                    AllocationAuthorityError,
                    Phase4AllocationStore,
                )
                from .strategy_execution_registry import (
                    StrategyRegistryIntegrityError,
                    StrategyRegistryStore,
                )

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
                try:
                    verified_registry = StrategyRegistryStore(self.storage).load_verified(
                        str(proposal["strategy_registry_snapshot_id"]),
                        conn=conn,
                        expected_run_id=str(run_id),
                    )
                    registry_decisions = {
                        decision.strategy_version: decision
                        for decision in (
                            *verified_registry.evaluation.authorized,
                            *verified_registry.evaluation.rejected,
                        )
                    }
                    registry_decision = registry_decisions.get(
                        str(proposal["strategy_version"])
                    )
                    if registry_decision is None or not registry_decision.authorized:
                        raise RuntimeError(
                            "atomic strategy sleeve reservation lacks registry authority"
                        )
                    verified_allocation = Phase4AllocationStore(
                        self.storage
                    ).load_verified(
                        str(proposal["sleeve_allocation_id"]),
                        conn=conn,
                        expected_run_id=str(run_id),
                        expected_registry_snapshot_id=str(
                            proposal["strategy_registry_snapshot_id"]
                        ),
                        expected_strategy_version=str(proposal["strategy_version"]),
                        expected_config_hash=expected_config_hash,
                        require_executable=True,
                    )
                    allocation_payload = dict(verified_allocation.payload)
                    canonical_sleeves = verified_allocation.strategy_sleeves
                    canonical_sleeve = canonical_sleeves[proposal["strategy_version"]]
                except (AllocationAuthorityError, StrategyRegistryIntegrityError) as exc:
                    raise RuntimeError(
                        "atomic strategy sleeve persisted authority is invalid"
                    ) from exc
                if proposal["strategy_sleeve"] != proposal["strategy_version"]:
                    raise RuntimeError("atomic strategy sleeve identity does not match the strategy")
                if canonical_sleeve.get("strategy_version") != proposal["strategy_version"]:
                    raise RuntimeError("canonical allocation does not contain the requested strategy sleeve")
                if proposal["strategy_version"] not in set(allocation_payload.get("authorized_strategies") or []):
                    raise RuntimeError("canonical allocation does not authorize the requested strategy")
                if allocation_payload.get("registry_snapshot_id") != proposal["strategy_registry_snapshot_id"]:
                    raise RuntimeError("canonical allocation is not bound to the supplied registry snapshot")
                risk_unit = str(canonical_sleeve.get("risk_unit") or "")
                try:
                    canonical_risk = require_exact_decimal(
                        canonical_sleeve,
                        "remaining_risk_decimal",
                        minimum=ZERO,
                    )
                    canonical_notional = require_exact_decimal(
                        canonical_sleeve,
                        "remaining_notional_decimal",
                        minimum=ZERO,
                    )
                except ValueError as exc:
                    raise RuntimeError(
                        "canonical strategy sleeve exact capacity is unavailable"
                    ) from exc
                if canonical_risk is None:
                    raise RuntimeError("canonical strategy risk capacity is unavailable")
                if risk_unit == "pct_equity":
                    replay = allocation_payload.get("raw_replay_inputs") or {}
                    portfolio_snapshot = replay.get("portfolio_snapshot") or {}
                    equity = decimal_value(
                        portfolio_snapshot.get("portfolio_equity"),
                        "canonical sleeve equity",
                        minimum=ZERO,
                    )
                    if equity is None or equity <= ZERO:
                        raise RuntimeError("canonical sleeve equity conversion is unavailable")
                    canonical_risk = equity * canonical_risk / Decimal("100")
                elif risk_unit != "stop_risk_dollars":
                    raise RuntimeError("canonical sleeve risk unit is unsupported")
                supplied_notional = decimal_value(
                    proposal["sleeve_notional_ceiling"],
                    "proposal strategy sleeve notional ceiling",
                    minimum=ZERO,
                )
                supplied_risk = decimal_value(
                    proposal["sleeve_stop_risk_ceiling"],
                    "proposal strategy sleeve stop-risk ceiling",
                    minimum=ZERO,
                )
                if canonical_notional is None or supplied_notional is None or supplied_risk is None:
                    raise RuntimeError("canonical strategy sleeve ceilings are unavailable")
                if supplied_notional > canonical_notional or supplied_risk > canonical_risk:
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
                    pending_claim_map: dict[str, tuple[Decimal, Decimal]] = {}
                    for claim in pending_claims:
                        if not isinstance(claim, dict):
                            raise TypeError("pending claim snapshot rows must be mappings")
                        proposal_id = str(claim.get("proposal_id") or "")
                        # The float fields are display projections only.  The
                        # allocation snapshot must carry canonical decimal
                        # evidence so the reservation race check cannot be
                        # influenced by a binary-float reconstruction.
                        claim_notional = require_exact_decimal(
                            claim,
                            "notional_decimal",
                            minimum=ZERO,
                        )
                        claim_risk = require_exact_decimal(
                            claim,
                            "stop_risk_decimal",
                            minimum=ZERO,
                        )
                        if (
                            not proposal_id or proposal_id in pending_claim_map
                            or claim_notional is None or claim_risk is None
                        ):
                            raise ValueError("pending claim snapshot identity or amount is invalid")
                        pending_claim_map[proposal_id] = (claim_notional, claim_risk)
                except (KeyError, TypeError, ValueError, OverflowError) as exc:
                    raise RuntimeError("canonical strategy sleeve reservation snapshot is unavailable") from exc
                current_rows = conn.execute(
                    """SELECT rr.id,rr.active_notional,rr.active_stop_risk,
                              rr.active_notional_decimal,rr.active_stop_risk_decimal,i.proposal_id
                       FROM risk_reservations rr
                       LEFT JOIN order_intents i ON i.id=rr.intent_id
                       WHERE rr.state='active' AND rr.strategy_version=?""",
                    (proposal["strategy_version"],),
                ).fetchall()
                incremental_notional = ZERO
                incremental_risk = ZERO
                included_id_set = set(included_ids)
                for current_row in current_rows:
                    if str(current_row["id"]) in included_id_set:
                        continue
                    pending_notional, pending_risk = pending_claim_map.get(
                        str(current_row["proposal_id"] or ""), (ZERO, ZERO)
                    )
                    active_notional = require_exact_decimal(
                        current_row,
                        "active_notional_decimal",
                        minimum=ZERO,
                    )
                    active_risk = require_exact_decimal(
                        current_row,
                        "active_stop_risk_decimal",
                        minimum=ZERO,
                    )
                    if active_notional is None or active_risk is None:
                        raise RuntimeError("active strategy sleeve reservation exact amount is unavailable")
                    incremental_notional += max(ZERO, active_notional - pending_notional)
                    incremental_risk += max(ZERO, active_risk - pending_risk)
                candidate_pending_notional, candidate_pending_risk = pending_claim_map.get(
                    str(proposal.get("proposal_id") or proposal.get("id") or ""), (ZERO, ZERO)
                )
                candidate_incremental_notional = max(ZERO, reserved_notional - candidate_pending_notional)
                candidate_incremental_risk = max(ZERO, reserved_stop_risk - candidate_pending_risk)
                if incremental_notional + candidate_incremental_notional > effective_notional_ceiling:
                    raise RuntimeError("atomic reservation blocked by strategy sleeve notional ceiling")
                if incremental_risk + candidate_incremental_risk > effective_risk_ceiling:
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
                       incremental_risk,rotation_step_id,approval_authority_fingerprint)
                   VALUES({','.join('?' for _ in range(60))})""",
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
                    legacy_float(decimal_value(proposal.get("approved_quantity_ceiling", quantity), "approved quantity ceiling")),
                    legacy_float(decimal_value(proposal.get("approved_notional_ceiling", proposal.get("approved_notional", requested_notional)), "approved notional ceiling")) if proposal.get("approved_notional_ceiling", proposal.get("approved_notional", requested_notional)) is not None else None,
                    legacy_quantity,
                    legacy_requested_notional,
                    0.0,
                    legacy_reference,
                    legacy_stop_price,
                    legacy_reserved_notional,
                    legacy_reserved_stop_risk,
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
                    legacy_float(decimal_value(proposal.get("initial_risk_dollars"), "initial risk dollars")) if proposal.get("initial_risk_dollars") is not None else None,
                    proposal.get("config_hash"),
                    proposal.get("evidence_version", EVIDENCE_VERSION),
                    proposal.get("formula_version", ACCOUNTING_VERSION),
                    proposal.get("strategy_registry_snapshot_id"),
                    proposal.get("strategy_sleeve"),
                    proposal.get("sleeve_allocation_id"),
                    legacy_float(decimal_value(proposal.get("sleeve_notional_ceiling"), "sleeve notional ceiling")) if proposal.get("sleeve_notional_ceiling") is not None else None,
                    legacy_float(decimal_value(proposal.get("sleeve_stop_risk_ceiling"), "sleeve stop risk ceiling")) if proposal.get("sleeve_stop_risk_ceiling") is not None else None,
                    proposal.get("winner_expansion_decision_id"),
                    proposal.get("pyramiding_milestone_id"),
                    proposal.get("pyramiding_milestone_key"),
                    proposal.get("management_mode"),
                    legacy_float(decimal_value(proposal.get("pre_add_open_risk"), "pre-add open risk")) if proposal.get("pre_add_open_risk") is not None else None,
                    legacy_float(decimal_value(proposal.get("post_add_open_risk"), "post-add open risk")) if proposal.get("post_add_open_risk") is not None else None,
                    legacy_incremental_risk,
                    proposal.get("rotation_step_id"),
                    proposal.get("approval_authority_fingerprint"),
                ),
            )
            conn.execute(
                """UPDATE order_intents SET displayed_fingerprint=?,execution_path=?,risk_snapshot_id=?,
                       canonical_quantity=?,canonical_notional=?,canonical_stop_risk=?,
                       approved_quantity_ceiling_decimal=?,approved_notional_ceiling_decimal=?,
                       requested_quantity_decimal=?,requested_notional_decimal=?,
                       reference_price_decimal=?,intended_stop_price_decimal=?,
                       reserved_notional_decimal=?,reserved_stop_risk_decimal=?,
                       canonical_quantity_decimal=?,canonical_notional_decimal=?,canonical_stop_risk_decimal=?,
                       initial_risk_dollars_decimal=?,incremental_risk_decimal=?,
                       filled_quantity_decimal='0',decimal_provenance=?,decimal_accounting_version=?
                       WHERE id=?""",
                (
                    proposal.get("displayed_fingerprint") or proposal.get("approval_authority_fingerprint"),
                    proposal.get("execution_path"),
                    proposal.get("risk_snapshot_id"),
                    legacy_float(sizing.quantity),
                    legacy_float(sizing.notional),
                    legacy_float(sizing.stop_risk),
                    decimal_text(approved_quantity_decimal),
                    decimal_text(approved_notional_decimal) if approved_notional_decimal is not None else None,
                    decimal_text(quantity),
                    decimal_text(requested_notional) if requested_notional is not None else None,
                    decimal_text(reference),
                    decimal_text(stop_price) if stop_price is not None else None,
                    decimal_text(reserved_notional),
                    decimal_text(reserved_stop_risk),
                    decimal_text(sizing.quantity),
                    decimal_text(sizing.notional),
                    decimal_text(sizing.stop_risk),
                    decimal_text(initial_risk_decimal) if initial_risk_decimal is not None else None,
                    decimal_text(incremental_risk),
                    EXACT_DECIMAL_PROVENANCE,
                    FIXED_POINT_ACCOUNTING_VERSION,
                    intent_id,
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
                    legacy_reserved_notional,
                    legacy_reserved_notional,
                    legacy_reserved_stop_risk,
                    legacy_reserved_stop_risk,
                    "active",
                    now,
                    now,
                    proposal.get("strategy_version"),
                    proposal.get("strategy_sleeve"),
                    proposal.get("sleeve_allocation_id"),
                    legacy_float(decimal_value(proposal.get("sleeve_notional_ceiling"), "sleeve notional ceiling")) if proposal.get("sleeve_notional_ceiling") is not None else None,
                    legacy_float(decimal_value(proposal.get("sleeve_stop_risk_ceiling"), "sleeve stop risk ceiling")) if proposal.get("sleeve_stop_risk_ceiling") is not None else None,
                    legacy_incremental_risk,
                    legacy_reserved_stop_risk,
                    "stop_risk_dollars",
                    proposal.get("conversion_equity"),
                    proposal.get("conversion_equity_as_of") or now,
                    proposal.get("risk_formula_version") or "risk_unit_to_stop_risk_dollars_v1",
                ),
            )
            conversion_equity_decimal = decimal_value(
                proposal.get("conversion_equity"),
                "conversion equity",
                minimum=ZERO,
                allow_none=True,
            )
            conn.execute(
                """UPDATE risk_reservations SET
                   initial_notional_decimal=?,active_notional_decimal=?,
                   initial_stop_risk_decimal=?,active_stop_risk_decimal=?,
                   incremental_risk_decimal=?,risk_value_decimal=?,conversion_equity_decimal=?,
                   decimal_provenance=?,decimal_accounting_version=?
                   WHERE intent_id=?""",
                (
                    decimal_text(reserved_notional), decimal_text(reserved_notional),
                    decimal_text(reserved_stop_risk), decimal_text(reserved_stop_risk),
                    decimal_text(incremental_risk), decimal_text(reserved_stop_risk),
                    decimal_text(conversion_equity_decimal) if conversion_equity_decimal is not None else None,
                    EXACT_DECIMAL_PROVENANCE, FIXED_POINT_ACCOUNTING_VERSION, intent_id,
                ),
            )
            conn.execute(
                """INSERT INTO order_events(
                       id,intent_id,event_key,from_state,to_state,event_type,safe_detail,created_at,
                       transition_counter,decimal_provenance,decimal_accounting_version)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
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
                    EXACT_DECIMAL_PROVENANCE,
                    FIXED_POINT_ACCOUNTING_VERSION,
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
                    legacy_reserved_notional if side == "buy" else legacy_float(decimal_value(proposal.get("notional"), "order notional")) if proposal.get("notional") is not None else None,
                    legacy_quantity,
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
            order_notional_decimal = requested_notional
            if side != "buy" and proposal.get("notional") is not None:
                order_notional_decimal = decimal_value(
                    proposal.get("notional"), "order notional", minimum=ZERO, allow_none=True,
                )
            conn.execute(
                """UPDATE orders SET notional_decimal=?,qty_decimal=?,decimal_provenance=?,
                   decimal_accounting_version=? WHERE id=?""",
                (
                    decimal_text(order_notional_decimal) if order_notional_decimal is not None else None,
                    decimal_text(quantity), EXACT_DECIMAL_PROVENANCE,
                    FIXED_POINT_ACCOUNTING_VERSION, intent_id,
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
                """INSERT INTO order_events(
                       id,intent_id,event_key,from_state,to_state,event_type,safe_detail,created_at,
                       transition_counter,decimal_provenance,decimal_accounting_version)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
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
                    EXACT_DECIMAL_PROVENANCE,
                    FIXED_POINT_ACCOUNTING_VERSION,
                ),
            )
            conn.execute(
                "UPDATE orders SET broker_order_id=COALESCE(?,broker_order_id),status=?,updated_at=? WHERE id=?",
                (broker_order_id, destination.value, now, intent_id),
            )
            if destination in TERMINAL_STATES:
                conn.execute(
                    """UPDATE risk_reservations SET active_notional=0,active_stop_risk=0,
                       active_notional_decimal='0',active_stop_risk_decimal='0',state='released',
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
        cumulative_quantity: Any,
        fill_price: Any,
        broker_event_key: str,
        broker_order_id: str | None = None,
        occurred_at: str | None = None,
        fees: Any = 0,
        adjustments: Any = 0,
        source: str = "broker_fill",
        price_is_cumulative_average: bool = False,
        broker_evidence: Mapping[str, Any] | None = None,
        account_id_hash: str | None = None,
    ) -> dict[str, Any]:
        cumulative_decimal = decimal_value(
            cumulative_quantity, "cumulative_quantity", minimum=ZERO
        )
        price_decimal = decimal_value(fill_price, "fill_price", minimum=ZERO)
        fees_decimal = decimal_value(fees, "fees", minimum=ZERO)
        adjustments_decimal = decimal_value(adjustments, "adjustments")
        assert (
            cumulative_decimal is not None
            and price_decimal is not None
            and fees_decimal is not None
            and adjustments_decimal is not None
        )
        if cumulative_decimal == ZERO:
            raise ValueError("cumulative_quantity must be positive for a fill event")
        if price_decimal == ZERO:
            raise ValueError("fill_price must be positive for a fill event")
        now = iso_now()
        with self.storage.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            intent = conn.execute("SELECT * FROM order_intents WHERE id=?", (intent_id,)).fetchone()
            if not intent:
                raise LookupError(f"order intent not found: {intent_id}")
            requested_raw = (
                intent["requested_quantity_decimal"]
                if "requested_quantity_decimal" in intent.keys()
                and intent["requested_quantity_decimal"] not in (None, "")
                else intent["requested_quantity"]
            )
            requested_authority = decimal_value(
                requested_raw, "requested_quantity", minimum=ZERO
            )
            assert requested_authority is not None
            if not str(broker_event_key or "").strip():
                raise ValueError("broker_event_key is required")
            if broker_evidence is None:
                # Existing unit fixtures use direct scalar calls.  Keep that
                # path explicitly test-only; production callers must provide
                # the raw broker response envelope so a fill cannot be
                # fabricated by supplying plausible quantity and price.
                if os.getenv("TRADING_AGENT_TESTING") != "1":
                    raise ValueError("verified broker evidence is required before recording a fill")
                broker_evidence = {
                    "source": "test_fixture_broker_evidence",
                    "broker_order_id": broker_order_id or f"test-broker-order:{intent_id}",
                    "client_order_id": intent["client_order_id"],
                    "symbol": intent["symbol"],
                    "side": intent["side"],
                    "status": "filled" if cumulative_decimal >= requested_authority else "partially_filled",
                    "execution_id": broker_event_key,
                    "filled_qty": decimal_text(cumulative_decimal),
                    "filled_avg_price": decimal_text(price_decimal),
                    "fees": decimal_text(fees_decimal),
                    "adjustments": decimal_text(adjustments_decimal),
                }
            evidence_payload = _broker_evidence_payload(broker_evidence)
            evidence_order_id = str(
                evidence_payload.get("broker_order_id") or broker_order_id or intent["broker_order_id"] or ""
            )
            evidence_client_id = str(
                evidence_payload.get("client_order_id") or intent["client_order_id"] or ""
            )
            evidence_symbol = str(evidence_payload.get("symbol") or intent["symbol"] or "").upper()
            evidence_side = str(evidence_payload.get("side") or intent["side"] or "").lower()
            evidence_status = str(
                evidence_payload.get("status")
                or ("filled" if cumulative_decimal >= requested_authority else "partially_filled")
            ).lower()
            if not evidence_order_id or not evidence_client_id:
                raise ValueError("broker evidence must contain broker and client order identities")
            if evidence_client_id != str(intent["client_order_id"]):
                raise ValueError("broker evidence client-order identity does not match intent")
            if intent["broker_order_id"] not in (None, "") and evidence_order_id != str(intent["broker_order_id"]):
                raise ValueError("broker evidence broker-order identity does not match intent")
            if evidence_symbol != str(intent["symbol"]).upper() or evidence_side != str(intent["side"]).lower():
                raise ValueError("broker evidence symbol/side does not match intent")
            if evidence_status not in {"partially_filled", "partial_fill", "filled", "late_fill_after_cancelled"}:
                raise ValueError("broker evidence status is not a fill status")
            reported_quantity = evidence_payload.get("filled_qty", evidence_payload.get("cumulative_quantity"))
            reported_price = evidence_payload.get("filled_avg_price", evidence_payload.get("fill_price"))
            if reported_quantity not in (None, "") and decimal_value(reported_quantity, "broker evidence filled quantity", minimum=ZERO) != cumulative_decimal:
                raise ValueError("broker evidence quantity does not match the fill event")
            if reported_price not in (None, "") and decimal_value(reported_price, "broker evidence fill price", minimum=ZERO) != price_decimal:
                raise ValueError("broker evidence price does not match the fill event")
            evidence_payload.update(
                {
                    "intent_id": str(intent["id"]),
                    "broker_event_key": str(broker_event_key),
                    "broker_order_id": evidence_order_id,
                    "client_order_id": evidence_client_id,
                    "symbol": evidence_symbol,
                    "side": evidence_side,
                    "status": evidence_status,
                    "filled_qty": decimal_text(cumulative_decimal),
                    "filled_avg_price": decimal_text(price_decimal),
                    "fees": decimal_text(fees_decimal),
                    "adjustments": decimal_text(adjustments_decimal),
                }
            )
            evidence_fingerprint = _broker_evidence_fingerprint(evidence_payload)
            evidence_id = evidence_fingerprint[:32]
            prior_evidence = conn.execute(
                "SELECT * FROM broker_fill_evidence WHERE broker_event_key=?",
                (broker_event_key,),
            ).fetchone()
            if prior_evidence is not None:
                if (
                    str(prior_evidence["intent_id"]) != str(intent["id"])
                    or str(prior_evidence["payload_fingerprint"]) != evidence_fingerprint
                    or str(prior_evidence["broker_order_id"]) != evidence_order_id
                    or str(prior_evidence["client_order_id"]) != evidence_client_id
                ):
                    raise ValueError("duplicate broker fill event payload conflicts with immutable evidence")
                prior = conn.execute(
                    "SELECT * FROM broker_fill_events WHERE broker_event_key=?",
                    (broker_event_key,),
                ).fetchone()
                if prior is not None:
                    return dict(intent)
            else:
                conn.execute(
                    """INSERT INTO broker_fill_evidence(
                       id,intent_id,broker_event_key,broker_order_id,client_order_id,symbol,side,
                       remote_status,payload,payload_fingerprint,evidence_source,account_id_hash,captured_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        evidence_id,
                        intent["id"],
                        broker_event_key,
                        evidence_order_id,
                        evidence_client_id,
                        evidence_symbol,
                        evidence_side,
                        evidence_status,
                        json.dumps(evidence_payload, sort_keys=True, separators=(",", ":"), default=str),
                        evidence_fingerprint,
                        str(evidence_payload.get("source") or source),
                        account_id_hash,
                        occurred_at or now,
                    ),
                )
            if intent["broker_order_id"] in (None, ""):
                conn.execute(
                    "UPDATE order_intents SET broker_order_id=?,updated_at=? WHERE id=?",
                    (evidence_order_id, now, intent_id),
                )
            requested = requested_authority
            previous = require_exact_decimal(
                intent,
                "filled_quantity_decimal",
                minimum=ZERO,
            )
            assert requested is not None and previous is not None
            if requested == ZERO:
                raise ValueError("requested_quantity must be positive")
            if cumulative_decimal < previous:
                # Retain/dedupe the stale broker event but never reduce quantity.
                counter = int(intent["transition_counter"] or 0) + 1
                conn.execute(
                    """INSERT INTO broker_fill_events(id,intent_id,broker_event_key,broker_order_id,cumulative_filled_quantity,
                           delta_quantity,fill_price,occurred_at,received_at,payload) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (
                        str(uuid.uuid4()), intent_id, broker_event_key, broker_order_id,
                        legacy_float(cumulative_decimal), 0.0, legacy_float(price_decimal),
                        occurred_at or now, now,
                        json_dumps(
                            {
                                "out_of_order": True,
                                "retained_cumulative": decimal_text(previous),
                                "canonical_decimal_evidence": True,
                                "broker_evidence_id": evidence_id,
                                "broker_evidence_fingerprint": evidence_fingerprint,
                            }
                        ),
                    ),
                )
                conn.execute(
                    """UPDATE broker_fill_events SET
                       cumulative_filled_quantity_decimal=?,delta_quantity_decimal='0',
                       fill_price_decimal=?,fees_decimal=?,adjustments_decimal=?,
                       decimal_provenance=?,decimal_accounting_version=?
                       WHERE broker_event_key=?""",
                    (
                        decimal_text(cumulative_decimal), decimal_text(price_decimal),
                        decimal_text(fees_decimal), decimal_text(adjustments_decimal),
                        EXACT_DECIMAL_PROVENANCE, FIXED_POINT_ACCOUNTING_VERSION,
                        broker_event_key,
                    ),
                )
                conn.execute(
                    """UPDATE pnl_ledger_status SET confidence='partially_reconstructed',
                           provenance='late lower cumulative broker event; authoritative history required',
                           last_event_at=?,updated_at=? WHERE scope='prospective'""",
                    (occurred_at or now, now),
                )
                conn.execute(
                    """INSERT INTO order_events(
                           id,intent_id,event_key,from_state,to_state,event_type,filled_quantity,fill_price,
                           safe_detail,created_at,transition_counter,filled_quantity_decimal,fill_price_decimal,
                           decimal_provenance,decimal_accounting_version)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        str(uuid.uuid4()), intent_id,
                        f"{intent_id}:out_of_order_fill:{broker_event_key}",
                        intent["state"], intent["state"], "out_of_order_fill_ignored",
                        legacy_float(cumulative_decimal), legacy_float(price_decimal),
                        json_dumps({"reported": decimal_text(cumulative_decimal), "retained": decimal_text(previous)}),
                        now, counter, decimal_text(cumulative_decimal), decimal_text(price_decimal),
                        EXACT_DECIMAL_PROVENANCE, FIXED_POINT_ACCOUNTING_VERSION,
                    ),
                )
                conn.execute("UPDATE order_intents SET transition_counter=?,updated_at=? WHERE id=?", (counter, now, intent_id))
                return dict(intent)
            if cumulative_decimal > requested:
                raise ValueError("broker fill cumulative quantity exceeds the requested quantity")
            cumulative = cumulative_decimal
            delta = max(ZERO, cumulative - previous)
            if delta == ZERO and (
                fees_decimal != ZERO or adjustments_decimal != ZERO
            ):
                raise ValueError(
                    "zero-delta fill events cannot introduce fees or adjustments"
                )
            prior_avg = require_exact_decimal(
                intent,
                "average_fill_price_decimal",
                minimum=ZERO,
                allow_none=True,
            ) or ZERO
            delta_fill_price = price_decimal
            if price_is_cumulative_average and delta > ZERO:
                delta_fill_price = max(
                    ZERO,
                    ((cumulative * price_decimal) - (previous * prior_avg)) / delta,
                )
                average = price_decimal
            else:
                average = (
                    ((previous * prior_avg) + (delta * price_decimal)) / cumulative
                    if cumulative > ZERO
                    else None
                )
            quote = {
                "bid": intent["quote_bid"], "ask": intent["quote_ask"],
                "midpoint": ((float(intent["quote_bid"]) + float(intent["quote_ask"])) / 2.0)
                if intent["quote_bid"] is not None and intent["quote_ask"] is not None else None,
            }
            shortfall = implementation_shortfall_bps(
                quote, intent["side"], float(average or price_decimal)
            )
            current = OrderState(intent["state"])
            late_after_cancel = current == OrderState.CANCELLED and cumulative > previous
            target = current if late_after_cancel else (
                OrderState.FILLED
                if cumulative >= requested
                else OrderState.PARTIALLY_FILLED
            )
            if current != target:
                validate_transition(current, target)
            counter = int(intent["transition_counter"] or 0) + 1
            fill_event_id = str(uuid.uuid4())
            conn.execute(
                """INSERT INTO broker_fill_events(id,intent_id,broker_event_key,broker_order_id,cumulative_filled_quantity,
                       delta_quantity,fill_price,occurred_at,received_at,payload) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    fill_event_id, intent_id, broker_event_key, broker_order_id,
                    legacy_float(cumulative), legacy_float(delta), legacy_float(delta_fill_price),
                    occurred_at or now, now,
                        json_dumps(
                            {
                                "aggregate": True,
                                "reported_price": decimal_text(price_decimal),
                                "price_semantics": "cumulative_average"
                                if price_is_cumulative_average
                                else "delta_execution",
                                "canonical_decimal_evidence": True,
                                "broker_evidence_id": evidence_id,
                                "broker_evidence_fingerprint": evidence_fingerprint,
                            }
                        ),
                ),
            )
            conn.execute(
                """UPDATE broker_fill_events SET
                   cumulative_filled_quantity_decimal=?,delta_quantity_decimal=?,
                   fill_price_decimal=?,fees_decimal=?,adjustments_decimal=?,
                   decimal_provenance=?,decimal_accounting_version=?
                   WHERE broker_event_key=?""",
                (
                    decimal_text(cumulative), decimal_text(delta),
                    decimal_text(delta_fill_price), decimal_text(fees_decimal),
                    decimal_text(adjustments_decimal), EXACT_DECIMAL_PROVENANCE,
                    FIXED_POINT_ACCOUNTING_VERSION, broker_event_key,
                ),
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
                fees=fees_decimal,
                adjustments=adjustments_decimal,
                source=source,
            )
            from .profit_milestones import apply_take_profit_fill_in_transaction

            apply_take_profit_fill_in_transaction(
                conn,
                intent=dict(intent),
                fill_event_id=fill_event_id,
                broker_event_key=broker_event_key,
                cumulative_quantity=legacy_float(cumulative),
                delta_quantity=legacy_float(delta),
                fill_price=legacy_float(delta_fill_price),
                occurred_at=occurred_at or now,
                now=now,
            )
            conn.execute(
                """UPDATE order_intents SET filled_quantity=?,average_fill_price=?,state=?,broker_order_id=COALESCE(?,broker_order_id),
                       implementation_shortfall_bps=?,filled_quantity_decimal=?,average_fill_price_decimal=?,
                       decimal_provenance=?,decimal_accounting_version=?,
                       updated_at=?,terminal_at=?,transition_counter=? WHERE id=?""",
                (
                    legacy_float(cumulative), legacy_float(average), target.value, broker_order_id,
                    shortfall, decimal_text(cumulative),
                    decimal_text(average) if average is not None else None,
                    EXACT_DECIMAL_PROVENANCE, FIXED_POINT_ACCOUNTING_VERSION,
                    now, now if target == OrderState.FILLED else None, counter, intent_id,
                ),
            )
            remaining_ratio_decimal = max(ZERO, requested - cumulative) / requested
            reservation_state = "released" if target == OrderState.FILLED or late_after_cancel else "active"
            if late_after_cancel:
                remaining_ratio_decimal = ZERO
            reservation = conn.execute(
                "SELECT * FROM risk_reservations WHERE intent_id=? AND state IN ('active','released')",
                (intent_id,),
            ).fetchone()
            if reservation is None:
                raise ValueError("risk reservation is missing while recording a fill")
            initial_notional = require_exact_decimal(
                reservation, "initial_notional_decimal", minimum=ZERO
            )
            initial_stop_risk = require_exact_decimal(
                reservation, "initial_stop_risk_decimal", minimum=ZERO
            )
            assert initial_notional is not None and initial_stop_risk is not None
            active_notional_decimal = initial_notional * remaining_ratio_decimal
            active_stop_risk_decimal = initial_stop_risk * remaining_ratio_decimal
            conn.execute(
                """UPDATE risk_reservations SET active_notional=?,active_stop_risk=?,
                       active_notional_decimal=?,active_stop_risk_decimal=?,
                       state=?,released_at=CASE WHEN ?='released' THEN COALESCE(released_at,?) ELSE released_at END,
                       release_reason=CASE WHEN ?='released' THEN COALESCE(release_reason,'filled') ELSE release_reason END,
                       updated_at=?,version=version+1 WHERE intent_id=?""",
                (
                    legacy_float(active_notional_decimal), legacy_float(active_stop_risk_decimal),
                    decimal_text(active_notional_decimal), decimal_text(active_stop_risk_decimal),
                    reservation_state, reservation_state, now, reservation_state, now, intent_id,
                ),
            )
            conn.execute(
                """INSERT INTO order_events(
                       id,intent_id,event_key,from_state,to_state,event_type,broker_event_id,
                       filled_quantity,fill_price,safe_detail,created_at,transition_counter,
                       filled_quantity_decimal,fill_price_decimal,decimal_provenance,decimal_accounting_version)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    str(uuid.uuid4()), intent_id, f"{intent_id}:fill:{broker_event_key}",
                    current.value, target.value,
                    "late_fill_after_cancelled" if late_after_cancel else (
                        "final_fill" if target == OrderState.FILLED else "partial_fill"
                    ),
                    broker_event_key, legacy_float(cumulative), legacy_float(delta_fill_price),
                    json_dumps({"delta_quantity": decimal_text(delta)}), now, counter,
                    decimal_text(cumulative), decimal_text(delta_fill_price),
                    EXACT_DECIMAL_PROVENANCE, FIXED_POINT_ACCOUNTING_VERSION,
                ),
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
                    (legacy_float(cumulative), legacy_float(average), occurred_at or now, json_dumps({"aggregate": True, "intent_id": intent_id, "canonical_decimal_evidence": True}), shortfall, target.value, target.value, target.value, intent_id),
                )
                conn.execute(
                    """UPDATE fills SET qty_decimal=?,price_decimal=?,decimal_provenance=?,
                       decimal_accounting_version=? WHERE order_id=?""",
                    (
                        decimal_text(cumulative),
                        decimal_text(average) if average is not None else None,
                        EXACT_DECIMAL_PROVENANCE, FIXED_POINT_ACCOUNTING_VERSION, intent_id,
                    ),
                )
            else:
                conn.execute(
                    """INSERT INTO fills(
                       run_id,order_id,qty,price,filled_at,payload,implementation_shortfall_bps,
                       fill_notification_status,qty_decimal,price_decimal,decimal_provenance,
                       decimal_accounting_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        intent["run_id"], intent_id, legacy_float(cumulative), legacy_float(average),
                        occurred_at or now,
                        json_dumps({"aggregate": True, "intent_id": intent_id, "canonical_decimal_evidence": True}),
                        shortfall, "pending", decimal_text(cumulative),
                        decimal_text(average) if average is not None else None,
                        EXACT_DECIMAL_PROVENANCE, FIXED_POINT_ACCOUNTING_VERSION,
                    ),
                )
        return self.get_intent(intent_id)

    def active_reservations(self) -> dict[str, Any]:
        rows = self.storage.fetch_all(
            "SELECT * FROM risk_reservations WHERE state='active'"
        )
        total_notional_decimal = ZERO
        total_stop_risk_decimal = ZERO
        by_symbol: dict[str, float] = {}
        by_cluster: dict[str, float] = {}
        by_symbol_decimal: dict[str, Decimal] = {}
        by_cluster_decimal: dict[str, Decimal] = {}
        for row in rows:
            notional = require_exact_decimal(
                row, "active_notional_decimal", minimum=ZERO
            )
            stop_risk = require_exact_decimal(
                row, "active_stop_risk_decimal", minimum=ZERO
            )
            assert notional is not None and stop_risk is not None
            total_notional_decimal += notional
            total_stop_risk_decimal += stop_risk
            by_symbol[row["symbol"]] = by_symbol.get(row["symbol"], 0.0) + float(notional)
            by_symbol_decimal[row["symbol"]] = by_symbol_decimal.get(row["symbol"], ZERO) + notional
            if row.get("cluster_name"):
                by_cluster[row["cluster_name"]] = by_cluster.get(row["cluster_name"], 0.0) + float(notional)
                by_cluster_decimal[row["cluster_name"]] = by_cluster_decimal.get(row["cluster_name"], ZERO) + notional
        return {
            # Decimal text is the authoritative aggregate. Legacy numeric
            # fields remain as compatibility projections for older callers.
            "active_reserved_notional_decimal": decimal_text(total_notional_decimal),
            "active_reserved_stop_risk_decimal": decimal_text(total_stop_risk_decimal),
            "active_reserved_notional": float(total_notional_decimal),
            "active_reserved_stop_risk": float(total_stop_risk_decimal),
            "symbol_reserved_notional": by_symbol,
            "cluster_reserved_notional": by_cluster,
            "symbol_reserved_notional_decimal": {
                symbol: decimal_text(value) for symbol, value in by_symbol_decimal.items()
            },
            "cluster_reserved_notional_decimal": {
                cluster: decimal_text(value) for cluster, value in by_cluster_decimal.items()
            },
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
            "SELECT COUNT(*) AS n FROM order_intents WHERE state IN ('created','reserved','retryable_pre_submission')"
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
            "active_intents_missing_reservations": "SELECT COUNT(*) n FROM order_intents i LEFT JOIN risk_reservations r ON r.intent_id=i.id WHERE i.state IN ('created','reserved','retryable_pre_submission','submitting','submitted','partially_filled','cancel_pending','unknown','reconciliation_required') AND r.id IS NULL",
            "duplicate_client_order_ids": "SELECT COUNT(*) n FROM (SELECT client_order_id FROM order_intents GROUP BY client_order_id HAVING COUNT(*)>1)",
            "fills_exceeding_quantity": "SELECT 0 n",
            "stale_unknown_intents": "SELECT COUNT(*) n FROM order_intents WHERE state='unknown' AND (julianday('now')-julianday(updated_at))*86400>300",
            "stale_partial_fills": "SELECT COUNT(*) n FROM order_intents WHERE state='partially_filled' AND (julianday('now')-julianday(updated_at))*86400>300",
            "position_state_without_active_lifecycle": "SELECT COUNT(*) n FROM position_management_state s LEFT JOIN position_lifecycles l ON l.id=s.position_lifecycle_id AND l.state='active' WHERE s.position_lifecycle_id IS NOT NULL AND l.id IS NULL",
            "state_latest_event_mismatch": """SELECT COUNT(*) n FROM order_intents i
                WHERE COALESCE((SELECT e.to_state FROM order_events e WHERE e.intent_id=i.id
                                ORDER BY e.transition_counter DESC,e.created_at DESC LIMIT 1),'') <> i.state""",
            "transition_counter_mismatch": """SELECT COUNT(*) n FROM order_intents i
                WHERE COALESCE((SELECT MAX(e.transition_counter) FROM order_events e WHERE e.intent_id=i.id),-1)
                      <> i.transition_counter""",
            "fill_ledger_mismatch": "SELECT 0 n",
            "broker_fill_evidence_orphaned": """SELECT COUNT(*) n FROM broker_fill_evidence e
                LEFT JOIN order_intents i ON i.id=e.intent_id
                LEFT JOIN broker_fill_events f ON f.broker_event_key=e.broker_event_key
                WHERE i.id IS NULL OR f.id IS NULL""",
            "broker_fill_evidence_identity_mismatch": """SELECT COUNT(*) n FROM broker_fill_evidence e
                JOIN order_intents i ON i.id=e.intent_id
                WHERE e.client_order_id<>i.client_order_id
                   OR COALESCE(i.broker_order_id,'')<>e.broker_order_id
                   OR upper(e.symbol)<>upper(i.symbol)
                   OR lower(e.side)<>lower(i.side)""",
            "broker_fill_events_missing_immutable_evidence": """SELECT COUNT(*) n FROM broker_fill_events f
                WHERE (json_valid(f.payload)<>1
                   OR COALESCE(json_extract(f.payload,'$.broker_evidence_id'),'')=''
                   OR NOT EXISTS(
                       SELECT 1 FROM broker_fill_evidence e
                       WHERE e.id=json_extract(f.payload,'$.broker_evidence_id')
                         AND e.broker_event_key=f.broker_event_key
                         AND e.intent_id=f.intent_id
                   ))
                  AND f.occurred_at>=COALESCE((SELECT value FROM runtime_metadata WHERE key='final_hardening_effective_at'),'9999')""",
            "broker_relevant_missing_identity": """SELECT COUNT(*) n FROM order_intents
                WHERE state IN ('submitting','submitted','partially_filled','cancel_pending','unknown','reconciliation_required')
                  AND COALESCE(client_order_id,'')='' AND COALESCE(broker_order_id,'')=''""",
            "accepted_approvals_missing_display_authority": """SELECT COUNT(*) n FROM approvals
                WHERE status IN ('accepted','consumed') AND (
                  display_envelope_id IS NULL OR displayed_fingerprint IS NULL OR authority_fingerprint<>displayed_fingerprint)
                  AND created_at>=COALESCE((SELECT value FROM runtime_metadata WHERE key='final_hardening_effective_at'),'9999')""",
            "approvals_with_missing_display_row": """SELECT COUNT(*) n FROM approvals a
                LEFT JOIN proposal_display_envelopes d ON d.id=a.display_envelope_id
                WHERE a.status IN ('accepted','consumed') AND d.id IS NULL
                  AND a.created_at>=COALESCE((SELECT value FROM runtime_metadata WHERE key='final_hardening_effective_at'),'9999')""",
            "display_proposal_fingerprint_mismatch": """SELECT COUNT(*) n FROM proposal_display_envelopes d
                JOIN trade_proposals p ON p.id=d.proposal_id
                WHERE COALESCE(p.displayed_fingerprint,'')<>d.displayed_fingerprint
                   OR COALESCE(p.proposal_version,1)<>d.proposal_version""",
            "intent_approval_display_mismatch": """SELECT COUNT(*) n FROM order_intents i
                JOIN approvals a ON a.id=i.approval_id
                WHERE (COALESCE(i.approval_authority_fingerprint,'')<>COALESCE(a.authority_fingerprint,'')
                   OR COALESCE(i.displayed_fingerprint,'')<>COALESCE(a.displayed_fingerprint,''))
                  AND i.created_at>=COALESCE((SELECT value FROM runtime_metadata WHERE key='final_hardening_effective_at'),'9999')""",
            "intent_canonical_sizing_mismatch": "SELECT 0 n",
            "intents_missing_authoritative_risk_snapshot": """SELECT COUNT(*) n FROM order_intents i
                LEFT JOIN execution_risk_snapshots s ON s.id=i.risk_snapshot_id
                WHERE i.created_at IS NOT NULL AND (s.id IS NULL OR s.authoritative<>1
                  OR s.proposal_id<>i.proposal_id OR s.approval_id<>i.approval_id)
                  AND i.created_at>=COALESCE((SELECT value FROM runtime_metadata WHERE key='final_hardening_effective_at'),'9999')""",
            "ambiguous_broker_state_marked_not_invoked": """SELECT COUNT(*) n FROM order_intents
                WHERE state IN ('unknown','reconciliation_required')
                  AND COALESCE(broker_invocation_occurred,0)<>1
                  AND EXISTS(SELECT 1 FROM order_events e WHERE e.intent_id=order_intents.id
                             AND e.event_type='broker_submission_ambiguous')
                  AND created_at>=COALESCE((SELECT value FROM runtime_metadata WHERE key='final_hardening_effective_at'),'9999')""",
            "duplicate_active_exit_blockers": """SELECT COUNT(*) n FROM (
                SELECT symbol FROM exit_blocker_states WHERE active=1 GROUP BY symbol HAVING COUNT(*)>1)""",
            "terminal_exit_blocker_marked_active": """SELECT COUNT(*) n FROM exit_blocker_states
                WHERE active=1 AND state IN ('cleared','terminal_attempt_failed','position_closed','superseded')""",
            "orphaned_profitability_validation_decisions": """SELECT COUNT(*) n
                FROM profitability_validation_decisions d
                LEFT JOIN profitability_validation_families f ON f.id=d.family_id
                WHERE f.id IS NULL""",
            "orphaned_profitability_validation_folds": """SELECT COUNT(*) n
                FROM profitability_validation_folds vf
                LEFT JOIN profitability_validation_decisions d ON d.id=vf.decision_id
                WHERE d.id IS NULL""",
            "incomplete_profitability_validation_families": """SELECT COUNT(*) n
                FROM profitability_validation_families f
                WHERE json_valid(f.hypotheses_json)<>1
                  OR json_valid(f.observations_json)<>1
                  OR CASE WHEN json_valid(f.hypotheses_json)=1 THEN
                    json_type(f.hypotheses_json)<>'array'
                    OR json_array_length(f.hypotheses_json)<>(
                      SELECT COUNT(*)
                      FROM profitability_validation_decisions d
                      WHERE d.family_id=f.id)
                    ELSE 0 END
                  OR CASE WHEN json_valid(f.observations_json)=1 THEN
                    json_type(f.observations_json)<>'array'
                    ELSE 0 END""",
            "strategy_validation_authority_mismatch": """SELECT COUNT(*) n
                FROM strategy_policy_decisions p
                LEFT JOIN strategy_performance_snapshots s
                  ON s.id=p.performance_snapshot_id
                LEFT JOIN profitability_validation_decisions d
                  ON d.id=p.validation_decision_id
                LEFT JOIN profitability_validation_families f
                  ON f.id=p.validation_family_id
                WHERE COALESCE(p.validation_status,'disabled')<>'disabled'
                  AND (s.id IS NULL OR d.id IS NULL OR f.id IS NULL
                    OR p.validation_family_id<>d.family_id
                    OR p.strategy_version<>d.strategy_version
                    OR p.validation_status<>d.status
                    OR p.validation_fingerprint<>d.decision_fingerprint
                    OR s.validation_family_id<>p.validation_family_id
                    OR s.validation_decision_id<>p.validation_decision_id
                    OR s.validation_status<>p.validation_status
                    OR s.validation_fingerprint<>p.validation_fingerprint)""",
            "orphaned_profit_attribution_records": """SELECT COUNT(*) n
                FROM profit_attribution_records a
                LEFT JOIN position_lifecycles l ON l.id=a.position_lifecycle_id
                WHERE l.id IS NULL""",
            "profit_attribution_reconciliation_mismatch": """SELECT COUNT(*) n
                FROM profit_attribution_records
                WHERE status IN ('complete','partial') AND (
                  reconciliation_residual IS NULL
                  OR typeof(reconciliation_residual)<>'text'
                  OR reconciliation_residual<>'0'
                  OR CASE WHEN json_valid(components_json)=1 THEN
                    status='complete' AND (
                      json_extract(components_json,'$.variance_reconciliation_residual') IS NULL
                      OR json_type(components_json,'$.variance_reconciliation_residual')<>'text'
                      OR json_extract(components_json,'$.variance_reconciliation_residual')<>'0')
                    ELSE 1 END)""",
            "strategy_trade_attribution_mismatch": """SELECT COUNT(*) n
                FROM strategy_trade_records t
                LEFT JOIN profit_attribution_records a ON a.id=t.profit_attribution_id
                WHERE t.evidence_class='actual_paper'
                  AND t.attribution_status IN ('complete','partial')
                  AND t.updated_at>=COALESCE((
                    SELECT applied_at FROM schema_migrations
                    WHERE version='profit_attribution_records_v1'), '9999')
                  AND (a.id IS NULL
                    OR t.position_lifecycle_id IS NULL
                    OR a.position_lifecycle_id<>t.position_lifecycle_id
                    OR a.status<>t.attribution_status)""",
            "counterfactual_profitability_evidence": """SELECT COUNT(*) n
                FROM profitability_validation_families f,
                  json_each(CASE WHEN json_valid(f.observations_json)=1
                    THEN f.observations_json ELSE '[]' END) o
                WHERE CASE WHEN json_valid(o.value)=1
                    THEN json_extract(o.value,'$.evidence_class')
                    ELSE 'invalid' END
                  NOT IN ('shadow_oos','actual_paper')""",
            "orphaned_crypto_asset_capabilities": """SELECT COUNT(*) n
                FROM crypto_asset_capabilities a
                LEFT JOIN crypto_capability_snapshots s ON s.id=a.snapshot_id
                WHERE s.id IS NULL""",
            "incomplete_authoritative_crypto_capabilities": """SELECT COUNT(*) n
                FROM crypto_capability_snapshots s
                WHERE s.authoritative=1 AND (
                  COALESCE(s.paper_account_id_hash,'')=''
                  OR COALESCE(s.config_hash,'')=''
                  OR COALESCE(s.official_contract_fingerprint,'')=''
                  OR json_valid(s.failure_reasons_json)<>1
                  OR json_valid(s.evidence_json)<>1
                  OR CASE WHEN json_valid(s.failure_reasons_json)=1
                    THEN json_array_length(s.failure_reasons_json)<>0 ELSE 1 END
                  OR s.asset_count<>(SELECT COUNT(*) FROM crypto_asset_capabilities a WHERE a.snapshot_id=s.id)
                  OR EXISTS(SELECT 1 FROM crypto_asset_capabilities a
                            WHERE a.snapshot_id=s.id AND (
                              a.authoritative<>1 OR a.asset_class<>'crypto'
                              OR a.exchange<>'CRYPTO' OR a.status<>'active'
                              OR a.tradable<>1 OR a.fractionable<>1
                              OR a.marginable<>0 OR a.shortable<>0 OR a.easy_to_borrow<>0
                              OR COALESCE(a.min_order_size,'')=''
                              OR COALESCE(a.min_trade_increment,'')=''
                              OR COALESCE(a.price_increment,'')=''))
                )""",
            "orphaned_crypto_market_evidence": """SELECT COUNT(*) n
                FROM crypto_market_data_evidence e
                LEFT JOIN crypto_capability_snapshots s ON s.id=e.capability_snapshot_id
                WHERE s.id IS NULL
                   OR s.snapshot_fingerprint<>e.capability_snapshot_fingerprint
                   OR s.config_hash<>e.config_hash""",
            "incomplete_authoritative_crypto_market_evidence": """SELECT COUNT(*) n
                FROM crypto_market_data_evidence e
                LEFT JOIN crypto_capability_snapshots s ON s.id=e.capability_snapshot_id
                WHERE (e.execution_eligible=1 AND e.authoritative<>1)
                   OR (e.authoritative=1 AND (
                  s.id IS NULL OR s.authoritative<>1
                  OR s.snapshot_fingerprint<>e.capability_snapshot_fingerprint
                  OR s.config_hash<>e.config_hash
                  OR COALESCE(e.capability_snapshot_fingerprint,'')=''
                  OR COALESCE(e.config_hash,'')=''
                  OR COALESCE(e.bid_price,'')=''
                  OR COALESCE(e.ask_price,'')=''
                  OR COALESCE(e.quote_timestamp,'')=''
                  OR COALESCE(e.orderbook_bid_price,'')=''
                  OR COALESCE(e.orderbook_ask_price,'')=''
                  OR COALESCE(e.orderbook_timestamp,'')=''
                  OR json_valid(e.failure_reasons_json)<>1
                  OR json_valid(e.warnings_json)<>1
                  OR json_valid(e.evidence_json)<>1
                  OR (e.execution_eligible=1 AND CASE
                    WHEN json_valid(e.failure_reasons_json)=1
                    THEN json_array_length(e.failure_reasons_json)<>0 ELSE 1 END)
                ))""",
            "crypto_research_evidence_binding_mismatch": """SELECT COUNT(*) n
                FROM crypto_research_snapshots r
                LEFT JOIN crypto_capability_snapshots c ON c.id=r.capability_snapshot_id
                LEFT JOIN crypto_market_data_evidence m ON m.id=r.market_evidence_id
                WHERE (r.capability_authoritative=1 AND (
                         c.id IS NULL OR c.authoritative<>1
                         OR c.snapshot_fingerprint<>r.capability_snapshot_fingerprint))
                   OR (r.market_evidence_authoritative=1 AND (
                         m.id IS NULL OR m.authoritative<>1
                         OR m.evidence_fingerprint<>r.market_evidence_fingerprint
                         OR m.symbol<>r.symbol
                         OR m.research_run_id<>r.research_run_id
                         OR m.capability_snapshot_id<>r.capability_snapshot_id))""",
            "orphaned_crypto_risk_snapshots": """SELECT COUNT(*) n
                FROM crypto_risk_snapshots r
                LEFT JOIN crypto_capability_snapshots c ON c.id=r.capability_snapshot_id
                LEFT JOIN crypto_market_data_evidence m ON m.id=r.market_evidence_id
                WHERE c.id IS NULL OR m.id IS NULL
                   OR c.snapshot_fingerprint<>r.capability_snapshot_fingerprint
                   OR m.evidence_fingerprint<>r.market_evidence_fingerprint
                   OR m.capability_snapshot_id<>r.capability_snapshot_id
                   OR c.config_hash<>r.config_hash OR m.config_hash<>r.config_hash""",
            "incomplete_authoritative_crypto_risk_snapshots": """SELECT COUNT(*) n
                FROM crypto_risk_snapshots r
                WHERE r.authoritative=1 AND (
                  COALESCE(r.paper_account_id_hash,'')=''
                  OR COALESCE(r.config_hash,'')=''
                  OR json_valid(r.account_json)<>1
                  OR json_valid(r.positions_json)<>1
                  OR json_valid(r.open_orders_json)<>1
                  OR json_valid(r.durable_state_json)<>1
                  OR json_valid(r.loss_evidence_json)<>1
                  OR json_valid(r.volatility_evidence_json)<>1
                  OR json_valid(r.aggregate_json)<>1
                  OR json_valid(r.derived_authority_json)<>1
                  OR json_valid(r.failure_reasons_json)<>1
                  OR json_valid(r.snapshot_json)<>1
                  OR CASE WHEN json_valid(r.failure_reasons_json)=1
                    THEN json_array_length(r.failure_reasons_json)<>0 ELSE 1 END
                  OR COALESCE(json_extract(r.derived_authority_json,'$.hard_notional_ceiling'),'')=''
                  OR COALESCE(json_extract(r.derived_authority_json,'$.hard_stop_risk_ceiling'),'')=''
                )""",
            "orphaned_crypto_sizing_decisions": """SELECT COUNT(*) n
                FROM crypto_sizing_decisions s
                LEFT JOIN crypto_risk_snapshots r ON r.id=s.risk_snapshot_id
                LEFT JOIN crypto_capability_snapshots c ON c.id=s.capability_snapshot_id
                LEFT JOIN crypto_market_data_evidence m ON m.id=s.market_evidence_id
                WHERE r.id IS NULL OR c.id IS NULL OR m.id IS NULL
                   OR r.snapshot_fingerprint<>s.risk_snapshot_fingerprint
                   OR c.snapshot_fingerprint<>s.capability_snapshot_fingerprint
                   OR m.evidence_fingerprint<>s.market_evidence_fingerprint
                   OR r.capability_snapshot_id<>s.capability_snapshot_id
                   OR r.market_evidence_id<>s.market_evidence_id
                   OR r.config_hash<>s.config_hash""",
            "invalid_crypto_sizing_authority": """SELECT COUNT(*) n
                FROM crypto_sizing_decisions s
                WHERE s.execution_authorized<>0
                   OR (s.authoritative=1 AND (
                     s.eligible<>1
                     OR COALESCE(s.canonical_quantity,'')=''
                     OR COALESCE(s.canonical_notional,'')=''
                     OR COALESCE(s.canonical_stop_risk,'')=''
                     OR json_valid(s.blockers_json)<>1
                     OR json_array_length(s.blockers_json)<>0
                     OR json_valid(s.binding_caps_json)<>1
                     OR json_valid(s.decision_json)<>1))""",
            "orphaned_crypto_risk_decisions": """SELECT COUNT(*) n
                FROM crypto_risk_decisions d
                LEFT JOIN crypto_risk_snapshots r ON r.id=d.snapshot_id
                LEFT JOIN crypto_sizing_decisions s ON s.id=d.sizing_decision_id
                WHERE r.id IS NULL OR s.id IS NULL
                   OR r.snapshot_fingerprint<>d.snapshot_fingerprint
                   OR s.decision_fingerprint<>d.sizing_fingerprint
                   OR s.risk_snapshot_id<>d.snapshot_id
                   OR d.config_hash<>r.config_hash OR d.config_hash<>s.config_hash""",
            "orphaned_crypto_strategy_decisions": """SELECT COUNT(*) n
                FROM crypto_strategy_decisions d
                LEFT JOIN crypto_market_data_evidence m ON m.id=d.market_evidence_id
                LEFT JOIN crypto_research_runs rr ON rr.id=d.research_run_id
                WHERE m.id IS NULL OR rr.id IS NULL
                   OR m.evidence_fingerprint<>d.market_evidence_fingerprint
                   OR m.research_run_id<>d.research_run_id
                   OR m.symbol<>d.symbol OR m.config_hash<>d.config_hash
                   OR rr.run_id<>d.run_id""",
            "invalid_crypto_strategy_authority": """SELECT COUNT(*) n
                FROM crypto_strategy_decisions d
                WHERE d.proposal_authorized<>0 OR d.execution_authorized<>0
                   OR d.lifecycle NOT IN ('RESEARCH_ONLY','PAPER_ACTIVE')
                   OR json_valid(d.blockers_json)<>1
                   OR json_valid(d.decision_json)<>1
                   OR (d.signal_eligible=1 AND (
                     d.selected_strategy IS NULL OR d.action<>'entry'
                     OR d.stop_price IS NULL OR d.target_price IS NULL
                     OR json_array_length(d.blockers_json)<>0))""",
            "orphaned_crypto_proposal_previews": """SELECT COUNT(*) n
                FROM crypto_proposal_previews p
                LEFT JOIN crypto_strategy_decisions d ON d.id=p.strategy_decision_id
                LEFT JOIN crypto_risk_decisions r ON r.id=p.risk_decision_id
                LEFT JOIN crypto_risk_snapshots s ON s.id=p.risk_snapshot_id
                LEFT JOIN crypto_sizing_decisions z ON z.id=p.sizing_decision_id
                LEFT JOIN crypto_capability_snapshots c ON c.id=p.capability_snapshot_id
                LEFT JOIN crypto_market_data_evidence m ON m.id=p.market_evidence_id
                WHERE d.id IS NULL OR r.id IS NULL OR s.id IS NULL OR z.id IS NULL
                   OR c.id IS NULL OR m.id IS NULL
                   OR d.decision_fingerprint<>p.strategy_decision_fingerprint
                   OR r.decision_fingerprint<>p.risk_decision_fingerprint
                   OR s.snapshot_fingerprint<>p.risk_snapshot_fingerprint
                   OR z.decision_fingerprint<>p.sizing_decision_fingerprint
                   OR c.snapshot_fingerprint<>p.capability_snapshot_fingerprint
                   OR m.evidence_fingerprint<>p.market_evidence_fingerprint
                   OR p.config_hash<>d.config_hash OR p.config_hash<>r.config_hash
                   OR p.config_hash<>s.config_hash OR p.config_hash<>z.config_hash""",
            "invalid_crypto_proposal_preview_authority": """SELECT COUNT(*) n
                FROM crypto_proposal_previews p
                WHERE p.status<>'research_only_preview'
                   OR p.manual_approval_eligible<>0 OR p.execution_authorized<>0
                   OR json_valid(p.display_json)<>1 OR json_valid(p.proposal_json)<>1
                   OR json_extract(p.display_json,'$.approval_command_enabled')<>0""",
            "crypto_execution_authority_escape": """SELECT
                  (SELECT COUNT(*) FROM crypto_sizing_decisions WHERE execution_authorized<>0)
                  + (SELECT COUNT(*) FROM crypto_risk_decisions WHERE execution_authorized<>0)
                  + (SELECT COUNT(*) FROM crypto_strategy_decisions WHERE proposal_authorized<>0 OR execution_authorized<>0)
                  + (SELECT COUNT(*) FROM crypto_proposal_previews WHERE manual_approval_eligible<>0 OR execution_authorized<>0) n""",
            "orphaned_crypto_profitability_validation": """SELECT COUNT(*) n
                FROM crypto_profitability_decisions p
                LEFT JOIN profitability_validation_families f
                  ON f.id=p.validation_family_id
                LEFT JOIN profitability_validation_decisions d
                  ON d.id=p.validation_decision_id
                WHERE f.id IS NULL OR d.id IS NULL
                   OR p.validation_family_id<>d.family_id
                   OR p.config_hash<>f.config_hash
                   OR p.validation_decision_fingerprint<>d.decision_fingerprint
                   OR p.walk_forward_status<>d.status
                   OR p.validation_sample_count<>d.sample_count
                   OR p.validation_fold_count<>d.fold_count
                   OR p.validation_reason<>d.reason""",
            "invalid_crypto_profitability_authority": """SELECT COUNT(*) n
                FROM crypto_profitability_decisions p
                WHERE p.validation_status='validated'
                  AND (p.walk_forward_status<>'validated'
                       OR json_valid(p.rejection_reasons_json)<>1
                       OR json_array_length(p.rejection_reasons_json)<>0)""",
            "invalid_cross_asset_allocation_plan": """SELECT COUNT(*) n
                FROM cross_asset_allocation_plans p
                WHERE p.execution_authorized<>0
                   OR COALESCE(p.portfolio_snapshot_id,'')=''
                   OR COALESCE(p.portfolio_snapshot_fingerprint,'')=''
                   OR COALESCE(p.candidate_set_fingerprint,'')=''
                   OR COALESCE(p.policy_fingerprint,'')=''
                   OR COALESCE(p.plan_fingerprint,'')=''
                   OR COALESCE(p.config_hash,'')=''
                   OR json_valid(p.candidate_set_json)<>1
                   OR json_valid(p.policy_json)<>1
                   OR json_valid(p.plan_json)<>1
                   OR CASE
                        WHEN json_valid(p.plan_json)=1
                        THEN COALESCE(json_type(p.plan_json,'$.execution_authorized'),'')<>'false'
                        ELSE 1
                      END""",
            "cross_asset_execution_authority_escape": """SELECT COUNT(*) n
                FROM cross_asset_allocation_plans
                WHERE execution_authorized<>0""",
            # These three counters are recomputed from canonical Decimal
            # evidence below.  Keep a harmless SQL placeholder here so the
            # report remains a single named-counter map without allowing
            # SQLite REAL arithmetic to decide execution integrity.
            "performance_lab_actual_without_fill": "SELECT 0 n",
            "performance_lab_fill_not_actual": "SELECT 0 n",
            "performance_lab_invalid_fill_evidence": "SELECT 0 n",
            "performance_lab_orphaned_proposal_link": """SELECT COUNT(*) n
                FROM performance_setups ps
                LEFT JOIN trade_proposals p ON p.id=ps.proposal_id
                WHERE ps.asset_class<>'crypto' AND ps.proposed=1 AND ps.proposal_id IS NOT NULL AND p.id IS NULL""",
        }
        report = {
            name: int(self.storage.fetch_all(sql)[0]["n"])
            for name, sql in checks.items()
        }
        # Recompute the safety-critical accounting counters from canonical
        # Decimal text for post-hardening intents.  The SQL expressions above
        # remain compatibility diagnostics for historical REAL rows, but they
        # must not decide whether current execution state is internally sound.
        hardening_boundary = self.storage.fetch_all(
            "SELECT value FROM runtime_metadata WHERE key='final_hardening_effective_at'"
        )
        boundary = str(hardening_boundary[0]["value"]) if hardening_boundary else "9999"

        def exact_row_value(row: Mapping[str, Any], field: str) -> Decimal | None:
            raw = row.get(f"{field}_decimal")
            if raw in (None, ""):
                return None
            try:
                return decimal_value(raw, field, minimum=ZERO)
            except ValueError:
                return None

        def exact_positive_value(row: Mapping[str, Any], field: str) -> Decimal | None:
            try:
                value = require_exact_decimal(
                    row,
                    f"{field}_decimal",
                    minimum=ZERO,
                )
            except ValueError:
                return None
            return value if value is not None and value > ZERO else None

        performance_rows = self.storage.fetch_all(
            """SELECT po.*,ps.asset_class,ps.proposal_id AS setup_proposal_id
               FROM performance_outcomes po
               JOIN performance_setups ps ON ps.id=po.setup_id
               WHERE COALESCE(ps.asset_class,'equity')<>'crypto'"""
        )
        fill_rows = self.storage.fetch_all("SELECT * FROM fills")
        order_rows = self.storage.fetch_all("SELECT * FROM orders")
        fills_by_id = {str(row["id"]): row for row in fill_rows}
        orders_by_id = {str(row["id"]): row for row in order_rows}
        fills_by_proposal: dict[str, list[dict[str, Any]]] = {}
        for fill in fill_rows:
            order = orders_by_id.get(str(fill.get("order_id")))
            proposal_id = str(order.get("proposal_id") or "") if order else ""
            if proposal_id:
                fills_by_proposal.setdefault(proposal_id, []).append(fill)

        def valid_actual_performance_evidence(row: Mapping[str, Any]) -> bool:
            fill_id = row.get("fill_id")
            fill = fills_by_id.get(str(fill_id)) if fill_id is not None else None
            if fill is None:
                return False
            order = orders_by_id.get(str(fill.get("order_id")))
            if order is None:
                return False
            fill_qty = exact_positive_value(fill, "qty")
            fill_price = exact_positive_value(fill, "price")
            entry_qty = exact_positive_value(row, "entry_qty")
            entry_price = exact_positive_value(row, "entry_price")
            entry_notional = exact_positive_value(row, "entry_notional")
            if None in (fill_qty, fill_price, entry_qty, entry_price, entry_notional):
                return False
            assert (
                fill_qty is not None
                and fill_price is not None
                and entry_qty is not None
                and entry_price is not None
                and entry_notional is not None
            )
            return (
                str(row.get("order_id") or "") == str(fill.get("order_id") or "")
                and str(order.get("proposal_id") or "") == str(row.get("setup_proposal_id") or "")
                and entry_qty == fill_qty
                and entry_price == fill_price
                and entry_notional == fill_qty * fill_price
            )

        actual_without_fill = 0
        fill_not_actual = 0
        invalid_fill_evidence = 0
        for row in performance_rows:
            actual = str(row.get("actual_or_shadow") or "")
            evidence_valid = actual == "actual_fill" and valid_actual_performance_evidence(row)
            if actual in {"actual", "actual_fill"} and not evidence_valid:
                actual_without_fill += 1
            candidates = fills_by_proposal.get(str(row.get("setup_proposal_id") or ""), [])
            if candidates:
                if any(
                    exact_positive_value(fill, "qty") is None
                    or exact_positive_value(fill, "price") is None
                    for fill in candidates
                ):
                    invalid_fill_evidence += 1
                if not evidence_valid:
                    fill_not_actual += 1
        report["performance_lab_actual_without_fill"] = actual_without_fill
        report["performance_lab_fill_not_actual"] = fill_not_actual
        report["performance_lab_invalid_fill_evidence"] = invalid_fill_evidence

        intent_rows = self.storage.fetch_all(
            """SELECT id,created_at,requested_quantity_decimal,filled_quantity_decimal,
                      canonical_quantity_decimal,canonical_notional_decimal,
                      canonical_stop_risk_decimal,reference_price_decimal
               FROM order_intents WHERE created_at>=?""",
            (boundary,),
        )
        exact_overfills = 0
        exact_sizing_mismatches = 0
        for row in intent_rows:
            requested = exact_row_value(row, "requested_quantity")
            filled = exact_row_value(row, "filled_quantity")
            if requested is None or filled is None or filled > requested:
                exact_overfills += 1
            canonical_quantity = exact_row_value(row, "canonical_quantity")
            canonical_notional = exact_row_value(row, "canonical_notional")
            canonical_stop_risk = exact_row_value(row, "canonical_stop_risk")
            reference = exact_row_value(row, "reference_price")
            if (
                canonical_quantity is None
                or canonical_notional is None
                or canonical_stop_risk is None
                or reference is None
                or canonical_quantity != requested
                or canonical_notional != canonical_quantity * reference
                or canonical_stop_risk < ZERO
            ):
                exact_sizing_mismatches += 1
        report["fills_exceeding_quantity"] = exact_overfills
        report["intent_canonical_sizing_mismatch"] = exact_sizing_mismatches

        exact_ledger_mismatches = 0
        for row in intent_rows:
            event_rows = self.storage.fetch_all(
                """SELECT cumulative_filled_quantity_decimal
                   FROM broker_fill_events WHERE intent_id=?""",
                (row["id"],),
            )
            cumulative_values: list[Decimal] = []
            invalid_event = False
            for event in event_rows:
                value = event.get("cumulative_filled_quantity_decimal")
                if value in (None, ""):
                    invalid_event = True
                    break
                try:
                    parsed = decimal_value(value, "cumulative filled quantity", minimum=ZERO)
                except ValueError:
                    invalid_event = True
                    break
                if parsed is not None:
                    cumulative_values.append(parsed)
            filled = exact_row_value(row, "filled_quantity")
            expected = max(cumulative_values, default=ZERO)
            if invalid_event or filled is None or expected != filled:
                exact_ledger_mismatches += 1
        report["fill_ledger_mismatch"] = exact_ledger_mismatches
        report["broker_fill_evidence_payload_invalid"] = 0
        report["broker_fill_evidence_payload_fingerprint_mismatch"] = 0
        report["broker_fill_event_evidence_fingerprint_mismatch"] = 0
        evidence_by_id: dict[str, dict[str, Any]] = {}
        for evidence in self.storage.fetch_all("SELECT * FROM broker_fill_evidence"):
            evidence_by_id[str(evidence["id"])] = evidence
            try:
                payload = json.loads(str(evidence["payload"]))
                if not isinstance(payload, Mapping):
                    raise ValueError("broker evidence payload is not an object")
                if _broker_evidence_fingerprint(payload) != str(evidence["payload_fingerprint"]):
                    report["broker_fill_evidence_payload_fingerprint_mismatch"] += 1
            except (TypeError, ValueError, json.JSONDecodeError):
                report["broker_fill_evidence_payload_invalid"] += 1
        for event in self.storage.fetch_all(
            """SELECT broker_event_key,payload FROM broker_fill_events
               WHERE occurred_at>=COALESCE((SELECT value FROM runtime_metadata WHERE key='final_hardening_effective_at'),'9999')"""
        ):
            try:
                payload = json.loads(str(event["payload"]))
                evidence_id = str(payload.get("broker_evidence_id") or "")
                evidence = evidence_by_id.get(evidence_id)
                if evidence is None or str(payload.get("broker_evidence_fingerprint") or "") != str(evidence["payload_fingerprint"]):
                    report["broker_fill_event_evidence_fingerprint_mismatch"] += 1
            except (TypeError, ValueError, json.JSONDecodeError):
                report["broker_fill_event_evidence_fingerprint_mismatch"] += 1
        from .fixed_point_accounting import fixed_point_integrity_report
        from .allocation_authority import allocation_authority_integrity_report
        from .strategy_execution_registry import strategy_registry_integrity_report
        from .crypto_paper_lane import CryptoPaperLaneStore

        report.update(fixed_point_integrity_report(self.storage))
        report.update(strategy_registry_integrity_report(self.storage))
        report.update(allocation_authority_integrity_report(self.storage))
        report.update(CryptoPaperLaneStore(self.storage).integrity_report())
        return report


class Executor:
    def __init__(
        self,
        broker: Any,
        risk_engine: RiskEngine,
        storage: Any | None = None,
        run_id: str | None = None,
        fault_hook: Any | None = None,
        recovery_proven_no_submit: bool = False,
        trusted_evidence_providers: Mapping[str, Callable[[], Any]] | None = None,
        cluster_provider: Callable[[str], Any] | None = None,
    ) -> None:
        self.broker = broker
        self.risk_engine = risk_engine
        self.storage = storage
        self.run_id = run_id
        self.fault_hook = fault_hook
        self.recovery_proven_no_submit = recovery_proven_no_submit
        self.trusted_evidence_providers = dict(trusted_evidence_providers or {})
        self.cluster_provider = cluster_provider

    def _fault(self, boundary: str, **detail: Any) -> None:
        if self.fault_hook is not None:
            self.fault_hook(boundary, detail)

    def _verify_submission_adapter_available(self) -> None:
        """Fail closed before durable authority records possible broker I/O."""

        if self.broker is None or not callable(getattr(self.broker, "submit_order", None)):
            raise BrokerSubmissionNotAttempted("broker submission adapter is unavailable")
        checker = getattr(self.broker, "submission_available", None)
        if callable(checker):
            try:
                available = checker()
            except Exception as exc:
                raise BrokerSubmissionNotAttempted(
                    "broker submission adapter availability could not be verified"
                ) from exc
            if available is not True:
                raise BrokerSubmissionNotAttempted("broker submission adapter is unavailable")

    def _load_approval_authority(
        self,
        proposal: dict[str, Any],
        *,
        approval_id: str | None,
        source_type: str,
        client_order_id: str,
    ) -> tuple[Any, dict[str, Any] | None, str | None]:
        """Load and verify the durable approval before any intent can submit.

        A proposal's ``status`` and a caller-supplied boolean are only display
        and validation hints.  Submission authority is the durable workflow,
        its one-to-one proposal linkage, and the consumed approval record.  The
        only approval-less path is recovery of an already-linked intent; it
        cannot create a new intent or a new authority chain.
        """
        from .approval_authority import authority_envelope
        from .approval_workflow import ApprovalWorkflowState, ApprovalWorkflowStore

        if self.storage is None:
            return None, None, "durable storage is required before broker submission"

        workflow_store = ApprovalWorkflowStore(self.storage)
        proposal_id = str(proposal.get("proposal_id") or proposal.get("id") or "")
        if not proposal_id:
            return workflow_store, None, "proposal identity is required for manual approval"

        action_key = logical_action_key(proposal, source_type)
        intent_rows = self.storage.fetch_all(
            "SELECT * FROM order_intents WHERE logical_action_key=?",
            (action_key,),
        )
        existing_intent = intent_rows[0] if intent_rows else None

        # Recovery may reuse the approval already bound to a durable intent,
        # but an approval-less new logical action is never executable.
        effective_approval_id = str(approval_id or (existing_intent or {}).get("approval_id") or "")
        if not effective_approval_id:
            return workflow_store, existing_intent, "approval_id is required for every new execution intent"

        workflow = workflow_store.get_by_approval(effective_approval_id)
        if workflow is None:
            return workflow_store, existing_intent, "durable approval workflow is required"
        if str(workflow.get("proposal_id") or "") != proposal_id:
            return workflow_store, existing_intent, "approval workflow is not linked to this proposal"

        from .approval_display import validate_consumed_display_authority

        try:
            with self.storage.connect() as conn:
                stored_proposal, approval, stored_envelope = validate_consumed_display_authority(
                    conn,
                    approval_id=effective_approval_id,
                    proposal_id=proposal_id,
                    source_type=source_type,
                )
        except RuntimeError as exc:
            return workflow_store, existing_intent, str(exc)

        def parse_expiry(value: Any) -> datetime | None:
            if value is None or value == "":
                return None
            try:
                parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                return None
            return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)

        stored_expiry = parse_expiry(stored_envelope.get("expires_at"))
        if stored_expiry is None:
            return workflow_store, existing_intent, "authoritative approval expiry is invalid"
        if stored_expiry <= datetime.now(UTC):
            return workflow_store, existing_intent, "approval or proposal has expired"

        approved_fingerprint = str(approval.get("authority_fingerprint") or "")

        exact_extended_fields = {
            "approval_source_type": "approval_source_type",
            "execution_path": "execution_path",
            "request_basis": "request_basis",
            "rotation_group_id": "rotation_group_id",
            "rotation_step_id": "rotation_step_id",
            "emergency_exit_triggered": "emergency_triggered",
            "emergency_exit_hard_trigger": "emergency_trigger_identity",
            "emergency_exit_trigger_reason": "emergency_trigger_reason",
            "emergency_exit_mode": "emergency_trigger_mode",
            "proposal_version": "proposal_version",
            "display_context_type": "display_context_type",
            "display_context_id": "display_context_id",
        }
        for candidate_field, envelope_field in exact_extended_fields.items():
            if (
                proposal.get(candidate_field) not in (None, "")
                and proposal.get(candidate_field) != stored_envelope.get(envelope_field)
            ):
                return workflow_store, existing_intent, f"approved {candidate_field} does not match the displayed proposal"

        # Older internal callers may omit fields that are durably present on the
        # proposal row. Hydrate only absent values from that authoritative row;
        # any caller-supplied value is still compared exactly below.
        for field in (
            "strategy_version", "position_lifecycle_id", "relationship_type",
            "relationship_group_id", "config_hash",
        ):
            if proposal.get(field) in (None, "") and stored_envelope.get(field) is not None:
                proposal[field] = stored_envelope[field]
        if proposal.get("formula_versions") in (None, "") and stored_envelope.get("formula_versions"):
            proposal["formula_versions"] = stored_envelope["formula_versions"]
        caller_envelope = authority_envelope(proposal, proposal_id=proposal_id)
        exact_fields = (
            "proposal_id", "symbol", "side", "action", "position_lifecycle_id",
            "strategy_version", "relationship_type", "relationship_group_id",
            "expires_at", "config_hash", "formula_versions",
        )
        for field in exact_fields:
            if caller_envelope.get(field) != stored_envelope.get(field):
                return workflow_store, existing_intent, f"approved {field} does not match the stored proposal"

        def number(value: Any) -> Decimal | None:
            try:
                result = decimal_value(value, "approved ceiling")
            except ValueError:
                return None
            return result

        try:
            sizing = canonical_sizing(proposal)
            enforce_ceilings(sizing, proposal)
        except (TypeError, ValueError, RuntimeError):
            return workflow_store, existing_intent, "approved quantity is invalid"
        candidate_limits = (
            ("quantity", sizing.quantity, stored_envelope.get("max_quantity")),
            ("notional", sizing.notional, stored_envelope.get("max_notional")),
            ("stop risk", sizing.stop_risk, stored_envelope.get("max_stop_risk")),
        )
        isolated_display_fixture = (
            os.getenv("TRADING_AGENT_TESTING") == "1"
            and str(stored_envelope.get("display_context_type") or "") == "test_fixture"
        )
        if not isolated_display_fixture:
            for label, actual, maximum in candidate_limits:
                if maximum is not None:
                    try:
                        maximum_decimal = decimal_value(maximum, f"displayed maximum {label}")
                    except ValueError:
                        return workflow_store, existing_intent, f"displayed maximum {label} is invalid"
                    if maximum_decimal is None or actual > maximum_decimal:
                        return workflow_store, existing_intent, f"approved {label} may only stay equal or decrease"
        ceiling_fields = (
            ("approved quantity", "approved_quantity_ceiling", "max_quantity"),
            ("approved notional", "approved_notional_ceiling", "max_notional"),
            ("approved stop risk", "approved_stop_risk_ceiling", "max_stop_risk"),
        )
        for label, candidate_key, envelope_key in ceiling_fields:
            candidate_ceiling = number(proposal.get(candidate_key))
            maximum = stored_envelope.get(envelope_key)
            if maximum is not None and candidate_ceiling is None and (
                not isolated_display_fixture or candidate_key in proposal
            ):
                return workflow_store, existing_intent, f"{label} ceiling is required"
            if candidate_ceiling is not None and maximum is not None:
                try:
                    maximum_decimal = decimal_value(maximum, f"displayed maximum {label}")
                except ValueError:
                    return workflow_store, existing_intent, f"displayed maximum {label} is invalid"
                if maximum_decimal is None or candidate_ceiling > maximum_decimal:
                    return workflow_store, existing_intent, f"approved {label} may not increase"
        proposal["approval_authority_fingerprint"] = approved_fingerprint
        proposal["displayed_fingerprint"] = approved_fingerprint
        proposal["execution_path"] = stored_envelope.get("execution_path")
        proposal["approval_source_type"] = stored_envelope.get("approval_source_type")
        proposal["request_basis"] = stored_envelope.get("request_basis")
        proposal["proposal_version"] = stored_envelope.get("proposal_version")
        proposal["display_envelope_id"] = approval.get("display_envelope_id")
        proposal["display_context_type"] = stored_envelope.get("display_context_type")
        proposal["display_context_id"] = stored_envelope.get("display_context_id")
        proposal["config_hash"] = stored_envelope.get("config_hash")
        proposal["formula_versions"] = stored_envelope.get("formula_versions")

        try:
            state = ApprovalWorkflowState(str(workflow.get("state")))
        except ValueError:
            return workflow_store, existing_intent, "approval workflow state is invalid"
        if existing_intent is not None:
            if str(existing_intent.get("approval_id") or "") != effective_approval_id:
                return workflow_store, existing_intent, "existing intent is linked to a different approval"
            if str(workflow.get("intent_id") or "") != str(existing_intent.get("id") or ""):
                return workflow_store, existing_intent, "approval workflow is not linked to the existing intent"
            if state in {
                ApprovalWorkflowState.RECEIVED,
                ApprovalWorkflowState.AUTHORIZED,
                ApprovalWorkflowState.TARGET_RESOLVED,
                ApprovalWorkflowState.VALIDATING,
                ApprovalWorkflowState.APPROVED_PENDING_INTENT,
                ApprovalWorkflowState.BLOCKED,
                ApprovalWorkflowState.MANUAL_REVIEW,
            }:
                return workflow_store, existing_intent, "approval workflow is not executable for the existing intent"
        elif not approval_id:
            return workflow_store, existing_intent, "approval_id is required for every new execution intent"
        elif state not in {
            ApprovalWorkflowState.VALIDATING,
            ApprovalWorkflowState.APPROVED_PENDING_INTENT,
        }:
            return workflow_store, existing_intent, "approval workflow is not executable"

        return workflow_store, existing_intent, None

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
        if proposal.get("status") != "approved":
            return ExecutionResult(False, "blocked", None, reason="validated durable approval required")
        if self.storage is None:
            return ExecutionResult(False, "blocked", None, reason="durable storage is required before broker submission")
        action_key = logical_action_key(proposal, source_type)
        client_order_id = stable_client_order_id(action_key)
        candidate = {**proposal, "client_order_id": client_order_id, "trading_mode": "paper"}

        # Broker absence is deterministic: no adapter I/O can have occurred.
        # Preserve an already-created reservation for a valid approval and make
        # the state explicitly retryable; never classify this as UNKNOWN and
        # never invoke lookup reconciliation.
        if self.broker is None:
            rows = self.storage.fetch_all(
                "SELECT * FROM order_intents WHERE logical_action_key=?",
                (action_key,),
            )
            if rows:
                existing = rows[0]
                existing_state = str(existing.get("state") or "")
                invocation_occurred = int(existing.get("broker_invocation_occurred") or 0)
                # A missing broker client proves that this call cannot have
                # performed I/O, but it does not erase ambiguity recorded by a
                # prior attempt. UNKNOWN/reconciliation-required intents and
                # any intent with an invocation marker remain lookup/manual-
                # review only and are never relabelled retryable.
                if invocation_occurred or existing_state in {
                    OrderState.UNKNOWN.value,
                    OrderState.RECONCILIATION_REQUIRED.value,
                }:
                    return ExecutionResult(
                        False,
                        existing_state or OrderState.UNKNOWN.value,
                        str(existing.get("client_order_id") or client_order_id),
                        reason=(
                            "existing durable intent requires reconciliation; broker was unavailable before this call"
                            if existing_state in {OrderState.UNKNOWN.value, OrderState.RECONCILIATION_REQUIRED.value}
                            else "existing intent records a prior broker invocation; automatic resubmission is disabled"
                        ),
                        intent_id=str(existing.get("id") or "") or None,
                    )
                if existing_state in {
                    OrderState.CREATED.value,
                    OrderState.RESERVED.value,
                }:
                    existing = DurableExecutionStore(self.storage).transition(
                        str(existing["id"]),
                        OrderState.RETRYABLE_PRE_SUBMISSION,
                        event_type="broker_unavailable_before_submission",
                        expected_state=OrderState(existing_state),
                        safe_summary="broker unavailable before any invocation; reservation preserved",
                    )
                elif existing_state == OrderState.SUBMITTING.value:
                    if not self.recovery_proven_no_submit:
                        return ExecutionResult(
                            False,
                            existing_state,
                            str(existing.get("client_order_id") or client_order_id),
                            reason="submission state requires explicit proof of no prior broker invocation before retry",
                            intent_id=str(existing.get("id") or "") or None,
                        )
                    existing = DurableExecutionStore(self.storage).transition(
                        str(existing["id"]),
                        OrderState.RETRYABLE_PRE_SUBMISSION,
                        event_type="broker_unavailable_recovery_deferred_before_submission",
                        expected_state=OrderState.SUBMITTING,
                        safe_summary="broker unavailable; explicit no-invocation proof preserved retryable recovery",
                    )
                elif existing_state not in {
                    OrderState.CREATED.value,
                    OrderState.RETRYABLE_PRE_SUBMISSION.value,
                }:
                    return ExecutionResult(
                        False,
                        existing_state or OrderState.RETRYABLE_PRE_SUBMISSION.value,
                        str(existing.get("client_order_id") or client_order_id),
                        reason="existing durable intent is not eligible for pre-submission recovery",
                        intent_id=str(existing.get("id") or "") or None,
                    )
                return ExecutionResult(
                    False,
                    OrderState.RETRYABLE_PRE_SUBMISSION.value,
                    str(existing.get("client_order_id") or client_order_id),
                    reason="broker unavailable before submission; retry is permitted only while approval remains current",
                    intent_id=str(existing.get("id") or "") or None,
                )
            return ExecutionResult(
                False,
                OrderState.RETRYABLE_PRE_SUBMISSION.value,
                client_order_id,
                reason="broker unavailable before intent creation; no broker invocation occurred",
            )

        workflow_store, existing_intent, authority_error = self._load_approval_authority(
            candidate,
            approval_id=approval_id,
            source_type=source_type,
            client_order_id=client_order_id,
        )
        if authority_error:
            return ExecutionResult(False, "blocked", client_order_id, reason=authority_error)
        execution_run_id = str((existing_intent or {}).get("run_id") or self.run_id or proposal.get("run_id") or "") or None
        recovery_exclusion_allowed = bool(existing_intent) and (
            (
                str((existing_intent or {}).get("state") or "")
                == OrderState.RETRYABLE_PRE_SUBMISSION.value
                and int((existing_intent or {}).get("broker_invocation_occurred") or 0) == 0
            )
            or (
                str((existing_intent or {}).get("state") or "") == OrderState.SUBMITTING.value
                and self.recovery_proven_no_submit
                and int((existing_intent or {}).get("broker_invocation_occurred") or 0) == 0
            )
        )
        try:
            from .execution_risk_snapshot import capture_execution_risk_snapshot

            risk_snapshot = capture_execution_risk_snapshot(
                self.storage,
                self.broker,
                proposal_id=str(candidate.get("proposal_id") or candidate.get("id") or ""),
                approval_id=str(approval_id or (existing_intent or {}).get("approval_id") or ""),
                run_id=execution_run_id,
                context=context,
                config=getattr(self.risk_engine, "config", {}) or {},
                candidate=candidate,
                trusted_providers=self.trusted_evidence_providers,
                cluster_provider=self.cluster_provider,
                recovery_intent_id=(str(existing_intent["id"]) if recovery_exclusion_allowed else None),
                recovery_logical_action_key=(action_key if recovery_exclusion_allowed else None),
                recovery_proven_no_invocation=self.recovery_proven_no_submit,
                telegram_required=str(candidate.get("action") or "entry").lower() in {"entry", "add"},
            )
        except Exception as exc:
            return ExecutionResult(
                False, "blocked", client_order_id,
                reason=(
                    f"authoritative execution risk snapshot unavailable: {type(exc).__name__}: {exc}"
                    if os.getenv("TRADING_AGENT_TESTING") == "1"
                    else f"authoritative execution risk snapshot unavailable: {type(exc).__name__}"
                ),
            )
        candidate["risk_snapshot_id"] = risk_snapshot["id"]
        snapshot_body = snapshot_body_from_row(risk_snapshot)
        final_context = {
            **dict(snapshot_body["risk_context"]),
            "approval_valid": True,
            "final_revalidation": True,
            "authoritative_risk_snapshot_id": risk_snapshot["id"],
            "autonomous_entry_requested": candidate.get("autonomous_entry_requested") is True,
            "autonomous_exit_requested": candidate.get("autonomous_exit_requested") is True,
        }
        decision = self.risk_engine.evaluate(candidate, final_context, final=True)
        if not decision.passed:
            return ExecutionResult(False, "blocked", client_order_id, reason="; ".join(decision.reasons))
        context = final_context

        workflow = None
        effective_approval_id = approval_id
        if workflow_store is not None:
            effective_approval_id = str(
                approval_id or (existing_intent or {}).get("approval_id") or ""
            ) or None
            # Persist the final local validation decision before intent creation.
            # create_or_get_intent then links this workflow in the same SQLite
            # transaction as the intent and reservation.
            from .approval_workflow import ApprovalWorkflowConflict, ApprovalWorkflowState

            workflow = workflow_store.get_by_approval(str(effective_approval_id)) if effective_approval_id else None
            if workflow is None:
                return ExecutionResult(False, "blocked", client_order_id, reason="durable approval workflow is required")
            if workflow["state"] == ApprovalWorkflowState.VALIDATING.value and existing_intent is None:
                try:
                    workflow_store.transition(
                        workflow["id"],
                        ApprovalWorkflowState.APPROVED_PENDING_INTENT,
                        expected_state=ApprovalWorkflowState.VALIDATING,
                        validation_status="passed",
                        safe_detail="final local validation passed",
                    )
                except ApprovalWorkflowConflict:
                    # Another worker may have advanced the same durable
                    # approval between authority loading and this CAS. The
                    # intent creation transaction below remains the single
                    # winner; re-read the workflow and continue only if its
                    # new state is still executable.
                    workflow = workflow_store.get(workflow["id"])
                    if workflow["state"] not in {
                        ApprovalWorkflowState.APPROVED_PENDING_INTENT.value,
                        ApprovalWorkflowState.INTENT_CREATED.value,
                        ApprovalWorkflowState.SUBMISSION_PENDING.value,
                        ApprovalWorkflowState.SUBMISSION_STARTED.value,
                        ApprovalWorkflowState.SUBMITTED.value,
                        ApprovalWorkflowState.UNKNOWN.value,
                        ApprovalWorkflowState.TERMINAL.value,
                    }:
                        return ExecutionResult(False, "blocked", client_order_id, reason="approval workflow changed before execution")
            elif not workflow.get("intent_id") and workflow["state"] != ApprovalWorkflowState.APPROVED_PENDING_INTENT.value:
                return ExecutionResult(False, "blocked", client_order_id, reason="approval workflow is not executable")

        store = DurableExecutionStore(self.storage)
        try:
            self._fault("before_intent_persistence", client_order_id=client_order_id)
            intent = store.create_or_get_intent(
                candidate,
                run_id=execution_run_id,
                source_type=source_type,
                approval_id=effective_approval_id,
            )
        except (ValueError, PermissionError, RuntimeError, sqlite3.Error) as exc:
            return ExecutionResult(False, "blocked", client_order_id, reason=f"intent persistence blocked: {str(exc)}")

        intent_id = str(intent["id"])
        self._fault("after_intent_and_reservation_commit", intent_id=intent_id)
        state = OrderState(intent["state"])
        exact_pre_broker_recovery = (
            state == OrderState.RETRYABLE_PRE_SUBMISSION
            and int(intent.get("broker_invocation_occurred") or 0) == 0
        ) or (
            state == OrderState.SUBMITTING
            and self.recovery_proven_no_submit
            and int(intent.get("broker_invocation_occurred") or 0) == 0
        )
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
            elif state == OrderState.RETRYABLE_PRE_SUBMISSION:
                intent = store.transition(
                    intent_id,
                    OrderState.SUBMITTING,
                    event_type="broker_submission_retried_before_any_prior_invocation",
                    expected_state=OrderState.RETRYABLE_PRE_SUBMISSION,
                )
            if workflow_store is not None and workflow is not None:
                current_workflow = workflow_store.get(workflow["id"])
                if current_workflow["state"] == ApprovalWorkflowState.SUBMISSION_PENDING.value:
                    workflow_store.transition(current_workflow["id"], ApprovalWorkflowState.SUBMISSION_STARTED)
        except InvalidOrderTransition:
            current = store.get_intent(intent_id)
            return ExecutionResult(False, current["state"], current["client_order_id"], reason="another worker owns submission", intent_id=intent_id)

        try:
            # Deterministic crash injection happens before the complete final
            # evidence refresh; nothing may intervene between that refresh,
            # the atomic invocation marker, and adapter I/O.
            self._fault("immediately_before_broker_submit", intent_id=intent_id)
            refreshed_body = verify_snapshot_immediately_before_broker(
                self.storage,
                self.broker,
                snapshot_id=str(intent.get("risk_snapshot_id") or ""),
                intent_id=intent_id,
                logical_action_key=str(intent.get("logical_action_key") or ""),
                proposal_id=str(intent.get("proposal_id") or ""),
                approval_id=str(intent.get("approval_id") or ""),
                run_id=execution_run_id,
                config=getattr(self.risk_engine, "config", {}) or {},
                candidate=candidate,
                source_type=source_type,
                trusted_providers=self.trusted_evidence_providers,
                cluster_provider=self.cluster_provider,
                telegram_required=str(candidate.get("action") or "entry").lower() in {"entry", "add"},
            )
            refreshed_intent = store.get_intent(intent_id)
            refreshed_context = {
                **dict(refreshed_body["risk_context"]),
                "approval_valid": True,
                "final_revalidation": True,
                "authoritative_risk_snapshot_id": refreshed_intent.get("risk_snapshot_id"),
                "autonomous_entry_requested": candidate.get("autonomous_entry_requested") is True,
                "autonomous_exit_requested": candidate.get("autonomous_exit_requested") is True,
            }
            refreshed_decision = self.risk_engine.evaluate(candidate, refreshed_context, final=True)
            if not refreshed_decision.passed:
                raise RuntimeError("final pre-broker risk controls failed: " + "; ".join(refreshed_decision.reasons))
            self._verify_submission_adapter_available()
            self._fault("after_final_authority_before_invocation_marker", intent_id=intent_id)
            from .approval_display import validate_consumed_display_authority

            invocation_time = iso_now()
            with self.storage.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                current = conn.execute("SELECT * FROM order_intents WHERE id=?", (intent_id,)).fetchone()
                reservation_count = int(conn.execute(
                    "SELECT COUNT(*) FROM risk_reservations WHERE intent_id=? AND state='active'",
                    (intent_id,),
                ).fetchone()[0])
                if (
                    current is None
                    or str(current["state"] or "") != OrderState.SUBMITTING.value
                    or int(current["broker_invocation_occurred"] or 0) != 0
                    or str(current["risk_snapshot_id"] or "") != str(refreshed_intent.get("risk_snapshot_id") or "")
                    or reservation_count != 1
                ):
                    raise RuntimeError("intent or reservation changed at the final adapter boundary")
                validate_consumed_display_authority(
                    conn,
                    approval_id=str(current["approval_id"] or ""),
                    proposal_id=str(current["proposal_id"] or ""),
                    source_type=source_type,
                )
                current_workflow = conn.execute(
                    "SELECT state,intent_id FROM approval_workflows WHERE approval_id=?",
                    (str(current["approval_id"] or ""),),
                ).fetchone()
                if (
                    current_workflow is None
                    or str(current_workflow["state"] or "") != ApprovalWorkflowState.SUBMISSION_STARTED.value
                    or str(current_workflow["intent_id"] or "") != intent_id
                ):
                    raise RuntimeError("approval workflow changed at the final adapter boundary")
                updated = conn.execute(
                    """UPDATE order_intents
                       SET broker_invocation_started_at=?,broker_invocation_occurred=1,updated_at=?
                       WHERE id=? AND state=? AND COALESCE(broker_invocation_occurred,0)=0""",
                    (invocation_time, invocation_time, intent_id, OrderState.SUBMITTING.value),
                ).rowcount
                if updated != 1:
                    raise RuntimeError("broker invocation authority was concurrently consumed")
            # The marker transaction is committed before adapter I/O.  A crash
            # after this boundary is therefore always reconciled as possibly
            # submitted and is never eligible for automatic resubmission.
            self._fault("after_invocation_marker_before_adapter", intent_id=intent_id)
        except Exception as exc:
            store.transition(
                intent_id,
                OrderState.REJECTED,
                event_type="authoritative_snapshot_changed_before_broker",
                error_category=type(exc).__name__,
                safe_summary=f"pre-broker control failure: {type(exc).__name__}: {str(exc)[:300]}",
                expected_state=OrderState.SUBMITTING,
            )
            if workflow_store is not None and workflow is not None:
                current_workflow = workflow_store.get(workflow["id"])
                if current_workflow["state"] == ApprovalWorkflowState.SUBMISSION_STARTED.value:
                    workflow_store.transition(current_workflow["id"], ApprovalWorkflowState.TERMINAL)
            return ExecutionResult(
                False,
                OrderState.REJECTED.value,
                intent["client_order_id"],
                reason=f"authoritative execution evidence changed or expired before broker invocation: {str(exc)}",
                intent_id=intent_id,
            )

        try:
            order_args: dict[str, float]
            if intent["request_basis"] == "notional" and intent.get("requested_notional") is not None:
                order_args = {"notional": float(intent["requested_notional"])}
            else:
                order_args = {"qty": float(intent["requested_quantity"])}
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
                raw_filled_quantity = _value(response, "filled_qty", None)
                raw_fill_price = _value(response, "filled_avg_price", None)
                try:
                    filled_quantity_decimal = decimal_value(
                        raw_filled_quantity, "broker filled quantity", minimum=ZERO
                    )
                    fill_price_decimal = decimal_value(
                        raw_fill_price, "broker average fill price", minimum=ZERO
                    )
                    if filled_quantity_decimal == ZERO or fill_price_decimal == ZERO:
                        raise ValueError("broker fill quantity and price must be positive")
                except ValueError:
                    filled_quantity_decimal = None
                    fill_price_decimal = None
                if filled_quantity_decimal is None or fill_price_decimal is None:
                    store.transition(
                        intent_id,
                        OrderState.UNKNOWN,
                        event_type="broker_fill_response_incomplete",
                        broker_order_id=broker_order_id,
                        safe_summary="filled response omitted reliable quantity or price; reconciliation required",
                    )
                    target = OrderState.UNKNOWN
                else:
                    event_key = str(
                        _value(response, "execution_id", "")
                        or f"{broker_order_id or intent['client_order_id']}:{decimal_text(filled_quantity_decimal)}:{decimal_text(fill_price_decimal)}"
                    )
                    store.record_fill(
                        intent_id,
                        cumulative_quantity=filled_quantity_decimal,
                        fill_price=fill_price_decimal,
                        broker_event_key=event_key,
                        broker_order_id=broker_order_id,
                        occurred_at=str(_value(response, "filled_at", iso_now())),
                        price_is_cumulative_average=True,
                        broker_evidence={
                            "source": "broker_submission_response",
                            "broker_order_id": broker_order_id,
                            "client_order_id": intent["client_order_id"],
                            "symbol": intent["symbol"],
                            "side": intent["side"],
                            "status": remote_status,
                            "execution_id": _value(response, "execution_id", None),
                            "filled_qty": filled_quantity_decimal,
                            "filled_avg_price": fill_price_decimal,
                            "filled_at": _value(response, "filled_at", None),
                            "payload": _broker_evidence_safe(response if isinstance(response, Mapping) else vars(response) if hasattr(response, "__dict__") else {}),
                        },
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
            request_may_have_reached = getattr(exc, "request_may_have_reached_broker", None)
            if request_may_have_reached is False:
                self.storage.execute(
                    "UPDATE order_intents SET broker_invocation_occurred=0,updated_at=? WHERE id=?",
                    (iso_now(), intent_id),
                )
                store.transition(
                    intent_id,
                    OrderState.REJECTED,
                    event_type="broker_submission_not_attempted",
                    error_category=type(exc).__name__,
                    safe_summary="adapter proved no broker request began; fresh approval required",
                )
                if workflow_store is not None and workflow is not None:
                    current_workflow = workflow_store.get(workflow["id"])
                    if current_workflow["state"] == ApprovalWorkflowState.SUBMISSION_STARTED.value:
                        workflow_store.transition(current_workflow["id"], ApprovalWorkflowState.TERMINAL)
                return ExecutionResult(False, "rejected", intent["client_order_id"], reason="broker request was not attempted", intent_id=intent_id)
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
    approval_id: str,
) -> ExecutionResult:
    return Executor(broker, risk_engine, storage, run_id).execute(
        proposal, context, approval_id=approval_id
    )
