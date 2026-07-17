from __future__ import annotations

import hashlib
import socket
import ssl
from datetime import UTC, datetime, timedelta
from typing import Any

from .broker_interface import BrokerInterface
from .capabilities import require_live_trading_support
from .runtime_guards import WallClockTimeout, wall_clock_timeout
from .utils import get_secret


class AlpacaBrokerError(RuntimeError):
    def __init__(self, category: str, operation: str, original: BaseException | None = None) -> None:
        self.category = category
        self.operation = operation
        self.original = original
        self.request_may_have_reached_broker = operation == "submit_order"
        super().__init__(f"{category}: Alpaca {operation} failed")


class AlpacaBroker(BrokerInterface):
    def __init__(self, config: dict[str, Any], api_key: str | None = None, secret_key: str | None = None) -> None:
        self.config = config
        self.mode = config.get("mode", "paper")
        self.paper_requested = self.mode == "paper"
        self.configured_trading_endpoint = str(
            config.get("alpaca", {}).get("paper_trading_endpoint", "https://paper-api.alpaca.markets")
            if self.paper_requested
            else config.get("alpaca", {}).get("live_trading_endpoint", "https://api.alpaca.markets")
        )
        if self.paper_requested and "paper" not in self.configured_trading_endpoint.lower():
            raise RuntimeError("paper mode requires an explicitly paper Alpaca trading endpoint")
        if self.mode != "paper":
            # This build has no live capability. Fail before reading any key so
            # live credentials cannot be selected through configuration alone.
            require_live_trading_support()
        key = api_key or get_secret("ALPACA_API_KEY")
        secret = secret_key or get_secret("ALPACA_SECRET_KEY")
        if not key or not secret:
            raise RuntimeError("Alpaca credentials are not configured")
        self.timeout_cfg = config.get("alpaca", {}).get("timeouts", {})
        try:
            from alpaca.data.historical import CryptoHistoricalDataClient, StockHistoricalDataClient
            from alpaca.trading.client import TradingClient
        except ImportError as exc:
            raise RuntimeError("Install alpaca-py before using AlpacaBroker") from exc
        # The public constructor argument and the configured endpoint are both
        # identity inputs. The SDK's private URL/sandbox fields are only
        # supplemental evidence in paper_account_identity().
        self.trading = TradingClient(key, secret, paper=self.paper_requested)
        self.data = StockHistoricalDataClient(key, secret)
        # Authentication is optional for Alpaca crypto data, but the official
        # SDK documents a higher rate limit when keys are supplied.  Use the
        # same paper credentials while keeping the data and equity clients
        # separate by asset class.
        self._crypto_data = CryptoHistoricalDataClient(key, secret)

    def _timeout_seconds(self, kind: str) -> float:
        defaults = {
            "read": 10.0,
            "market_data": 10.0,
            "reconcile": 10.0,
            "order_submission": 20.0,
            "order_lookup": 10.0,
        }
        return float(self.timeout_cfg.get(f"{kind}_seconds", defaults[kind]))

    def _classify_error(self, exc: BaseException) -> str:
        if isinstance(exc, WallClockTimeout):
            return "alpaca_timeout"
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, (socket.timeout, TimeoutError)) or isinstance(exc, (socket.timeout, TimeoutError)):
            return "alpaca_timeout"
        if isinstance(reason, socket.gaierror):
            return "alpaca_dns_error"
        if isinstance(reason, ssl.SSLError) or isinstance(exc, ssl.SSLError):
            return "alpaca_tls_error"
        status_code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
        message = str(exc).lower()
        if status_code == 429 or "rate limit" in message or "too many requests" in message:
            return "alpaca_rate_limit"
        if status_code in {401, 403} or "unauthorized" in message or "forbidden" in message:
            return "alpaca_auth_error"
        if exc.__class__.__name__.lower().endswith("apierror") or status_code is not None:
            return "alpaca_api_error"
        return "alpaca_unknown_error"

    def _call(self, operation: str, kind: str, func: Any) -> Any:
        try:
            with wall_clock_timeout(self._timeout_seconds(kind), f"alpaca_{operation}"):
                return func()
        except AlpacaBrokerError:
            raise
        except WallClockTimeout as exc:
            raise AlpacaBrokerError(self._classify_error(exc), operation, exc) from None
        except Exception as exc:
            raise AlpacaBrokerError(self._classify_error(exc), operation, exc) from None

    def get_account(self) -> Any:
        return self._call("get_account", "read", self.trading.get_account)

    def paper_account_identity(self) -> dict[str, Any]:
        account = self.get_account()
        account_id = getattr(account, "id", None) or getattr(account, "account_number", None)
        raw_status = getattr(account, "status", "")
        account_status = str(getattr(raw_status, "value", raw_status) or "").lower()
        currency = str(getattr(account, "currency", "USD") or "").upper()
        account_blocked = bool(getattr(account, "account_blocked", False))
        trading_blocked = bool(getattr(account, "trading_blocked", False))
        public_constructor_identity = self.paper_requested and self.mode == "paper"
        configured_paper_endpoint = "paper" in self.configured_trading_endpoint.lower()
        sdk_base_url = str(getattr(self.trading, "_base_url", ""))
        sdk_sandbox = getattr(self.trading, "_sandbox", None)
        sdk_endpoint_evidence = "paper" in sdk_base_url.lower() if sdk_base_url else None
        endpoint_consistent = sdk_endpoint_evidence in {None, True}
        return {
            "verified": bool(
                public_constructor_identity and configured_paper_endpoint and endpoint_consistent
                and account_id and account_status == "active"
                and not account_blocked and not trading_blocked and currency == "USD"
            ),
            "mode": self.mode,
            "endpoint_class": "paper" if configured_paper_endpoint and endpoint_consistent else "ambiguous",
            "account_status": account_status,
            "account_id_present": bool(account_id),
            "account_id_hash": hashlib.sha256(str(account_id).encode("utf-8")).hexdigest() if account_id else "",
            "account_currency": currency,
            "paper_constructor_requested": public_constructor_identity,
            "configured_endpoint_paper": configured_paper_endpoint,
            "sdk_sandbox_evidence": sdk_sandbox,
        }

    def get_positions(self) -> list[Any]:
        return list(self._call("get_positions", "read", self.trading.get_all_positions))

    def get_open_orders(self) -> list[Any]:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest
        return list(self._call("get_open_orders", "read", lambda: self.trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))))

    def get_latest_price(self, symbol: str) -> Any:
        from alpaca.data.requests import StockLatestTradeRequest
        return self._call("get_latest_price", "market_data", lambda: self.data.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=symbol))[symbol])

    def get_latest_quote(self, symbol: str) -> Any:
        from alpaca.data.requests import StockLatestQuoteRequest
        return self._call("get_latest_quote", "market_data", lambda: self.data.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=symbol))[symbol])

    def get_historical_bars(self, symbol: str, timeframe: str = "1Day", limit: int = 250) -> Any:
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        tf = TimeFrame.Day if timeframe.lower() in {"1day", "day", "1d"} else TimeFrame.Hour
        request = StockBarsRequest(symbol_or_symbols=symbol, timeframe=tf, start=datetime.now().astimezone() - timedelta(days=max(limit * 2, 365)), limit=limit)
        return self._call("get_historical_bars", "market_data", lambda: self.data.get_stock_bars(request).df)

    def _get_crypto_data_client(self) -> Any:
        if self._crypto_data is None:
            raise RuntimeError("authenticated alpaca-py crypto data client is unavailable")
        return self._crypto_data

    def get_crypto_assets(self) -> list[Any]:
        """Read active crypto pairs and their current broker precision fields."""

        from alpaca.trading.enums import AssetClass, AssetStatus
        from alpaca.trading.requests import GetAssetsRequest

        request = GetAssetsRequest(status=AssetStatus.ACTIVE, asset_class=AssetClass.CRYPTO)
        return list(self._call(
            "get_crypto_assets",
            "read",
            lambda: self.trading.get_all_assets(request),
        ))

    def get_crypto_historical_bars(self, symbol: str, timeframe: str = "1Hour", limit: int = 500) -> Any:
        from alpaca.data.enums import CryptoFeed
        from alpaca.data.requests import CryptoBarsRequest
        from alpaca.data.timeframe import TimeFrame

        tf = TimeFrame.Day if timeframe.lower() in {"1day", "day", "1d"} else TimeFrame.Hour
        lookback_days = max(30, int(limit / 24) + 5) if tf != TimeFrame.Day else max(limit + 5, 30)
        request = CryptoBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=tf,
            start=datetime.now(UTC) - timedelta(days=lookback_days),
            limit=limit,
        )
        return self._call(
            "get_crypto_historical_bars",
            "market_data",
            lambda: self._get_crypto_data_client().get_crypto_bars(request, feed=CryptoFeed.US).df,
        )

    def get_crypto_latest_quote(self, symbol: str) -> Any:
        from alpaca.data.enums import CryptoFeed
        from alpaca.data.requests import CryptoLatestQuoteRequest

        request = CryptoLatestQuoteRequest(symbol_or_symbols=symbol)
        return self._call(
            "get_crypto_latest_quote",
            "market_data",
            lambda: self._get_crypto_data_client().get_crypto_latest_quote(request, feed=CryptoFeed.US)[symbol],
        )

    def get_crypto_latest_trade(self, symbol: str) -> Any:
        from alpaca.data.enums import CryptoFeed
        from alpaca.data.requests import CryptoLatestTradeRequest

        request = CryptoLatestTradeRequest(symbol_or_symbols=symbol)
        return self._call(
            "get_crypto_latest_trade",
            "market_data",
            lambda: self._get_crypto_data_client().get_crypto_latest_trade(request, feed=CryptoFeed.US)[symbol],
        )

    def get_crypto_latest_orderbook(self, symbol: str) -> Any:
        from alpaca.data.enums import CryptoFeed
        from alpaca.data.requests import CryptoLatestOrderbookRequest

        request = CryptoLatestOrderbookRequest(symbol_or_symbols=symbol)
        return self._call(
            "get_crypto_latest_orderbook",
            "market_data",
            lambda: self._get_crypto_data_client().get_crypto_latest_orderbook(request, feed=CryptoFeed.US)[symbol],
        )

    def _looks_like_crypto_symbol(self, symbol: str) -> bool:
        raw = str(symbol or "").strip().upper()
        crypto_config = self.config.get("crypto") or {}
        configured_pairs = {
            str(value or "").strip().upper().replace("-", "/")
            for value in (
                list(crypto_config.get("symbols") or ("BTC/USD", "ETH/USD"))
                + list(crypto_config.get("optional_symbols") or ("SOL/USD",))
            )
        }
        configured_legacy = {value.replace("/", "") for value in configured_pairs}
        bases = {value.split("/", 1)[0] for value in configured_pairs if "/" in value}
        compact = raw.replace("/", "").replace("-", "")
        legacy_pair = any(
            compact.startswith(base) and compact[len(base):] in {"BTC", "USD", "USDC", "USDT"}
            for base in bases
        )
        return "/" in raw or "-" in raw or compact in configured_legacy or legacy_pair

    def submit_order(self, symbol: str, side: str, notional_or_qty: dict[str, float], order_type: str = "market", limit_price: float | None = None, client_order_id: str | None = None) -> Any:
        if self.mode != "paper":
            require_live_trading_support()
        if self._looks_like_crypto_symbol(symbol):
            from .broker_interface import BrokerSubmissionNotAttempted

            raise BrokerSubmissionNotAttempted(
                "crypto submission is disabled in the data/capability stage; "
                "the equity DAY-order adapter cannot be used for crypto"
            )
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest
        from .broker_interface import BrokerSubmissionNotAttempted
        try:
            common = dict(symbol=symbol, side=OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL, time_in_force=TimeInForce.DAY, client_order_id=client_order_id, **notional_or_qty)
            request = LimitOrderRequest(limit_price=limit_price, **common) if order_type == "limit" else MarketOrderRequest(**common)
        except Exception as exc:
            raise BrokerSubmissionNotAttempted("order request validation failed before broker I/O") from exc
        return self._call("submit_order", "order_submission", lambda: self.trading.submit_order(order_data=request))

    def cancel_order(self, order_id: str) -> Any:
        return self._call("cancel_order", "order_submission", lambda: self.trading.cancel_order_by_id(order_id))

    def get_order(self, order_id: str) -> Any:
        return self._call("get_order", "order_lookup", lambda: self.trading.get_order_by_id(order_id))

    def get_order_by_client_order_id(self, client_order_id: str) -> Any:
        return self._call("get_order_by_client_order_id", "order_lookup", lambda: self.trading.get_order_by_client_id(client_order_id))

    def get_clock(self) -> Any:
        return self._call("get_clock", "read", self.trading.get_clock)

    def get_loss_metrics(self) -> dict[str, float | str | None]:
        """Return explicit, versioned dollar loss metrics from Alpaca."""
        account = self.get_account()
        equity = float(account.equity)
        last_equity = float(account.last_equity)
        daily_loss = max(0.0, last_equity - equity)

        weekly_loss: float | None = None
        try:
            from alpaca.trading.requests import GetPortfolioHistoryRequest

            history = self._call(
                "get_portfolio_history",
                "read",
                lambda: self.trading.get_portfolio_history(
                    GetPortfolioHistoryRequest(period="1W", timeframe="1D", extended_hours=False)
                ),
            )
            equities = [float(value) for value in (getattr(history, "equity", None) or []) if value is not None and float(value) > 0]
            if len(equities) >= 2:
                weekly_loss = max(0.0, equities[0] - equities[-1])
        except Exception:
            weekly_loss = None
        return {
            "daily_loss_dollars": daily_loss,
            "weekly_loss_dollars": weekly_loss,
            "reference_equity": last_equity,
            "daily_loss_confidence": "verified",
            "weekly_loss_confidence": "verified" if weekly_loss is not None else "unavailable",
            "provenance": "alpaca_account_and_portfolio_history",
            "metrics_version": "loss_controls_v2",
        }

    def is_market_open(self) -> bool:
        return bool(self.get_clock().is_open)

    def get_asset(self, symbol: str) -> Any | None:
        try:
            return self._call("get_asset", "read", lambda: self.trading.get_asset(symbol))
        except Exception:
            return None


AlpacaPaperBroker = AlpacaBroker
