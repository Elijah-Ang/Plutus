import json
import uuid
import time
import pytest
import math
from datetime import datetime, UTC, timedelta
import pandas as pd
from unittest.mock import MagicMock, patch

from app.storage import Storage
from app.service import TradingService
from app.strategy_rule_based import Signal

class MockClock:
    def __init__(self, is_open=True, timestamp=None):
        self.is_open = is_open
        self.timestamp = timestamp or datetime.now(UTC)
        self.next_close = self.timestamp + timedelta(hours=2)

class MockBroker:
    def __init__(self):
        self.positions = []
        self.orders = []
        self.open = True
        self.price = 100.0
        self.price_time = datetime.now(UTC)
        self.clock = MockClock()

    def get_positions(self):
        return self.positions

    def get_open_orders(self):
        return self.orders

    def is_market_open(self):
        return self.open

    def get_latest_price(self, symbol):
        return type("T", (), {"price": self.price, "timestamp": self.price_time})()

    def get_historical_bars(self, symbol, timeframe, limit=250):
        data = {
            "open": [self.price] * limit,
            "high": [self.price + 1] * limit,
            "low": [self.price - 1] * limit,
            "close": [self.price] * limit,
            "volume": [10000.0] * limit
        }
        return pd.DataFrame(data)

    def get_account(self):
        return type("A", (), {
            "buying_power": 1000000.0,
            "equity": 1000000.0,
            "last_equity": 1000000.0,
            "cash": 1000000.0,
            "long_market_value": 0.0,
            "short_market_value": 0.0
        })()

    def get_loss_metrics(self):
        return {"daily_loss": 0.0, "weekly_loss": 0.0}

    def get_clock(self):
        return self.clock

    def submit_order(self, symbol, side, order_args, order_type, limit_price, client_order_id):
        order = type("O", (), {"status": "submitted", "id": f"mock-order-{uuid.uuid4().hex[:10]}"})()
        self.orders.append(order)
        return order

class MockTelegramBot:
    def __init__(self):
        self.updates = []
        self.sent_messages = []
        self.chat_id = "12345"

    def get_updates(self, offset=None, timeout=0):
        return self.updates

    def send_message(self, text, chat_id=None):
        self.sent_messages.append((text, chat_id or self.chat_id))
        return type("M", (), {"message_id": 999})()

    def is_authorized(self, sender_id):
        return sender_id == "authorized_user_123"

    def handle_command(self, text, sender):
        return f"Handled {text}"

@pytest.fixture
def temp_storage(tmp_path):
    db_file = tmp_path / "test_trading.db"
    storage = Storage(db_file)
    storage.initialize()
    return storage

@pytest.fixture
def base_config():
    return {
        "mode": "paper",
        "live_enabled": False,
        "risk": {
            "max_trade_notional_paper": 5,
            "max_trades_per_day": 10,
            "max_open_positions": 5,
            "min_historical_bars": 10,
            "max_price_age_seconds": 120,
            "require_gpt_review_for_buy_proposals": True,
            "approved_strategy_versions": ["rule_based_v1"]
        },
        "watchlist": ["SPY", "QQQ"],
        "emergency_exit": {
            "enabled": True
        },
        "telegram": {
            "approval_enabled": True,
            "telegram_approval_listener_enabled": True,
            "telegram_approval_poll_interval_seconds": 30,
            "telegram_approval_listener_mode": "approval_only",
            "market_scan_processes_telegram_updates": True,
            "approval_price_refresh_required": True,
            "approval_max_price_age_seconds": 60,
            "approval_max_price_move_bps": 25,
            "allow_plain_yes_only_when_single_pending_proposal": True
        }
    }

def test_sleep_mode_toggle_and_expiry(temp_storage, base_config):
    broker = MockBroker()
    service = TradingService(base_config, temp_storage, broker, "test_run_id")
    service.telegram = MockTelegramBot()

    # Use a fixed reference timestamp for message tests
    now_ref = 1729000000.0
    service.listener_started_at = now_ref - 100.0

    # 1. Unauthorized command is ignored (message is fresh)
    service.telegram.updates = [{
        "update_id": 1,
        "message": {
            "message_id": 101,
            "date": int(now_ref - 10), # 10s old, not stale
            "text": "sleep",
            "from": {"id": "unauthorized_user_666"},
            "chat": {"id": "12345"}
        }
    }]
    with patch("time.time", return_value=now_ref):
        service.process_telegram()
    assert temp_storage.get_control_state("sleep_mode_active") is None
    audit_events = temp_storage.fetch_all("SELECT * FROM audit_events WHERE event_type='sleep_mode_command_ignored_unauthorized'")
    assert len(audit_events) == 1

    # 2. Command older than 24h is ignored
    # We mock time.time() to return:
    # - now_ref on the first call (Phase 0 check, message is 10s old, not stale)
    # - now_ref + 90000 on the second call (age check, message is 25 hours old)
    service.telegram.updates = [{
        "update_id": 2,
        "message": {
            "message_id": 102,
            "date": int(now_ref - 10),
            "text": "sleep mode on",
            "from": {"id": "authorized_user_123"},
            "chat": {"id": "12345"}
        }
    }]
    with patch("time.time", side_effect=[now_ref, now_ref + 90000]):
        service.process_telegram()
    assert temp_storage.get_control_state("sleep_mode_active") is None
    audit_events = temp_storage.fetch_all("SELECT * FROM audit_events WHERE event_type='sleep_mode_command_ignored_old'")
    assert len(audit_events) == 1

    # 3. Valid command activates sleep mode
    service.telegram.updates = [{
        "update_id": 3,
        "message": {
            "message_id": 103,
            "date": int(now_ref - 10),
            "text": "sleep",
            "from": {"id": "authorized_user_123"},
            "chat": {"id": "12345"}
        }
    }]
    with patch("time.time", return_value=now_ref):
        service.process_telegram()
    assert int(temp_storage.get_control_state("sleep_mode_active")) == 1
    assert temp_storage.get_control_state("sleep_mode_last_command") == "sleep"
    audit_events = temp_storage.fetch_all("SELECT * FROM audit_events WHERE event_type='sleep_mode_enabled'")
    assert len(audit_events) == 1
    assert any("Sleep mode ON" in m[0] for m in service.telegram.sent_messages)

    # 4. Wake up command deactivates sleep mode and triggers wake summary
    service.telegram.updates = [{
        "update_id": 4,
        "message": {
            "message_id": 104,
            "date": int(now_ref - 5),
            "text": "wake up",
            "from": {"id": "authorized_user_123"},
            "chat": {"id": "12345"}
        }
    }]
    service.telegram.sent_messages.clear()
    with patch("time.time", return_value=now_ref):
        service.process_telegram()
    assert int(temp_storage.get_control_state("sleep_mode_active")) == 0
    assert temp_storage.get_control_state("sleep_mode_last_command") == "awake"
    audit_events = temp_storage.fetch_all("SELECT * FROM audit_events WHERE event_type='sleep_mode_disabled'")
    assert len(audit_events) == 1
    assert any("Sleep mode OFF" in m[0] for m in service.telegram.sent_messages)
    assert any("Overnight summary" in m[0] for m in service.telegram.sent_messages)

def test_sleep_mode_buy_suppression(temp_storage, base_config):
    broker = MockBroker()
    service = TradingService(base_config, temp_storage, broker, "test_run_id")
    service.telegram = MockTelegramBot()

    # Turn sleep mode ON manually in database
    temp_storage.set_control_state("sleep_mode_active", "1", "test", "test", "test", 1, 1, int(time.time()))

    # Mock evaluate_symbol to return a BUY signal
    def mock_eval(*args, **kwargs):
        return Signal("ENTRY", "buy", "SPY", "Good trend", 0.9, {"volatility_20": 0.15})

    with patch("app.service.evaluate_symbol", mock_eval), patch.object(service.ai, "review") as mock_review:
        service.scan()
        # GPT review should not have been called because sleep mode is active
        mock_review.assert_not_called()

        # Check market memory to verify the candidate was suppressed
        rows = temp_storage.fetch_all("SELECT * FROM market_memory WHERE symbol='SPY'")
        assert len(rows) > 0
        assert rows[0]["candidate_suppression_reason"] == "suppressed_by_sleep_mode"
        assert rows[0]["proposal_generated"] == 0
        assert "suppressed by sleep mode" in rows[0]["no_action_reason"]

        # Verify no BUY proposal was generated in database
        proposals = temp_storage.fetch_all("SELECT * FROM trade_proposals WHERE symbol='SPY' AND side='buy'")
        assert len(proposals) == 0

def test_emergency_exit_risk_scoring(temp_storage, base_config):
    broker = MockBroker()
    service = TradingService(base_config, temp_storage, broker, "test_run_id")
    service.telegram = MockTelegramBot()

    indicators = {"volatility_20": 0.30} # elevated regime -> 4 points

    # Mock features to return ma_50 = 105, ma_200 = 110, close = 100
    mock_features = pd.DataFrame([{
        "close": 100.0,
        "ma_50": 105.0,
        "ma_200": 110.0
    }, {
        "close": 98.0,
        "ma_50": 104.5,
        "ma_200": 109.8
    }])

    # 1. Test basic calculations with valid entry price
    # Drawdown = (98 - 100)/100 = -2.0% -> 0 points
    # Trend = close (98) < ma_200 (109.8) -> 20 points
    # Adverse Move ATR = (100 - 98) / ATR.
    # Let's say ATR is calculated as 2.0. Adverse Move ATR = 1.0 -> 10 points
    # Volatility = 0.30 -> 4 points
    # Near Close = broker clock timestamp difference is not set -> 0 points
    # Quality = price age not in snapshot -> 0 points, position verified -> 3 points, no conflict -> 2 points = 5 points
    
    bars = broker.get_historical_bars("SPY", "1Day", 250)
    with patch("app.features.build_features", return_value=mock_features):
        score, breakdown, triggered, reason = service.calculate_emergency_exit_risk_score(
            "SPY", -0.02, 100.0, 98.0, indicators, bars
        )
        assert score > 0
        assert breakdown["trend_points"] == 20
        assert breakdown["vol_points"] == 4

    # 2. Test ATR fallback when high/low columns are missing
    bars_no_hl = pd.DataFrame({"close": [100.0] * 5})
    # ATR fallback: atr_value = current_price * (vol_20 / sqrt(252))
    # atr_value = 100.0 * (0.30 / 15.8745) = 1.889
    with patch("app.features.build_features", return_value=mock_features):
        score, breakdown, triggered, reason = service.calculate_emergency_exit_risk_score(
            "SPY", -0.02, 100.0, 98.0, indicators, bars_no_hl
        )
        assert breakdown["is_atr_proxy"] is True
        assert breakdown["atr_value"] > 0

    # 3. Test missing average entry price fallback
    # If avg entry price is 0/None, drawdown points = 35, adverse points = 15
    with patch("app.features.build_features", return_value=mock_features):
        score, breakdown, triggered, reason = service.calculate_emergency_exit_risk_score(
            "SPY", 0.0, 0.0, 98.0, indicators, bars
        )
        assert breakdown["drawdown_points"] == 35
        assert breakdown["adverse_points"] == 15

def test_emergency_exit_triggers_and_modes(temp_storage, base_config):
    broker = MockBroker()
    service = TradingService(base_config, temp_storage, broker, "test_run_id")
    service.telegram = MockTelegramBot()

    # Active paper position in broker - setup for Extreme Mode (-15% drawdown)
    broker.price = 85.0
    broker.positions = [type("Pos", (), {"symbol": "SPY", "qty": 10, "avg_entry_price": 100.0, "current_price": 85.0})()]

    # Make the broker clock show < 30 minutes to close -> 10 points near close
    broker.clock.timestamp = datetime.now(UTC)
    broker.clock.next_close = broker.clock.timestamp + timedelta(minutes=15)

    # Mock evaluate_symbol to return a normal HOLD signal with high volatility (0.48 -> extreme regime -> 10 points)
    def mock_eval(*args, **kwargs):
        return Signal("HOLD", None, "SPY", "No signal", 0.0, {"volatility_20": 0.48})

    # Mock features to trigger emergency exit: close = 85 (drawdown -15%), ma_50 = 100, ma_200 = 105
    mock_features = pd.DataFrame([{
        "close": 85.0,
        "ma_50": 100.0,
        "ma_200": 105.0
    }])

    # 1. Extreme Mode: drawdown <= -12% -> triggers immediate execution
    with patch("app.service.evaluate_symbol", mock_eval), patch("app.features.build_features", return_value=mock_features), patch.object(service, "revalidate_and_execute_emergency_exit", return_value=(True, "submitted")) as mock_exec:
        service.scan()
        
        # Verify proposal created
        props = temp_storage.fetch_all("SELECT * FROM trade_proposals WHERE symbol='SPY' AND emergency_exit_triggered=1")
        assert len(props) == 1
        assert props[0]["emergency_exit_mode"] == "extreme"
        assert props[0]["emergency_exit_wait_seconds"] == 0
        assert props[0]["emergency_exit_final_decision"] == "submitted"
        assert props[0]["status"] == "approved"
        
        mock_exec.assert_called_once()
        assert any("EXTREME EMERGENCY EXIT" in m[0] for m in service.telegram.sent_messages)

    # Reset
    temp_storage.execute("DELETE FROM trade_proposals")
    service.telegram.sent_messages.clear()
    
    # Setup for Normal Mode (-9% drawdown)
    broker.price = 91.0
    broker.positions = [type("Pos", (), {"symbol": "SPY", "qty": 10, "avg_entry_price": 100.0, "current_price": 91.0})()] # -9% drawdown

    mock_features_normal = pd.DataFrame([{
        "close": 91.0,
        "ma_50": 100.0,
        "ma_200": 105.0
    }])
    
    # 2. Normal Mode: wait 60s
    with patch("app.service.evaluate_symbol", mock_eval), patch("app.features.build_features", return_value=mock_features_normal):
        service.scan()
        props = temp_storage.fetch_all("SELECT * FROM trade_proposals WHERE symbol='SPY' AND emergency_exit_triggered=1")
        assert len(props) == 1
        assert props[0]["emergency_exit_mode"] == "normal"
        assert props[0]["emergency_exit_wait_seconds"] == 60
        assert props[0]["status"] == "pending"
        assert any("auto-execute in 60 seconds" in m[0] for m in service.telegram.sent_messages)

    # 3. Sleep Mode: sleep_mode_active = 1 -> wait 15s
    temp_storage.execute("DELETE FROM trade_proposals")
    service.telegram.sent_messages.clear()
    temp_storage.set_control_state("sleep_mode_active", "1", "test", "test", "test", 1, 1, int(time.time()))
    with patch("app.service.evaluate_symbol", mock_eval), patch("app.features.build_features", return_value=mock_features_normal):
        service.scan()
        props = temp_storage.fetch_all("SELECT * FROM trade_proposals WHERE symbol='SPY' AND emergency_exit_triggered=1")
        assert len(props) == 1
        assert props[0]["emergency_exit_mode"] == "sleep"
        assert props[0]["emergency_exit_wait_seconds"] == 15
        assert props[0]["status"] == "pending"
        assert any("Auto-executing in 15 seconds" in m[0] for m in service.telegram.sent_messages)

def test_gpt_exit_explanation_timeout(temp_storage, base_config):
    broker = MockBroker()
    service = TradingService(base_config, temp_storage, broker, "test_run_id")

    # 1. GPT returns response in time
    service.ai.review = MagicMock(return_value={"gpt_confidence": "High", "gpt_caution": "Low", "main_risk": "Vol", "telegram_message": "Exit text"})
    res = service.get_gpt_exit_explanation({"symbol": "SPY"})
    assert res["status"] == "Completed"
    assert res["telegram_message"] == "Exit text"

    # 2. GPT times out
    def slow_review(*args, **kwargs):
        time.sleep(0.5)
        return {"telegram_message": "Too slow"}
    service.ai.review = slow_review
    res = service.get_gpt_exit_explanation({"symbol": "SPY"}, timeout=0.01)
    assert "Not available" in res["status"]
    assert res["telegram_message"] is None

def test_revalidate_and_execute_emergency_exit(temp_storage, base_config):
    broker = MockBroker()
    service = TradingService(base_config, temp_storage, broker, "test_run_id")
    service.telegram = MockTelegramBot()

    proposal = {
        "id": "prop_emerg_123",
        "symbol": "SPY",
        "qty": 10.0,
        "latest_price": 100.0
    }

    # Setup matching position in broker
    broker.positions = [type("Pos", (), {"symbol": "SPY", "qty": 10.0})()]
    broker.price = 100.0
    broker.price_time = datetime.now(UTC)

    # 1. Revalidation passes -> executes successfully
    success, desc = service.revalidate_and_execute_emergency_exit(proposal)
    assert success is True
    assert desc == "submitted"
    orders = temp_storage.fetch_all("SELECT * FROM orders WHERE proposal_id='prop_emerg_123'")
    assert len(orders) == 1
    assert orders[0]["qty"] == 10.0

    # 2. Blocked by live trading configuration
    temp_storage.execute("DELETE FROM orders")
    base_config["mode"] = "live"
    base_config["live_enabled"] = True
    success, desc = service.revalidate_and_execute_emergency_exit(proposal)
    assert success is False
    assert "live" in desc


def test_emergency_exit_missing_entry_price(temp_storage, base_config):
    broker = MockBroker()
    service = TradingService(base_config, temp_storage, broker, "test_run_id")
    service.telegram = MockTelegramBot()

    # Active paper position in broker with missing entry price (avg_entry_price = 0/None)
    broker.price = 85.0
    broker.positions = [type("Pos", (), {"symbol": "SPY", "qty": 10, "avg_entry_price": 0.0, "current_price": 85.0})()]

    # Make the broker clock show < 30 minutes to close -> 10 points near close
    broker.clock.timestamp = datetime.now(UTC)
    broker.clock.next_close = broker.clock.timestamp + timedelta(minutes=15)

    def mock_eval(*args, **kwargs):
        return Signal("HOLD", None, "SPY", "No signal", 0.0, {"volatility_20": 0.48})

    mock_features = pd.DataFrame([{
        "close": 85.0,
        "ma_50": 100.0,
        "ma_200": 105.0
    }])

    # 1. Missing entry price + no fills -> blocked, no order submitted, audit event created
    with patch("app.service.evaluate_symbol", mock_eval), patch("app.features.build_features", return_value=mock_features), patch.object(service, "revalidate_and_execute_emergency_exit", return_value=(True, "submitted")) as mock_exec:
        service.scan()
        
        # Verify proposal created is blocked
        props = temp_storage.fetch_all("SELECT * FROM trade_proposals WHERE symbol='SPY' AND emergency_exit_triggered=1")
        assert len(props) == 1
        assert props[0]["status"] == "blocked"
        assert props[0]["emergency_exit_block_reason"] == "emergency_drawdown_unavailable"
        assert props[0]["emergency_exit_final_decision"] == "blocked"
        
        # revalidate_and_execute_emergency_exit should NOT be called
        mock_exec.assert_not_called()
        
        # Alert message should be sent
        assert any("drawdown could not be reliably calculated" in m[0] for m in service.telegram.sent_messages)
        
        # Audit event created
        audits = temp_storage.fetch_all("SELECT * FROM audit_events WHERE event_type='emergency_exit_blocked_drawdown_unavailable'")
        assert len(audits) == 1

    # 2. Reliable fills fallback works
    temp_storage.execute("DELETE FROM trade_proposals")
    temp_storage.execute("DELETE FROM audit_events")
    service.telegram.sent_messages.clear()
    
    # Insert a buy fill price of 100.0
    temp_storage.execute(
        "INSERT INTO orders(id,run_id,proposal_id,broker_order_id,client_order_id,symbol,side,qty,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("order123", "test_run_id", "prop123", "b123", "c123", "SPY", "buy", 10.0, "filled", datetime.now(UTC).isoformat(), datetime.now(UTC).isoformat())
    )
    temp_storage.execute(
        "INSERT INTO fills(run_id,order_id,qty,price,filled_at) VALUES(?,?,?,?,?)",
        ("test_run_id", "order123", 10.0, 100.0, datetime.now(UTC).isoformat())
    )

    with patch("app.service.evaluate_symbol", mock_eval), patch("app.features.build_features", return_value=mock_features), patch.object(service, "revalidate_and_execute_emergency_exit", return_value=(True, "submitted")) as mock_exec:
        service.scan()
        
        # Since avg_entry_price fallback found (100.0) -> drawdown is calculated -> triggers extreme auto-exit
        props = temp_storage.fetch_all("SELECT * FROM trade_proposals WHERE symbol='SPY' AND emergency_exit_triggered=1")
        assert len(props) == 1
        assert props[0]["status"] == "approved"
        assert props[0]["emergency_exit_mode"] == "extreme"
        
        mock_exec.assert_called_once()
        assert any("EXTREME EMERGENCY EXIT" in m[0] for m in service.telegram.sent_messages)

    # 3. Broker average entry price is preferred over fills fallback
    temp_storage.execute("DELETE FROM trade_proposals")
    service.telegram.sent_messages.clear()
    
    # Set broker position avg_entry_price to 120.0 (fills price is 100.0)
    broker.price = 114.0 # drawdown (114 - 120)/120 = -5% (not triggering extreme mode exit drawdown <= -12%)
    broker.positions = [type("Pos", (), {"symbol": "SPY", "qty": 10, "avg_entry_price": 120.0, "current_price": 114.0})()]
    mock_features_pref = pd.DataFrame([{
        "close": 114.0,
        "ma_50": 120.0,
        "ma_200": 125.0
    }])

    with patch("app.service.evaluate_symbol", mock_eval), patch("app.features.build_features", return_value=mock_features_pref), patch.object(service, "revalidate_and_execute_emergency_exit", return_value=(True, "submitted")) as mock_exec:
        service.scan()
        
        # Verify that since avg_entry_price of 120.0 is used (preferred over fills 100.0),
        # drawdown is -5% which does not match any hard triggers -> no emergency exit triggered!
        props = temp_storage.fetch_all("SELECT * FROM trade_proposals WHERE symbol='SPY' AND emergency_exit_triggered=1")
        assert len(props) == 0
