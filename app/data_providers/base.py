from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class DataProviderError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProviderResponse:
    provider: str
    endpoint: str
    status: str
    data: Any
    error: str | None = None
    cached: bool = False


class MarketResearchProvider(Protocol):
    name: str

    def health(self) -> ProviderResponse:
        ...

    def list_symbols(self, exchange: str = "US", limit: int | None = None) -> ProviderResponse:
        ...

    def search_symbols(self, query: str, limit: int | None = None) -> ProviderResponse:
        ...

    def get_historical_bars(self, symbol: str, period: str = "d", limit: int = 250) -> ProviderResponse:
        ...

    def get_intraday_bars(self, symbol: str, interval: str = "5m", limit: int = 100) -> ProviderResponse:
        ...

    def get_latest_quote(self, symbol: str) -> ProviderResponse:
        ...

    def get_news(self, symbol: str | None = None, topic: str | None = None, limit: int = 10) -> ProviderResponse:
        ...

    def get_fundamentals(self, symbol: str) -> ProviderResponse:
        ...

    def get_technical_indicators(self, symbol: str, function: str = "sma", period: int = 50) -> ProviderResponse:
        ...

    def get_screener_results(self, filters: dict[str, Any] | None = None, limit: int = 100) -> ProviderResponse:
        ...
