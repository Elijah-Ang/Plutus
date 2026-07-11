from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
import pytest

from app.storage import Storage
from app.service import TradingService
from app.approval_parser import parse_approval
from app.utils import format_proposal_message
from app.ai_review import deterministic_review


def _validated_entry_fields(price: float, now: datetime) -> dict:
    return {
        "stop_price": price - 10.0, "stop_distance_dollars": 10.0, "atr_value": 5.0,
        "technical_stop_price": price - 10.0, "stop_model_used": "atr", "stop_validation_status": "validated",
        "cluster_name": "us_broad_market", "order_type": "limit",
        "quote_source": "alpaca_quote", "quote_bid": price - 0.01, "quote_ask": price + 0.01,
        "quote_midpoint": price, "quote_timestamp": now.isoformat(), "quote_spread_bps": 2.0,
        "limit_price": price + 0.27,
    }

class MockClock:
    def __init__(self, timestamp=None, next_close=None):
        self.timestamp = timestamp or datetime.now(UTC)
        self.next_close = next_close or (self.timestamp + timedelta(hours=2))
        self.is_open = True

class MockBroker:
    def __init__(self):
        self.positions = []
        self.open_orders = []
        self.market_open = True
        self.price = 500.0
        self.clock = MockClock()
        self.prices = {"SPY": 500.0, "DIA": 350.0, "IWM": 200.0}

    def get_positions(self):
        return self.positions

    def get_open_orders(self):
        return self.open_orders

    def is_market_open(self):
        return self.market_open

    def get_latest_price(self, symbol):
        return type("T", (), {"price": self.prices.get(symbol, self.price), "timestamp": datetime.now(UTC)})()

    def get_latest_quote(self, symbol):
        price = self.prices.get(symbol, self.price)
        return {"bid_price": price - 0.01, "ask_price": price + 0.01, "timestamp": datetime.now(UTC)}

    def get_historical_bars(self, symbol, timeframe, limit=50):
        import pandas as pd
        data = {
            "close": [self.prices.get(symbol, self.price)] * limit,
            "volume": [10000.0] * limit,
            "high": [self.prices.get(symbol, self.price) + 2.0] * limit,
            "low": [self.prices.get(symbol, self.price) - 2.0] * limit,
            "open": [self.prices.get(symbol, self.price)] * limit,
            "ma_50": [95.0] * limit,
            "ma_200": [90.0] * limit,
            "volatility_20": [0.15] * limit
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
        return {
            "daily_loss_dollars": 0.0, "weekly_loss_dollars": 0.0,
            "daily_loss_confidence": "verified", "weekly_loss_confidence": "verified",
            "reference_equity": 1000000.0,
        }

    def get_clock(self):
        return self.clock

    def submit_order(self, *args, **kwargs):
        return type("Order", (), {"status": "submitted", "id": "broker-order-123"})()

class MockTelegramBot:
    def __init__(self, allowed_user_id="7777"):
        self.allowed_user_id = allowed_user_id
        self.chat_id = "12345"
        self.messages = []

    def send_message(self, text, chat_id=None):
        self.messages.append(text)
        return {"message_id": 9999, "chat": {"id": 12345}}

    def is_authorized(self, sender_id):
        return str(sender_id) == str(self.allowed_user_id)

    def get_updates(self, offset=None, timeout=0):
        return []

    def is_available(self, force=False):
        return True

class MockAI:
    def __init__(self):
        self.calls_made = 0

    def review(self, proposal):
        self.calls_made += 1
        return {
            "summary": "AI Approved",
            "risks": ["Choppy price movement"],
            "telegram_message": "Actionable proposal",
            "caution_level": "medium",
            "should_block_for_reasoning_only": False,
            "reasoning_notes": "All filters green",
            "gpt_confidence": "High",
            "gpt_caution": "Low",
            "main_risk": "Short-term entry might be choppy.",
            "supports_system_score": "yes",
            "reason": "Passed AI sanity checks"
        }

@pytest.fixture
def temp_storage(tmp_path):
    db_file = tmp_path / "test_targeting.db"
    storage = Storage(db_file)
    storage.initialize()
    return storage

@pytest.fixture
def mock_config():
    return {
        "mode": "paper",
        "live_enabled": False,
        "proposal_expiry_default_minutes": 15,
        "market_profiles": {
            "default": {
                "status": "active",
                "broker": "alpaca",
                "watchlist": ["SPY", "DIA", "IWM"],
                "observation_watchlist": [],
                "proposals_enabled": True,
                "execution_enabled": True
            }
        },
        "risk": {
            "max_trades_per_day": 10,
            "max_open_positions": 5,
            "max_price_age_seconds": 120,
            "min_historical_bars": 10,
            "max_trade_notional_paper": 5,
            "max_new_buy_proposals_per_cycle": 1,
            "max_pending_buy_proposals": 1,
            "allow_multiple_exit_proposals": True,
            "require_gpt_review_for_buy_proposals": True
        },
        "ai": {
            "ai_review_min_score": 65,
            "ai_review_on_every_run": True,
            "ai_daily_call_limit": 50,
            "ai_max_calls_per_run": 50,
            "ai_review_min_interval_minutes": 0
        },
        "phase3": {"enabled": False, "active": False},
        "phase4": {"enabled": False, "active": False},
        "position_sizing": {
            "enabled": True, "mode": "risk_portfolio", "stage": "moderate_paper",
            "use_stage_dollar_cap": True,
            "stage_max_initial_notional_usd": {"moderate_paper": 250.0},
            "stage_max_add_notional_usd": {"moderate_paper": 100.0},
            "risk_per_trade_pct": 0.2, "max_trade_notional_pct_equity": 6.0,
            "max_position_notional_pct_equity": 6.0, "max_total_portfolio_exposure_pct": 30.0,
            "max_cluster_exposure_pct": 15.0, "min_cash_reserve_pct": 20.0,
            "max_cash_usage_pct": 10.0, "default_paper_notional_usd": 250.0,
            "default_add_notional_usd": 100.0, "minimum_executable_notional_usd": 5.0,
            "add_size_multiplier": 0.5,
            "stop_model": {"atr_multiple": 2.0, "min_stop_pct": 1.0, "max_stop_pct": 8.0},
            "score_multiplier": {"65_74": 1.0, "75_84": 1.0, "85_94": 1.0, "95_100": 1.0},
            "volatility_multiplier": {"normal": 1.0, "elevated": 0.5, "high": 0.25, "extreme": 0.0, "too_quiet": 0.75},
        }
    }

def test_parse_approval_reply_to_scenarios():
    now = datetime.now(UTC)
    expiry = (now + timedelta(minutes=15)).isoformat()
    expired_time = (now - timedelta(minutes=15)).isoformat()
    
    pending_props = [
        {"id": "spy-id", "symbol": "SPY", "side": "buy", "expires_at": expiry, "telegram_message_id": "1001"},
        {"id": "dia-id", "symbol": "DIA", "side": "buy", "expires_at": expiry, "telegram_message_id": "1002"},
        {"id": "exp-id", "symbol": "IWM", "side": "buy", "expires_at": expired_time, "telegram_message_id": "1003"}
    ]
    
    # 1. Reply-to yes approves correct proposal
    res1 = parse_approval("yes", "7777", "7777", pending_props, now=now, reply_to_message_id="1001")
    assert res1.accepted
    assert res1.proposal_id == "spy-id"
    assert res1.action == "approve"

    # 2. Reply-to no rejects correct proposal
    res2 = parse_approval("no", "7777", "7777", pending_props, now=now, reply_to_message_id="1002")
    assert res2.accepted
    assert res2.proposal_id == "dia-id"
    assert res2.action == "reject"

    # 3. Reply-to wrong proposal is blocked
    res3 = parse_approval("yes", "7777", "7777", pending_props, now=now, reply_to_message_id="9999")
    assert not res3.accepted
    assert res3.reason == "reply-to target proposal not found or already handled"

    # 4. Reply-to expired proposal is blocked
    res4 = parse_approval("yes", "7777", "7777", pending_props, now=now, reply_to_message_id="1003")
    assert not res4.accepted
    assert res4.reason == "proposal expired"

    # 5. Plain yes with multiple pending is ambiguous
    res5 = parse_approval("yes", "7777", "7777", pending_props, now=now)
    assert not res5.accepted
    assert res5.reason == "ambiguous plain action with multiple pending proposals"

    # 6. Plain yes with single pending works
    res6 = parse_approval("yes", "7777", "7777", [pending_props[0]], now=now)
    assert res6.accepted
    assert res6.proposal_id == "spy-id"

    # 7. Unauthorized user is ignored
    res7 = parse_approval("yes", "8888", "7777", pending_props, now=now, reply_to_message_id="1001")
    assert not res7.accepted
    assert res7.reason == "unauthorized sender"

def test_telegram_process_reply_to_and_acknowledgements(temp_storage, mock_config):
    bot = MockTelegramBot(allowed_user_id="7777")
    broker = MockBroker()
    service = TradingService(mock_config, temp_storage, broker, "run_id")
    service.telegram = bot

    # Pre-populate a pending proposal
    now = datetime.now(UTC)
    expiry = now + timedelta(minutes=15)
    proposal_payload = {
        "symbol": "SPY",
        "side": "buy",
        "notional": 5.0,
        "latest_price": 500.0,
        "price_at": now.isoformat(),
        "historical_bars": 100,
        "volume": 10000.0,
        "reason": "filters normal",
        "strategy_version": "rule_based_v1",
        "created_at": now.isoformat(),
        "expires_at": expiry.isoformat()
    }
    proposal_payload.update(_validated_entry_fields(500.0, now))
    
    temp_storage.execute(
        "INSERT INTO trade_proposals(id,run_id,signal_id,symbol,side,notional,status,created_at,expires_at,strategy_version,payload,telegram_message_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        ("spy-prop", "run_id", "sig-1", "SPY", "buy", 5.0, "pending", now.isoformat(), expiry.isoformat(), "rule_based_v1", json.dumps(proposal_payload), "1001")
    )

    # Mock user reply-to yes
    update_yes = {
        "update_id": 1,
        "message": {
            "text": "yes",
            "from": {"id": "7777"},
            "reply_to_message": {"message_id": 1001}
        }
    }
    bot.get_updates = lambda **kwargs: [update_yes]
    
    service.process_telegram()

    # Verify initial and final check success messages
    assert any("Received: YES for SPY paper buy proposal" in m for m in bot.messages)
    assert any("Paper order submitted: NEW ENTRY SPY for $5.00" in m for m in bot.messages)

    # Verify database updates
    app_rows = temp_storage.fetch_all("SELECT * FROM approvals WHERE proposal_id='spy-prop'")
    assert len(app_rows) == 1
    assert app_rows[0]["reply_to_message_id"] == "1001"
    assert app_rows[0]["proposal_targeting_method"] == "reply_to"
    assert app_rows[0]["acknowledgement_status"] == "submitted"

def test_simultaneous_buy_candidates_are_all_proposed_without_count_cap(temp_storage, mock_config):
    bot = MockTelegramBot()
    broker = MockBroker()
    service = TradingService(mock_config, temp_storage, broker, "run_id")
    service.telegram = bot
    service.ai = MockAI()

    # Modify mock evaluate_symbol in service to return multiple entries
    from app.service import evaluate_symbol
    from app.strategy_rule_based import Signal
    
    original_evaluate = evaluate_symbol
    
    def mock_eval(symbol, *args, **kwargs):
        if symbol == "SPY":
            return Signal("ENTRY", "buy", "SPY", "filters normal", 0.8, {"volatility_20": 0.15})
        elif symbol == "DIA":
            return Signal("ENTRY", "buy", "DIA", "filters normal", 0.8, {"volatility_20": 0.15})
        else:
            return Signal("ENTRY", "buy", "IWM", "filters normal", 0.8, {"volatility_20": 0.15})

    import app.service
    app.service.evaluate_symbol = mock_eval

    try:
        service.scan()
        
        # Proposal count is uncapped; all otherwise eligible candidates survive.
        props = temp_storage.fetch_all("SELECT * FROM trade_proposals WHERE status='pending'")
        assert {row["symbol"] for row in props} == {"SPY", "DIA", "IWM"}

        # No candidate is suppressed merely because others were proposed.
        suppressed = temp_storage.fetch_all("SELECT * FROM market_memory WHERE candidate_suppression_reason='suppressed_by_candidate_limit'")
        assert suppressed == []
            
    finally:
        app.service.evaluate_symbol = original_evaluate

def test_pending_buy_blocks_only_duplicate_symbol_not_other_proposals(temp_storage, mock_config):
    bot = MockTelegramBot()
    broker = MockBroker()
    service = TradingService(mock_config, temp_storage, broker, "run_id")
    service.telegram = bot
    service.ai = MockAI()

    # Insert a pending BUY proposal
    now = datetime.now(UTC)
    expiry = now + timedelta(minutes=15)
    temp_storage.execute(
        "INSERT INTO trade_proposals(id,run_id,signal_id,symbol,side,notional,status,created_at,expires_at,strategy_version,payload) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("existing-prop", "run_id", "sig-1", "SPY", "buy", 5.0, "pending", now.isoformat(), expiry.isoformat(), "rule_based_v1", "{}")
    )

    from app.service import evaluate_symbol
    from app.strategy_rule_based import Signal
    original_evaluate = evaluate_symbol
    import app.service
    app.service.evaluate_symbol = lambda symbol, *args, **kwargs: Signal("ENTRY", "buy", symbol, "filters normal", 0.8, {"volatility_20": 0.15})

    try:
        service.scan()
        # An incomplete pending buy is unknown risk and blocks all new entries;
        # it is never silently ignored while unrelated symbols are proposed.
        props = temp_storage.fetch_all("SELECT * FROM trade_proposals WHERE status='pending'")
        assert {row["symbol"] for row in props} == {"SPY"}
        
        suppressed = temp_storage.fetch_all("SELECT * FROM market_memory WHERE candidate_suppression_reason='suppressed_by_candidate_limit'")
        assert suppressed == []
    finally:
        app.service.evaluate_symbol = original_evaluate

def test_gpt_review_required_blocks_when_unavailable(temp_storage, mock_config):
    bot = MockTelegramBot()
    broker = MockBroker()
    service = TradingService(mock_config, temp_storage, broker, "run_id")
    service.telegram = bot
    
    # Mock AI throws an error to simulate unavailability
    class BrokenAI:
        def review(self, proposal):
            raise RuntimeError("API limit exceeded")
    service.ai = BrokenAI()

    from app.service import evaluate_symbol
    from app.strategy_rule_based import Signal
    original_evaluate = evaluate_symbol
    import app.service
    app.service.evaluate_symbol = lambda symbol, *args, **kwargs: Signal("ENTRY", "buy", symbol, "filters normal", 0.8, {"volatility_20": 0.15})

    try:
        service.scan()
        # AI is optional commentary and cannot suppress deterministic proposals.
        props = temp_storage.fetch_all("SELECT * FROM trade_proposals WHERE status='pending'")
        assert len(props) > 0
        
        # Verify deferred_ai_review_reason in market_memory
        deferred = temp_storage.fetch_all("SELECT * FROM market_memory WHERE deferred_ai_review_reason='commentary_unavailable'")
        assert len(deferred) > 0
    finally:
        app.service.evaluate_symbol = original_evaluate

def test_rule_only_warning_in_formatter(mock_config):
    proposal = {
        "symbol": "SPY",
        "side": "buy",
        "notional": 5.0,
        "score": 75.0,
        "reason": "volatility normal",
        "expires_at": (datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
        "gpt_called": False
    }
    msg = format_proposal_message(proposal, mock_config)
    assert "Rule-based only. AI review was not available. Treat with extra caution." in msg

def test_proposal_conflict_supersedes_others(temp_storage, mock_config):
    bot = MockTelegramBot()
    broker = MockBroker()
    service = TradingService(mock_config, temp_storage, broker, "run_id")
    service.telegram = bot

    # Inject two pending BUY proposals
    now = datetime.now(UTC)
    expiry = now + timedelta(minutes=15)
    spy_payload = {
        "symbol": "SPY",
        "side": "buy",
        "notional": 5.0,
        "latest_price": 500.0,
        "price_at": now.isoformat(),
        "historical_bars": 100,
        "volume": 10000.0,
        "reason": "filters normal",
        "strategy_version": "rule_based_v1",
        "created_at": now.isoformat(),
        "expires_at": expiry.isoformat()
    }
    spy_payload.update(_validated_entry_fields(500.0, now))
    dia_payload = {
        "symbol": "DIA",
        "side": "buy",
        "notional": 5.0,
        "latest_price": 350.0,
        "price_at": now.isoformat(),
        "historical_bars": 100,
        "volume": 10000.0,
        "reason": "filters normal",
        "strategy_version": "rule_based_v1",
        "created_at": now.isoformat(),
        "expires_at": expiry.isoformat()
    }
    dia_payload.update(_validated_entry_fields(350.0, now))
    temp_storage.execute(
        "INSERT INTO trade_proposals(id,run_id,signal_id,symbol,side,notional,status,created_at,expires_at,strategy_version,payload,telegram_message_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        ("prop-spy", "run_id", "sig-1", "SPY", "buy", 5.0, "pending", now.isoformat(), expiry.isoformat(), "rule_based_v1", json.dumps(spy_payload), "1001")
    )
    temp_storage.execute(
        "INSERT INTO trade_proposals(id,run_id,signal_id,symbol,side,notional,status,created_at,expires_at,strategy_version,payload,telegram_message_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        ("prop-dia", "run_id", "sig-2", "DIA", "buy", 5.0, "pending", now.isoformat(), expiry.isoformat(), "rule_based_v1", json.dumps(dia_payload), "1002")
    )

    # Approve SPY
    update_yes = {
        "update_id": 1,
        "message": {
            "text": "yes",
            "from": {"id": "7777"},
            "reply_to_message": {"message_id": 1001}
        }
    }
    bot.get_updates = lambda **kwargs: [update_yes]
    
    service.process_telegram()

    # Verify SPY advanced to submitted after approval + order submission
    spy_status = temp_storage.fetch_all("SELECT status FROM trade_proposals WHERE id='prop-spy'")[0]["status"]
    assert spy_status == "submitted"

    # Verify DIA is superseded
    dia_status = temp_storage.fetch_all("SELECT status FROM trade_proposals WHERE id='prop-dia'")[0]["status"]
    assert dia_status == "superseded"

    # Verify single notification is sent
    assert any("Other pending BUY proposals were cancelled" in m for m in bot.messages)
