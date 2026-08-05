from __future__ import annotations

import hashlib
import socket
import ssl
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from .broker_interface import BrokerInterface
from .runtime_guards import WallClockTimeout, wall_clock_timeout
from .utils import get_secret


class AlpacaBrokerError(RuntimeError):
    def __init__(self, category: str, operation: str, original: BaseException | None = None) -> None:
        self.category = category
        self.operation = operation
        self.original = original
        self.request_may_have_reached_broker = operation in {"submit_order", "submit_crypto_order"}
        super().__init__(f"{category}: Alpaca {operation} failed")


class AlpacaBroker(BrokerInterface):
    def __init__(self, config: dict[str, Any], api_key: str | None = None, secret_key: str | None = None) -> None:
        self.config = config
        self.mode = config.get("mode", "paper")
        self.paper_requested = self.mode == "paper"
        if self.mode != "paper":
            raise PermissionError("live trading is not supported by this paper-only broker adapter")
        self.configured_trading_endpoint = str(
            config.get("alpaca", {}).get("paper_trading_endpoint", "https://paper-api.alpaca.markets")
        )
        if "paper" not in self.configured_trading_endpoint.lower():
            raise RuntimeError("paper mode requires an explicitly paper Alpaca trading endpoint")
        self.equity_realtime_data_feed = str(
            (config.get("alpaca", {}) or {}).get("equity_realtime_data_feed") or ""
        ).strip().lower()
        if self.equity_realtime_data_feed not in {"iex", "sip"}:
            raise RuntimeError("Alpaca real-time equity data feed must be explicitly iex or sip")
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

    def submission_available(self) -> bool:
        """Return a local adapter proof without sending an order."""

        return bool(
            self.mode == "paper"
            and "paper" in self.configured_trading_endpoint.lower()
            and callable(getattr(self.trading, "submit_order", None))
        )

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
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockLatestTradeRequest
        feed = {"iex": DataFeed.IEX, "sip": DataFeed.SIP}[self.equity_realtime_data_feed]
        return self._call(
            "get_latest_price",
            "market_data",
            lambda: self.data.get_stock_latest_trade(
                StockLatestTradeRequest(symbol_or_symbols=symbol, feed=feed)
            )[symbol],
        )

    def get_latest_quote(self, symbol: str) -> Any:
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockLatestQuoteRequest
        feed = {"iex": DataFeed.IEX, "sip": DataFeed.SIP}[self.equity_realtime_data_feed]
        quote = self._call(
            "get_latest_quote",
            "market_data",
            lambda: self.data.get_stock_latest_quote(
                StockLatestQuoteRequest(symbol_or_symbols=symbol, feed=feed)
            )[symbol],
        )
        # Preserve the exact requested feed as trusted evidence. Alpaca's Quote
        # model contains exchange codes but does not retain the request feed.
        return {
            "bid_price": getattr(quote, "bid_price", None),
            "ask_price": getattr(quote, "ask_price", None),
            "bid_size": getattr(quote, "bid_size", None),
            "ask_size": getattr(quote, "ask_size", None),
            "bid_exchange": getattr(quote, "bid_exchange", None),
            "ask_exchange": getattr(quote, "ask_exchange", None),
            "timestamp": getattr(quote, "timestamp", None),
            "feed": self.equity_realtime_data_feed,
        }

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
        # Alpaca returns the first page in ascending time order. A broad
        # lookback with a smaller limit therefore returns old bars and can
        # make the strategy appear stale even while latest quotes are live.
        # Bound the request to the latest requested intervals and provide an
        # explicit end so the first page is the current page.
        interval = timedelta(days=1) if tf == TimeFrame.Day else timedelta(hours=1)
        bar_count = max(int(limit), 1)
        end = datetime.now(UTC)
        request = CryptoBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=tf,
            start=end - interval * bar_count,
            end=end,
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
            raise PermissionError("live trading is not supported by this paper-only broker adapter")
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

    def submit_crypto_order(
        self,
        symbol: str,
        side: str,
        notional_or_qty: dict[str, Any],
        order_type: str = "limit",
        limit_price: Any | None = None,
        client_order_id: str | None = None,
        time_in_force: str = "gtc",
    ) -> Any:
        """Submit a bounded spot-crypto paper order through Alpaca's crypto path.

        This method is intentionally separate from ``submit_order`` so a
        crypto pair can never fall through the equity DAY-order adapter.  The
        lane gate and caller must already have proved the current paper
        authority and paper identity; this adapter still rechecks the hard
        paper boundary before constructing an SDK request.
        """

        if self.mode != "paper":
            raise PermissionError("live trading is not supported by this paper-only broker adapter")
        raw = str(symbol or "").strip().upper().replace("-", "/")
        configured = {
            str(value or "").strip().upper().replace("-", "/")
            for value in ((self.config.get("crypto") or {}).get("symbols") or ("BTC/USD", "ETH/USD"))
        }
        if raw not in configured:
            from .broker_interface import BrokerSubmissionNotAttempted

            raise BrokerSubmissionNotAttempted("crypto pair is not explicitly configured")
        if str(side or "").lower() not in {"buy", "sell"}:
            from .broker_interface import BrokerSubmissionNotAttempted

            raise BrokerSubmissionNotAttempted("crypto side must be buy or sell")
        if str(order_type or "").lower() != "limit":
            from .broker_interface import BrokerSubmissionNotAttempted

            raise BrokerSubmissionNotAttempted("the supervised crypto lane only permits limit orders")
        tif = str(time_in_force or "").lower()
        if tif not in {"gtc", "ioc"}:
            from .broker_interface import BrokerSubmissionNotAttempted

            raise BrokerSubmissionNotAttempted("unsupported crypto time-in-force")
        if not client_order_id:
            from .broker_interface import BrokerSubmissionNotAttempted

            raise BrokerSubmissionNotAttempted("crypto client order identity is required")
        if limit_price is None:
            from .broker_interface import BrokerSubmissionNotAttempted

            raise BrokerSubmissionNotAttempted("crypto limit price is required")
        if set(notional_or_qty) not in ({"qty"}, {"notional"}):
            from .broker_interface import BrokerSubmissionNotAttempted

            raise BrokerSubmissionNotAttempted("crypto order must contain exactly qty or notional")
        try:
            from alpaca.trading.enums import OrderSide, TimeInForce
            from alpaca.trading.requests import LimitOrderRequest

            tif_enum = TimeInForce.GTC if tif == "gtc" else TimeInForce.IOC
            common = dict(
                symbol=raw,
                side=OrderSide.BUY if str(side or "").lower() == "buy" else OrderSide.SELL,
                time_in_force=tif_enum,
                limit_price=str(limit_price),
                client_order_id=str(client_order_id),
                **{key: str(value) for key, value in notional_or_qty.items()},
            )
            request = LimitOrderRequest(**common)
        except Exception as exc:
            from .broker_interface import BrokerSubmissionNotAttempted

            raise BrokerSubmissionNotAttempted("crypto order request validation failed before broker I/O") from exc
        return self._call("submit_crypto_order", "order_submission", lambda: self.trading.submit_order(order_data=request))

    def crypto_submission_available(self) -> bool:
        """Prove the paper crypto adapter exists before durable invocation marking."""

        return bool(
            self.mode == "paper"
            and self.paper_requested
            and callable(getattr(self, "submit_crypto_order", None))
            and callable(getattr(self.trading, "submit_order", None))
        )

    def cancel_crypto_order(self, order_id: str) -> Any:
        """Cancel a paper crypto order through the explicit crypto path."""

        if self.mode != "paper" or not self.paper_requested:
            from .broker_interface import BrokerSubmissionNotAttempted

            raise BrokerSubmissionNotAttempted("crypto cancellation is paper-only")
        if not str(order_id or "").strip():
            from .broker_interface import BrokerSubmissionNotAttempted

            raise BrokerSubmissionNotAttempted("crypto broker order identity is required")
        return self._call(
            "cancel_crypto_order",
            "order_submission",
            lambda: self.trading.cancel_order_by_id(str(order_id)),
        )

    def crypto_cancellation_available(self) -> bool:
        return bool(
            self.mode == "paper"
            and self.paper_requested
            and callable(getattr(self, "cancel_crypto_order", None))
            and callable(getattr(self.trading, "cancel_order_by_id", None))
        )

    def cancel_order(self, order_id: str) -> Any:
        return self._call("cancel_order", "order_submission", lambda: self.trading.cancel_order_by_id(order_id))

    def get_order(self, order_id: str) -> Any:
        return self._call("get_order", "order_lookup", lambda: self.trading.get_order_by_id(order_id))

    def get_order_by_client_order_id(self, client_order_id: str) -> Any:
        return self._call("get_order_by_client_order_id", "order_lookup", lambda: self.trading.get_order_by_client_id(client_order_id))

    def get_clock(self) -> Any:
        return self._call("get_clock", "read", self.trading.get_clock)

    def get_loss_metrics(self) -> dict[str, float | str | None]:
        """Return explicit, versioned dollar loss metrics from Alpaca.

        The adapter boundary never performs account arithmetic in binary
        floating point.  Decimal strings remain auditable JSON values; callers
        that need numeric operations parse them into their own exact domain.
        """

        def amount(value: Any, label: str) -> Decimal:
            try:
                parsed = Decimal(str(value))
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise RuntimeError(f"{label} is not a finite decimal") from exc
            if not parsed.is_finite():
                raise RuntimeError(f"{label} is not a finite decimal")
            return parsed

        def text(value: Decimal) -> str:
            rendered = format(value, "f")
            if "." in rendered:
                rendered = rendered.rstrip("0").rstrip(".")
            return rendered or "0"

        account = self.get_account()
        equity = amount(account.equity, "account equity")
        last_equity = amount(account.last_equity, "prior account equity")
        daily_loss = max(Decimal("0"), last_equity - equity)

        weekly_loss: Decimal | None = None
        try:
            from alpaca.trading.requests import GetPortfolioHistoryRequest

            history = self._call(
                "get_portfolio_history",
                "read",
                lambda: self.trading.get_portfolio_history(
                    GetPortfolioHistoryRequest(period="1W", timeframe="1D", extended_hours=False)
                ),
            )
            equities = [
                amount(value, "portfolio history equity")
                for value in (getattr(history, "equity", None) or [])
                if value is not None and amount(value, "portfolio history equity") > 0
            ]
            if len(equities) >= 2:
                weekly_loss = max(Decimal("0"), equities[0] - equities[-1])
        except Exception:
            weekly_loss = None
        return {
            "daily_loss_dollars": text(daily_loss),
            "weekly_loss_dollars": None if weekly_loss is None else text(weekly_loss),
            "reference_equity": text(last_equity),
            "daily_loss_confidence": "verified",
            "weekly_loss_confidence": "verified" if weekly_loss is not None else "unavailable",
            "provenance": "alpaca_account_and_portfolio_history",
            "metrics_version": "loss_controls_v2",
            "captured_at": datetime.now(UTC).isoformat(),
        }

    def is_market_open(self) -> bool:
        return bool(self.get_clock().is_open)

    def get_asset(self, symbol: str) -> Any | None:
        try:
            return self._call("get_asset", "read", lambda: self.trading.get_asset(symbol))
        except Exception:
            return None


AlpacaPaperBroker = AlpacaBroker
