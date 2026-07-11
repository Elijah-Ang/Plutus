"""Validated notional policy shared by sizing and final risk validation."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class NotionalPolicy:
    minimum_executable_notional_usd: float
    default_notional_usd: float
    stage_max_notional_usd: float | None
    equity_max_notional_usd: float | None
    absolute_max_notional_usd: float | None

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


def effective_notional_policy(config: Mapping[str, Any], equity: float, *, is_add: bool = False) -> NotionalPolicy:
    """Return the single effective notional policy.

    Legacy names are read only for test/snapshot compatibility. The production
    configuration uses explicit USD names. No value is ever raised to satisfy
    the executable minimum; callers must block if their constrained result is
    below it.
    """

    if not math.isfinite(float(equity)) or float(equity) <= 0:
        raise ValueError("positive authoritative equity is required")
    sizing = config.get("position_sizing", {}) or {}
    risk = config.get("risk", {}) or {}
    minimum = sizing.get("minimum_executable_notional_usd", sizing.get("min_paper_notional", 5.0))
    default = sizing.get("default_paper_notional_usd", sizing.get("base_paper_notional", 10.0))
    if is_add:
        default = sizing.get("default_add_notional_usd", sizing.get("suggested_add_notional", default))
    minimum_value = _required_positive(minimum, "position_sizing.minimum_executable_notional_usd")
    default_value = _required_positive(default, "position_sizing.default_paper_notional_usd")

    stage = str(sizing.get("stage", "moderate_paper"))
    stage_map_key = "stage_max_add_notional_usd" if is_add else "stage_max_initial_notional_usd"
    legacy_stage_map_key = "stage_max_add_notional" if is_add else "stage_max_initial_notional"
    stage_map = sizing.get(stage_map_key, sizing.get(legacy_stage_map_key, {})) or {}
    stage_value = None if sizing.get("use_stage_dollar_cap", True) is False else stage_map.get(stage)
    stage_max = _positive_or_none(stage_value, f"position_sizing.{stage_map_key}.{stage}")

    pct = sizing.get("max_trade_notional_pct_equity", sizing.get("max_trade_notional_pct_of_equity"))
    equity_max = None
    if pct is not None:
        pct_value = _positive_or_none(pct, "position_sizing.max_trade_notional_pct_equity")
        equity_max = float(equity) * pct_value / 100.0

    absolute = sizing.get("absolute_max_notional_usd")
    if absolute is None:
        absolute = risk.get("max_trade_notional_paper")
    absolute_max = _positive_or_none(absolute, "position_sizing.absolute_max_notional_usd")

    policy = NotionalPolicy(minimum_value, default_value, stage_max, equity_max, absolute_max)
    for label, ceiling in zip(("stage maximum", "equity maximum", "absolute maximum"), policy.ceiling_values):
        if ceiling < minimum_value - 1e-9:
            raise ValueError(f"{label} is below the executable minimum")
    return policy
