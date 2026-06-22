from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class BrokerInterface(ABC):
    @abstractmethod
    def get_account(self) -> Any: ...

    @abstractmethod
    def get_positions(self) -> list[Any]: ...

    @abstractmethod
    def get_open_orders(self) -> list[Any]: ...

    @abstractmethod
    def get_latest_price(self, symbol: str) -> Any: ...

    @abstractmethod
    def get_historical_bars(self, symbol: str, timeframe: str, limit: int) -> Any: ...

    @abstractmethod
    def submit_order(self, symbol: str, side: str, notional_or_qty: dict[str, float], order_type: str, limit_price: float | None = None, client_order_id: str | None = None) -> Any: ...

    @abstractmethod
    def cancel_order(self, order_id: str) -> Any: ...

    @abstractmethod
    def get_order(self, order_id: str) -> Any: ...

    @abstractmethod
    def get_order_by_client_order_id(self, client_order_id: str) -> Any: ...

    @abstractmethod
    def get_clock(self) -> Any: ...

    @abstractmethod
    def get_loss_metrics(self) -> dict[str, float | None]: ...

    @abstractmethod
    def is_market_open(self) -> bool: ...
