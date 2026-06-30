import json
import uuid
import pytest
from datetime import datetime, UTC, timedelta
import pandas as pd
from unittest.mock import MagicMock, patch

from app.utils import format_proposal_message, translate_reason
from app.storage import Storage
from app.service import TradingService
from app.strategy_rule_based import Signal
from app.execution import ExecutionResult

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

    def get_historical_bars(self, symbol, timeframe, limit):
        data = {"close": [100.0] * limit, "volume": [10000.0] * limit}
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
            "max_trades_per_day": 1,
            "max_open_positions": 1,
            "min_historical_bars": 10,
            "max_price_age_seconds": 120,
            "require_gpt_review_for_buy_proposals": True,
            "approved_strategy_versions": ["rule_based_v1"]
        },
        "watchlist": ["SPY", "QQQ", "DIA"],
        "telegram": {
            "approval_enabled": True,
            "telegram_approval_listener_enabled": True,
            "telegram_approval_poll_interval_seconds": 30,
            "telegram_approval_listener_mode": "approval_only",
            "market_scan_processes_telegram_updates": False,
            "approval_price_refresh_required": True,
            "approval_max_price_age_seconds": 60,
            "approval_max_price_move_bps": 25,
            "allow_plain_yes_only_when_single_pending_proposal": True
        }
    }

@pytest.fixture(autouse=True)
def mock_system_dependencies():
    with patch("app.service.get_power_status") as mock_power, \
         patch("app.service.internet_available") as mock_internet:
        mock_power.return_value = type("P", (), {"connected": True})()
        mock_internet.return_value = True
        yield

def test_proposal_message_ranking_and_ai_wording():
    # Verify ranking and AI block formatting in proposal message
    config = {
        "mode": "paper",
        "risk": {
            "require_gpt_review_for_buy_proposals": True
        }
    }
    
    # 1. AI Review completed BUY proposal
    proposal = {
        "symbol": "DIA",
        "side": "buy",
        "notional": 5.0,
        "score": 85.0,
        "watchlist_order": 3,
        "true_score_rank": 3,
        "total_active_symbols": 4,
        "proposal_eligible_rank": 1,
        "selection_reason": "Selected because higher-ranked candidates were recently proposed and are still in cooldown.",
        "expires_at": "2026-06-18T18:06:20.493310+00:00",
        "gpt_called": True,
        "review": {
            "gpt_confidence": "High",
            "gpt_caution": "Low",
            "main_risk": "Short-term profit taking pressure."
        }
    }
    
    msg = format_proposal_message(proposal, config)
    assert "Watchlist order: #3 of 4" in msg
    assert "Score rank: #3 of 4 active ETFs" in msg
    assert "Eligible proposal rank: #1 currently eligible candidate" in msg
    assert "Selection reason: Selected because higher-ranked candidates were recently proposed and are still in cooldown." in msg
    assert "AI review: Completed" in msg
    assert "AI confidence: High" in msg
    assert "AI caution: Low" in msg
    assert "Main risk:\nShort-term profit taking pressure." in msg

    # 2. Rule-only mode or AI not available
    proposal_no_ai = {
        "symbol": "DIA",
        "side": "buy",
        "notional": 5.0,
        "score": 85.0,
        "watchlist_order": 1,
        "true_score_rank": 1,
        "total_active_symbols": 4,
        "proposal_eligible_rank": 1,
        "selection_reason": "Selected because it was the strongest eligible candidate.",
        "expires_at": "2026-06-18T18:06:20.493310+00:00",
        "gpt_called": False,
        "review": None
    }
    msg_no_ai = format_proposal_message(proposal_no_ai, config)
    assert "Watchlist order: #1 of 4" in msg_no_ai
    assert "Score rank: #1 of 4 active ETFs" in msg_no_ai
    assert "Eligible proposal rank: #1 currently eligible candidate" in msg_no_ai
    assert "Selection reason: Selected because it was the strongest eligible candidate." in msg_no_ai
    assert "AI review: Not available" in msg_no_ai
    assert "Rule-based only. AI review was not available. Treat with extra caution." in msg_no_ai

def test_telegram_listener_mode_lightweight(temp_storage, base_config):
    # Verify listener mode doesn't execute scans or GPT review
    broker = MockBroker()
    service = TradingService(base_config, temp_storage, broker, "run-1")
    
    # Mock TelegramBot to return some updates
    tg_mock = MagicMock()
    tg_mock.get_updates.return_value = []
    service.telegram = tg_mock
    
    # Spy/Mock scan and ai reviewer to verify they are not called
    service.scan = MagicMock()
    service.ai.review = MagicMock()
    
    service.process_telegram()
    
    # Verify process_telegram ran but scan and AI review were NOT touched
    tg_mock.get_updates.assert_called()
    service.scan.assert_not_called()
    service.ai.review.assert_not_called()

def test_yes_acknowledgement_and_stale_price_block(temp_storage, base_config):
    # YES reply to a proposal with stale price blocks the execution with clear message
    broker = MockBroker()
    
    # Set the broker price time to be stale (e.g. 70 seconds ago, config allows 60)
    broker.price_time = datetime.now(UTC) - timedelta(seconds=70)
    
    service = TradingService(base_config, temp_storage, broker, "run-1")
    service.telegram = MagicMock()
    service.telegram.allowed_user_id = "12345"
    service.telegram.is_authorized.return_value = True
    
    # Create a pending proposal in database
    pid = "proposal-1"
    temp_storage.execute(
        "INSERT INTO trade_proposals(id,run_id,signal_id,symbol,side,notional,status,created_at,expires_at,strategy_version,payload) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (pid, "run-1", "sig-1", "DIA", "buy", 5.0, "pending", datetime.now(UTC).isoformat(), (datetime.now(UTC) + timedelta(minutes=15)).isoformat(), "rule_based_v1", json.dumps({"symbol": "DIA", "side": "buy", "notional": 5.0, "latest_price": 100.0, "price_at": datetime.now(UTC).isoformat(), "historical_bars": 100, "volume": 10000.0, "reason": "filters normal"}))
    )
    
    # Mock Telegram message reply
    update = {
        "update_id": 100,
        "message": {
            "message_id": 200,
            "text": "yes",
            "from": {"id": "12345"},
            "reply_to_message": {"message_id": 999}  # Match proposal telegram msg id
        }
    }
    # Update proposal with telegram message id
    temp_storage.execute("UPDATE trade_proposals SET telegram_message_id='999' WHERE id=?", (pid,))
    
    service.telegram.get_updates.side_effect = [[update], []]
    
    service.process_telegram()
    
    # Verify YES receipt was immediately acknowledged first
    service.telegram.send_message.assert_any_call("✅ Received: YES for DIA paper buy proposal. I will now run the final safety check. No order will be placed unless the final check passes.")
    
    expected_block_msg = "No order placed for DIA. Final validation could not get a fresh Alpaca price within the allowed window. A new proposal is required."
    service.telegram.send_message.assert_any_call(expected_block_msg)
    
    # Verify approvals row was updated with audit details
    approval = temp_storage.fetch_all("SELECT * FROM approvals WHERE proposal_id=?", (pid,))[0]
    assert approval["acknowledgement_status"] == "blocked"
    assert approval["final_order_decision"] == "blocked"
    assert "could not get a fresh Alpaca price" in approval["final_block_reason"]
    assert approval["refreshed_price"] == 100.0
    assert approval["refreshed_price_age_seconds"] >= 70.0

def test_yes_acknowledgement_and_price_move_block(temp_storage, base_config):
    # YES reply with fresh price but price moves too much blocks the execution
    broker = MockBroker()
    # proposal price is 100.0, let's make refreshed price 101.0 (1% move = 100 bps, limit is 25 bps)
    broker.price = 101.0
    broker.price_time = datetime.now(UTC) # fresh
    
    service = TradingService(base_config, temp_storage, broker, "run-1")
    service.telegram = MagicMock()
    service.telegram.allowed_user_id = "12345"
    service.telegram.is_authorized.return_value = True
    
    pid = "proposal-2"
    temp_storage.execute(
        "INSERT INTO trade_proposals(id,run_id,signal_id,symbol,side,notional,status,created_at,expires_at,strategy_version,payload) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (pid, "run-1", "sig-1", "DIA", "buy", 5.0, "pending", datetime.now(UTC).isoformat(), (datetime.now(UTC) + timedelta(minutes=15)).isoformat(), "rule_based_v1", json.dumps({"symbol": "DIA", "side": "buy", "notional": 5.0, "latest_price": 100.0, "price_at": datetime.now(UTC).isoformat(), "historical_bars": 100, "volume": 10000.0, "reason": "filters normal"}))
    )
    
    update = {
        "update_id": 101,
        "message": {
            "message_id": 201,
            "text": "yes",
            "from": {"id": "12345"},
            "reply_to_message": {"message_id": 999}
        }
    }
    temp_storage.execute("UPDATE trade_proposals SET telegram_message_id='999' WHERE id=?", (pid,))
    
    service.telegram.get_updates.side_effect = [[update], []]
    
    service.process_telegram()
    
    # Verify YES receipt acknowledged
    service.telegram.send_message.assert_any_call("✅ Received: YES for DIA paper buy proposal. I will now run the final safety check. No order will be placed unless the final check passes.")
    
    # Verify blocked due to price movement
    approval = temp_storage.fetch_all("SELECT * FROM approvals WHERE proposal_id=?", (pid,))[0]
    assert approval["final_order_decision"] == "blocked"
    assert "Price moved too much" in approval["final_block_reason"]
    assert approval["price_move_bps_since_proposal"] == 100.0

def test_yes_acknowledgement_and_successful_execution(temp_storage, base_config):
    # YES reply with fresh price and safe movement executes paper order successfully
    broker = MockBroker()
    broker.price = 100.1 # 10 bps move (limit is 25)
    broker.price_time = datetime.now(UTC) # fresh
    
    service = TradingService(base_config, temp_storage, broker, "run-1")
    service.telegram = MagicMock()
    service.telegram.allowed_user_id = "12345"
    service.telegram.is_authorized.return_value = True
    
    pid = "proposal-3"
    temp_storage.execute(
        "INSERT INTO trade_proposals(id,run_id,signal_id,symbol,side,notional,status,created_at,expires_at,strategy_version,payload) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (pid, "run-1", "sig-1", "DIA", "buy", 5.0, "pending", datetime.now(UTC).isoformat(), (datetime.now(UTC) + timedelta(minutes=15)).isoformat(), "rule_based_v1", json.dumps({"symbol": "DIA", "side": "buy", "notional": 5.0, "latest_price": 100.0, "price_at": datetime.now(UTC).isoformat(), "historical_bars": 100, "volume": 10000.0, "reason": "filters normal"}))
    )
    
    update = {
        "update_id": 102,
        "message": {
            "message_id": 202,
            "text": "yes",
            "from": {"id": "12345"},
            "reply_to_message": {"message_id": 999}
        }
    }
    temp_storage.execute("UPDATE trade_proposals SET telegram_message_id='999' WHERE id=?", (pid,))
    
    service.telegram.get_updates.side_effect = [[update], []]
    
    service.process_telegram()
    
    # Verify YES receipt
    service.telegram.send_message.assert_any_call("✅ Received: YES for DIA paper buy proposal. I will now run the final safety check. No order will be placed unless the final check passes.")
    
    approval = temp_storage.fetch_all("SELECT * FROM approvals WHERE proposal_id=?", (pid,))[0]
    service.telegram.send_message.assert_any_call("✅ Paper order submitted: NEW ENTRY DIA for $5.00. Approved: $5.00. Final: $5.00. Price: $100.10 (approx 0.0500 shares). Mode: paper only.")
    
    # Verify approvals row fields
    assert approval["final_order_decision"] == "submitted"
    assert approval["refreshed_price"] == 100.1
    assert abs(approval["price_move_bps_since_proposal"] - 10.0) < 1e-4

def test_no_acknowledgement(temp_storage, base_config):
    # Verify NO reply is acknowledged immediately and rejects the proposal
    broker = MockBroker()
    service = TradingService(base_config, temp_storage, broker, "run-1")
    service.telegram = MagicMock()
    service.telegram.allowed_user_id = "12345"
    service.telegram.is_authorized.return_value = True
    
    pid = "proposal-4"
    temp_storage.execute(
        "INSERT INTO trade_proposals(id,run_id,signal_id,symbol,side,notional,status,created_at,expires_at,strategy_version,payload) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (pid, "run-1", "sig-1", "DIA", "buy", 5.0, "pending", datetime.now(UTC).isoformat(), (datetime.now(UTC) + timedelta(minutes=15)).isoformat(), "rule_based_v1", json.dumps({"symbol": "DIA", "side": "buy", "notional": 5.0, "latest_price": 100.0}))
    )
    
    update = {
        "update_id": 103,
        "message": {
            "message_id": 203,
            "text": "no",
            "from": {"id": "12345"},
            "reply_to_message": {"message_id": 999}
        }
    }
    temp_storage.execute("UPDATE trade_proposals SET telegram_message_id='999' WHERE id=?", (pid,))
    
    service.telegram.get_updates.side_effect = [[update], []]
    
    service.process_telegram()
    
    # Verify NO message immediately acknowledged
    service.telegram.send_message.assert_called_once_with("❌ Received: NO for DIA paper buy proposal. Proposal rejected. No order will be placed.")
    
    # Verify proposal rejected in db
    prop = temp_storage.fetch_all("SELECT * FROM trade_proposals WHERE id=?", (pid,))[0]
    assert prop["status"] == "rejected"

def test_expired_acknowledgement(temp_storage, base_config):
    # Verify reply to expired proposal is acknowledged correctly
    broker = MockBroker()
    service = TradingService(base_config, temp_storage, broker, "run-1")
    service.telegram = MagicMock()
    service.telegram.allowed_user_id = "12345"
    service.telegram.is_authorized.return_value = True
    
    pid = "proposal-5"
    temp_storage.execute(
        "INSERT INTO trade_proposals(id,run_id,signal_id,symbol,side,notional,status,created_at,expires_at,strategy_version,payload) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (pid, "run-1", "sig-1", "DIA", "buy", 5.0, "expired", datetime.now(UTC).isoformat(), (datetime.now(UTC) - timedelta(minutes=1)).isoformat(), "rule_based_v1", json.dumps({"symbol": "DIA", "side": "buy", "notional": 5.0, "latest_price": 100.0}))
    )
    
    update = {
        "update_id": 104,
        "message": {
            "message_id": 204,
            "text": "yes",
            "from": {"id": "12345"},
            "reply_to_message": {"message_id": 999}
        }
    }
    temp_storage.execute("UPDATE trade_proposals SET telegram_message_id='999' WHERE id=?", (pid,))
    
    service.telegram.get_updates.side_effect = [[update], []]
    
    service.process_telegram()
    
    # Verify expired acknowledgement sent
    service.telegram.send_message.assert_called_once_with("⏳ This proposal has already expired. No order was placed.")

def test_ambiguous_plain_yes_multiple_pending(temp_storage, base_config):
    # Plain yes when multiple proposals pending should return ambiguous warning
    broker = MockBroker()
    service = TradingService(base_config, temp_storage, broker, "run-1")
    service.telegram = MagicMock()
    service.telegram.allowed_user_id = "12345"
    service.telegram.is_authorized.return_value = True
    
    # Insert two pending proposals
    temp_storage.execute(
        "INSERT INTO trade_proposals(id,run_id,signal_id,symbol,side,notional,status,created_at,expires_at,strategy_version,payload) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("prop-1", "run-1", "sig-1", "DIA", "buy", 5.0, "pending", datetime.now(UTC).isoformat(), (datetime.now(UTC) + timedelta(minutes=15)).isoformat(), "rule_based_v1", json.dumps({"symbol": "DIA", "side": "buy", "notional": 5.0}))
    )
    temp_storage.execute(
        "INSERT INTO trade_proposals(id,run_id,signal_id,symbol,side,notional,status,created_at,expires_at,strategy_version,payload) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("prop-2", "run-1", "sig-2", "QQQ", "buy", 5.0, "pending", datetime.now(UTC).isoformat(), (datetime.now(UTC) + timedelta(minutes=15)).isoformat(), "rule_based_v1", json.dumps({"symbol": "QQQ", "side": "buy", "notional": 5.0}))
    )
    
    update = {
        "update_id": 105,
        "message": {
            "message_id": 205,
            "text": "yes",
            "from": {"id": "12345"}
        }
    }
    
    service.telegram.get_updates.side_effect = [[update], []]
    
    service.process_telegram()
    
    # Verify ambiguous warning is sent
    service.telegram.send_message.assert_called_once_with("I found multiple pending proposals. Please reply directly to the proposal message, or include the symbol/proposal ID.")

def test_live_trading_and_auto_execution_hard_blocked(temp_storage, base_config):
    # Verify live trading and auto-execution remain strictly blocked
    broker = MockBroker()
    
    # Test case 1: live trading disabled by default when mode=live but live_enabled is False
    base_config["mode"] = "live"
    base_config["live_enabled"] = False
    
    service = TradingService(base_config, temp_storage, broker, "run-1")
    service.telegram = MagicMock()
    service.telegram.allowed_user_id = "12345"
    service.telegram.is_authorized.return_value = True
    service.telegram.get_updates.return_value = [
        {
            "update_id": 106,
            "message": {
                "message_id": 206,
                "text": "yes",
                "from": {"id": "12345"}
            }
        }
    ]
    
    service.process_telegram()
    service.telegram.send_message.assert_any_call("Blocked for safety: live trading is disabled.")
    
    # Test case 2: even if live_enabled is True, RiskEngine blocks it
    base_config["live_enabled"] = True
    service2 = TradingService(base_config, temp_storage, broker, "run-2")
    service2.telegram = MagicMock()
    service2.telegram.allowed_user_id = "12345"
    service2.telegram.is_authorized.return_value = True
    
    pid = "proposal-live"
    temp_storage.execute(
        "INSERT INTO trade_proposals(id,run_id,signal_id,symbol,side,notional,status,created_at,expires_at,strategy_version,payload) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (pid, "run-2", "sig-live", "DIA", "buy", 5.0, "pending", datetime.now(UTC).isoformat(), (datetime.now(UTC) + timedelta(minutes=15)).isoformat(), "rule_based_v1", json.dumps({"symbol": "DIA", "side": "buy", "notional": 5.0, "latest_price": 100.0, "price_at": datetime.now(UTC).isoformat(), "historical_bars": 100, "volume": 10000.0, "reason": "filters normal"}))
    )
    
    update = {
        "update_id": 107,
        "message": {
            "message_id": 207,
            "text": "yes",
            "from": {"id": "12345"},
            "reply_to_message": {"message_id": 999}
        }
    }
    temp_storage.execute("UPDATE trade_proposals SET telegram_message_id='999' WHERE id=?", (pid,))
    service2.telegram.get_updates.side_effect = [[update], []]
    service2.process_telegram()
    
    # Verify it got blocked by RiskEngine
    approval = temp_storage.fetch_all("SELECT * FROM approvals WHERE proposal_id=?", (pid,))[0]
    assert approval["final_order_decision"] == "blocked"
    assert "this build supports paper mode only" in approval["final_block_reason"]
    
    # Auto-execution blocked assert test
    proposal = {"symbol": "DIA", "side": "buy", "score": 95}
    assert service. _should_auto_execute(proposal) is False
