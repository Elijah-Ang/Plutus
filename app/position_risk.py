"""Canonical long-position risk for operational winner expansion.

This module is deliberately pure.  It does not read storage, mutate a stop,
reserve capital, approve a proposal, or submit an order.  Callers provide one
authoritative snapshot and persist the returned decision before relying on it.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from typing import Any, Mapping


POSITION_RISK_FORMULA_VERSION = "position_open_risk_v1_operational_paper"
HARD_TRADE_RISK_CEILING_PCT = 0.35
DEPLOYMENT_MODE_TRADE_RISK_CAP_PCT: dict[str, float] = {
    "DEFENSIVE": 0.15,
    "NORMAL": 0.20,
    "OPPORTUNISTIC": 0.30,
    "AGGRESSIVE": 0.35,
}
DEPLOYMENT_MODES = frozenset(DEPLOYMENT_MODE_TRADE_RISK_CAP_PCT)
CAP_ORDER = (
    "defensive_no_add",
    "stop_monotonicity",
    "mode_incremental_risk",
    "deployment_mode_trade_risk",
    "adaptive_conviction_position_risk",
    "phase3_position_risk",
    "hard_position_risk",
    "portfolio_heat",
    "symbol_exposure",
    "cluster_exposure",
    "portfolio_gross_exposure",
)


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _nonnegative(value: Any, name: str) -> float:
    number = _finite(value)
    if number is None or number < 0:
        raise ValueError(f"{name} must be a finite non-negative number")
    return number


def _positive(value: Any, name: str) -> float:
    number = _finite(value)
    if number is None or number <= 0:
        raise ValueError(f"{name} must be a finite positive number")
    return number


def _fingerprint(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class PositionRiskInput:
    symbol: str
    position_lifecycle_id: str
    deployment_mode: str
    current_shares: float
    proposed_add_shares: float
    current_market_price: float
    proposed_add_price: float
    current_protective_stop: float
    proposed_tightened_stop: float
    portfolio_equity: float

    # Realized profit is never silently netted.  Both flags must be true and
    # the same conservative credit is applied to pre- and post-ADD gross risk.
    realized_profit_credit_dollars: float = 0.0
    realized_profit_credit_eligible: bool = False
    realized_profit_credit_verified: bool = False
    realized_profit_evidence_id: str | None = None

    # Existing commitments.  ``portfolio_*_excluding_position`` must exclude
    # the position represented by current_shares so the post-position risk is
    # substituted rather than double counted.
    same_symbol_reserved_stop_risk_dollars: float = 0.0
    active_reserved_stop_risk_dollars: float = 0.0
    portfolio_open_risk_excluding_position_dollars: float = 0.0
    active_reserved_exposure_dollars: float = 0.0
    same_symbol_reserved_exposure_dollars: float = 0.0
    cluster_reserved_exposure_dollars: float = 0.0
    portfolio_gross_exposure_excluding_position_dollars: float = 0.0
    cluster_exposure_excluding_position_dollars: float = 0.0

    # Position-risk caps are percentages of equity.  Adaptive and Phase 3 may
    # only tighten the deployment-mode cap; the hard ceiling is always 0.35%.
    deployment_mode_trade_risk_cap_pct: float | None = None
    adaptive_conviction_position_risk_cap_pct: float | None = None
    phase3_position_risk_cap_pct: float = HARD_TRADE_RISK_CEILING_PCT
    hard_trade_risk_cap_pct: float = HARD_TRADE_RISK_CEILING_PCT
    portfolio_heat_cap_pct: float = 1.75
    symbol_exposure_cap_pct: float = 6.0
    cluster_exposure_cap_pct: float = 15.0
    portfolio_gross_exposure_cap_pct: float = 50.0

    # NORMAL permits only rounding tolerance.  Favourable modes require an
    # explicit configured allowance; there is intentionally no dollar default.
    mode_incremental_risk_allowance_pct: float | None = None
    rounding_tolerance_dollars: float = 0.01
    stop_rounding_tolerance: float = 1e-9
    as_of: str | None = None
    config_hash: str | None = None


@dataclass(frozen=True)
class PositionRiskDecision:
    symbol: str
    position_lifecycle_id: str
    deployment_mode: str
    eligible: bool
    reason: str
    blocking_reasons: tuple[str, ...]
    binding_cap: str

    pre_add_shares: float
    pre_add_stop: float
    pre_add_open_risk_gross: float
    pre_add_open_risk_net: float
    proposed_add_shares: float
    proposed_add_price: float
    proposed_tightened_stop: float
    post_add_total_shares: float
    post_add_open_risk_gross: float
    post_add_open_risk_net: float
    incremental_risk: float
    consumed_risk: float
    released_risk: float
    realized_profit_credit_requested: float
    realized_profit_credit_applied: float

    pre_position_commitment_risk: float
    post_position_commitment_risk: float
    mode_incremental_risk_allowance_dollars: float
    position_risk_cap_dollars: float
    projected_portfolio_heat_dollars: float
    projected_symbol_exposure_dollars: float
    projected_cluster_exposure_dollars: float
    projected_portfolio_gross_exposure_dollars: float
    caps: dict[str, float]
    raw_inputs: dict[str, Any]
    formula_version: str
    decision_fingerprint: str

    def summary(self) -> dict[str, Any]:
        return {
            "eligible": self.eligible,
            "reason": self.reason,
            "binding_cap": self.binding_cap,
            "pre_add_open_risk": self.pre_add_open_risk_net,
            "post_add_open_risk": self.post_add_open_risk_net,
            "incremental_risk": self.incremental_risk,
            "released_risk": self.released_risk,
            "post_add_total_shares": self.post_add_total_shares,
            "proposed_tightened_stop": self.proposed_tightened_stop,
            "formula_version": self.formula_version,
            "decision_fingerprint": self.decision_fingerprint,
        }


class PositionRiskEngine:
    """Calculate authoritative pre/post risk for one proposed long ADD."""

    def evaluate(self, value: PositionRiskInput | Mapping[str, Any]) -> PositionRiskDecision:
        inputs = value if isinstance(value, PositionRiskInput) else PositionRiskInput(**dict(value))
        mode = str(inputs.deployment_mode or "").upper()
        if mode not in DEPLOYMENT_MODES:
            raise ValueError(f"deployment_mode must be one of {sorted(DEPLOYMENT_MODES)}")
        symbol = str(inputs.symbol or "").upper()
        lifecycle = str(inputs.position_lifecycle_id or "")
        if not symbol or not lifecycle:
            raise ValueError("symbol and position_lifecycle_id are required")

        current_shares = _positive(inputs.current_shares, "current_shares")
        add_shares = _positive(inputs.proposed_add_shares, "proposed_add_shares")
        current_price = _positive(inputs.current_market_price, "current_market_price")
        add_price = _positive(inputs.proposed_add_price, "proposed_add_price")
        current_stop = _positive(inputs.current_protective_stop, "current_protective_stop")
        proposed_stop = _positive(inputs.proposed_tightened_stop, "proposed_tightened_stop")
        equity = _positive(inputs.portfolio_equity, "portfolio_equity")
        tolerance = _nonnegative(inputs.rounding_tolerance_dollars, "rounding_tolerance_dollars")
        stop_tolerance = _nonnegative(inputs.stop_rounding_tolerance, "stop_rounding_tolerance")

        numeric_nonnegative = {
            "realized_profit_credit_dollars": inputs.realized_profit_credit_dollars,
            "same_symbol_reserved_stop_risk_dollars": inputs.same_symbol_reserved_stop_risk_dollars,
            "active_reserved_stop_risk_dollars": inputs.active_reserved_stop_risk_dollars,
            "portfolio_open_risk_excluding_position_dollars": inputs.portfolio_open_risk_excluding_position_dollars,
            "active_reserved_exposure_dollars": inputs.active_reserved_exposure_dollars,
            "same_symbol_reserved_exposure_dollars": inputs.same_symbol_reserved_exposure_dollars,
            "cluster_reserved_exposure_dollars": inputs.cluster_reserved_exposure_dollars,
            "portfolio_gross_exposure_excluding_position_dollars": inputs.portfolio_gross_exposure_excluding_position_dollars,
            "cluster_exposure_excluding_position_dollars": inputs.cluster_exposure_excluding_position_dollars,
        }
        amounts = {name: _nonnegative(number, name) for name, number in numeric_nonnegative.items()}

        pre_gross = current_shares * max(current_price - current_stop, 0.0)
        post_gross = (
            current_shares * max(current_price - proposed_stop, 0.0)
            + add_shares * max(add_price - proposed_stop, 0.0)
        )
        requested_credit = amounts["realized_profit_credit_dollars"]
        credit_authorized = (
            inputs.realized_profit_credit_eligible is True
            and inputs.realized_profit_credit_verified is True
            and bool(inputs.realized_profit_evidence_id)
        )
        # Applying one identical credit to both sides prevents a claimed credit
        # from manufacturing artificial risk-neutrality for the ADD itself.
        applied_credit = min(requested_credit, pre_gross, post_gross) if credit_authorized else 0.0
        pre_net = max(0.0, pre_gross - applied_credit)
        post_net = max(0.0, post_gross - applied_credit)
        same_symbol_reserved_risk = amounts["same_symbol_reserved_stop_risk_dollars"]
        pre_commitment = pre_net + same_symbol_reserved_risk
        post_commitment = post_net + same_symbol_reserved_risk
        incremental = post_commitment - pre_commitment
        consumed = max(0.0, incremental)
        released = max(0.0, -incremental)

        configured_mode_cap = (
            DEPLOYMENT_MODE_TRADE_RISK_CAP_PCT[mode]
            if inputs.deployment_mode_trade_risk_cap_pct is None
            else _nonnegative(inputs.deployment_mode_trade_risk_cap_pct, "deployment_mode_trade_risk_cap_pct")
        )
        adaptive_cap = (
            configured_mode_cap
            if inputs.adaptive_conviction_position_risk_cap_pct is None
            else _nonnegative(inputs.adaptive_conviction_position_risk_cap_pct, "adaptive_conviction_position_risk_cap_pct")
        )
        phase3_cap = _nonnegative(inputs.phase3_position_risk_cap_pct, "phase3_position_risk_cap_pct")
        requested_hard_cap = _nonnegative(inputs.hard_trade_risk_cap_pct, "hard_trade_risk_cap_pct")
        hard_cap = min(HARD_TRADE_RISK_CEILING_PCT, requested_hard_cap)
        effective_position_cap_pct = min(configured_mode_cap, adaptive_cap, phase3_cap, hard_cap)
        position_cap_dollars = equity * effective_position_cap_pct / 100.0

        allowance_pct = inputs.mode_incremental_risk_allowance_pct
        if mode == "DEFENSIVE":
            allowance_dollars = 0.0
        elif mode == "NORMAL":
            allowance_dollars = tolerance
        elif allowance_pct is None:
            allowance_dollars = 0.0
        else:
            allowance_dollars = equity * _nonnegative(allowance_pct, "mode_incremental_risk_allowance_pct") / 100.0

        total_reserved_stop = amounts["active_reserved_stop_risk_dollars"]
        if total_reserved_stop + tolerance < same_symbol_reserved_risk:
            raise ValueError("active_reserved_stop_risk_dollars cannot be below the same-symbol reserved risk")
        projected_heat = (
            amounts["portfolio_open_risk_excluding_position_dollars"]
            + total_reserved_stop
            + post_net
        )
        post_position_notional = current_shares * current_price + add_shares * add_price
        projected_symbol = post_position_notional + amounts["same_symbol_reserved_exposure_dollars"]
        projected_cluster = (
            amounts["cluster_exposure_excluding_position_dollars"]
            + post_position_notional
            + amounts["cluster_reserved_exposure_dollars"]
        )
        projected_gross = (
            amounts["portfolio_gross_exposure_excluding_position_dollars"]
            + post_position_notional
            + amounts["active_reserved_exposure_dollars"]
        )

        heat_cap = equity * _nonnegative(inputs.portfolio_heat_cap_pct, "portfolio_heat_cap_pct") / 100.0
        symbol_cap = equity * _nonnegative(inputs.symbol_exposure_cap_pct, "symbol_exposure_cap_pct") / 100.0
        cluster_cap = equity * _nonnegative(inputs.cluster_exposure_cap_pct, "cluster_exposure_cap_pct") / 100.0
        gross_cap = equity * _nonnegative(inputs.portfolio_gross_exposure_cap_pct, "portfolio_gross_exposure_cap_pct") / 100.0
        caps = {
            "deployment_mode_trade_risk_pct": configured_mode_cap,
            "adaptive_conviction_position_risk_pct": adaptive_cap,
            "phase3_position_risk_pct": phase3_cap,
            "hard_position_risk_pct": hard_cap,
            "effective_position_risk_dollars": position_cap_dollars,
            "mode_incremental_risk_allowance_dollars": allowance_dollars,
            "portfolio_heat_dollars": heat_cap,
            "symbol_exposure_dollars": symbol_cap,
            "cluster_exposure_dollars": cluster_cap,
            "portfolio_gross_exposure_dollars": gross_cap,
        }

        blockers: list[str] = []
        failed_caps: list[str] = []

        def block(cap: str, condition: bool, reason: str) -> None:
            if condition:
                failed_caps.append(cap)
                blockers.append(reason)

        block("defensive_no_add", mode == "DEFENSIVE", "DEFENSIVE deployment mode forbids pyramiding")
        block(
            "stop_monotonicity",
            proposed_stop + stop_tolerance < current_stop,
            "proposed protective stop would move downward for a long position",
        )
        if mode in {"OPPORTUNISTIC", "AGGRESSIVE"} and allowance_pct is None:
            block(
                "mode_incremental_risk",
                True,
                f"{mode} requires an explicit configured incremental-risk allowance",
            )
        block(
            "mode_incremental_risk",
            consumed > allowance_dollars + tolerance,
            f"incremental risk exceeds the {mode} allowance",
        )
        block(
            "deployment_mode_trade_risk",
            post_commitment > equity * configured_mode_cap / 100.0 + tolerance,
            "combined position risk exceeds the deployment-mode trade-risk cap",
        )
        block(
            "adaptive_conviction_position_risk",
            post_commitment > equity * adaptive_cap / 100.0 + tolerance,
            "combined position risk exceeds the Adaptive Conviction allowance",
        )
        block(
            "phase3_position_risk",
            post_commitment > equity * phase3_cap / 100.0 + tolerance,
            "combined position risk exceeds the Phase 3 position-risk cap",
        )
        block(
            "hard_position_risk",
            post_commitment > equity * hard_cap / 100.0 + tolerance,
            "combined position risk exceeds the hard 0.35% ceiling",
        )
        block("portfolio_heat", projected_heat > heat_cap + tolerance, "projected portfolio heat exceeds its cap")
        block("symbol_exposure", projected_symbol > symbol_cap + tolerance, "projected symbol exposure exceeds its cap")
        block("cluster_exposure", projected_cluster > cluster_cap + tolerance, "projected cluster exposure exceeds its cap")
        block("portfolio_gross_exposure", projected_gross > gross_cap + tolerance, "projected gross exposure exceeds its cap")

        if failed_caps:
            binding_cap = next(name for name in CAP_ORDER if name in failed_caps)
        else:
            utilization = {
                "mode_incremental_risk": consumed / max(allowance_dollars, tolerance),
                "deployment_mode_trade_risk": post_commitment / max(equity * configured_mode_cap / 100.0, tolerance),
                "adaptive_conviction_position_risk": post_commitment / max(equity * adaptive_cap / 100.0, tolerance),
                "phase3_position_risk": post_commitment / max(equity * phase3_cap / 100.0, tolerance),
                "hard_position_risk": post_commitment / max(equity * hard_cap / 100.0, tolerance),
                "portfolio_heat": projected_heat / max(heat_cap, tolerance),
                "symbol_exposure": projected_symbol / max(symbol_cap, tolerance),
                "cluster_exposure": projected_cluster / max(cluster_cap, tolerance),
                "portfolio_gross_exposure": projected_gross / max(gross_cap, tolerance),
            }
            binding_cap = max(CAP_ORDER[2:], key=lambda name: (utilization.get(name, -1.0), -CAP_ORDER.index(name)))

        raw_inputs = asdict(inputs)
        raw_inputs.update({
            "normalized_symbol": symbol,
            "normalized_deployment_mode": mode,
            "realized_profit_credit_authorized": credit_authorized,
            "realized_profit_credit_policy": "verified_and_eligible_same_credit_pre_and_post",
            "portfolio_risk_accounting": "replace_current_position_then_add_active_reservations",
        })
        decision_payload = {
            "raw_inputs": raw_inputs,
            "calculation": {
                "pre_gross": pre_gross,
                "post_gross": post_gross,
                "credit": applied_credit,
                "pre_net": pre_net,
                "post_net": post_net,
                "pre_commitment": pre_commitment,
                "post_commitment": post_commitment,
                "incremental": incremental,
                "projected_heat": projected_heat,
                "projected_symbol": projected_symbol,
                "projected_cluster": projected_cluster,
                "projected_gross": projected_gross,
            },
            "caps": caps,
            "blockers": blockers,
            "formula_version": POSITION_RISK_FORMULA_VERSION,
        }
        fingerprint = _fingerprint(decision_payload)
        eligible = not blockers
        reason = (
            f"{mode} ADD keeps total position risk within all authoritative caps"
            if eligible
            else blockers[0]
        )
        return PositionRiskDecision(
            symbol=symbol,
            position_lifecycle_id=lifecycle,
            deployment_mode=mode,
            eligible=eligible,
            reason=reason,
            blocking_reasons=tuple(blockers),
            binding_cap=binding_cap,
            pre_add_shares=current_shares,
            pre_add_stop=current_stop,
            pre_add_open_risk_gross=pre_gross,
            pre_add_open_risk_net=pre_net,
            proposed_add_shares=add_shares,
            proposed_add_price=add_price,
            proposed_tightened_stop=proposed_stop,
            post_add_total_shares=current_shares + add_shares,
            post_add_open_risk_gross=post_gross,
            post_add_open_risk_net=post_net,
            incremental_risk=incremental,
            consumed_risk=consumed,
            released_risk=released,
            realized_profit_credit_requested=requested_credit,
            realized_profit_credit_applied=applied_credit,
            pre_position_commitment_risk=pre_commitment,
            post_position_commitment_risk=post_commitment,
            mode_incremental_risk_allowance_dollars=allowance_dollars,
            position_risk_cap_dollars=position_cap_dollars,
            projected_portfolio_heat_dollars=projected_heat,
            projected_symbol_exposure_dollars=projected_symbol,
            projected_cluster_exposure_dollars=projected_cluster,
            projected_portfolio_gross_exposure_dollars=projected_gross,
            caps=caps,
            raw_inputs=raw_inputs,
            formula_version=POSITION_RISK_FORMULA_VERSION,
            decision_fingerprint=fingerprint,
        )
