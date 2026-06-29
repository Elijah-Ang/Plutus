from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.power import PowerStatus


@pytest.fixture(autouse=True)
def default_service_power_and_internet(monkeypatch):
    monkeypatch.setattr("app.service.get_power_status", lambda: PowerStatus(True, "test", "AC power connected", 100.0))
    monkeypatch.setattr("app.service.internet_available", lambda: True)


@pytest.fixture
def safe_config():
    return {
        "mode": "paper", "live_enabled": False, "explicit_live_confirmation": False,
        "approved_strategy_versions": ["rule_based_v1"],
        "risk": {"max_trade_notional_paper": 5, "max_trade_notional_live": 5, "max_trades_per_day": 1,
                 "max_open_positions": 1, "allow_margin": False, "allow_shorting": False,
                 "allowed_order_types": ["market"], "max_price_age_seconds": 120, "min_historical_bars": 50,
                 "max_price_gap_pct": 15, "stop_if_daily_loss_exceeds": 5, "stop_if_weekly_loss_exceeds": 10},
    }


@pytest.fixture
def proposal():
    now = datetime.now(UTC)
    return {"id": "p1", "status": "pending", "symbol": "QQQ", "side": "buy", "action": "entry",
            "notional": 5, "latest_price": 500, "price_at": now.isoformat(), "historical_bars": 250,
            "volume": 1000, "price_gap_pct": 0, "created_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=10)).isoformat(), "strategy_version": "rule_based_v1",
            "reason": "trend passed", "order_type": "market", "asset_class": "equity"}


@pytest.fixture
def context():
    return {"power_connected": True, "internet_available": True, "database_writable": True,
            "broker_available": True, "telegram_available": True, "market_open": True, "kill_switch": False,
            "open_positions": 0, "trades_today": 0, "duplicate_order": False, "same_symbol_position": False,
            "uses_margin": False, "daily_loss": 0, "weekly_loss": 0, "buying_power": 100}
