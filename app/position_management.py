from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pandas as pd


PRIORITY = {
    "EMERGENCY_EXIT": 1,
    "NORMAL_RISK_EXIT": 2,
    "PROFIT_PROTECT_EXIT": 3,
    "TAKE_PROFIT_PARTIAL": 4,
    "TRAILING_STOP_EXIT": 5,
    "TIME_STOP_EXIT": 6,
    "HEALTHY_PULLBACK_ADD": 7,
    "HOLD": 8,
}


@dataclass(frozen=True)
class PositionManagementDecision:
    symbol: str
    decision_type: str
    priority: int
    action: str
    reason: str
    current_price: float
    avg_entry_price: float
    quantity: float
    unrealized_profit_pct: float
    highest_price_since_entry: float | None
    max_unrealized_profit_pct: float | None
    pullback_from_peak_pct: float | None
    drawdown_from_entry_pct: float | None
    drawdown_from_peak_pct: float | None
    profit_giveback_ratio: float | None
    current_r_multiple: float | None
    trailing_stop_price: float | None
    suggested_sell_fraction: float | None
    suggested_add_notional: float | None
    blocking_reasons: list[str] = field(default_factory=list)
    is_actionable: bool = False
    atr_value: float | None = None
    atr_pct: float | None = None
    dip_trap_classification: str = "not_applicable"
    take_profit_level: int | None = None
    position_age_days: float | None = None
    position_age_cycles: int | None = None
    exit_review_needed: bool = False


def _cfg(config: dict[str, Any]) -> dict[str, Any]:
    return config.get("position_management", {}) or {}


def _nested(config: dict[str, Any], name: str) -> dict[str, Any]:
    return _cfg(config).get(name, {}) or {}


def _atr14(bars: pd.DataFrame) -> float | None:
    if bars is None or bars.empty or not {"high", "low", "close"}.issubset(bars.columns):
        return None
    high = bars["high"].astype(float)
    low = bars["low"].astype(float)
    close = bars["close"].astype(float)
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    atr = tr.rolling(14).mean().iloc[-1]
    return None if pd.isna(atr) else float(atr)


def _latest_ma(bars: pd.DataFrame, window: int) -> float | None:
    if bars is None or bars.empty or "close" not in bars.columns or len(bars) < window:
        return None
    value = bars["close"].astype(float).rolling(window).mean().iloc[-1]
    return None if pd.isna(value) else float(value)


def _fallback_unrealized_threshold(current_r: float | None, unrealized_pct: float, r_threshold: float, pct_threshold: float) -> bool:
    if current_r is not None:
        return current_r >= r_threshold
    return unrealized_pct >= pct_threshold


class PositionManagementEngine:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config

    def classify(
        self,
        *,
        symbol: str,
        current_price: float,
        avg_entry_price: float,
        quantity: float,
        bars: pd.DataFrame,
        previous_state: dict[str, Any] | None = None,
        initial_stop_price: float | None = None,
        trade_score: float = 0.0,
        score_improvement: float = 0.0,
        emergency_exit_score: float | None = None,
        normal_exit_signal: bool = False,
        volatility_regime: str = "normal",
        has_open_order: bool = False,
        position_age_days: float | None = None,
        position_age_cycles: int | None = None,
        now: datetime | None = None,
    ) -> PositionManagementDecision:
        now = now or datetime.now(UTC)
        config = _cfg(self.config)
        if not config.get("enabled", True):
            return self._decision(symbol, "HOLD", "hold", "position management disabled", current_price, avg_entry_price, quantity)

        if current_price <= 0 or avg_entry_price <= 0 or quantity <= 0:
            return self._decision(symbol, "HOLD", "hold", "invalid position inputs", current_price, avg_entry_price, quantity, ["invalid position inputs"])

        unrealized_pct = (current_price - avg_entry_price) / avg_entry_price * 100.0
        previous_high = None
        if previous_state:
            previous_high = previous_state.get("highest_price_since_entry")
        try:
            previous_high = float(previous_high) if previous_high is not None else None
        except (TypeError, ValueError):
            previous_high = None
        highest = max(previous_high or current_price, current_price)
        max_profit_pct = (highest - avg_entry_price) / avg_entry_price * 100.0
        pullback_pct = (highest - current_price) / highest * 100.0 if highest > 0 else None
        drawdown_from_entry_pct = min(0.0, unrealized_pct)
        drawdown_from_peak_pct = ((current_price - highest) / highest * 100.0) if highest > 0 else None
        giveback = None
        if max_profit_pct > 0:
            giveback = max(0.0, (max_profit_pct - unrealized_pct) / max_profit_pct)

        current_r = None
        if initial_stop_price is not None and initial_stop_price < avg_entry_price:
            risk_per_share = avg_entry_price - initial_stop_price
            if risk_per_share > 0:
                current_r = (current_price - avg_entry_price) / risk_per_share

        atr = _atr14(bars)
        atr_pct = (atr / current_price * 100.0) if atr and current_price > 0 else None
        ma50 = _latest_ma(bars, 50)
        ma200 = _latest_ma(bars, 200)

        if emergency_exit_score is not None and emergency_exit_score >= 85:
            return self._decision(symbol, "EMERGENCY_EXIT", "sell", "emergency exit risk overrides profit logic", current_price, avg_entry_price, quantity, is_actionable=True, unrealized_pct=unrealized_pct, highest=highest, max_profit_pct=max_profit_pct, pullback_pct=pullback_pct, drawdown_from_entry_pct=drawdown_from_entry_pct, drawdown_from_peak_pct=drawdown_from_peak_pct, giveback=giveback, current_r=current_r, atr=atr, atr_pct=atr_pct, position_age_days=position_age_days, position_age_cycles=position_age_cycles, exit_review_needed=True)
        if normal_exit_signal:
            return self._decision(symbol, "NORMAL_RISK_EXIT", "sell", "normal risk exit signal overrides profit logic", current_price, avg_entry_price, quantity, is_actionable=True, unrealized_pct=unrealized_pct, highest=highest, max_profit_pct=max_profit_pct, pullback_pct=pullback_pct, drawdown_from_entry_pct=drawdown_from_entry_pct, drawdown_from_peak_pct=drawdown_from_peak_pct, giveback=giveback, current_r=current_r, atr=atr, atr_pct=atr_pct, position_age_days=position_age_days, position_age_cycles=position_age_cycles, exit_review_needed=True)

        trailing_stop = self._trailing_stop(previous_state, highest, current_price, atr)

        profit_protect = self._profit_protect_decision(symbol, current_price, avg_entry_price, quantity, unrealized_pct, highest, max_profit_pct, pullback_pct, giveback, current_r, trailing_stop, atr, atr_pct)
        if profit_protect:
            return profit_protect

        take_profit = self._take_profit_decision(symbol, current_price, avg_entry_price, quantity, unrealized_pct, highest, max_profit_pct, pullback_pct, giveback, current_r, atr, atr_pct)
        if take_profit:
            return take_profit

        trailing = self._trailing_stop_decision(symbol, current_price, avg_entry_price, quantity, unrealized_pct, highest, max_profit_pct, pullback_pct, giveback, current_r, trailing_stop, atr, atr_pct)
        if trailing:
            return trailing

        time_stop = self._time_stop_decision(
            symbol, current_price, avg_entry_price, quantity, unrealized_pct, highest, max_profit_pct,
            pullback_pct, drawdown_from_entry_pct, drawdown_from_peak_pct, giveback, current_r, trailing_stop,
            atr, atr_pct, ma50, ma200, trade_score, score_improvement, position_age_days, position_age_cycles,
        )
        if time_stop:
            return time_stop

        pullback = self._healthy_pullback_decision(
            symbol, current_price, avg_entry_price, quantity, unrealized_pct, highest,
            max_profit_pct, pullback_pct, giveback, current_r, trailing_stop, atr, atr_pct,
            ma50, ma200, trade_score, score_improvement, emergency_exit_score, volatility_regime, has_open_order,
        )
        if pullback:
            return pullback

        return self._decision(
            symbol, "HOLD", "hold", "no position-management action qualified", current_price, avg_entry_price, quantity,
            unrealized_pct=unrealized_pct, highest=highest, max_profit_pct=max_profit_pct, pullback_pct=pullback_pct,
            drawdown_from_entry_pct=drawdown_from_entry_pct, drawdown_from_peak_pct=drawdown_from_peak_pct,
            giveback=giveback, current_r=current_r, trailing_stop=trailing_stop, atr=atr, atr_pct=atr_pct,
            dip_trap_classification=self._classify_dip_trap(unrealized_pct, current_price, avg_entry_price, ma50, ma200, giveback, trailing_stop, emergency_exit_score, volatility_regime),
            position_age_days=position_age_days, position_age_cycles=position_age_cycles,
        )

    def _take_profit_decision(self, symbol: str, current_price: float, avg_entry_price: float, quantity: float, unrealized_pct: float, highest: float, max_profit_pct: float, pullback_pct: float | None, giveback: float | None, current_r: float | None, atr: float | None, atr_pct: float | None) -> PositionManagementDecision | None:
        cfg = _nested(self.config, "profit_taking")
        if not cfg.get("enabled", True):
            return None
        state = {}
        levels = [
            (3, float(cfg.get("level_3_r", 3.0)), float(cfg.get("fallback_level_3_profit_pct", 5.0)), float(cfg.get("level_3_sell_fraction", cfg.get("fallback_level_3_sell_fraction", 0.50)))),
            (2, float(cfg.get("level_2_r", 2.0)), float(cfg.get("fallback_level_2_profit_pct", 3.0)), float(cfg.get("level_2_sell_fraction", cfg.get("fallback_level_2_sell_fraction", 0.33)))),
            (1, float(cfg.get("level_1_r", 1.5)), float(cfg.get("fallback_level_1_profit_pct", 2.0)), float(cfg.get("level_1_sell_fraction", cfg.get("fallback_level_1_sell_fraction", 0.25)))),
        ]
        # State is attached by the caller in previous_state; use the local variable name for clarity in tests.
        del state
        return self._take_profit_from_levels(symbol, current_price, avg_entry_price, quantity, unrealized_pct, highest, max_profit_pct, pullback_pct, giveback, current_r, atr, atr_pct, levels)

    def _take_profit_from_levels(self, symbol: str, current_price: float, avg_entry_price: float, quantity: float, unrealized_pct: float, highest: float, max_profit_pct: float, pullback_pct: float | None, giveback: float | None, current_r: float | None, atr: float | None, atr_pct: float | None, levels: list[tuple[int, float, float, float]]) -> PositionManagementDecision | None:
        cfg = _nested(self.config, "profit_taking")
        # Prefer lower level first so the system scales out sequentially instead of skipping directly to level 3.
        for level, r_threshold, pct_threshold, fraction in sorted(levels, key=lambda item: item[0]):
            key = f"take_profit_level_{level}_hit"
            if bool(getattr(self, "_previous_state", {}).get(key, 0)):
                continue
            if _fallback_unrealized_threshold(current_r, unrealized_pct, r_threshold, pct_threshold):
                notional = quantity * current_price * fraction
                min_notional = float(cfg.get("minimum_notional_to_sell", 1.0))
                if notional < min_notional:
                    return self._decision(symbol, "TAKE_PROFIT_PARTIAL", "hold", "not actionable - below minimum sell notional", current_price, avg_entry_price, quantity, ["below minimum sell notional"], unrealized_pct=unrealized_pct, highest=highest, max_profit_pct=max_profit_pct, pullback_pct=pullback_pct, giveback=giveback, current_r=current_r, suggested_sell_fraction=fraction, atr=atr, atr_pct=atr_pct, take_profit_level=level, exit_review_needed=True)
                return self._decision(symbol, "TAKE_PROFIT_PARTIAL", "sell", f"level {level} profit target reached; sell a partial position to lock gains", current_price, avg_entry_price, quantity, is_actionable=True, unrealized_pct=unrealized_pct, highest=highest, max_profit_pct=max_profit_pct, pullback_pct=pullback_pct, giveback=giveback, current_r=current_r, suggested_sell_fraction=fraction, atr=atr, atr_pct=atr_pct, take_profit_level=level, exit_review_needed=True)
        return None

    def _profit_protect_decision(self, symbol: str, current_price: float, avg_entry_price: float, quantity: float, unrealized_pct: float, highest: float, max_profit_pct: float, pullback_pct: float | None, giveback: float | None, current_r: float | None, trailing_stop: float | None, atr: float | None, atr_pct: float | None) -> PositionManagementDecision | None:
        cfg = _nested(self.config, "profit_protection")
        if not cfg.get("enabled", True) or unrealized_pct <= 0:
            return None
        active = bool(getattr(self, "_previous_state", {}).get("profit_protection_active", 0))
        active = active or _fallback_unrealized_threshold(current_r, unrealized_pct, float(cfg.get("activate_at_r", 1.0)), float(cfg.get("fallback_activate_at_profit_pct", 2.0)))
        if not active or max_profit_pct < float(cfg.get("min_peak_profit_pct", 2.0)) or giveback is None:
            return None
        exit_ratio = float(cfg.get("giveback_exit_ratio", 0.55))
        warn_ratio = float(cfg.get("giveback_warning_ratio", 0.40))
        if giveback >= exit_ratio:
            fraction = float(cfg.get("exit_sell_fraction", 0.50))
            return self._decision(symbol, "PROFIT_PROTECT_EXIT", "sell", f"profitable trade gave back {giveback:.1%} of peak profit", current_price, avg_entry_price, quantity, is_actionable=True, unrealized_pct=unrealized_pct, highest=highest, max_profit_pct=max_profit_pct, pullback_pct=pullback_pct, giveback=giveback, current_r=current_r, trailing_stop=trailing_stop, suggested_sell_fraction=fraction, atr=atr, atr_pct=atr_pct, exit_review_needed=True)
        if giveback >= warn_ratio:
            fraction = float(cfg.get("protect_sell_fraction", 0.25))
            return self._decision(symbol, "PROFIT_PROTECT_EXIT", "sell", f"profit protection warning: trade gave back {giveback:.1%} of peak profit", current_price, avg_entry_price, quantity, is_actionable=True, unrealized_pct=unrealized_pct, highest=highest, max_profit_pct=max_profit_pct, pullback_pct=pullback_pct, giveback=giveback, current_r=current_r, trailing_stop=trailing_stop, suggested_sell_fraction=fraction, atr=atr, atr_pct=atr_pct, exit_review_needed=True)
        return None

    def _trailing_stop(self, previous_state: dict[str, Any] | None, highest: float, current_price: float, atr: float | None) -> float | None:
        cfg = _nested(self.config, "trailing_stop")
        if not cfg.get("enabled", True):
            return None
        previous = None
        if previous_state:
            previous = previous_state.get("trailing_stop_price")
        try:
            previous = float(previous) if previous is not None else None
        except (TypeError, ValueError):
            previous = None
        if atr and atr > 0:
            calculated = highest - float(cfg.get("atr_multiplier", 2.0)) * atr
        else:
            calculated = highest * (1 - float(cfg.get("fallback_trailing_pct", 1.5)) / 100.0)
        stop = max(previous or calculated, calculated)
        return min(stop, highest, current_price if highest <= current_price else stop)

    def _trailing_stop_decision(self, symbol: str, current_price: float, avg_entry_price: float, quantity: float, unrealized_pct: float, highest: float, max_profit_pct: float, pullback_pct: float | None, giveback: float | None, current_r: float | None, trailing_stop: float | None, atr: float | None, atr_pct: float | None) -> PositionManagementDecision | None:
        cfg = _nested(self.config, "trailing_stop")
        if not cfg.get("enabled", True) or trailing_stop is None:
            return None
        start_gain = float(cfg.get("trailing_stop_start_gain_pct", cfg.get("fallback_activate_at_profit_pct", 2.0)))
        active = _fallback_unrealized_threshold(current_r, max_profit_pct, float(cfg.get("activate_at_r", 1.0)), start_gain)
        if active and current_price < trailing_stop:
            return self._decision(symbol, "TRAILING_STOP_EXIT", "sell", "trailing stop was breached after the trade moved in our favor", current_price, avg_entry_price, quantity, is_actionable=True, unrealized_pct=unrealized_pct, highest=highest, max_profit_pct=max_profit_pct, pullback_pct=pullback_pct, drawdown_from_entry_pct=min(0.0, unrealized_pct), drawdown_from_peak_pct=((current_price - highest) / highest * 100.0) if highest > 0 else None, giveback=giveback, current_r=current_r, trailing_stop=trailing_stop, suggested_sell_fraction=float(cfg.get("sell_fraction", 0.50)), atr=atr, atr_pct=atr_pct, exit_review_needed=True)
        return None

    def _time_stop_decision(self, symbol: str, current_price: float, avg_entry_price: float, quantity: float, unrealized_pct: float, highest: float, max_profit_pct: float, pullback_pct: float | None, drawdown_from_entry_pct: float | None, drawdown_from_peak_pct: float | None, giveback: float | None, current_r: float | None, trailing_stop: float | None, atr: float | None, atr_pct: float | None, ma50: float | None, ma200: float | None, trade_score: float, score_improvement: float, position_age_days: float | None, position_age_cycles: int | None) -> PositionManagementDecision | None:
        cfg = _nested(self.config, "time_stop")
        if not cfg.get("enabled", False):
            return None
        min_cycles = int(cfg.get("min_hold_cycles_before_time_stop", 12))
        min_days = float(cfg.get("min_hold_days_before_time_stop", 3.0))
        cycles_ok = position_age_cycles is not None and position_age_cycles >= min_cycles
        days_ok = position_age_days is not None and position_age_days >= min_days
        if not cycles_ok and not days_ok:
            return None

        max_gain = float(cfg.get("max_unrealized_gain_pct", 0.5))
        weak_score = float(cfg.get("weak_trade_score_below", 60.0))
        deterioration = float(cfg.get("deteriorating_score_delta_below", -5.0))
        weak_trend = (ma50 is not None and current_price < ma50) or (ma200 is not None and current_price < ma200)
        no_progress = unrealized_pct <= max_gain and max_profit_pct <= max(max_gain, float(cfg.get("max_peak_gain_pct", 1.0)))
        deteriorating = score_improvement <= deterioration
        weak_score_hit = trade_score < weak_score
        if not any((no_progress, weak_trend, deteriorating, weak_score_hit)):
            return None

        blockers = []
        if no_progress:
            blockers.append("no meaningful gain after hold period")
        if weak_trend:
            blockers.append("trend weakened")
        if deteriorating:
            blockers.append("score deteriorated")
        if weak_score_hit:
            blockers.append("trade score is weak")
        sell_fraction = float(cfg.get("sell_fraction", 1.0))
        return self._decision(
            symbol, "TIME_STOP_EXIT", "sell", "time stop review: " + "; ".join(blockers),
            current_price, avg_entry_price, quantity, blockers, is_actionable=bool(cfg.get("proposal_enabled", True)),
            unrealized_pct=unrealized_pct, highest=highest, max_profit_pct=max_profit_pct,
            pullback_pct=pullback_pct, drawdown_from_entry_pct=drawdown_from_entry_pct,
            drawdown_from_peak_pct=drawdown_from_peak_pct, giveback=giveback, current_r=current_r,
            trailing_stop=trailing_stop, suggested_sell_fraction=sell_fraction, atr=atr, atr_pct=atr_pct,
            position_age_days=position_age_days, position_age_cycles=position_age_cycles, exit_review_needed=True,
        )

    def _healthy_pullback_decision(self, symbol: str, current_price: float, avg_entry_price: float, quantity: float, unrealized_pct: float, highest: float, max_profit_pct: float, pullback_pct: float | None, giveback: float | None, current_r: float | None, trailing_stop: float | None, atr: float | None, atr_pct: float | None, ma50: float | None, ma200: float | None, trade_score: float, score_improvement: float, emergency_exit_score: float | None, volatility_regime: str, has_open_order: bool) -> PositionManagementDecision | None:
        cfg = _nested(self.config, "healthy_pullback_add")
        if not cfg.get("enabled", True):
            return None
        blockers: list[str] = []
        if unrealized_pct < float(cfg.get("minimum_unrealized_profit_pct", 0.5)):
            blockers.append("position is not sufficiently profitable")
        if cfg.get("require_price_above_avg_entry", True) and current_price <= avg_entry_price:
            blockers.append("price is not above average entry")
        if cfg.get("require_price_above_ma50", True) and ma50 is not None and current_price <= ma50:
            blockers.append("price is below MA50")
        if cfg.get("require_price_above_ma200_if_available", True) and ma200 is not None and current_price <= ma200:
            blockers.append("price is below MA200")
        if trade_score < float(cfg.get("minimum_trade_score", 85)):
            blockers.append("trade score below healthy-pullback threshold")
        if score_improvement < float(cfg.get("minimum_score_improvement", 5)):
            blockers.append("setup did not materially strengthen")
        if emergency_exit_score is not None and emergency_exit_score >= float(cfg.get("max_emergency_exit_score", 40)):
            blockers.append("emergency exit risk too high")
        if giveback is not None and giveback >= float(cfg.get("max_profit_giveback_ratio", 0.35)):
            blockers.append("profit giveback too large")
        allowed_pullback = float(cfg.get("fallback_max_pullback_pct", 1.0))
        if atr_pct is not None:
            allowed_pullback = max(allowed_pullback, float(cfg.get("max_pullback_atr_multiple", 1.5)) * atr_pct)
        if pullback_pct is not None and pullback_pct > allowed_pullback:
            blockers.append("pullback is too deep")
        if volatility_regime in {"high", "extreme"}:
            blockers.append("volatility regime too risky")
        if trailing_stop is not None and current_price < trailing_stop:
            blockers.append("trailing stop breached")
        if has_open_order:
            blockers.append("open order already exists for symbol")

        classification = self._classify_dip_trap(unrealized_pct, current_price, avg_entry_price, ma50, ma200, giveback, trailing_stop, emergency_exit_score, volatility_regime)
        if blockers:
            return self._decision(symbol, "HOLD", "hold", "not a healthy pullback add: " + "; ".join(blockers), current_price, avg_entry_price, quantity, blockers, unrealized_pct=unrealized_pct, highest=highest, max_profit_pct=max_profit_pct, pullback_pct=pullback_pct, giveback=giveback, current_r=current_r, trailing_stop=trailing_stop, atr=atr, atr_pct=atr_pct, dip_trap_classification=classification)
        add_notional = float(cfg.get("suggested_add_notional", self.config.get("position_sizing", {}).get("max_add_paper_notional", 10.0)))
        return self._decision(symbol, "HEALTHY_PULLBACK_ADD", "buy", "healthy pullback inside a winning position; this is not averaging down", current_price, avg_entry_price, quantity, is_actionable=True, unrealized_pct=unrealized_pct, highest=highest, max_profit_pct=max_profit_pct, pullback_pct=pullback_pct, giveback=giveback, current_r=current_r, trailing_stop=trailing_stop, suggested_add_notional=add_notional, atr=atr, atr_pct=atr_pct, dip_trap_classification=classification)

    def _classify_dip_trap(self, unrealized_pct: float, current_price: float, avg_entry_price: float, ma50: float | None, ma200: float | None, giveback: float | None, trailing_stop: float | None, emergency_exit_score: float | None, volatility_regime: str) -> str:
        if emergency_exit_score is not None and emergency_exit_score >= 85:
            return "emergency_breakdown"
        if current_price <= avg_entry_price or unrealized_pct < 0:
            return "trap_losing_position"
        if trailing_stop is not None and current_price < trailing_stop:
            return "trap_trailing_stop_breached"
        if ma50 is not None and current_price < ma50:
            return "trap_trend_break"
        if ma200 is not None and current_price < ma200:
            return "trap_major_trend_break"
        if giveback is not None and giveback >= 0.35:
            return "profit_fade"
        if volatility_regime in {"high", "extreme"}:
            return "trap_volatility"
        return "healthy_pullback"

    def _decision(self, symbol: str, decision_type: str, action: str, reason: str, current_price: float, avg_entry_price: float, quantity: float, blocking_reasons: list[str] | None = None, *, is_actionable: bool = False, unrealized_pct: float | None = None, highest: float | None = None, max_profit_pct: float | None = None, pullback_pct: float | None = None, drawdown_from_entry_pct: float | None = None, drawdown_from_peak_pct: float | None = None, giveback: float | None = None, current_r: float | None = None, trailing_stop: float | None = None, suggested_sell_fraction: float | None = None, suggested_add_notional: float | None = None, atr: float | None = None, atr_pct: float | None = None, dip_trap_classification: str = "not_applicable", take_profit_level: int | None = None, position_age_days: float | None = None, position_age_cycles: int | None = None, exit_review_needed: bool = False) -> PositionManagementDecision:
        actual_unrealized = unrealized_pct if unrealized_pct is not None else ((current_price - avg_entry_price) / avg_entry_price * 100.0 if avg_entry_price > 0 else 0.0)
        actual_highest = highest if highest is not None else current_price
        return PositionManagementDecision(
            symbol=symbol,
            decision_type=decision_type,
            priority=PRIORITY[decision_type],
            action=action,
            reason=reason,
            current_price=current_price,
            avg_entry_price=avg_entry_price,
            quantity=quantity,
            unrealized_profit_pct=actual_unrealized,
            highest_price_since_entry=actual_highest,
            max_unrealized_profit_pct=max_profit_pct,
            pullback_from_peak_pct=pullback_pct,
            drawdown_from_entry_pct=drawdown_from_entry_pct if drawdown_from_entry_pct is not None else min(0.0, actual_unrealized),
            drawdown_from_peak_pct=drawdown_from_peak_pct if drawdown_from_peak_pct is not None else (((current_price - actual_highest) / actual_highest * 100.0) if actual_highest and actual_highest > 0 else None),
            profit_giveback_ratio=giveback,
            current_r_multiple=current_r,
            trailing_stop_price=trailing_stop,
            suggested_sell_fraction=suggested_sell_fraction,
            suggested_add_notional=suggested_add_notional,
            blocking_reasons=blocking_reasons or [],
            is_actionable=is_actionable,
            atr_value=atr,
            atr_pct=atr_pct,
            dip_trap_classification=dip_trap_classification,
            take_profit_level=take_profit_level,
            position_age_days=position_age_days,
            position_age_cycles=position_age_cycles,
            exit_review_needed=exit_review_needed,
        )

    def with_previous_state(self, state: dict[str, Any] | None) -> "PositionManagementEngine":
        self._previous_state = state or {}
        return self
