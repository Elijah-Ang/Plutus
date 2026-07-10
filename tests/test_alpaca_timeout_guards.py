from __future__ import annotations

import time
import uuid
import socket
import ssl
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.broker_alpaca import AlpacaBroker, AlpacaBrokerError
from app.execution import Executor
from app.service import TradingService
from app.storage import Storage


class DummyTelegram:
    allowed_user_id = "123"

    def send_message(self, *args, **kwargs):
        return None

    def get_updates(self, *args, **kwargs):
        return []

    def is_available(self, *args, **kwargs):
        return True


def _broker(timeout: float = 0.05) -> AlpacaBroker:
    return AlpacaBroker(
        {
            "mode": "paper",
            "live_enabled": False,
            "alpaca": {
                "timeouts": {
                    "read_seconds": timeout,
                    "market_data_seconds": timeout,
                    "order_lookup_seconds": timeout,
                    "order_submission_seconds": timeout,
                }
            },
        },
        "dummy-key",
        "dummy-secret",
    )


def _sleeping_call(*args, **kwargs):
    time.sleep(2)


def test_alpaca_sdk_timeout_is_caught_and_classified():
    broker = _broker()
    broker.trading.get_account = _sleeping_call

    started = time.monotonic()
    with pytest.raises(AlpacaBrokerError) as exc_info:
        broker.get_account()

    assert time.monotonic() - started < 1.0
    assert exc_info.value.category == "alpaca_timeout"
    assert exc_info.value.operation == "get_account"


def test_alpaca_error_classifier_categories():
    broker = _broker()
    assert broker._classify_error(TimeoutError("timed out")) == "alpaca_timeout"
    assert broker._classify_error(socket.gaierror("temporary failure in name resolution")) == "alpaca_dns_error"
    assert broker._classify_error(ssl.SSLError("tls failed")) == "alpaca_tls_error"
    assert broker._classify_error(type("APIError", (Exception,), {"status_code": 429})("rate")) == "alpaca_rate_limit"
    assert broker._classify_error(type("APIError", (Exception,), {"status_code": 403})("forbidden")) == "alpaca_auth_error"
    assert broker._classify_error(type("APIError", (Exception,), {"status_code": 500})("server")) == "alpaca_api_error"


def test_scanner_side_market_data_stall_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr("app.service.TelegramBot", lambda: DummyTelegram())
    storage = Storage(tmp_path / "test.db")
    storage.initialize()
    broker = _broker()
    broker.data.get_stock_bars = _sleeping_call
    service = TradingService(
        {
            "mode": "paper",
            "live_enabled": False,
            "dynamic_universe": {"runtime_orchestration": {"max_forward_outcome_updates_per_cycle": 1}},
        },
        storage,
        broker,
        "run-scan-timeout",
    )
    storage.execute(
        """INSERT INTO trade_outcomes(
            id, trade_id, actual_or_shadow, symbol, entry_time, entry_price, outcome_status,
            stop_hit, target_reached, add_on_improved, beat_shadow_alternatives, updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (str(uuid.uuid4()), "shadow-1", "shadow", "SPY", datetime(2026, 1, 1, tzinfo=UTC).isoformat(), 100.0, "pending", 0, 0, None, None, datetime.now(UTC).isoformat()),
    )

    started = time.monotonic()
    service._update_forward_outcomes()

    assert time.monotonic() - started < 1.0
    row = storage.fetch_all("SELECT outcome_status FROM trade_outcomes WHERE trade_id='shadow-1'")[0]
    # Runtime Phase 1 never makes an extra market-data request; without a scan
    # cache the legacy row stays pending for a later cached-bar cycle.
    assert row["outcome_status"] == "pending"


def test_listener_approval_validation_stall_is_bounded_and_blocks_without_order(tmp_path, monkeypatch):
    monkeypatch.setattr("app.service.TelegramBot", lambda: DummyTelegram())
    storage = Storage(tmp_path / "test.db")
    storage.initialize()
    broker = _broker()
    broker.data.get_stock_latest_trade = _sleeping_call
    broker.trading.get_clock = lambda: SimpleNamespace(is_open=True)
    service = TradingService(
        {
            "mode": "paper",
            "live_enabled": False,
            "telegram": {"approval_price_refresh_required": True, "approval_max_price_age_seconds": 120, "approval_max_price_move_bps": 25},
            "position_sizing": {"enabled": False},
        },
        storage,
        broker,
        "run-listener-timeout",
    )
    row = {"id": "proposal-1", "symbol": "SPY", "side": "buy", "notional": 5.0, "current_price": 100.0}
    proposal = {"id": "proposal-1", "symbol": "SPY", "side": "buy", "notional": 5.0, "latest_price": 100.0, "price_at": datetime.now(UTC).isoformat()}

    started = time.monotonic()
    result, *_ = service._execute_final_revalidation(row, proposal, "SPY", "buy", False, "approval-1")

    assert time.monotonic() - started < 1.0
    assert result.submitted is False
    assert result.status == "blocked"
    assert result.reason == "Price refresh failed or price is unavailable"
    assert storage.fetch_all("SELECT * FROM orders") == []


def test_order_submission_timeout_is_unknown_and_not_retried(tmp_path):
    broker = _broker()
    calls = {"submit": 0}

    def _submit_order(*args, **kwargs):
        calls["submit"] += 1
        time.sleep(2)

    broker.trading.submit_order = _submit_order

    class PassingRisk:
        def evaluate(self, proposal, context, final=False):
            return SimpleNamespace(passed=True, reasons=[])

    from app.storage import Storage
    storage = Storage(tmp_path / "timeout.db")
    storage.initialize()
    now = datetime.now(UTC)
    proposal = {
        "id": "p1", "status": "approved", "symbol": "SPY", "side": "buy", "action": "entry",
        "notional": 5.0, "latest_price": 100.0, "price_at": now.isoformat(),
        "created_at": now.isoformat(), "expires_at": (now + timedelta(minutes=5)).isoformat(),
    }
    result = Executor(broker, PassingRisk(), storage, "run").execute(proposal, {"approval_valid": True})

    assert calls["submit"] == 1
    assert result.submitted is False
    assert result.status == "unknown"
    assert result.reason == "manual review required: AlpacaBrokerError"
