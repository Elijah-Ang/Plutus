from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from .features import build_features

STRATEGY_VERSION = "rule_based_v1"


@dataclass(frozen=True)
class Signal:
    action: str
    side: str | None
    symbol: str
    reason: str
    confidence: float
    indicators: dict[str, Any]
    strategy_version: str = STRATEGY_VERSION


def evaluate_symbol(symbol: str, bars: pd.DataFrame, has_position: bool = False, has_open_order: bool = False, market_open: bool = True, maximum_volatility_20d: float = 0.50, stop_drawdown_pct: float = 0.08, position_drawdown_pct: float = 0.0) -> Signal:
    features = build_features(bars)
    if len(features) < 50:
        return Signal("HOLD", None, symbol, "insufficient history", 0.0, {})
    row = features.iloc[-1]
    indicators = {k: (None if pd.isna(v) else float(v)) for k, v in row.items()}
    above_50 = row["close"] > row["ma_50"]
    above_200 = pd.isna(row["ma_200"]) or row["close"] > row["ma_200"]
    volatility_ok = pd.notna(row["volatility_20"]) and row["volatility_20"] <= maximum_volatility_20d
    if has_position and (not above_50 or position_drawdown_pct <= -abs(stop_drawdown_pct)):
        return Signal("EXIT", "sell", symbol, "close below 50-day MA or stop drawdown reached", 0.8, indicators)
    if not has_position and not has_open_order and market_open and above_50 and above_200 and volatility_ok:
        return Signal("ENTRY", "buy", symbol, "trend filters passed and volatility within limit", 0.7, indicators)
    reasons = []
    if not market_open: reasons.append("market closed")
    if has_position: reasons.append("position already exists")
    if has_open_order: reasons.append("open order already exists")
    if not above_50: reasons.append("below 50-day MA")
    if not above_200: reasons.append("below 200-day MA")
    if not volatility_ok: reasons.append("volatility unavailable or too high")
    return Signal("HOLD", None, symbol, "; ".join(reasons) or "no entry or exit", 0.0, indicators)


class RuleBasedStrategy:
    version = STRATEGY_VERSION

    def evaluate(self, symbol: str, bars: pd.DataFrame, **kwargs: Any) -> Signal:
        return evaluate_symbol(symbol, bars, **kwargs)
