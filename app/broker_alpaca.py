from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Any

from .broker_interface import BrokerInterface
from .utils import get_secret


class AlpacaBroker(BrokerInterface):
    def __init__(self, config: dict[str, Any], api_key: str | None = None, secret_key: str | None = None) -> None:
        self.config = config
        self.mode = config.get("mode", "paper")
        if self.mode != "paper":
            if config.get("live_enabled") is not True or config.get("explicit_live_confirmation") is not True:
                raise PermissionError("Live broker initialization requires both live_enabled and explicit confirmation")
        key = api_key or get_secret("ALPACA_API_KEY")
        secret = secret_key or get_secret("ALPACA_SECRET_KEY")
        if not key or not secret:
            raise RuntimeError("Alpaca credentials are not configured")
        try:
            from alpaca.data.historical import StockHistoricalDataClient
            from alpaca.trading.client import TradingClient
        except ImportError as exc:
            raise RuntimeError("Install alpaca-py before using AlpacaBroker") from exc
        self.trading = TradingClient(key, secret, paper=self.mode == "paper")
        self.data = StockHistoricalDataClient(key, secret)

    def get_account(self) -> Any:
        return self.trading.get_account()

    def get_positions(self) -> list[Any]:
        return list(self.trading.get_all_positions())

    def get_open_orders(self) -> list[Any]:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest
        return list(self.trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN)))

    def get_latest_price(self, symbol: str) -> Any:
        from alpaca.data.requests import StockLatestTradeRequest
        return self.data.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=symbol))[symbol]

    def get_historical_bars(self, symbol: str, timeframe: str = "1Day", limit: int = 250) -> Any:
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        tf = TimeFrame.Day if timeframe.lower() in {"1day", "day", "1d"} else TimeFrame.Hour
        request = StockBarsRequest(symbol_or_symbols=symbol, timeframe=tf, start=datetime.now().astimezone() - timedelta(days=max(limit * 2, 365)), limit=limit)
        return self.data.get_stock_bars(request).df

    def submit_order(self, symbol: str, side: str, notional_or_qty: dict[str, float], order_type: str = "market", limit_price: float | None = None, client_order_id: str | None = None) -> Any:
        if self.mode != "paper" and not (self.config.get("live_enabled") is True and self.config.get("explicit_live_confirmation") is True):
            raise PermissionError("Live order blocked by safety gates")
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest
        common = dict(symbol=symbol, side=OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL, time_in_force=TimeInForce.DAY, client_order_id=client_order_id, **notional_or_qty)
        request = LimitOrderRequest(limit_price=limit_price, **common) if order_type == "limit" else MarketOrderRequest(**common)
        return self.trading.submit_order(order_data=request)

    def cancel_order(self, order_id: str) -> Any:
        return self.trading.cancel_order_by_id(order_id)

    def get_order(self, order_id: str) -> Any:
        return self.trading.get_order_by_id(order_id)

    def is_market_open(self) -> bool:
        return bool(self.trading.get_clock().is_open)


AlpacaPaperBroker = AlpacaBroker
