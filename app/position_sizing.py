"""Validated notional policy shared by sizing and final risk validation."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

from .formula_versions import SIZING_POLICY_VERSION, STOP_POLICY_VERSION


@dataclass(frozen=True)
class NotionalPolicy:
    minimum_executable_notional_usd: float
    default_notional_usd: float
    stage_max_notional_usd: float | None
    equity_max_notional_usd: float | None
    absolute_max_notional_usd: float | None
    policy_version: str = SIZING_POLICY_VERSION

    @property
    def ceiling_values(self) -> tuple[float, ...]:
        return tuple(
            value
            for value in (
                self.stage_max_notional_usd,
                self.equity_max_notional_usd,
                self.absolute_max_notional_usd,
            )
            if value is not None
        )

    @property
    def maximum_allowed_notional_usd(self) -> float:
        ceilings = self.ceiling_values
        return min(ceilings) if ceilings else float("inf")

    def named_ceilings(self) -> dict[str, float]:
        """Return only finite ceilings, retaining the source of each cap."""
        values = {
            "stage": self.stage_max_notional_usd,
            "equity": self.equity_max_notional_usd,
            "absolute": self.absolute_max_notional_usd,
        }
        return {name: float(value) for name, value in values.items() if value is not None}


def notional_from_stop_risk(stop_risk_dollars: float, entry_price: float, stop_distance_dollars: float) -> float:
    """Convert stop-risk dollars to position notional without unit mixing."""
    values = (stop_risk_dollars, entry_price, stop_distance_dollars)
    if any(not isinstance(value, (int, float)) or not math.isfinite(float(value)) for value in values):
        raise ValueError("stop risk, entry price, and stop distance must be finite")
    if stop_risk_dollars < 0 or entry_price <= 0 or stop_distance_dollars <= 0:
        raise ValueError("stop risk must be non-negative and price/distance must be positive")
    result = float(stop_risk_dollars) * float(entry_price) / float(stop_distance_dollars)
    if not math.isfinite(result) or result < 0:
        raise ValueError("converted notional is unsafe")
    return result


def _positive_or_none(value: Any, label: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{label} must be numeric")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be numeric") from exc
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{label} must be finite and positive")
    return result


def _required_positive(value: Any, label: str) -> float:
    result = _positive_or_none(value, label)
    if result is None:
        raise ValueError(f"{label} is required")
    return result


def validate_stop_evidence(
    *,
    entry_price: Any,
    stop_price: Any,
    stop_distance_dollars: Any,
    atr_value: Any = None,
    technical_stop_price: Any = None,
    stop_model_used: Any = None,
    stop_validation_status: Any = None,
) -> dict[str, Any]:
    """Validate a stop without inventing a percentage fallback.

    A stop is executable only when it is positive, below a long entry, and is
    supported by at least one finite ATR or technical level.  The result is a
    structured audit value rather than a truthy/falsey shortcut so callers can
    persist exactly why an entry was blocked.
    """

    def finite(value: Any) -> float | None:
        try:
            if value is None or isinstance(value, bool):
                return None
            number = float(value)
            return number if math.isfinite(number) else None
        except (TypeError, ValueError):
            return None

    price = finite(entry_price)
    stop = finite(stop_price)
    distance = finite(stop_distance_dollars)
    atr = finite(atr_value)
    technical = finite(technical_stop_price)
    atr_valid = atr is not None and atr > 0
    technical_valid = technical is not None and price is not None and 0 < technical < price
    geometry_valid = (
        price is not None
        and price > 0
        and stop is not None
        and 0 < stop < price
        and distance is not None
        and distance > 0
        and math.isclose(price - stop, distance, rel_tol=1e-7, abs_tol=1e-9)
    )
    model = str(stop_model_used or "")
    model_reject = not model or "fallback" in model.lower() or "fixed" in model.lower() or model == "default"
    status = str(stop_validation_status or "")
    valid = geometry_valid and (atr_valid or technical_valid) and not model_reject and status in {"validated", ""}
    reasons: list[str] = []
    if not geometry_valid:
        reasons.append("stop geometry is missing or invalid")
    if not atr_valid and not technical_valid:
        reasons.append("validated ATR or technical stop evidence is missing")
    if model_reject:
        reasons.append("percentage/fallback stop is not executable")
    if status not in {"validated", ""}:
        reasons.append(f"stop validation status is {status}")
    return {
        "valid": valid,
        "entry_price": price,
        "stop_price": stop,
        "stop_distance_dollars": distance,
        "atr_value": atr,
        "technical_stop_price": technical,
        "atr_valid": atr_valid,
        "technical_valid": technical_valid,
        "stop_policy_version": STOP_POLICY_VERSION,
        "reason": "; ".join(reasons) if reasons else "validated ATR or technical stop",
    }


def effective_notional_policy(config: Mapping[str, Any], equity: float, *, is_add: bool = False) -> NotionalPolicy:
    """Return the single effective notional policy.

    Only canonical keys are accepted. No value is ever raised to satisfy the
    executable minimum; callers must block if their constrained result is below
    it. ``absolute_max_notional_usd`` is optional because stage, equity, cash,
    buying-power, exposure, and risk ceilings may already be sufficient.
    """

    if not math.isfinite(float(equity)) or float(equity) <= 0:
        raise ValueError("positive authoritative equity is required")
    sizing = config.get("position_sizing", {}) or {}
    risk = config.get("risk", {}) or {}
    minimum = sizing.get("minimum_executable_notional_usd")
    default = sizing.get("default_add_notional_usd" if is_add else "default_paper_notional_usd")
    if is_add:
        default = sizing.get("default_add_notional_usd")
    minimum_value = _required_positive(minimum, "position_sizing.minimum_executable_notional_usd")
    default_value = _required_positive(default, "position_sizing.default_add_notional_usd" if is_add else "position_sizing.default_paper_notional_usd")

    stage = str(sizing.get("stage"))
    if not stage:
        raise ValueError("position_sizing.stage is required")
    stage_map_key = "stage_max_add_notional_usd" if is_add else "stage_max_initial_notional_usd"
    stage_map = sizing.get(stage_map_key)
    if not isinstance(stage_map, Mapping):
        raise ValueError(f"position_sizing.{stage_map_key} must be a mapping")
    stage_value = None if sizing.get("use_stage_dollar_cap") is False else stage_map.get(stage)
    stage_max = _positive_or_none(stage_value, f"position_sizing.{stage_map_key}.{stage}")

    pct = sizing.get("max_trade_notional_pct_equity")
    equity_max = None
    if pct is not None:
        pct_value = _positive_or_none(pct, "position_sizing.max_trade_notional_pct_equity")
        equity_max = float(equity) * pct_value / 100.0

    absolute = sizing.get("absolute_max_notional_usd")
    absolute_max = _positive_or_none(absolute, "position_sizing.absolute_max_notional_usd")

    policy = NotionalPolicy(minimum_value, default_value, stage_max, equity_max, absolute_max)
    for label, ceiling in policy.named_ceilings().items():
        if ceiling < minimum_value - 1e-9:
            raise ValueError(f"{label} maximum is below the executable minimum")
    return policy
