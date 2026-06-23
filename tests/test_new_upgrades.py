import json
import uuid
import sys
import time
import pytest
import re
from datetime import datetime, UTC, timedelta
from unittest.mock import MagicMock, patch
import pandas as pd

from app.storage import Storage
from app.service import TradingService
from app.utils import redact_sensitive_url
from app.telegram_bot import TelegramBot
from tests.test_sleep_and_emergency import MockBroker, temp_storage, base_config

class FakeTelegramBot:
    def __init__(self):
        self.updates = []
        self.sent_messages = []
        self.chat_id = "12345"
        self.allowed_user_id = "authorized_user_123"

    def get_updates(self, offset=None, timeout=0):
        return self.updates

    def send_message(self, text, chat_id=None):
        self.sent_messages.append((text, chat_id or self.chat_id))
        return type("M", (), {"message_id": 999})()

    def is_authorized(self, sender_id):
        return sender_id == "authorized_user_123"

    def handle_command(self, text, sender):
        return f"Handled {text}"
        
    def is_available(self, *args, **kwargs):
        return True

def test_telegram_duplicate_and_fallback_routing(temp_storage, base_config):
    # Add DIA to watchlist so risk check approved_universe passes
    base_config["watchlist"].append("DIA")
    
    broker = MockBroker()
    service = TradingService(base_config, temp_storage, broker, "test_run_id")
    service.telegram = FakeTelegramBot()
    
    now_ref = 1729000000.0
    service.listener_started_at = now_ref - 100.0
    
    # Insert a trade proposal
    proposal_id = "test-prop-id"
    payload_dict = {
        "id": proposal_id,
        "symbol": "DIA",
        "side": "buy",
        "action": "entry",
        "latest_price": 100.0,
        "price_at": (datetime.now(UTC) - timedelta(seconds=10)).isoformat(),
        "historical_bars": 100,
        "volume": 10000,
        "notional": 5.0,
        "created_at": (datetime.now(UTC) - timedelta(seconds=10)).isoformat(),
        "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
        "strategy_version": "rule_based_v1",
        "reason": "test_setup",
        "order_type": "market",
        "asset_class": "equity"
    }
    temp_storage.execute(
        "INSERT INTO trade_proposals(id,run_id,symbol,side,notional,status,expires_at,created_at,telegram_message_id,payload,strategy_version) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (proposal_id, "test_run_id", "DIA", "buy", 5.0, "pending", payload_dict["expires_at"], payload_dict["created_at"], "1001", json.dumps(payload_dict), "rule_based_v1")
    )
    
    # 1. Reply-to YES that submits order sends YES received, order submitted, and NO ambiguity fallback
    service.telegram.updates = [{
        "update_id": 1001,
        "message": {
            "message_id": 2001,
            "date": int(now_ref - 5),
            "text": "yes",
            "from": {"id": "authorized_user_123"},
            "chat": {"id": "12345"},
            "reply_to_message": {
                "message_id": 1001
            }
        }
    }]
    
    broker.open = True
    broker.price = 100.0
    broker.price_time = datetime.now(UTC)
    
    with patch("time.time", return_value=now_ref):
        service.process_telegram()
        
    # Check messages sent
    msgs = [m[0] for m in service.telegram.sent_messages]
    print("Messages sent:", msgs)
    assert any("Received: YES" in m for m in msgs)
    assert any("Paper order submitted" in m for m in msgs)
    assert not any("could not match your reply" in m for m in msgs)
    assert not any("already handled earlier" in m for m in msgs)
    
    # 2. Second processing of the duplicate update ID is ignored by processed_update_ids set & database check
    service.telegram.sent_messages.clear()
    # If the exact same update ID is delivered again in the same list
    service.telegram.updates = [
        {
            "update_id": 1001,
            "message": {
                "message_id": 2001,
                "date": int(now_ref - 5),
                "text": "yes",
                "from": {"id": "authorized_user_123"},
                "chat": {"id": "12345"}
            }
        },
        {
            "update_id": 1001,
            "message": {
                "message_id": 2001,
                "date": int(now_ref - 5),
                "text": "yes",
                "from": {"id": "authorized_user_123"},
                "chat": {"id": "12345"}
            }
        }
    ]
    with patch("time.time", return_value=now_ref):
        service.process_telegram()
    # It should be ignored because the database level duplicate prevention flags it
    assert len(service.telegram.sent_messages) == 0

    # 3. Plain YES with no pending proposals but recent approval responds "already handled earlier" rather than fallback
    # Create another proposal and approval for the fallback check
    proposal_id2 = "test-prop-id2"
    payload_dict2 = {**payload_dict, "id": proposal_id2, "symbol": "QQQ"}
    base_config["watchlist"].append("QQQ")
    temp_storage.execute(
        "INSERT INTO trade_proposals(id,run_id,symbol,side,notional,status,expires_at,created_at,telegram_message_id,payload,strategy_version) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (proposal_id2, "test_run_id", "QQQ", "buy", 5.0, "approved", payload_dict2["expires_at"], payload_dict2["created_at"], "1002", json.dumps(payload_dict2), "rule_based_v1")
    )
    
    service.telegram.sent_messages.clear()
    service.telegram.updates = [{
        "update_id": 1002,
        "message": {
            "message_id": 2002,
            "date": int(now_ref),
            "text": "yes",
            "from": {"id": "authorized_user_123"},
            "chat": {"id": "12345"}
        }
    }]
    # Insert approval
    temp_storage.execute(
        "INSERT INTO approvals(id,run_id,proposal_id,sender_id,raw_message,parsed_action,authorized,status,created_at,approval_received_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("app2", "test_run_id", proposal_id2, "authorized_user_123", "yes", "approve", 1, "consumed", datetime.now(UTC).isoformat(), datetime.now(UTC).isoformat())
    )
    
    with patch("time.time", return_value=now_ref):
        service.process_telegram()
        
    msgs = [m[0] for m in service.telegram.sent_messages]
    print("Messages sent for plain yes duplicate:", msgs)
    assert any("already handled earlier" in m for m in msgs)
    assert not any("could not match your reply" in m for m in msgs)


def test_explicit_buy_guardrails(temp_storage, base_config):
    # Enable the new gates in the config
    base_config["risk"]["allow_add_to_existing_position"] = False
    base_config["risk"]["block_new_buys_when_any_position_open"] = True
    base_config["risk"]["block_new_buys_after_buy_order_submitted_today"] = True
    base_config["risk"]["block_same_symbol_rebuy_while_position_open"] = True
    base_config["risk"]["max_trades_per_day"] = 1
    base_config["risk"]["max_open_positions"] = 1
    
    broker = MockBroker()
    service = TradingService(base_config, temp_storage, broker, "test_run_id")
    
    # 1. Holding DIA position blocks another DIA BUY proposal
    # Put a position in the broker
    broker.positions = [type("Pos", (), {"symbol": "DIA", "qty": 10, "avg_entry_price": 500.0, "current_price": 505.0})()]
    
    # Evaluate risk engine for a DIA buy entry proposal
    port_ctx = service._portfolio_context({"symbol": "DIA", "side": "buy"})
    mock_prop = {
        "symbol": "DIA",
        "side": "buy",
        "action": "entry",
        "latest_price": 505.0,
        "price_at": datetime.now(UTC).isoformat(),
        "historical_bars": 100,
        "volume": 10000,
        "notional": 5.0,
        "created_at": datetime.now(UTC).isoformat(),
        "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
        "strategy_version": "rule_based_v1",
        "reason": "test"
    }
    
    decision = service._risk_engine("test_id", "proposal").evaluate(mock_prop, port_ctx)
    assert not decision.passed
    reasons = decision.reasons
    print("Reasons for DIA block:", reasons)
    assert any("adding to existing position is disabled" in r for r in reasons)
    assert any("new buys blocked when any position is open" in r for r in reasons)
    assert any("same symbol rebuy is blocked while position is open" in r for r in reasons)
    
    # 2. Holding DIA blocks SPY buy proposal when max_open_positions = 1
    port_ctx_spy = service._portfolio_context({"symbol": "SPY", "side": "buy"})
    mock_prop_spy = {**mock_prop, "symbol": "SPY"}
    decision_spy = service._risk_engine("test_id2", "proposal").evaluate(mock_prop_spy, port_ctx_spy)
    assert not decision_spy.passed
    assert any("new buys blocked when any position is open" in r for r in decision_spy.reasons)
    
    # 3. Submitted BUY today blocks further BUY proposals if max_trades_per_day = 1
    # Remove positions, but add an order to the orders table today
    broker.positions = []
    temp_storage.execute(
        "INSERT INTO orders(id,run_id,proposal_id,symbol,side,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
        ("order1", "test_run_id", "prop_dummy", "DIA", "buy", "filled", datetime.now(UTC).isoformat(), datetime.now(UTC).isoformat())
    )
    
    port_ctx_spy2 = service._portfolio_context({"symbol": "SPY", "side": "buy"})
    decision_spy2 = service._risk_engine("test_id3", "proposal").evaluate(mock_prop_spy, port_ctx_spy2)
    assert not decision_spy2.passed
    assert any("new buys blocked since a buy order was already submitted today" in r for r in decision_spy2.reasons)
    
    # 4. Sell/exit proposals remain allowed when holding a position
    broker.positions = [type("Pos", (), {"symbol": "DIA", "qty": 10, "avg_entry_price": 500.0, "current_price": 505.0})()]
    mock_exit_prop = {**mock_prop, "action": "exit", "side": "sell"}
    port_ctx_exit = service._portfolio_context({"symbol": "DIA", "side": "sell"})
    decision_exit = service._risk_engine("test_exit_id", "proposal").evaluate(mock_exit_prop, port_ctx_exit)
    # Ensure it's not blocked by the buy-specific checks
    assert not any("adding to existing position" in r for r in decision_exit.reasons)
    assert not any("new buys blocked" in r for r in decision_exit.reasons)


def test_digest_status_improvements(temp_storage, base_config):
    # Set up config with market profiles and digest settings
    base_config["market_profiles"] = {
        "usa": {
            "status": "active",
            "watchlist": ["SPY"],
            "observation_watchlist": ["XLV"],
            "proposals_enabled": True,
            "execution_enabled": True,
            "broker": "alpaca"
        }
    }
    base_config["digest"] = {
        "telegram_digest_enabled": True,
        "telegram_digest_min_cycles_required": 1,
        "telegram_digest_send_when_market_closed": True,
        "telegram_digest_interval_minutes": 30
    }
    broker = MockBroker()
    service = TradingService(base_config, temp_storage, broker, "test_run_id")
    
    # Insert memory records representing different gate violations
    window_start = datetime.now(UTC) - timedelta(minutes=10)
    
    # 1. XLV observation-only symbol
    temp_storage.execute(
        "INSERT INTO market_memory(run_id,market_profile,symbol,price,volatility,signal,score,classification,reason,proposal_allowed,proposal_generated,no_action_reason,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("run1", "usa", "XLV", 100.0, 0.15, "ENTRY", 70.0, "Moderate", "test", 0, 0, "symbol not in active watchlist", window_start.isoformat())
    )
    
    # 2. SPY score below threshold
    temp_storage.execute(
        "INSERT INTO market_memory(run_id,market_profile,symbol,price,volatility,signal,score,classification,reason,proposal_allowed,proposal_generated,no_action_reason,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("run1", "usa", "SPY", 100.0, 0.15, "ENTRY", 50.0, "Weak", "test", 0, 0, "trade score below threshold", window_start.isoformat())
    )
    
    # Trigger check_and_send_digest
    service.telegram = FakeTelegramBot()
    service.telegram.updates = []
    
    # We must patch is_market_open to return True and mock audit, run_cycle, etc.
    with patch.object(service.broker, "is_market_open", return_value=True), patch("time.time", return_value=time.time()):
        service.check_and_send_digest()
        
    msgs = [m[0] for m in service.telegram.sent_messages]
    print("Digest messages:", msgs)
    assert len(msgs) == 1
    digest_text = msgs[0]
    
    assert "XLV" in digest_text
    assert "Observation only — no proposal allowed" in digest_text
    assert "SPY" in digest_text
    assert "No proposal — score below threshold" in digest_text


def test_telegram_token_redaction():
    raw_token = "12345:abc"
    sensitive_url = f"https://api.telegram.org/bot{raw_token}/getUpdates"
    
    redacted = redact_sensitive_url(sensitive_url)
    assert raw_token not in redacted
    assert "bot<redacted_token>" in redacted
    
    # Test requests exceptions wrapping in TelegramBot
    bot = TelegramBot(token=raw_token, chat_id="123", allowed_user_id="123")
    
    # Mock requests.post to raise an error
    with patch("requests.post", side_effect=RuntimeError(f"Error connecting to {sensitive_url}")):
        with pytest.raises(RuntimeError) as exc_info:
            bot.get_updates()
        
        err_msg = str(exc_info.value)
        print("Scrubbed error message:", err_msg)
        assert raw_token not in err_msg
        assert "bot<redacted_token>" in err_msg
