from __future__ import annotations

from datetime import UTC, datetime, timedelta
import os
import socket
import sys
import types
import urllib.request
from types import SimpleNamespace

import pytest
import requests

# Establish a fully synthetic credential boundary before importing application
# modules. SecretStore refuses Keychain access whenever this marker is present.
os.environ.update({
    "TRADING_AGENT_TESTING": "1",
    "ALPACA_API_KEY": "synthetic-test-alpaca-key",
    "ALPACA_SECRET_KEY": "synthetic-test-alpaca-secret",
    "TELEGRAM_BOT_TOKEN": "999999:synthetic-test-token",
    "TELEGRAM_CHAT_ID": "synthetic-test-chat-id",
    "TELEGRAM_ALLOWED_USER_ID": "synthetic-test-sender-id",
    "OPENAI_API_KEY": "synthetic-test-openai-key",
    "EODHD_API_TOKEN": "synthetic-test-eodhd-token",
})


# Inject a local SDK double for broker-construction tests. Production code still
# imports the real alpaca-py package; the offline suite supplies the dependency
# explicitly instead of using a TEST-symbol or testing-mode execution bypass.
if "alpaca" not in sys.modules:
    alpaca = types.ModuleType("alpaca")
    alpaca_data = types.ModuleType("alpaca.data")
    alpaca_data_enums = types.ModuleType("alpaca.data.enums")
    alpaca_data_historical = types.ModuleType("alpaca.data.historical")
    alpaca_data_requests = types.ModuleType("alpaca.data.requests")
    alpaca_data_timeframe = types.ModuleType("alpaca.data.timeframe")
    alpaca_trading = types.ModuleType("alpaca.trading")
    alpaca_trading_client = types.ModuleType("alpaca.trading.client")
    alpaca_trading_enums = types.ModuleType("alpaca.trading.enums")
    alpaca_trading_requests = types.ModuleType("alpaca.trading.requests")

    class _FakeTradingClient:
        def __init__(self, key, secret, paper=True):
            self.paper = paper
            self._sandbox = paper
            self._base_url = "https://paper-api.alpaca.markets" if paper else "https://api.alpaca.markets"
        def get_account(self):
            return SimpleNamespace(id="offline-paper-account", account_number="offline-paper-account", status="ACTIVE", currency="USD", account_blocked=False, trading_blocked=False)
        def get_clock(self):
            return SimpleNamespace(is_open=True, timestamp=datetime.now(UTC), next_close=datetime.now(UTC) + timedelta(hours=1))
        def get_all_positions(self): return []
        def get_orders(self, *args, **kwargs): return []
        def get_order_by_client_id(self, client_order_id): raise RuntimeError("offline order not found")
        def get_order_by_id(self, order_id): raise RuntimeError("offline order not found")
        def submit_order(self, **kwargs): return SimpleNamespace(id="offline-order", status="submitted")

    class _FakeStockHistoricalDataClient:
        def __init__(self, key, secret): pass
        def get_stock_bars(self, *args, **kwargs): return []
        def get_stock_latest_trade(self, request, *args, **kwargs):
            return {request.symbol_or_symbols: SimpleNamespace(price=100.0, timestamp=datetime.now(UTC))}
        def get_stock_latest_quote(self, request, *args, **kwargs):
            return {request.symbol_or_symbols: SimpleNamespace(bid_price=99.99, ask_price=100.01, timestamp=datetime.now(UTC))}

    class _FakeCryptoHistoricalDataClient:
        def __init__(self, key=None, secret=None): pass
        def get_crypto_bars(self, *args, **kwargs): return SimpleNamespace(df=[])
        def get_crypto_latest_quote(self, *args, **kwargs): return {}
        def get_crypto_latest_trade(self, *args, **kwargs): return {}
        def get_crypto_latest_orderbook(self, *args, **kwargs): return {}

    class _Enum:
        BUY = "buy"
        SELL = "sell"
        DAY = "day"

    class _EnumValue:
        def __init__(self, value): self.value = value

    class _AssetClass:
        CRYPTO = _EnumValue("crypto")

    class _AssetStatus:
        ACTIVE = _EnumValue("active")

    class _CryptoFeed:
        US = _EnumValue("us")

    class _DataFeed:
        IEX = _EnumValue("iex")
        SIP = _EnumValue("sip")

    class _TimeFrame:
        Day = _EnumValue("1Day")
        Hour = _EnumValue("1Hour")

    class _Request:
        def __init__(self, **kwargs): self.__dict__.update(kwargs)

    alpaca_trading_client.TradingClient = _FakeTradingClient
    alpaca_data_historical.StockHistoricalDataClient = _FakeStockHistoricalDataClient
    alpaca_data_historical.CryptoHistoricalDataClient = _FakeCryptoHistoricalDataClient
    alpaca_data_enums.CryptoFeed = _CryptoFeed
    alpaca_data_enums.DataFeed = _DataFeed
    alpaca_data_requests.StockLatestQuoteRequest = _Request
    alpaca_data_requests.StockLatestTradeRequest = _Request
    alpaca_data_requests.CryptoBarsRequest = _Request
    alpaca_data_requests.CryptoLatestQuoteRequest = _Request
    alpaca_data_requests.CryptoLatestTradeRequest = _Request
    alpaca_data_requests.CryptoLatestOrderbookRequest = _Request
    alpaca_data_timeframe.TimeFrame = _TimeFrame
    alpaca_trading_enums.OrderSide = _Enum
    alpaca_trading_enums.TimeInForce = _Enum
    alpaca_trading_enums.AssetClass = _AssetClass
    alpaca_trading_enums.AssetStatus = _AssetStatus
    alpaca_trading_requests.GetAssetsRequest = _Request
    alpaca_trading_requests.LimitOrderRequest = _Request
    alpaca_trading_requests.MarketOrderRequest = _Request
    sys.modules.update({
        "alpaca": alpaca, "alpaca.data": alpaca_data, "alpaca.data.enums": alpaca_data_enums,
        "alpaca.data.historical": alpaca_data_historical, "alpaca.data.requests": alpaca_data_requests,
        "alpaca.data.timeframe": alpaca_data_timeframe,
        "alpaca.trading": alpaca_trading, "alpaca.trading.client": alpaca_trading_client,
        "alpaca.trading.enums": alpaca_trading_enums, "alpaca.trading.requests": alpaca_trading_requests,
    })


def _blocked_network(*args, **kwargs):
    pytest.fail("outbound network access is forbidden in the offline test suite")


# Install the process-wide guard before any application import can initialize a
# client. Individual tests may replace these functions with local fakes.
socket.create_connection = _blocked_network
socket.socket.connect = _blocked_network
requests.sessions.Session.request = _blocked_network
urllib.request.urlopen = _blocked_network
try:
    import httpx
    httpx.Client.request = _blocked_network
    httpx.AsyncClient.request = _blocked_network
except ImportError:
    httpx = None
try:
    import urllib3
    urllib3.PoolManager.request = _blocked_network
except ImportError:
    urllib3 = None

from app.power import PowerStatus


@pytest.fixture(autouse=True)
def hard_offline_network_guard(monkeypatch):
    """Fail the suite immediately if production code attempts outbound I/O."""
    monkeypatch.setattr(socket, "create_connection", _blocked_network)
    monkeypatch.setattr(socket.socket, "connect", _blocked_network)
    monkeypatch.setattr(requests.sessions.Session, "request", _blocked_network)
    monkeypatch.setattr(urllib.request, "urlopen", _blocked_network)
    if httpx is not None:
        monkeypatch.setattr(httpx.Client, "request", _blocked_network)
        monkeypatch.setattr(httpx.AsyncClient, "request", _blocked_network)
    if urllib3 is not None:
        monkeypatch.setattr(urllib3.PoolManager, "request", _blocked_network)


@pytest.fixture(autouse=True)
def default_service_power_and_internet(monkeypatch):
    monkeypatch.setattr("app.service.get_power_status", lambda: PowerStatus(True, "test", "AC power connected", 100.0))
    monkeypatch.setattr("app.service.internet_available", lambda: True)
    monkeypatch.setattr("app.preflight.internet_available", lambda: True)
    monkeypatch.setattr("app.internet.internet_available", lambda *args, **kwargs: True)
    monkeypatch.setattr("app.telegram_bot.TelegramBot.is_available", lambda self, force=False: True)
    monkeypatch.setattr("app.telegram_bot.TelegramBot.send_message", lambda self, text, chat_id=None: {"message_id": 1})
    # Tests that inject a client still exercise AI parsing; service tests without an
    # injected client deterministically take the no-key fallback and never dial out.
    monkeypatch.setattr("app.ai_review.get_secret", lambda name: None)


@pytest.fixture
def safe_config():
    return {
        "mode": "paper", "live_enabled": False, "explicit_live_confirmation": False,
        "approved_strategy_versions": ["rule_based_v2"],
        "risk": {"max_trade_notional_paper": 5, "max_trade_notional_live": 5, "max_trades_per_day": 1,
                 "max_open_positions": 1, "allow_margin": False, "allow_shorting": False,
                 "allowed_order_types": ["market", "limit"], "max_price_age_seconds": 120, "min_historical_bars": 50,
                 "max_price_gap_pct": 15, "stop_if_daily_loss_pct_exceeds": 5, "stop_if_weekly_loss_pct_exceeds": 10,
                 "stop_if_daily_loss_dollars_exceeds": None, "stop_if_weekly_loss_dollars_exceeds": None},
    }


@pytest.fixture
def proposal():
    now = datetime.now(UTC)
    return {"id": "p1", "status": "pending", "symbol": "QQQ", "side": "buy", "action": "entry",
            "notional": 5, "latest_price": 500, "price_at": now.isoformat(), "historical_bars": 250,
            "volume": 1000, "price_gap_pct": 0, "created_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=10)).isoformat(), "strategy_version": "rule_based_v2",
            "reason": "trend passed", "order_type": "limit", "asset_class": "equity",
            "stop_price": 490.0, "stop_distance_dollars": 10.0, "atr_value": 5.0,
            "technical_stop_price": 490.0, "stop_model_used": "atr", "stop_validation_status": "validated",
            "quote_source": "alpaca_quote", "quote_bid": 499.9, "quote_ask": 500.1,
            "quote_midpoint": 500.0, "quote_timestamp": now.isoformat(), "quote_spread_bps": 4.0,
            "limit_price": 501.36}


@pytest.fixture
def context():
    return {"power_connected": True, "internet_available": True, "database_writable": True,
            "broker_available": True, "telegram_available": True, "market_open": True, "kill_switch": False,
            "open_positions": 0, "trades_today": 0, "duplicate_order": False, "same_symbol_position": False,
            "uses_margin": False, "daily_loss": 0, "weekly_loss": 0,
            "daily_loss_dollars": 0, "weekly_loss_dollars": 0,
            "daily_loss_pct": 0, "weekly_loss_pct": 0, "loss_reference_equity": 100,
            "daily_loss_confidence": "verified", "weekly_loss_confidence": "verified",
            "loss_provenance": "test", "loss_metrics_version": "loss_controls_v2", "buying_power": 100,
            "proposed_total_exposure_pct": 0.05, "proposed_symbol_exposure_pct": 0.05,
            "proposed_cluster_positions_count": 1, "proposed_cluster_exposure_pct": 0.05}
