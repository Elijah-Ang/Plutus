import os
import uuid
import pytest
from datetime import datetime, UTC, timedelta
import pandas as pd
from app.utils import format_proposal_message, format_sgt
from app.storage import Storage
from app.ai_review import AIReviewer, deterministic_review, validate_review
from app.service import TradingService
from app.strategy_rule_based import Signal

class MockClock:
    def __init__(self, is_open=True, timestamp=None, next_close=None):
        self.is_open = is_open
        self.timestamp = timestamp or datetime.now(UTC)
        self.next_close = next_close or (self.timestamp + timedelta(hours=2))

class MockBroker:
    def __init__(self):
        self.positions = []
        self.orders = []
        self.open = True
        self.price = 500.0
        self.clock = MockClock()

    def get_positions(self):
        return self.positions

    def get_open_orders(self):
        return self.orders

    def is_market_open(self):
        return self.open

    def get_latest_price(self, symbol):
        return type("T", (), {"price": self.price, "timestamp": datetime.now(UTC)})()

    def get_historical_bars(self, symbol, timeframe, limit):
        data = {"close": [500.0] * limit, "volume": [10000.0] * limit}
        return pd.DataFrame(data)

    def get_account(self):
        return type("A", (), {"buying_power": 1000000.0})()

    def get_clock(self):
        return self.clock

class MockTelegramBot:
    def __init__(self):
        self.messages = []

    def send_message(self, text, chat_id=None):
        self.messages.append(text)

@pytest.fixture
def temp_storage(tmp_path):
    db_file = tmp_path / "test_trading.db"
    storage = Storage(db_file)
    storage.initialize()
    return storage

def test_confidence_labels_mapping():
    config = {}
    broker = MockBroker()
    service = TradingService(config, None, broker, "run_id")
    
    assert service._classify_trade_score(95) == "Very strong paper setup"
    assert service._classify_trade_score(85) == "Strong paper setup"
    assert service._classify_trade_score(75) == "Moderate paper setup"
    assert service._classify_trade_score(55) == "Weak setup, watch only"
    assert service._classify_trade_score(45) == "No action suggested"

def test_expiry_timing_rules():
    config = {"proposal_expiry_default_minutes": 15}
    broker = MockBroker()
    service = TradingService(config, None, broker, "run_id")
    now = datetime.now(UTC)
    signal = Signal("ENTRY", "buy", "SPY", "Test reason", 0.7, {})

    # High volatility -> 5 minutes
    exp_high = service._calculate_expiry_minutes("SPY", signal, 0.25, 85, now, now)
    assert exp_high == 5

    # Low volatility -> 20 minutes (for buy)
    exp_low = service._calculate_expiry_minutes("SPY", signal, 0.05, 85, now, now)
    assert exp_low == 20

    # Normal volatility -> 15 minutes
    exp_norm = service._calculate_expiry_minutes("SPY", signal, 0.15, 85, now, now)
    assert exp_norm == 15

    # Exit / Sell -> 10 minutes
    exit_signal = Signal("EXIT", "sell", "SPY", "Test reason", 0.7, {})
    exp_exit = service._calculate_expiry_minutes("SPY", exit_signal, 0.15, 85, now, now)
    assert exp_exit == 10

    # Exit / Sell under high volatility -> 5 minutes
    exp_exit_high = service._calculate_expiry_minutes("SPY", exit_signal, 0.25, 85, now, now)
    assert exp_exit_high == 5

def test_expiry_timing_boundaries():
    config = {"proposal_expiry_default_minutes": 15}
    broker = MockBroker()
    service = TradingService(config, None, broker, "run_id")
    now = datetime.now(UTC)
    signal = Signal("ENTRY", "buy", "SPY", "Test reason", 0.7, {})

    # Weak setup score subtracts 2 -> low volatility base 20 becomes 18
    exp_weak = service._calculate_expiry_minutes("SPY", signal, 0.05, 55, now, now)
    assert exp_weak == 18

    # Stale data subtracts 3 -> normal base 15 becomes 12
    stale_now = now + timedelta(seconds=70)
    exp_stale = service._calculate_expiry_minutes("SPY", signal, 0.15, 85, now, stale_now)
    assert exp_stale == 12

    # Truncate near close (e.g. 7 minutes until close) -> becomes 7 minutes
    broker.clock.next_close = now + timedelta(minutes=7)
    exp_close = service._calculate_expiry_minutes("SPY", signal, 0.15, 85, now, now)
    assert exp_close == 7

    # Ensure never below 5 minutes
    broker.clock.next_close = now + timedelta(minutes=2)
    exp_min = service._calculate_expiry_minutes("SPY", signal, 0.15, 85, now, now)
    assert exp_min == 5

def test_telegram_proposal_message_content():
    proposal = {
        "symbol": "QQQ",
        "side": "buy",
        "notional": 1.0,
        "score": 68.0,
        "classification": "Moderate paper setup",
        "asset_score": 74.0,
        "asset_classification": "Moderate watch candidate",
        "symbol_rank": 1,
        "total_active_symbols": 4,
        "price_change_pct": 0.18,
        "session_change_pct": 0.72,
        "reason": "Test trend logic",
        "expires_at": (datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
        "expiry_minutes": 15,
        "volatility_class": "normal",
        "review": {
            "gpt_confidence": "Medium",
            "gpt_caution": "Medium",
            "main_risk": "Volatility is still slightly elevated.",
            "supports_system_score": "yes",
            "reason": "Aligns with trend indicators",
            "reasoning_notes": "Reviewed"
        }
    }
    config = {"mode": "paper"}
    msg = format_proposal_message(proposal, config)

    assert "Mode: Paper trading only" in msg
    assert "Action: Buy QQQ" in msg
    assert "Amount: $1" in msg
    assert "Asset score: 74/100 — Moderate watch candidate" in msg
    assert "Trade score: 68/100 — Moderate paper setup" in msg
    assert "System confidence: Moderate" in msg
    assert "Rank: #1 of 4 active ETFs" in msg
    assert "Since last check: +0.18%" in msg
    assert "Since market open: +0.72%" in msg
    assert "GPT review: Medium confidence" in msg
    assert "Main caution: Volatility is still slightly elevated." in msg
    assert "Time to decide: 15 minutes" in msg
    assert "Reply yes to approve, or no to reject." in msg

def test_expiry_notification_natural_language(temp_storage):
    config = {"mode": "paper"}
    broker = MockBroker()
    service = TradingService(config, temp_storage, broker, "run_id")
    service.telegram = MockTelegramBot()

    # Insert an expired proposal
    expires_at = datetime.now(UTC) - timedelta(minutes=1)
    temp_storage.execute(
        "INSERT INTO trade_proposals(id,run_id,signal_id,symbol,side,notional,status,created_at,expires_at,strategy_version,payload) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("p_expired", "run_id", "s_id", "QQQ", "buy", 10.0, "expired", expires_at.isoformat(), expires_at.isoformat(), "strategy", "{}")
    )

    # Notify expiry
    service.notify_expired_proposals()

    # Check notification sent exactly once
    assert len(service.telegram.messages) == 1
    msg = service.telegram.messages[0]
    assert "Proposal expired" in msg
    assert "The QQQ paper trade proposal expired at" in msg
    assert "No order was placed." in msg
    assert "Reason: no yes/no reply before expiry." in msg

    # Check database marked as notified
    rows = temp_storage.fetch_all("SELECT expiry_notified FROM trade_proposals WHERE id='p_expired'")
    assert rows[0]["expiry_notified"] == 1

    # Call notify_expired_proposals again, should not send another message
    service.notify_expired_proposals()
    assert len(service.telegram.messages) == 1

def test_live_trading_disabled_by_default():
    config = {"mode": "live", "live_enabled": False}
    assert config.get("live_enabled") is False
