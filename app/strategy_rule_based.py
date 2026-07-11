from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from .features import build_features

STRATEGY_VERSION = "rule_based_v2"
MIN_COMPLETED_DAILY_BARS = 200
MARKET_TIMEZONE = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class Signal:
    action: str
    side: str | None
    symbol: str
    reason: str
    confidence: float
    indicators: dict[str, Any]
    strategy_version: str = STRATEGY_VERSION


def completed_daily_bars(bars: pd.DataFrame, *, as_of: datetime | None = None) -> pd.DataFrame:
    """Normalize the same completed-bar point-in-time input for research/runtime."""
    if bars is None or bars.empty:
        return pd.DataFrame()
    required = {"close", "volume"}
    if not required.issubset(bars.columns):
        return pd.DataFrame()
    frame = bars.copy().sort_index()
    frame = frame.dropna(subset=["close", "volume"])
    moment = as_of or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    current_session = moment.astimezone(MARKET_TIMEZONE).date()
    try:
        session_dates = pd.DatetimeIndex(frame.index).tz_localize(None).date
        frame = frame.loc[[date != current_session for date in session_dates]]
    except (TypeError, ValueError):
        # Non-datetime indexes are still valid for historical point-in-time
        # fixtures; only a known current-session date is excluded.
        pass
    return frame


def evaluate_symbol(symbol: str, bars: pd.DataFrame, has_position: bool = False, has_open_order: bool = False, market_open: bool = True, maximum_volatility_20d: float = 0.50, stop_drawdown_pct: float = 0.08, position_drawdown_pct: float = 0.0) -> Signal:
    completed = completed_daily_bars(bars)
    if len(completed) < MIN_COMPLETED_DAILY_BARS:
        return Signal("HOLD", None, symbol, "insufficient completed daily history", 0.0, {})
    features = build_features(completed)
    row = features.iloc[-1]
    if pd.isna(row.get("ma_50")) or pd.isna(row.get("ma_200")):
        return Signal("HOLD", None, symbol, "missing MA50/MA200; fail-safe HOLD", 0.0, {})
    row = features.iloc[-1]
    indicators = {k: (None if pd.isna(v) else float(v)) for k, v in row.items()}
    above_50 = row["close"] > row["ma_50"]
    above_200 = row["close"] > row["ma_200"]
    
    vol_20 = indicators.get("volatility_20")
    
    # Volatility checks
    if vol_20 is None:
        volatility_ok = False
        vol_reason = "missing volatility data; fail-safe HOLD"
    elif vol_20 > maximum_volatility_20d:
        volatility_ok = False
        vol_reason = "extreme volatility; blocked"
    elif vol_20 > 0.35:
        volatility_ok = False
        vol_reason = "high volatility; watch only"
    elif vol_20 >= 0.25:
        volatility_ok = True
        vol_reason = "volatility elevated; reduced confidence"
    else:
        volatility_ok = True
        vol_reason = "volatility normal"

    if has_position and (not above_50 or position_drawdown_pct <= -abs(stop_drawdown_pct)):
        return Signal("EXIT", "sell", symbol, "close below 50-day MA or stop drawdown reached", 0.8, indicators)
        
    if not has_position and not has_open_order and market_open and above_50 and above_200 and volatility_ok:
        return Signal("ENTRY", "buy", symbol, f"trend filters passed and {vol_reason}", 0.7, indicators)

    reasons = []
    if not market_open: reasons.append("market closed")
    if has_position: reasons.append("position already exists")
    if has_open_order: reasons.append("open order already exists")
    if not above_50: reasons.append("below 50-day MA")
    if not above_200: reasons.append("below 200-day MA")
    reasons.append(vol_reason)
    
    return Signal("HOLD", None, symbol, "; ".join(reasons), 0.0, indicators)


class RuleBasedStrategy:
    version = STRATEGY_VERSION

    def evaluate(self, symbol: str, bars: pd.DataFrame, **kwargs: Any) -> Signal:
        return evaluate_symbol(symbol, bars, **kwargs)
