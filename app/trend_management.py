"""Deterministic, versioned trend-sensitive position management.

The engine classifies a long position and calculates a monotonic protective
stop.  It is pure: callers must durably persist a returned stop before using it
to authorize an ADD or treating it as current protection.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any, Mapping

from .formula_versions import TREND_MANAGEMENT_FORMULA_VERSION


class PositionManagementMode(StrEnum):
    DEFENSIVE_HARVEST = "DEFENSIVE_HARVEST"
    STANDARD_SCALE_OUT = "STANDARD_SCALE_OUT"
    TREND_HOLD = "TREND_HOLD"
    TREND_PYRAMID = "TREND_PYRAMID"
    PROFIT_PROTECT = "PROFIT_PROTECT"
    EXIT_REQUIRED = "EXIT_REQUIRED"


ALL_MODES = frozenset(PositionManagementMode)
ALLOWED_TRANSITIONS: dict[PositionManagementMode, frozenset[PositionManagementMode]] = {
    PositionManagementMode.DEFENSIVE_HARVEST: frozenset({
        PositionManagementMode.DEFENSIVE_HARVEST,
        PositionManagementMode.STANDARD_SCALE_OUT,
        PositionManagementMode.PROFIT_PROTECT,
        PositionManagementMode.EXIT_REQUIRED,
    }),
    PositionManagementMode.STANDARD_SCALE_OUT: ALL_MODES,
    PositionManagementMode.TREND_HOLD: ALL_MODES,
    PositionManagementMode.TREND_PYRAMID: ALL_MODES,
    PositionManagementMode.PROFIT_PROTECT: frozenset({
        PositionManagementMode.PROFIT_PROTECT,
        PositionManagementMode.DEFENSIVE_HARVEST,
        PositionManagementMode.STANDARD_SCALE_OUT,
        PositionManagementMode.EXIT_REQUIRED,
    }),
    PositionManagementMode.EXIT_REQUIRED: frozenset({PositionManagementMode.EXIT_REQUIRED}),
}


MODE_POLICY: dict[PositionManagementMode, dict[str, float | bool]] = {
    PositionManagementMode.DEFENSIVE_HARVEST: {
        "atr_multiplier": 1.50,
        "minimum_atr_multiplier": 1.00,
        "partial_exit_fraction": 0.33,
        "runner_fraction": 0.34,
        "allow_pyramiding": False,
        "defer_fixed_profit_target": False,
    },
    PositionManagementMode.STANDARD_SCALE_OUT: {
        "atr_multiplier": 2.00,
        "minimum_atr_multiplier": 1.25,
        "partial_exit_fraction": 0.25,
        "runner_fraction": 0.42,
        "allow_pyramiding": False,
        "defer_fixed_profit_target": False,
    },
    PositionManagementMode.TREND_HOLD: {
        "atr_multiplier": 2.75,
        "minimum_atr_multiplier": 1.75,
        "partial_exit_fraction": 0.10,
        "runner_fraction": 0.70,
        "allow_pyramiding": False,
        "defer_fixed_profit_target": True,
    },
    PositionManagementMode.TREND_PYRAMID: {
        "atr_multiplier": 3.00,
        "minimum_atr_multiplier": 2.00,
        "partial_exit_fraction": 0.0,
        "runner_fraction": 1.0,
        "allow_pyramiding": True,
        "defer_fixed_profit_target": True,
    },
    PositionManagementMode.PROFIT_PROTECT: {
        "atr_multiplier": 1.25,
        "minimum_atr_multiplier": 0.75,
        "partial_exit_fraction": 0.50,
        "runner_fraction": 0.25,
        "allow_pyramiding": False,
        "defer_fixed_profit_target": False,
    },
    PositionManagementMode.EXIT_REQUIRED: {
        "atr_multiplier": 0.0,
        "minimum_atr_multiplier": 0.0,
        "partial_exit_fraction": 1.0,
        "runner_fraction": 0.0,
        "allow_pyramiding": False,
        "defer_fixed_profit_target": False,
    },
}


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _positive(value: Any, name: str) -> float:
    number = _finite(value)
    if number is None or number <= 0:
        raise ValueError(f"{name} must be a finite positive number")
    return number


def _bounded(value: Any, name: str, low: float, high: float) -> float:
    number = _finite(value)
    if number is None or not low <= number <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return number


def _fingerprint(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class TrendManagementInput:
    symbol: str
    position_lifecycle_id: str
    current_price: float
    average_entry_price: float
    highest_price_since_entry: float
    current_protective_stop: float
    atr: float
    current_r_multiple: float
    peak_r_multiple: float
    trend_strength: float
    price_above_ma50: bool
    ma50_above_ma200: bool
    higher_highs_and_lows: bool
    market_regime: str
    volatility_regime: str
    deployment_mode: str
    execution_quality: float
    account_health: float
    account_drawdown_pct: float
    position_age_days: float
    previous_mode: str | None = None
    deterioration_detected: bool = False
    profit_protection_triggered: bool = False
    emergency_exit: bool = False
    normal_exit_signal: bool = False
    integrity_warning: bool = False
    reconciliation_warning: bool = False
    as_of: str | None = None
    minimum_price_increment: float = 0.01


@dataclass(frozen=True)
class TrendManagementDecision:
    symbol: str
    position_lifecycle_id: str
    previous_mode: str | None
    classified_mode: str
    mode: str
    transition: str
    transition_reason: str
    reason: str
    current_r_multiple: float
    peak_r_multiple: float
    profit_giveback_ratio: float
    atr: float
    effective_atr_multiplier: float
    calculated_stop_candidate: float
    prior_stop: float
    protective_stop: float
    stop_changed: bool
    stop_monotonic: bool
    recommended_partial_exit_fraction: float
    retained_runner_fraction: float
    allow_pyramiding: bool
    defer_fixed_profit_target: bool
    blocking_reasons: tuple[str, ...]
    raw_inputs: dict[str, Any]
    formula_version: str
    decision_fingerprint: str

    @property
    def is_exit_required(self) -> bool:
        return self.mode == PositionManagementMode.EXIT_REQUIRED.value


def validate_trend_decision_invariants(decision: TrendManagementDecision) -> None:
    try:
        mode = PositionManagementMode(decision.mode)
    except ValueError as exc:
        raise ValueError("trend decision has an invalid final mode") from exc
    policy = MODE_POLICY[mode]
    contradictions: list[str] = []
    expected_pyramiding = mode is PositionManagementMode.TREND_PYRAMID and bool(policy["allow_pyramiding"])
    if decision.allow_pyramiding != expected_pyramiding:
        contradictions.append("pyramiding authority contradicts final mode")
    if decision.defer_fixed_profit_target != bool(policy["defer_fixed_profit_target"]):
        contradictions.append("fixed-target deferral contradicts final mode")
    if abs(decision.recommended_partial_exit_fraction - float(policy["partial_exit_fraction"])) > 1e-12:
        contradictions.append("partial-exit fraction contradicts final mode")
    if abs(decision.retained_runner_fraction - float(policy["runner_fraction"])) > 1e-12:
        contradictions.append("runner retention contradicts final mode")
    if mode is PositionManagementMode.EXIT_REQUIRED and (
        decision.allow_pyramiding
        or decision.defer_fixed_profit_target
        or decision.retained_runner_fraction != 0.0
        or decision.recommended_partial_exit_fraction != 1.0
    ):
        contradictions.append("EXIT_REQUIRED must be a full non-runner exit with no ADD authority")
    if contradictions:
        raise ValueError("; ".join(contradictions))


class TrendManagementEngine:
    """Classify mode and calculate a durable-candidate long stop."""

    def evaluate(self, value: TrendManagementInput | Mapping[str, Any]) -> TrendManagementDecision:
        inputs = value if isinstance(value, TrendManagementInput) else TrendManagementInput(**dict(value))
        symbol = str(inputs.symbol or "").upper()
        lifecycle = str(inputs.position_lifecycle_id or "")
        if not symbol or not lifecycle:
            raise ValueError("symbol and position_lifecycle_id are required")
        current_price = _positive(inputs.current_price, "current_price")
        entry = _positive(inputs.average_entry_price, "average_entry_price")
        highest = _positive(inputs.highest_price_since_entry, "highest_price_since_entry")
        prior_stop = _positive(inputs.current_protective_stop, "current_protective_stop")
        atr = _positive(inputs.atr, "atr")
        r_multiple = _finite(inputs.current_r_multiple)
        peak_r = _finite(inputs.peak_r_multiple)
        age = _finite(inputs.position_age_days)
        drawdown = _finite(inputs.account_drawdown_pct)
        if r_multiple is None or peak_r is None or age is None or age < 0 or drawdown is None or drawdown < 0:
            raise ValueError("R multiples, position age, and account drawdown must be finite and non-negative where applicable")
        trend_strength = _bounded(inputs.trend_strength, "trend_strength", 0.0, 100.0)
        execution_quality = _bounded(inputs.execution_quality, "execution_quality", 0.0, 1.0)
        account_health = _bounded(inputs.account_health, "account_health", 0.0, 1.0)
        tick = _positive(inputs.minimum_price_increment, "minimum_price_increment")
        if highest + tick < current_price:
            raise ValueError("highest_price_since_entry cannot be below current_price")

        deployment_mode = str(inputs.deployment_mode or "").upper()
        if deployment_mode not in {"DEFENSIVE", "NORMAL", "OPPORTUNISTIC", "AGGRESSIVE"}:
            raise ValueError("invalid deployment_mode")
        regime = str(inputs.market_regime or "").lower()
        volatility = str(inputs.volatility_regime or "").lower()
        giveback = max(0.0, (peak_r - r_multiple) / peak_r) if peak_r > 0 else 0.0

        previous: PositionManagementMode | None = None
        if inputs.previous_mode:
            try:
                previous = PositionManagementMode(str(inputs.previous_mode))
            except ValueError as exc:
                raise ValueError("invalid previous_mode") from exc

        exit_reason: str | None = None
        if inputs.emergency_exit:
            exit_reason = "emergency exit is authoritative"
        elif inputs.normal_exit_signal:
            exit_reason = "normal risk exit signal is authoritative"
        elif prior_stop >= current_price - tick / 2.0:
            exit_reason = "current price has reached or breached the persisted protective stop"
        elif inputs.integrity_warning or inputs.reconciliation_warning:
            exit_reason = "execution or reconciliation integrity warning requires exit-first handling"

        protect_threshold = 0.35 if peak_r >= 3.0 else 0.50
        defensive = (
            deployment_mode == "DEFENSIVE"
            or regime in {"defensive", "risk_off", "extreme"}
            or volatility == "extreme"
            or account_health < 0.60
            or execution_quality < 0.55
            or drawdown >= 4.0
        )
        strong_trend = (
            trend_strength >= 75.0
            and inputs.price_above_ma50 is True
            and inputs.ma50_above_ma200 is True
            and inputs.higher_highs_and_lows is True
            and regime in {"favorable", "normal", "bull", "risk_on"}
            and volatility not in {"extreme", "dislocated"}
            and execution_quality >= 0.70
            and account_health >= 0.70
            and r_multiple >= 1.0
            and giveback < protect_threshold
        )

        if exit_reason is not None:
            classified = PositionManagementMode.EXIT_REQUIRED
            classified_reason = exit_reason
        elif inputs.profit_protection_triggered or inputs.deterioration_detected or giveback >= protect_threshold:
            classified = PositionManagementMode.PROFIT_PROTECT
            classified_reason = "trend deterioration or mature-profit giveback requires tighter protection"
        elif defensive:
            classified = PositionManagementMode.DEFENSIVE_HARVEST
            classified_reason = "defensive regime, execution, volatility, or account conditions require conservative harvesting"
        elif (
            strong_trend
            # NORMAL may classify a mature trend for pyramiding, but its
            # position-risk allowance remains zero.  The downstream winner
            # engine therefore permits only a genuinely risk-neutral ADD in
            # NORMAL while OPPORTUNISTIC/AGGRESSIVE may consume their explicit
            # bounded incremental-risk allowances.
            and deployment_mode in {"NORMAL", "OPPORTUNISTIC", "AGGRESSIVE"}
            and r_multiple >= 1.5
            and age >= 1.0
            and giveback <= 0.25
        ):
            classified = PositionManagementMode.TREND_PYRAMID
            classified_reason = "strong mature trend qualifies for risk-aware pyramiding"
        elif strong_trend:
            classified = PositionManagementMode.TREND_HOLD
            classified_reason = "strong favorable trend supports a larger runner and deferred fixed target"
        else:
            classified = PositionManagementMode.STANDARD_SCALE_OUT
            classified_reason = "mixed or ordinary trend retains standard staged management"

        mode = classified
        transition_reason = "classification accepted"
        if previous is PositionManagementMode.EXIT_REQUIRED:
            mode = PositionManagementMode.EXIT_REQUIRED
            transition_reason = "EXIT_REQUIRED is terminal for the current position lifecycle"
        elif previous is not None and classified not in ALLOWED_TRANSITIONS[previous]:
            # Recovery from defensive/profit-protect state must pass through the
            # ordinary mode before any later trend expansion.
            mode = PositionManagementMode.STANDARD_SCALE_OUT
            transition_reason = f"safe recovery transition from {previous.value} requires STANDARD_SCALE_OUT"
        transition = f"{previous.value if previous else 'UNINITIALIZED'}->{mode.value}"

        policy = MODE_POLICY[mode]
        base_multiplier = float(policy["atr_multiplier"])
        minimum_multiplier = float(policy["minimum_atr_multiplier"])
        effective_multiplier = base_multiplier
        if mode is not PositionManagementMode.EXIT_REQUIRED:
            # As a winner matures, the trail may remain volatility-aware but its
            # permitted giveback narrows deterministically.
            if peak_r >= 3.0:
                effective_multiplier -= 0.35
            if peak_r >= 5.0:
                effective_multiplier -= 0.25
            if giveback >= 0.25:
                effective_multiplier -= 0.35
            effective_multiplier = max(minimum_multiplier, effective_multiplier)
            calculated = highest - effective_multiplier * atr
            calculated = min(calculated, current_price - tick)
            protective_stop = max(prior_stop, calculated)
        else:
            calculated = prior_stop
            protective_stop = prior_stop

        stop_monotonic = protective_stop + 1e-9 >= prior_stop
        blocking: list[str] = []
        if not stop_monotonic:
            blocking.append("calculated stop would loosen persisted protection")
            protective_stop = prior_stop
        if protective_stop >= current_price and mode is not PositionManagementMode.EXIT_REQUIRED:
            # A previously persisted stop above the market is not loosened; the
            # authoritative outcome is an exit rather than rewriting history.
            mode = PositionManagementMode.EXIT_REQUIRED
            transition = f"{previous.value if previous else 'UNINITIALIZED'}->{mode.value}"
            transition_reason = "persisted stop is breached; stop cannot be loosened"
            classified_reason = transition_reason

        # The persisted-stop check can change the final mode after the first
        # policy lookup. Rebuild all mode-dependent values from the final mode.
        policy = MODE_POLICY[mode]
        if mode is PositionManagementMode.EXIT_REQUIRED:
            effective_multiplier = 0.0
            calculated = prior_stop
            protective_stop = prior_stop

        raw_inputs = asdict(inputs)
        raw_inputs.update({
            "normalized_deployment_mode": deployment_mode,
            "normalized_market_regime": regime,
            "normalized_volatility_regime": volatility,
            "computed_profit_giveback_ratio": giveback,
            "strong_trend": strong_trend,
            "defensive_conditions": defensive,
            "protect_threshold": protect_threshold,
        })
        payload = {
            "raw_inputs": raw_inputs,
            "classified_mode": classified.value,
            "operational_mode": mode.value,
            "transition": transition,
            "transition_reason": transition_reason,
            "calculated_stop": calculated,
            "protective_stop": protective_stop,
            "effective_atr_multiplier": effective_multiplier,
            "policy": policy,
            "formula_version": TREND_MANAGEMENT_FORMULA_VERSION,
        }
        fingerprint = _fingerprint(payload)
        decision = TrendManagementDecision(
            symbol=symbol,
            position_lifecycle_id=lifecycle,
            previous_mode=previous.value if previous else None,
            classified_mode=classified.value,
            mode=mode.value,
            transition=transition,
            transition_reason=transition_reason,
            reason=classified_reason,
            current_r_multiple=r_multiple,
            peak_r_multiple=peak_r,
            profit_giveback_ratio=giveback,
            atr=atr,
            effective_atr_multiplier=effective_multiplier,
            calculated_stop_candidate=calculated,
            prior_stop=prior_stop,
            protective_stop=protective_stop,
            stop_changed=protective_stop > prior_stop + 1e-9,
            stop_monotonic=stop_monotonic,
            recommended_partial_exit_fraction=float(policy["partial_exit_fraction"]),
            retained_runner_fraction=float(policy["runner_fraction"]),
            allow_pyramiding=bool(policy["allow_pyramiding"]) and mode is PositionManagementMode.TREND_PYRAMID,
            defer_fixed_profit_target=bool(policy["defer_fixed_profit_target"]),
            blocking_reasons=tuple(blocking),
            raw_inputs=raw_inputs,
            formula_version=TREND_MANAGEMENT_FORMULA_VERSION,
            decision_fingerprint=fingerprint,
        )
        validate_trend_decision_invariants(decision)
        return decision


__all__ = [
    "PositionManagementMode",
    "TrendManagementDecision",
    "TrendManagementEngine",
    "TrendManagementInput",
    "TREND_MANAGEMENT_FORMULA_VERSION",
    "validate_trend_decision_invariants",
]
