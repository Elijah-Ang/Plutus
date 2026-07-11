from __future__ import annotations

from datetime import UTC, datetime, timedelta
import os
import socket
import urllib.request

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
                 "allowed_order_types": ["market"], "max_price_age_seconds": 120, "min_historical_bars": 50,
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
            "reason": "trend passed", "order_type": "market", "asset_class": "equity"}


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
