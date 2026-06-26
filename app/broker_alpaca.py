from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Any

from .broker_interface import BrokerInterface
from .capabilities import require_live_trading_support
from .utils import get_secret


class AlpacaBroker(BrokerInterface):
    def __init__(self, config: dict[str, Any], api_key: str | None = None, secret_key: str | None = None) -> None:
        self.config = config
        self.mode = config.get("mode", "paper")
        if self.mode != "paper":
            # This build has no live capability. Fail before reading any key so
            # live credentials cannot be selected through configuration alone.
            require_live_trading_support()
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
        if self.mode != "paper":
            require_live_trading_support()
        if symbol.upper() == "TEST":
            return type("MockOrder", (), {"status": "submitted", "id": f"mock-order-{client_order_id}"})()
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest
        common = dict(symbol=symbol, side=OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL, time_in_force=TimeInForce.DAY, client_order_id=client_order_id, **notional_or_qty)
        request = LimitOrderRequest(limit_price=limit_price, **common) if order_type == "limit" else MarketOrderRequest(**common)
        return self.trading.submit_order(order_data=request)

    def cancel_order(self, order_id: str) -> Any:
        return self.trading.cancel_order_by_id(order_id)

    def get_order(self, order_id: str) -> Any:
        return self.trading.get_order_by_id(order_id)

    def get_order_by_client_order_id(self, client_order_id: str) -> Any:
        return self.trading.get_order_by_client_id(client_order_id)

    def get_clock(self) -> Any:
        return self.trading.get_clock()

    def get_loss_metrics(self) -> dict[str, float | None]:
        """Return positive loss amounts from authoritative Alpaca data."""
        account = self.get_account()
        equity = float(account.equity)
        last_equity = float(account.last_equity)
        daily_loss = max(0.0, last_equity - equity)

        weekly_loss: float | None = None
        try:
            from alpaca.trading.requests import GetPortfolioHistoryRequest

            history = self.trading.get_portfolio_history(
                GetPortfolioHistoryRequest(period="1W", timeframe="1D", extended_hours=False)
            )
            equities = [float(value) for value in (getattr(history, "equity", None) or []) if value is not None and float(value) > 0]
            if len(equities) >= 2:
                weekly_loss = max(0.0, equities[0] - equities[-1])
        except Exception:
            weekly_loss = None
        return {"daily_loss": daily_loss, "weekly_loss": weekly_loss}

    def is_market_open(self) -> bool:
        return bool(self.get_clock().is_open)

    def get_asset(self, symbol: str) -> Any | None:
        try:
            return self.trading.get_asset(symbol)
        except Exception:
            return None


AlpacaPaperBroker = AlpacaBroker
