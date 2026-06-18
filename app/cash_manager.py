from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class CashRecommendation:
    realized_profit: float
    settled_cash: float
    unrealized_pnl: float
    current_equity: float
    high_water_mark: float
    profit_above_high_water: float
    suggested_withdrawal: float
    reserve: float
    reinvest: float
    reason: str
    recommendation_only: bool = True


def calculate_cash_recommendation(realized_profit: float, settled_cash: float, unrealized_pnl: float, current_equity: float, high_water_mark: float, config: dict[str, Any]) -> CashRecommendation:
    positive_settled_profit = max(0.0, min(realized_profit, settled_cash))
    lock = positive_settled_profit * float(config.get("profit_lock_rate", 0.30))
    reserve = positive_settled_profit * float(config.get("cash_reserve_rate", 0.20))
    reinvest = positive_settled_profit * float(config.get("reinvest_rate", 0.50))
    minimum = float(config.get("minimum_withdrawal", 10))
    if lock < minimum:
        reason = "suggested withdrawal is below the minimum threshold"
        lock = 0.0
    else:
        reason = "based only on realized, settled positive profit"
    return CashRecommendation(realized_profit, settled_cash, unrealized_pnl, current_equity, high_water_mark, max(0.0, current_equity - high_water_mark), round(lock, 2), round(reserve, 2), round(reinvest, 2), reason)


class CashManager:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config

    def recommend(self, **values: float) -> dict[str, Any]:
        return asdict(calculate_cash_recommendation(config=self.config, **values))

    def withdraw(self, *_: Any, **__: Any) -> None:
        raise PermissionError("API withdrawals are disabled; cash management is recommendation-only")
