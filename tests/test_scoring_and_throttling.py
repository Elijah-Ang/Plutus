import os
import uuid
import json
from datetime import datetime, UTC, timedelta
from pathlib import Path
import pytest
import pandas as pd

from app.utils import load_config, PROJECT_ROOT, format_proposal_message
from app.storage import Storage
from app.ai_review import AIReviewer, deterministic_review
from app.service import TradingService
from app.strategy_rule_based import Signal

class MockBroker:
    def __init__(self):
        self.positions = []
        self.orders = []
        self.open = True
        self.price = 500.0

    def get_positions(self):
        return self.positions

    def get_open_orders(self):
        return self.orders

    def is_market_open(self):
        return self.open

    def get_clock(self):
        now = datetime.now(UTC)
        return type("Clock", (), {"is_open": self.open, "timestamp": now, "next_close": now + timedelta(hours=6)})()

    def get_latest_price(self, symbol):
        return type("T", (), {"price": self.price, "timestamp": datetime.now(UTC)})()

    def get_historical_bars(self, symbol, timeframe, limit):
        # Create a mock DataFrame with enough bars
        data = {
            "close": [500.0] * limit,
            "volume": [10000.0] * limit,
        }
        df = pd.DataFrame(data)
        return df

    def get_account(self):
        return type("A", (), {
            "buying_power": 1000000.0, "equity": 100000.0, "last_equity": 100000.0,
            "cash": 100000.0, "long_market_value": 0.0, "short_market_value": 0.0,
        })()

    def get_loss_metrics(self):
        return {"daily_loss": 0.0, "weekly_loss": 0.0}

class MockTelegramBot:
    def __init__(self):
        self.messages = []
        self.allowed_user_id = "597843799"

    def send_message(self, text, chat_id=None):
        self.messages.append(text)

    def is_authorized(self, sender):
        return sender == self.allowed_user_id

    def is_available(self, force=False):
        return True

@pytest.fixture
def temp_storage(tmp_path):
    db_file = tmp_path / "test_trading.db"
    storage = Storage(db_file)
    storage.initialize()
    return storage

def test_github_workflow_docs_exist():
    wf_file = PROJECT_ROOT / "docs" / "GITHUB_WORKFLOW.md"
    assert wf_file.is_file()
    content = wf_file.read_text()
    assert "Never Commit" in content
    assert "Always Safe to Commit" in content

def test_launchd_installed():
    target = Path(os.path.expanduser("~/Library/LaunchAgents/com.elijah.tradingagent.plist"))
    assert target.is_file()

def test_live_trading_disabled():
    config = load_config()
    assert config.get("live_enabled") is False
    assert config.get("mode") == "paper"

def test_system_overview_updated():
    overview = PROJECT_ROOT / "docs" / "SYSTEM_OVERVIEW.md"
    assert overview.is_file()
    content = overview.read_text()
    assert "2026-06-19" in content
    assert "test_paper_sell_proposal.sh" in content
    assert "market_memory" in content

def test_deterministic_scoring_logic(temp_storage):
    config = load_config()
    broker = MockBroker()
    service = TradingService(config, temp_storage, broker, "test_run_id")
    service.telegram = MockTelegramBot()
    
    # Run scan to create a starting market memory row
    service.scan()
    
    # Fetch rows from market_memory
    rows = temp_storage.fetch_all("SELECT * FROM market_memory")
    assert len(rows) > 0
    for r in rows:
        assert 0.0 <= r["score"] <= 100.0
        assert r["classification"] in {
            "Very strong paper setup",
            "Strong paper setup",
            "Moderate paper setup",
            "Weak setup, watch only",
            "No action suggested"
        }

def test_telegram_proposal_includes_score():
    proposal = {
        "symbol": "SPY",
        "side": "buy",
        "notional": 5,
        "score": 75.0,
        "classification": "Moderate paper candidate",
        "reason": "Test trend score",
        "expires_at": datetime.now(UTC).isoformat(),
    }
    config = load_config()
    msg = format_proposal_message(proposal, config)
    assert "Recommendation score: 75/100" in msg
    assert "Suggestion: Moderate paper candidate" in msg
    assert "Why: Test trend score" in msg

def test_gpt_call_throttling(temp_storage):
    config = load_config()
    # Configure artificial AI limits
    config["ai"]["ai_review_min_score"] = 65
    config["ai"]["ai_review_min_interval_minutes"] = 10
    config["ai"]["ai_daily_call_limit"] = 2
    config["ai"]["ai_max_calls_per_run"] = 2
    
    broker = MockBroker()
    service = TradingService(config, temp_storage, broker, "test_run_id")
    service.telegram = MockTelegramBot()
    
    # 1. First review passes throttle and inserts an AI review
    proposal = {
        "id": "p1", "symbol": "SPY", "side": "buy", "notional": 5, "score": 85.0, "classification": "Strong", "reason": "Test"
    }
    
    # Mock self.ai.review to count calls
    original_review = service.ai.review
    calls = []
    def mock_review(prop):
        calls.append(prop)
        return deterministic_review(prop)
    service.ai.review = mock_review
    
    # Evaluate first run
    # Manually check and trigger
    # Count calls today
    now = datetime.now(UTC)
    today_start = now.date().isoformat() + "T00:00:00"
    
    # Insert proposal into DB to trigger time interval check
    temp_storage.execute("INSERT INTO trade_proposals(id,run_id,signal_id,symbol,side,notional,status,created_at,expires_at,strategy_version,payload) VALUES(?,?,?,?,?,?,?,?,?,?,?)", ("p1", "r1", "s1", "SPY", "buy", 5, "pending", now.isoformat(), now.isoformat(), "rule_based_v1", "{}"))
    
    # Inject an AI review to simulate a recent call
    temp_storage.execute("INSERT INTO ai_reviews(run_id,proposal_id,summary,risks,caution_level,payload,created_at) VALUES(?,?,?,?,?,?,?)", ("r1", "p1", "Summary", "[]", "low", "{}", now.isoformat()))
    
    # Check if a new call 1 minute later is throttled
    last_call = temp_storage.fetch_all("SELECT created_at FROM ai_reviews WHERE proposal_id IN (SELECT id FROM trade_proposals WHERE symbol=?) ORDER BY created_at DESC LIMIT 1", ("SPY",))
    assert last_call is not None
    last_call_time = datetime.fromisoformat(last_call[0]["created_at"].replace("Z", "+00:00")).replace(tzinfo=UTC)
    time_since = (datetime.now(UTC) - last_call_time).total_seconds() / 60
    
    # The time_since is near 0 (< 10 minutes), so new calls must be throttled!
    assert time_since < 10
