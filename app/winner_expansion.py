"""Durable Phase 4.2C winner-expansion decisions and milestones.

The pure winner-expansion engine composes canonical position risk, trend state,
and explicit policy gates.  ``WinnerExpansionStore`` owns only local SQLite
state; it never approves a proposal, creates an execution intent, or calls a
broker.
"""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Mapping

from .position_risk import PositionRiskDecision, PositionRiskEngine, PositionRiskInput
from .trend_management import PositionManagementMode, TrendManagementDecision
from .utils import iso_now, json_dumps


WINNER_EXPANSION_SCHEMA_VERSION = "phase4_2c_winner_expansion_v1"
WINNER_EXPANSION_FORMULA_VERSION = "winner_expansion_v1_operational_paper"
PYRAMIDING_MILESTONE_VERSION = "pyramiding_milestone_v1"

MILESTONE_ACTIVE_STATES = frozenset({"PROPOSED", "APPROVED", "SUBMITTED", "PARTIALLY_FILLED"})
MILESTONE_TERMINAL_STATES = frozenset({"FILLED", "REJECTED", "EXPIRED", "BLOCKED", "CANCELLED"})
MILESTONE_STATES = MILESTONE_ACTIVE_STATES | MILESTONE_TERMINAL_STATES
MILESTONE_TRANSITIONS: dict[str, frozenset[str]] = {
    "PROPOSED": frozenset({"APPROVED", "REJECTED", "EXPIRED", "BLOCKED", "CANCELLED"}),
    "APPROVED": frozenset({"SUBMITTED", "REJECTED", "EXPIRED", "BLOCKED", "CANCELLED"}),
    "SUBMITTED": frozenset({"PARTIALLY_FILLED", "FILLED", "REJECTED", "EXPIRED", "CANCELLED"}),
    "PARTIALLY_FILLED": frozenset({"PARTIALLY_FILLED", "FILLED", "EXPIRED", "CANCELLED"}),
    "FILLED": frozenset(),
    "REJECTED": frozenset(),
    "EXPIRED": frozenset(),
    "BLOCKED": frozenset(),
    "CANCELLED": frozenset(),
}


def _fingerprint(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)
    except (TypeError, ValueError):
        return None


def apply_position_stop_history_schema(conn: Any) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS position_stop_history(
          id TEXT PRIMARY KEY,run_id TEXT,symbol TEXT NOT NULL,position_lifecycle_id TEXT NOT NULL,
          stop_sequence INTEGER NOT NULL CHECK(stop_sequence>=1),prior_stop REAL NOT NULL CHECK(prior_stop>0),
          new_stop REAL NOT NULL CHECK(new_stop>0),stop_change REAL NOT NULL CHECK(stop_change>=0),
          management_mode TEXT NOT NULL,source TEXT NOT NULL,stop_as_of TEXT NOT NULL,input_as_of TEXT,
          formula_version TEXT NOT NULL,config_hash TEXT,decision_fingerprint TEXT NOT NULL UNIQUE,
          raw_inputs_json TEXT NOT NULL,created_at TEXT NOT NULL,
          UNIQUE(position_lifecycle_id,stop_sequence))"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_position_stop_history_latest "
        "ON position_stop_history(position_lifecycle_id,stop_sequence DESC)"
    )


def apply_pyramiding_milestones_schema(conn: Any) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS pyramiding_milestones(
          id TEXT PRIMARY KEY,run_id TEXT,symbol TEXT NOT NULL,position_lifecycle_id TEXT NOT NULL,
          milestone_key TEXT NOT NULL,milestone_version TEXT NOT NULL,r_bucket INTEGER NOT NULL,
          price_advance_bucket INTEGER NOT NULL,stop_advance_bucket INTEGER NOT NULL,
          prior_filled_adds INTEGER NOT NULL CHECK(prior_filled_adds>=0),trend_mode TEXT NOT NULL,
          strength_score REAL NOT NULL,status TEXT NOT NULL,
          active_proposal_id TEXT,approval_id TEXT,intent_id TEXT,order_id TEXT,
          first_proposed_at TEXT,last_proposed_at TEXT,completed_at TEXT,rejected_at TEXT,expired_at TEXT,
          retry_count INTEGER NOT NULL DEFAULT 0 CHECK(retry_count>=0),
          generation INTEGER NOT NULL DEFAULT 1 CHECK(generation>=1),
          max_retries INTEGER NOT NULL DEFAULT 0 CHECK(max_retries>=0),retry_after TEXT,
          last_terminal_reason TEXT,identity_fingerprint TEXT NOT NULL,raw_inputs_json TEXT NOT NULL,
          created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
          UNIQUE(position_lifecycle_id,milestone_key))"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pyramiding_milestones_status "
        "ON pyramiding_milestones(status,retry_after,updated_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pyramiding_milestones_position "
        "ON pyramiding_milestones(position_lifecycle_id,created_at)"
    )


def apply_add_risk_decisions_schema(conn: Any) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS add_risk_decisions(
          id TEXT PRIMARY KEY,run_id TEXT,proposal_id TEXT,approval_id TEXT,decision_stage TEXT NOT NULL,
          symbol TEXT NOT NULL,position_lifecycle_id TEXT NOT NULL,milestone_id TEXT,milestone_key TEXT,
          deployment_mode TEXT NOT NULL,eligible INTEGER NOT NULL CHECK(eligible IN (0,1)),
          reason TEXT NOT NULL,blocking_reasons_json TEXT NOT NULL,binding_cap TEXT NOT NULL,
          pre_add_shares REAL NOT NULL,pre_add_stop REAL NOT NULL,pre_add_open_risk_gross REAL NOT NULL,
          pre_add_open_risk_net REAL NOT NULL,proposed_add_shares REAL NOT NULL,proposed_add_price REAL NOT NULL,
          proposed_tightened_stop REAL NOT NULL,post_add_total_shares REAL NOT NULL,
          post_add_open_risk_gross REAL NOT NULL,post_add_open_risk_net REAL NOT NULL,
          incremental_risk REAL NOT NULL,consumed_risk REAL NOT NULL,released_risk REAL NOT NULL,
          realized_profit_credit_requested REAL NOT NULL,realized_profit_credit_applied REAL NOT NULL,
          projected_portfolio_heat_dollars REAL NOT NULL,projected_symbol_exposure_dollars REAL NOT NULL,
          projected_cluster_exposure_dollars REAL NOT NULL,projected_portfolio_gross_exposure_dollars REAL NOT NULL,
          caps_json TEXT NOT NULL,raw_inputs_json TEXT NOT NULL,formula_version TEXT NOT NULL,
          config_hash TEXT,decision_fingerprint TEXT NOT NULL UNIQUE,created_at TEXT NOT NULL)"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_add_risk_decisions_position "
        "ON add_risk_decisions(position_lifecycle_id,created_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_add_risk_decisions_proposal "
        "ON add_risk_decisions(proposal_id,decision_stage,created_at)"
    )


def apply_trend_management_decisions_schema(conn: Any) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS trend_management_decisions(
          id TEXT PRIMARY KEY,run_id TEXT,symbol TEXT NOT NULL,position_lifecycle_id TEXT NOT NULL,
          previous_mode TEXT,classified_mode TEXT NOT NULL,management_mode TEXT NOT NULL,
          transition TEXT NOT NULL,transition_reason TEXT NOT NULL,reason TEXT NOT NULL,
          current_r_multiple REAL NOT NULL,peak_r_multiple REAL NOT NULL,profit_giveback_ratio REAL NOT NULL,
          atr REAL NOT NULL,effective_atr_multiplier REAL NOT NULL,calculated_stop_candidate REAL NOT NULL,
          prior_stop REAL NOT NULL,protective_stop REAL NOT NULL,stop_changed INTEGER NOT NULL,
          stop_monotonic INTEGER NOT NULL,recommended_partial_exit_fraction REAL NOT NULL,
          retained_runner_fraction REAL NOT NULL,allow_pyramiding INTEGER NOT NULL,
          defer_fixed_profit_target INTEGER NOT NULL,blocking_reasons_json TEXT NOT NULL,
          raw_inputs_json TEXT NOT NULL,formula_version TEXT NOT NULL,config_hash TEXT,
          decision_fingerprint TEXT NOT NULL UNIQUE,created_at TEXT NOT NULL)"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_trend_management_position "
        "ON trend_management_decisions(position_lifecycle_id,created_at)"
    )


def apply_winner_expansion_schema(conn: Any, *, record_migration: bool = True) -> None:
    """Apply all additive Phase 4.2C tables."""
    apply_position_stop_history_schema(conn)
    apply_pyramiding_milestones_schema(conn)
    apply_add_risk_decisions_schema(conn)
    apply_trend_management_decisions_schema(conn)
    if record_migration:
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version,applied_at,detail) VALUES(?,?,?)",
            (
                WINNER_EXPANSION_SCHEMA_VERSION,
                iso_now(),
                "additive canonical ADD risk, monotonic stop history, trend decisions, and durable pyramiding milestones",
            ),
        )


@dataclass(frozen=True)
class MilestoneIdentity:
    symbol: str
    position_lifecycle_id: str
    milestone_key: str
    r_bucket: int
    price_advance_bucket: int
    stop_advance_bucket: int
    prior_filled_adds: int
    trend_mode: str
    strength_score: float
    raw_inputs: dict[str, Any]
    milestone_version: str
    identity_fingerprint: str

    @classmethod
    def build(
        cls,
        *,
        symbol: str,
        position_lifecycle_id: str,
        current_r_multiple: float,
        price_advance_since_prior_entry_pct: float,
        stop_advance_r: float,
        prior_filled_adds: int,
        trend_mode: str,
        r_step: float = 0.50,
        price_advance_step_pct: float = 2.0,
        stop_advance_step_r: float = 0.50,
    ) -> "MilestoneIdentity":
        normalized_symbol = str(symbol or "").upper()
        lifecycle = str(position_lifecycle_id or "")
        if not normalized_symbol or not lifecycle:
            raise ValueError("symbol and position_lifecycle_id are required")
        r_value = _finite(current_r_multiple)
        price_advance = _finite(price_advance_since_prior_entry_pct)
        stop_advance = _finite(stop_advance_r)
        r_step_value = _finite(r_step)
        price_step_value = _finite(price_advance_step_pct)
        stop_step_value = _finite(stop_advance_step_r)
        if any(value is None for value in (r_value, price_advance, stop_advance, r_step_value, price_step_value, stop_step_value)):
            raise ValueError("milestone inputs must be finite")
        if r_step_value <= 0 or price_step_value <= 0 or stop_step_value <= 0:
            raise ValueError("milestone step sizes must be positive")
        if prior_filled_adds < 0:
            raise ValueError("prior_filled_adds cannot be negative")
        try:
            mode = PositionManagementMode(str(trend_mode)).value
        except ValueError as exc:
            raise ValueError("trend_mode is invalid") from exc
        r_bucket = max(0, math.floor(r_value / r_step_value))
        price_bucket = max(0, math.floor(price_advance / price_step_value))
        stop_bucket = max(0, math.floor(stop_advance / stop_step_value))
        raw = {
            "symbol": normalized_symbol,
            "position_lifecycle_id": lifecycle,
            "current_r_multiple": r_value,
            "price_advance_since_prior_entry_pct": price_advance,
            "stop_advance_r": stop_advance,
            "prior_filled_adds": prior_filled_adds,
            "trend_mode": mode,
            "r_step": r_step_value,
            "price_advance_step_pct": price_step_value,
            "stop_advance_step_r": stop_step_value,
            "r_bucket": r_bucket,
            "price_advance_bucket": price_bucket,
            "stop_advance_bucket": stop_bucket,
            "milestone_version": PYRAMIDING_MILESTONE_VERSION,
        }
        key = f"wm1:r{r_bucket}:p{price_bucket}:s{stop_bucket}:a{prior_filled_adds}:{mode}"
        fingerprint = _fingerprint({**raw, "milestone_key": key})
        strength = min(1.0, 0.35 * min(r_bucket / 3.0, 1.0) + 0.35 * min(price_bucket / 2.0, 1.0) + 0.30 * min(stop_bucket / 2.0, 1.0))
        return cls(
            symbol=normalized_symbol,
            position_lifecycle_id=lifecycle,
            milestone_key=key,
            r_bucket=r_bucket,
            price_advance_bucket=price_bucket,
            stop_advance_bucket=stop_bucket,
            prior_filled_adds=prior_filled_adds,
            trend_mode=mode,
            strength_score=strength,
            raw_inputs=raw,
            milestone_version=PYRAMIDING_MILESTONE_VERSION,
            identity_fingerprint=fingerprint,
        )


@dataclass(frozen=True)
class MilestoneClaim:
    accepted: bool
    milestone_id: str
    milestone_key: str
    status: str
    generation: int
    retry_count: int
    reason: str


@dataclass(frozen=True)
class WinnerExpansionInput:
    risk_input: PositionRiskInput
    trend_decision: TrendManagementDecision
    milestone: MilestoneIdentity
    average_entry_price: float
    strategy_state: str
    policy_adds_allowed: bool
    regime_supports_add: bool
    setup_score: float
    minimum_setup_score: float
    score_improvement: float
    minimum_score_improvement: float
    exit_or_deterioration_warning: bool
    reconciliation_ok: bool
    integrity_ok: bool
    quote_current: bool
    spread_ok: bool
    liquidity_ok: bool
    stop_current: bool
    milestone_available: bool
    adaptive_conviction_authorized: bool
    adaptive_sizing_authorized: bool
    phase3_validation_passed: bool
    phase3_validation_stage: str
    decision_stage: str = "proposal"


@dataclass(frozen=True)
class WinnerExpansionDecision:
    symbol: str
    position_lifecycle_id: str
    decision_stage: str
    eligible: bool
    reason: str
    blocking_reasons: tuple[str, ...]
    milestone_key: str
    milestone_strength: float
    strategy_state: str
    deployment_mode: str
    trend_mode: str
    proposed_add_shares: float
    proposed_add_notional: float
    required_protective_stop: float
    pre_add_open_risk: float
    post_add_open_risk: float
    incremental_risk: float
    released_risk: float
    binding_cap: str
    risk_decision: PositionRiskDecision
    raw_inputs: dict[str, Any]
    formula_version: str
    decision_fingerprint: str


class WinnerExpansionEngine:
    """Compose policy, trend, milestone, and canonical risk for an ADD."""

    def __init__(self, position_risk_engine: PositionRiskEngine | None = None) -> None:
        self.position_risk_engine = position_risk_engine or PositionRiskEngine()

    def evaluate(self, value: WinnerExpansionInput) -> WinnerExpansionDecision:
        if value.decision_stage not in {"proposal", "final_revalidation"}:
            raise ValueError("decision_stage must be proposal or final_revalidation")
        risk = self.position_risk_engine.evaluate(value.risk_input)
        trend = value.trend_decision
        milestone = value.milestone
        if risk.symbol != trend.symbol or risk.symbol != milestone.symbol:
            raise ValueError("risk, trend, and milestone symbols must match")
        if (
            risk.position_lifecycle_id != trend.position_lifecycle_id
            or risk.position_lifecycle_id != milestone.position_lifecycle_id
        ):
            raise ValueError("risk, trend, and milestone position lifecycles must match")
        entry = _finite(value.average_entry_price)
        score = _finite(value.setup_score)
        minimum_score = _finite(value.minimum_setup_score)
        improvement = _finite(value.score_improvement)
        minimum_improvement = _finite(value.minimum_score_improvement)
        if None in {entry, score, minimum_score, improvement, minimum_improvement} or entry <= 0:
            raise ValueError("entry and setup-quality inputs must be finite")

        policy_state = str(value.strategy_state or "").upper()
        blockers: list[str] = []

        def require(condition: bool, reason: str) -> None:
            if not condition:
                blockers.append(reason)

        require(policy_state != "PROBE", "PROBE strategy state forbids ADDs")
        require(value.policy_adds_allowed is True, "authoritative strategy policy does not authorize ADDs")
        require(risk.deployment_mode != "DEFENSIVE", "DEFENSIVE deployment mode forbids pyramiding")
        require(value.regime_supports_add is True, "current regime does not support winner expansion")
        require(value.risk_input.current_market_price > entry, "position is not profitable")
        require(value.risk_input.proposed_add_price >= entry, "ADD would average down")
        require(score >= minimum_score, "setup quality is below the ADD threshold")
        require(improvement >= minimum_improvement, "setup quality is not stable or improving")
        require(value.exit_or_deterioration_warning is False, "current exit or deterioration warning blocks ADD")
        require(value.reconciliation_ok is True, "reconciliation health is not authoritative")
        require(value.integrity_ok is True, "execution integrity warning blocks ADD")
        require(value.quote_current is True, "quote is stale or unavailable")
        require(value.spread_ok is True, "quote spread exceeds the execution limit")
        require(value.liquidity_ok is True, "liquidity evidence does not qualify")
        require(value.stop_current is True, "protective stop state is absent, stale, or inconsistent")
        require(
            value.risk_input.proposed_tightened_stop + value.risk_input.stop_rounding_tolerance
            >= value.risk_input.current_protective_stop,
            "proposed protective stop would move downward",
        )
        require(value.milestone_available is True, "pyramiding milestone is already active or consumed")
        require(trend.mode == PositionManagementMode.TREND_PYRAMID.value, "trend-management mode does not authorize pyramiding")
        require(trend.allow_pyramiding is True, "trend-management policy blocks pyramiding")
        require(trend.stop_monotonic is True, "trend stop is not monotonic")
        stop_tolerance = max(
            float(trend.raw_inputs.get("minimum_price_increment") or 0.0),
            value.risk_input.stop_rounding_tolerance,
        )
        require(
            abs(trend.protective_stop - value.risk_input.proposed_tightened_stop)
            <= max(stop_tolerance, 1e-9),
            "risk decision and trend decision disagree on the required stop",
        )
        require(value.adaptive_conviction_authorized is True, "Adaptive Conviction did not authorize the ADD")
        require(value.adaptive_sizing_authorized is True, "Adaptive Sizing did not authorize the ADD")
        required_phase3_stage = "final" if value.decision_stage == "final_revalidation" else "proposal"
        require(
            value.phase3_validation_passed is True
            and value.phase3_validation_stage == required_phase3_stage,
            f"Phase 3 {required_phase3_stage} validation has not passed",
        )
        require(risk.eligible, risk.reason)

        raw_inputs = {
            "winner_expansion": {
                key: item
                for key, item in asdict(value).items()
                if key not in {"risk_input", "trend_decision", "milestone"}
            },
            "risk_decision_fingerprint": risk.decision_fingerprint,
            "trend_decision_fingerprint": trend.decision_fingerprint,
            "milestone_identity_fingerprint": milestone.identity_fingerprint,
            "milestone_raw_inputs": milestone.raw_inputs,
        }
        payload = {
            "raw_inputs": raw_inputs,
            "blockers": blockers,
            "risk_decision_fingerprint": risk.decision_fingerprint,
            "trend_decision_fingerprint": trend.decision_fingerprint,
            "milestone_identity_fingerprint": milestone.identity_fingerprint,
            "formula_version": WINNER_EXPANSION_FORMULA_VERSION,
        }
        fingerprint = _fingerprint(payload)
        eligible = not blockers
        reason = (
            f"{risk.deployment_mode} winner ADD is {('risk-neutral' if risk.incremental_risk <= value.risk_input.rounding_tolerance_dollars else 'bounded')} and fully authorized"
            if eligible
            else blockers[0]
        )
        return WinnerExpansionDecision(
            symbol=risk.symbol,
            position_lifecycle_id=risk.position_lifecycle_id,
            decision_stage=value.decision_stage,
            eligible=eligible,
            reason=reason,
            blocking_reasons=tuple(blockers),
            milestone_key=milestone.milestone_key,
            milestone_strength=milestone.strength_score,
            strategy_state=policy_state,
            deployment_mode=risk.deployment_mode,
            trend_mode=trend.mode,
            proposed_add_shares=risk.proposed_add_shares,
            proposed_add_notional=risk.proposed_add_shares * risk.proposed_add_price,
            required_protective_stop=risk.proposed_tightened_stop,
            pre_add_open_risk=risk.pre_add_open_risk_net,
            post_add_open_risk=risk.post_add_open_risk_net,
            incremental_risk=risk.incremental_risk,
            released_risk=risk.released_risk,
            binding_cap=risk.binding_cap,
            risk_decision=risk,
            raw_inputs=raw_inputs,
            formula_version=WINNER_EXPANSION_FORMULA_VERSION,
            decision_fingerprint=fingerprint,
        )


class WinnerExpansionStore:
    """Persistence helpers with transactionally durable milestone claims."""

    def __init__(self, storage: Any) -> None:
        self.storage = storage

    def get_milestone(self, position_lifecycle_id: str, milestone_key: str) -> dict[str, Any] | None:
        rows = self.storage.fetch_all(
            "SELECT * FROM pyramiding_milestones WHERE position_lifecycle_id=? AND milestone_key=?",
            (position_lifecycle_id, milestone_key),
        )
        return rows[0] if rows else None

    def claim_milestone(
        self,
        identity: MilestoneIdentity,
        *,
        run_id: str | None,
        proposal_id: str,
        max_retries: int = 0,
        allow_retry: bool = False,
        now: str | None = None,
    ) -> MilestoneClaim:
        """Claim one logical milestone, or retry its existing durable row.

        Active and FILLED rows can never be claimed again.  Only REJECTED and
        EXPIRED rows participate in the explicit retry path.
        """
        if max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        current_at = now or iso_now()
        current_dt = _parse_time(current_at)
        if current_dt is None:
            raise ValueError("now must be an ISO timestamp")
        with self.storage.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM pyramiding_milestones WHERE position_lifecycle_id=? AND milestone_key=?",
                (identity.position_lifecycle_id, identity.milestone_key),
            ).fetchone()
            if row is None:
                milestone_id = str(uuid.uuid4())
                conn.execute(
                    """INSERT INTO pyramiding_milestones(
                      id,run_id,symbol,position_lifecycle_id,milestone_key,milestone_version,r_bucket,
                      price_advance_bucket,stop_advance_bucket,prior_filled_adds,trend_mode,strength_score,
                      status,active_proposal_id,first_proposed_at,last_proposed_at,retry_count,generation,
                      max_retries,identity_fingerprint,raw_inputs_json,created_at,updated_at)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        milestone_id, run_id, identity.symbol, identity.position_lifecycle_id,
                        identity.milestone_key, identity.milestone_version, identity.r_bucket,
                        identity.price_advance_bucket, identity.stop_advance_bucket,
                        identity.prior_filled_adds, identity.trend_mode, identity.strength_score,
                        "PROPOSED", proposal_id, current_at, current_at, 0, 1, max_retries,
                        identity.identity_fingerprint, json_dumps(identity.raw_inputs), current_at, current_at,
                    ),
                )
                return MilestoneClaim(True, milestone_id, identity.milestone_key, "PROPOSED", 1, 0, "new milestone claimed")

            existing = dict(row)
            status = str(existing["status"])
            milestone_id = str(existing["id"])
            generation = int(existing.get("generation") or 1)
            retry_count = int(existing.get("retry_count") or 0)
            stored_max = int(existing.get("max_retries") or 0)
            if status in MILESTONE_ACTIVE_STATES:
                return MilestoneClaim(False, milestone_id, identity.milestone_key, status, generation, retry_count, "milestone already has an active ADD action")
            if status == "FILLED":
                return MilestoneClaim(False, milestone_id, identity.milestone_key, status, generation, retry_count, "filled milestone is permanently consumed")
            if status not in {"REJECTED", "EXPIRED"}:
                return MilestoneClaim(False, milestone_id, identity.milestone_key, status, generation, retry_count, "terminal milestone is not retryable")
            if not allow_retry:
                return MilestoneClaim(False, milestone_id, identity.milestone_key, status, generation, retry_count, "retry requires explicit authorization")
            effective_max = max(stored_max, max_retries)
            if retry_count >= effective_max:
                return MilestoneClaim(False, milestone_id, identity.milestone_key, status, generation, retry_count, "milestone retry limit reached")
            retry_at = _parse_time(existing.get("retry_after"))
            if retry_at is not None and current_dt < retry_at:
                return MilestoneClaim(False, milestone_id, identity.milestone_key, status, generation, retry_count, "milestone retry cooldown is active")

            updated = conn.execute(
                """UPDATE pyramiding_milestones SET
                   status='PROPOSED',run_id=?,active_proposal_id=?,approval_id=NULL,intent_id=NULL,order_id=NULL,
                   last_proposed_at=?,retry_count=retry_count+1,generation=generation+1,max_retries=?,
                   retry_after=NULL,last_terminal_reason=NULL,updated_at=?
                   WHERE id=? AND status IN ('REJECTED','EXPIRED') AND retry_count=?""",
                (run_id, proposal_id, current_at, effective_max, current_at, milestone_id, retry_count),
            )
            if updated.rowcount != 1:
                return MilestoneClaim(False, milestone_id, identity.milestone_key, status, generation, retry_count, "milestone changed concurrently")
            return MilestoneClaim(True, milestone_id, identity.milestone_key, "PROPOSED", generation + 1, retry_count + 1, "explicit milestone retry claimed")

    def transition_milestone(
        self,
        position_lifecycle_id: str,
        milestone_key: str,
        target_status: str,
        *,
        approval_id: str | None = None,
        intent_id: str | None = None,
        order_id: str | None = None,
        terminal_reason: str | None = None,
        retry_after: str | None = None,
        now: str | None = None,
    ) -> dict[str, Any]:
        target = str(target_status or "").upper()
        if target not in MILESTONE_STATES:
            raise ValueError("invalid milestone status")
        current_at = now or iso_now()
        if retry_after is not None and _parse_time(retry_after) is None:
            raise ValueError("retry_after must be an ISO timestamp")
        with self.storage.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM pyramiding_milestones WHERE position_lifecycle_id=? AND milestone_key=?",
                (position_lifecycle_id, milestone_key),
            ).fetchone()
            if row is None:
                raise KeyError("pyramiding milestone not found")
            current = str(row["status"])
            if target == current and target == "PARTIALLY_FILLED":
                return dict(row)
            if target not in MILESTONE_TRANSITIONS[current]:
                raise ValueError(f"invalid milestone transition: {current} -> {target}")
            completed_at = current_at if target == "FILLED" else row["completed_at"]
            rejected_at = current_at if target == "REJECTED" else row["rejected_at"]
            expired_at = current_at if target == "EXPIRED" else row["expired_at"]
            updated = conn.execute(
                """UPDATE pyramiding_milestones SET status=?,approval_id=COALESCE(?,approval_id),
                   intent_id=COALESCE(?,intent_id),order_id=COALESCE(?,order_id),completed_at=?,
                   rejected_at=?,expired_at=?,retry_after=?,last_terminal_reason=?,updated_at=?
                   WHERE id=? AND status=?""",
                (
                    target, approval_id, intent_id, order_id, completed_at, rejected_at, expired_at,
                    retry_after if target in {"REJECTED", "EXPIRED"} else row["retry_after"],
                    terminal_reason if target in MILESTONE_TERMINAL_STATES else row["last_terminal_reason"],
                    current_at, row["id"], current,
                ),
            )
            if updated.rowcount != 1:
                raise RuntimeError("milestone transition lost a concurrent compare-and-swap")
            result = conn.execute("SELECT * FROM pyramiding_milestones WHERE id=?", (row["id"],)).fetchone()
            return dict(result)

    def persist_stop(
        self,
        decision: TrendManagementDecision,
        *,
        run_id: str | None,
        source: str,
        stop_as_of: str,
        config_hash: str | None = None,
    ) -> str:
        """Persist a stop candidate only if it does not loosen prior state."""
        if _parse_time(stop_as_of) is None:
            raise ValueError("stop_as_of must be an ISO timestamp")
        payload = {
            "trend_decision_fingerprint": decision.decision_fingerprint,
            "source": source,
            "stop_as_of": stop_as_of,
            "protective_stop": decision.protective_stop,
            "formula_version": decision.formula_version,
        }
        fingerprint = _fingerprint(payload)
        with self.storage.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT id FROM position_stop_history WHERE decision_fingerprint=?",
                (fingerprint,),
            ).fetchone()
            if existing is not None:
                return str(existing["id"])
            latest = conn.execute(
                """SELECT * FROM position_stop_history WHERE position_lifecycle_id=?
                   ORDER BY stop_sequence DESC LIMIT 1""",
                (decision.position_lifecycle_id,),
            ).fetchone()
            authoritative_prior = float(latest["new_stop"]) if latest is not None else decision.prior_stop
            if decision.protective_stop + 1e-9 < authoritative_prior:
                raise ValueError("refusing to persist a downward long-position stop")
            if latest is not None and abs(decision.protective_stop - authoritative_prior) <= 1e-9:
                return str(latest["id"])
            sequence = int(latest["stop_sequence"]) + 1 if latest is not None else 1
            identifier = str(uuid.uuid4())
            created_at = iso_now()
            conn.execute(
                """INSERT INTO position_stop_history(
                  id,run_id,symbol,position_lifecycle_id,stop_sequence,prior_stop,new_stop,stop_change,
                  management_mode,source,stop_as_of,input_as_of,formula_version,config_hash,
                  decision_fingerprint,raw_inputs_json,created_at)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    identifier, run_id, decision.symbol, decision.position_lifecycle_id, sequence,
                    authoritative_prior, decision.protective_stop,
                    max(0.0, decision.protective_stop - authoritative_prior), decision.mode,
                    source, stop_as_of, decision.raw_inputs.get("as_of"), decision.formula_version,
                    config_hash, fingerprint, json_dumps(decision.raw_inputs), created_at,
                ),
            )
            return identifier

    def persist_authoritative_stop(
        self,
        decision: TrendManagementDecision,
        *,
        run_id: str | None,
        source: str,
        stop_as_of: str,
        config_hash: str | None = None,
        peak_r_multiple: float | None = None,
    ) -> dict[str, Any]:
        """Append stop history and advance PM authority in one write lock.

        The transaction derives the effective prior stop from both durable
        history and position-management state, so a stale caller can never
        loosen a newer long-position stop.
        """
        if _parse_time(stop_as_of) is None:
            raise ValueError("stop_as_of must be an ISO timestamp")
        with self.storage.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            state = conn.execute(
                """SELECT * FROM position_management_state
                   WHERE symbol=? AND position_lifecycle_id=?""",
                (decision.symbol, decision.position_lifecycle_id),
            ).fetchone()
            if state is None:
                raise RuntimeError("position-management state is required for authoritative stop persistence")
            latest = conn.execute(
                """SELECT * FROM position_stop_history WHERE position_lifecycle_id=?
                   ORDER BY stop_sequence DESC LIMIT 1""",
                (decision.position_lifecycle_id,),
            ).fetchone()
            candidates = [float(decision.prior_stop)]
            for value in (
                state["authoritative_protective_stop"],
                latest["new_stop"] if latest is not None else None,
            ):
                if value is not None and float(value) > 0:
                    candidates.append(float(value))
            authoritative_prior = max(candidates)
            target_stop = max(authoritative_prior, float(decision.protective_stop))
            current_mode = str(state["management_mode"] or "")
            incoming_previous = str(decision.previous_mode or "")
            safety_rank = {
                PositionManagementMode.TREND_PYRAMID.value: 0,
                PositionManagementMode.TREND_HOLD.value: 1,
                PositionManagementMode.STANDARD_SCALE_OUT.value: 2,
                PositionManagementMode.DEFENSIVE_HARVEST.value: 3,
                PositionManagementMode.PROFIT_PROTECT.value: 4,
                PositionManagementMode.EXIT_REQUIRED.value: 5,
            }
            effective_mode = decision.mode
            preserved_current_mode = False
            if current_mode == PositionManagementMode.EXIT_REQUIRED.value:
                effective_mode = current_mode
                preserved_current_mode = True
            elif current_mode and incoming_previous != current_mode:
                # A decision calculated from an older PM state may still race
                # after a newer writer.  In that conflict preserve the safer
                # in-transaction mode; a fresh decision whose previous_mode
                # matches current state may follow the normal transition map.
                if safety_rank.get(current_mode, -1) > safety_rank.get(decision.mode, -1):
                    effective_mode = current_mode
                    preserved_current_mode = True
            sequence = int(latest["stop_sequence"]) if latest is not None else 0
            history_id = str(latest["id"]) if latest is not None else None
            effective_source = str(latest["source"]) if latest is not None else source
            effective_as_of = str(latest["stop_as_of"]) if latest is not None else stop_as_of
            effective_formula = str(latest["formula_version"]) if latest is not None else decision.formula_version

            if latest is None or target_stop > float(latest["new_stop"]) + 1e-9:
                payload = {
                    "trend_decision_fingerprint": decision.decision_fingerprint,
                    "source": source,
                    "stop_as_of": stop_as_of,
                    "protective_stop": target_stop,
                    "management_mode": effective_mode,
                    "formula_version": decision.formula_version,
                }
                fingerprint = _fingerprint(payload)
                existing = conn.execute(
                    "SELECT * FROM position_stop_history WHERE decision_fingerprint=?",
                    (fingerprint,),
                ).fetchone()
                if existing is not None:
                    history_id = str(existing["id"])
                    sequence = int(existing["stop_sequence"])
                    target_stop = max(target_stop, float(existing["new_stop"]))
                else:
                    sequence += 1
                    history_id = str(uuid.uuid4())
                    conn.execute(
                        """INSERT INTO position_stop_history(
                          id,run_id,symbol,position_lifecycle_id,stop_sequence,prior_stop,new_stop,stop_change,
                          management_mode,source,stop_as_of,input_as_of,formula_version,config_hash,
                          decision_fingerprint,raw_inputs_json,created_at)
                          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            history_id, run_id, decision.symbol, decision.position_lifecycle_id, sequence,
                            authoritative_prior, target_stop, max(0.0, target_stop - authoritative_prior),
                            effective_mode, source, stop_as_of, decision.raw_inputs.get("as_of"),
                            decision.formula_version, config_hash, fingerprint,
                            json_dumps(decision.raw_inputs), iso_now(),
                        ),
                    )
                effective_source = source
                effective_as_of = stop_as_of
                effective_formula = decision.formula_version

            # Revalidating an unchanged stop refreshes its validation age and
            # provenance without fabricating a stop-change history row.
            effective_source = source
            effective_as_of = stop_as_of
            effective_formula = decision.formula_version
            effective_trend_formula = (
                str(state["trend_management_formula_version"] or decision.formula_version)
                if preserved_current_mode else decision.formula_version
            )

            changed = conn.execute(
                """UPDATE position_management_state SET authoritative_protective_stop=?,
                   protective_stop_as_of=?,protective_stop_source=?,protective_stop_formula_version=?,
                   protective_stop_sequence=?,management_mode=?,trend_management_formula_version=?,
                   peak_r_multiple=MAX(COALESCE(peak_r_multiple,0),?),updated_at=?
                   WHERE symbol=? AND position_lifecycle_id=?""",
                (
                    target_stop, effective_as_of, effective_source, effective_formula, sequence,
                    effective_mode, effective_trend_formula,
                    max(0.0, float(peak_r_multiple or decision.peak_r_multiple)), stop_as_of,
                    decision.symbol, decision.position_lifecycle_id,
                ),
            )
            if changed.rowcount != 1:
                raise RuntimeError("authoritative stop update lost its position-management row")
            return {
                "history_id": history_id,
                "stop_sequence": sequence,
                "authoritative_protective_stop": target_stop,
                "protective_stop_as_of": effective_as_of,
            }

    def persist_trend_decision(
        self,
        decision: TrendManagementDecision,
        *,
        run_id: str | None,
        config_hash: str | None = None,
    ) -> str:
        from .trend_management import validate_trend_decision_invariants

        validate_trend_decision_invariants(decision)
        identifier = str(uuid.uuid4())
        self.storage.execute(
            """INSERT OR IGNORE INTO trend_management_decisions(
              id,run_id,symbol,position_lifecycle_id,previous_mode,classified_mode,management_mode,
              transition,transition_reason,reason,current_r_multiple,peak_r_multiple,profit_giveback_ratio,
              atr,effective_atr_multiplier,calculated_stop_candidate,prior_stop,protective_stop,stop_changed,
              stop_monotonic,recommended_partial_exit_fraction,retained_runner_fraction,allow_pyramiding,
              defer_fixed_profit_target,blocking_reasons_json,raw_inputs_json,formula_version,config_hash,
              decision_fingerprint,created_at)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                identifier, run_id, decision.symbol, decision.position_lifecycle_id, decision.previous_mode,
                decision.classified_mode, decision.mode, decision.transition, decision.transition_reason,
                decision.reason, decision.current_r_multiple, decision.peak_r_multiple,
                decision.profit_giveback_ratio, decision.atr, decision.effective_atr_multiplier,
                decision.calculated_stop_candidate, decision.prior_stop, decision.protective_stop,
                int(decision.stop_changed), int(decision.stop_monotonic),
                decision.recommended_partial_exit_fraction, decision.retained_runner_fraction,
                int(decision.allow_pyramiding), int(decision.defer_fixed_profit_target),
                json_dumps(decision.blocking_reasons), json_dumps(decision.raw_inputs),
                decision.formula_version, config_hash, decision.decision_fingerprint, iso_now(),
            ),
        )
        rows = self.storage.fetch_all(
            "SELECT id FROM trend_management_decisions WHERE decision_fingerprint=?",
            (decision.decision_fingerprint,),
        )
        return str(rows[0]["id"])

    def persist_add_risk_decision(
        self,
        decision: WinnerExpansionDecision,
        *,
        run_id: str | None,
        proposal_id: str | None = None,
        approval_id: str | None = None,
        milestone_id: str | None = None,
        config_hash: str | None = None,
    ) -> str:
        risk = decision.risk_decision
        identifier = str(uuid.uuid4())
        # Persist the canonical risk fingerprint, not the wrapper fingerprint,
        # so proposal/final stages can be independently audited by stage IDs.
        persistence_fingerprint = _fingerprint({
            "winner_expansion_decision": decision.decision_fingerprint,
            "decision_stage": decision.decision_stage,
            "proposal_id": proposal_id,
            "approval_id": approval_id,
        })
        self.storage.execute(
            """INSERT OR IGNORE INTO add_risk_decisions(
              id,run_id,proposal_id,approval_id,decision_stage,symbol,position_lifecycle_id,milestone_id,
              milestone_key,deployment_mode,eligible,reason,blocking_reasons_json,binding_cap,
              pre_add_shares,pre_add_stop,pre_add_open_risk_gross,pre_add_open_risk_net,
              proposed_add_shares,proposed_add_price,proposed_tightened_stop,post_add_total_shares,
              post_add_open_risk_gross,post_add_open_risk_net,incremental_risk,consumed_risk,released_risk,
              realized_profit_credit_requested,realized_profit_credit_applied,
              projected_portfolio_heat_dollars,projected_symbol_exposure_dollars,
              projected_cluster_exposure_dollars,projected_portfolio_gross_exposure_dollars,
              caps_json,raw_inputs_json,formula_version,config_hash,decision_fingerprint,created_at)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                identifier, run_id, proposal_id, approval_id, decision.decision_stage, decision.symbol,
                decision.position_lifecycle_id, milestone_id, decision.milestone_key,
                decision.deployment_mode, int(decision.eligible), decision.reason,
                json_dumps(decision.blocking_reasons), decision.binding_cap, risk.pre_add_shares,
                risk.pre_add_stop, risk.pre_add_open_risk_gross, risk.pre_add_open_risk_net,
                risk.proposed_add_shares, risk.proposed_add_price, risk.proposed_tightened_stop,
                risk.post_add_total_shares, risk.post_add_open_risk_gross, risk.post_add_open_risk_net,
                risk.incremental_risk, risk.consumed_risk, risk.released_risk,
                risk.realized_profit_credit_requested, risk.realized_profit_credit_applied,
                risk.projected_portfolio_heat_dollars, risk.projected_symbol_exposure_dollars,
                risk.projected_cluster_exposure_dollars, risk.projected_portfolio_gross_exposure_dollars,
                json_dumps(risk.caps), json_dumps({"winner": decision.raw_inputs, "risk": risk.raw_inputs}),
                risk.formula_version, config_hash, persistence_fingerprint, iso_now(),
            ),
        )
        rows = self.storage.fetch_all(
            "SELECT id FROM add_risk_decisions WHERE decision_fingerprint=?",
            (persistence_fingerprint,),
        )
        return str(rows[0]["id"])
