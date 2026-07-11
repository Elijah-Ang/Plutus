import json
import uuid
import time
import pytest
from datetime import datetime, UTC, timedelta
import pandas as pd
from unittest.mock import MagicMock, patch

from app.utils import format_proposal_message
from app.storage import Storage
from app.service import TradingService
from app.strategy_rule_based import Signal, evaluate_symbol

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

    def get_latest_quote(self, symbol):
        return {"bid_price": self.price - 0.01, "ask_price": self.price + 0.01, "timestamp": self.price_time}

    def get_historical_bars(self, symbol, timeframe, limit):
        data = {"open": [100.0] * limit, "high": [101.0] * limit,
                "low": [99.0] * limit, "close": [100.0] * limit, "volume": [10000.0] * limit}
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
            "max_open_positions": 2,
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

@pytest.fixture(autouse=True)
def mock_system_dependencies():
    with patch("app.service.get_power_status") as mock_power, \
         patch("app.service.internet_available") as mock_internet:
        mock_power.return_value = type("P", (), {"connected": True})()
        mock_internet.return_value = True
        yield

def test_telegram_stale_messages_ignored(temp_storage, base_config):
    # Verify stale Telegram updates (older than startup / 2m) are ignored
    broker = MockBroker()
    service = TradingService(base_config, temp_storage, broker, "run-1")
    service.telegram = MagicMock()
    service.telegram.chat_id = "12345"
    service.telegram.allowed_user_id = "12345"
    service.telegram.is_authorized.return_value = True

    # Message dated 5 minutes before listener startup
    stale_date = int(time.time() - 300)
    update_stale_yes = {
        "update_id": 100,
        "message": {
            "message_id": 200,
            "text": "yes",
            "date": stale_date,
            "from": {"id": "12345"}
        }
    }

    # Message dated 5 seconds after listener startup
    fresh_date = int(time.time() + 5)
    update_fresh_yes = {
        "update_id": 101,
        "message": {
            "message_id": 201,
            "text": "yes",
            "date": fresh_date,
            "from": {"id": "12345"}
        }
    }

    # Create proposal in db
    pid = "proposal-1"
    temp_storage.execute(
        "INSERT INTO trade_proposals(id,run_id,signal_id,symbol,side,notional,status,created_at,expires_at,strategy_version,payload) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (pid, "run-1", "sig-1", "DIA", "buy", 5.0, "pending", datetime.now(UTC).isoformat(), (datetime.now(UTC) + timedelta(minutes=15)).isoformat(), "rule_based_v1", json.dumps({"symbol": "DIA", "side": "buy", "notional": 5.0, "latest_price": 100.0, "price_at": datetime.now(UTC).isoformat()}))
    )

    # Setup listener startup time to current time
    service.listener_started_at = time.time()

    # Run process_telegram with stale update first
    service.telegram.get_updates.side_effect = [[update_stale_yes], []]
    service.process_telegram()

    # Verify stale update sent clarification but did NOT execute
    service.telegram.send_message.assert_any_call(
        "I ignored an old approval message from before the fast listener started. Please reply again to the current proposal if it is still pending.",
        service.telegram.chat_id
    )

    # Verify proposal status is still pending (stale yes was ignored)
    prop = temp_storage.fetch_all("SELECT status FROM trade_proposals WHERE id=?", (pid,))[0]
    assert prop["status"] == "pending"

    # Verify stale update is recorded in audit_events
    events = temp_storage.fetch_all("SELECT event_type FROM audit_events WHERE event_type='listener_bootstrap_update_ignored'")
    assert len(events) == 1

    # Now run with fresh update
    service.telegram.get_updates.side_effect = [[update_fresh_yes], []]
    service.process_telegram()

    # Proposal should be processed (approved/blocked/submitted)
    prop2 = temp_storage.fetch_all("SELECT status FROM trade_proposals WHERE id=?", (pid,))[0]
    assert prop2["status"] != "pending"

def test_true_score_ranking_and_watchlist_order(temp_storage, base_config):
    # Verify true score ranking is calculated correctly
    broker = MockBroker()
    service = TradingService(base_config, temp_storage, broker, "run-1")

    # Mock _calculate_asset_selection_score to return different scores
    def mock_calc(symbol, *args, **kwargs):
        if symbol == "SPY":
            return 90.0
        elif symbol == "DIA":
            return 75.0
        else:
            return 55.0
    service._calculate_asset_selection_score = mock_calc

    # Mock evaluate_symbol to return signals with different scores
    with patch("app.service.evaluate_symbol") as mock_eval:
        def side_effect(symbol, *args, **kwargs):
            if symbol == "SPY":
                return Signal("ENTRY", "buy", symbol, "trend up", 0.9, {"close": 100.0, "ma_50": 95.0, "volatility_20": 0.1})
            elif symbol == "QQQ":
                return Signal("ENTRY", "buy", symbol, "trend up", 0.5, {"close": 100.0, "ma_50": 98.0, "volatility_20": 0.1})
            else:
                return Signal("ENTRY", "buy", symbol, "trend up", 0.7, {"close": 100.0, "ma_50": 97.0, "volatility_20": 0.1})
        mock_eval.side_effect = side_effect

        # Run scan
        service.scan()

        # Verify stored ranks in market_memory
        results = temp_storage.fetch_all("SELECT symbol, true_score_rank, watchlist_order FROM market_memory WHERE run_id='run-1'")

        spy_row = next(r for r in results if r["symbol"] == "SPY")
        qqq_row = next(r for r in results if r["symbol"] == "QQQ")
        dia_row = next(r for r in results if r["symbol"] == "DIA")

        assert spy_row["watchlist_order"] == 1
        assert qqq_row["watchlist_order"] == 2
        assert dia_row["watchlist_order"] == 3

        assert spy_row["true_score_rank"] == 1
        assert dia_row["true_score_rank"] == 2
        assert qqq_row["true_score_rank"] == 3

        # Verify formatting matches new fields
        msg = format_proposal_message({
            "symbol": "DIA",
            "side": "buy",
            "notional": 5.0,
            "score": 80.0,
            "watchlist_order": 3,
            "total_active_symbols": 4,
            "true_score_rank": 2,
            "proposal_eligible_rank": 1,
            "selection_reason": "Selected because it was the strongest eligible candidate.",
            "expires_at": datetime.now(UTC).isoformat(),
            "gpt_called": False,
            "review": None
        }, base_config)

        assert "Watchlist order: #3 of 4" in msg
        assert "Score rank: #2 of 4 active ETFs" in msg
        assert "Eligible proposal rank: #1" in msg
        assert "Selection reason:" in msg

def test_drawdown_exit_functional(temp_storage, base_config):
    # Verify drawdown triggers EXIT when below -8%
    broker = MockBroker()

    spy_pos = type("P", (), {"symbol": "SPY", "qty": 10.0, "avg_entry_price": 100.0, "current_price": 90.0})()
    broker.positions = [spy_pos]
    broker.price = 90.0

    service = TradingService(base_config, temp_storage, broker, "run-1")

    with patch("app.service.evaluate_symbol", wraps=evaluate_symbol) as mock_eval:
        service.scan()

        # Verify it was called with position_drawdown_pct=-0.10
        called_args = mock_eval.call_args_list
        spy_call = next(c for c in called_args if c[0][0] == "SPY")
        assert spy_call[1]["position_drawdown_pct"] == -0.10

        spy_signal = temp_storage.fetch_all("SELECT action, reason FROM signals WHERE symbol='SPY'")[0]
        assert spy_signal["action"] == "EXIT"
        assert "stop drawdown reached" in spy_signal["reason"]

        qqq_signal = temp_storage.fetch_all("SELECT action FROM signals WHERE symbol='QQQ'")[0]
        assert qqq_signal["action"] != "EXIT"

def test_exit_priority_suppresses_buys(temp_storage, base_config):
    # Verify that if any exit candidate exists, buys are suppressed
    broker = MockBroker()

    spy_pos = type("P", (), {"symbol": "SPY", "qty": 10.0, "avg_entry_price": 100.0, "current_price": 90.0})()
    broker.positions = [spy_pos]
    broker.price = 90.0

    service = TradingService(base_config, temp_storage, broker, "run-1")
    service.telegram = MagicMock()
    service.telegram.is_available.return_value = True

    with patch("app.service.evaluate_symbol") as mock_eval:
        def side_effect(symbol, *args, **kwargs):
            if symbol == "SPY":
                return Signal("EXIT", "sell", symbol, "stop drawdown reached", 0.8, {"volatility_20": 0.1})
            elif symbol == "QQQ":
                return Signal("ENTRY", "buy", symbol, "volatility normal", 0.7, {"volatility_20": 0.1})
            else:
                return Signal("HOLD", None, symbol, "no trend", 0.0, {"volatility_20": 0.1})
        mock_eval.side_effect = side_effect

        service.scan()

    qqq_mem = temp_storage.fetch_all("SELECT proposal_allowed, no_action_reason, candidate_suppression_reason, exit_priority_applied FROM market_memory WHERE symbol='QQQ'")[0]
    assert qqq_mem["proposal_allowed"] == 0
    assert qqq_mem["candidate_suppression_reason"] == "suppressed_due_to_exit_priority"
    assert qqq_mem["exit_priority_applied"] == 1

    spy_mem = temp_storage.fetch_all("SELECT proposal_allowed, exit_priority_applied FROM market_memory WHERE symbol='SPY'")[0]
    assert spy_mem["proposal_allowed"] == 1
    assert spy_mem["exit_priority_applied"] == 1

def test_gpt_exit_explanation_timeout_fallback(temp_storage, base_config):
    # Verify that GPT is attempted for exit explanations, and fallback works on timeout
    broker = MockBroker()
    spy_pos = type("P", (), {"symbol": "SPY", "qty": 10.0, "avg_entry_price": 100.0, "current_price": 90.0})()
    broker.positions = [spy_pos]
    broker.price = 90.0

    service = TradingService(base_config, temp_storage, broker, "run-1")
    service.telegram = MagicMock()
    service.telegram.is_available.return_value = True

    # Mock AIReviewer.review to time out
    service.ai.review = MagicMock(side_effect=TimeoutError("GPT timed out"))

    with patch("app.service.evaluate_symbol") as mock_eval:
        mock_eval.return_value = Signal("EXIT", "sell", "SPY", "stop drawdown reached", 0.8, {"volatility_20": 0.1})
        service.scan()

    spy_prop = temp_storage.fetch_all("SELECT gpt_exit_explanation_status, gpt_exit_confidence FROM trade_proposals WHERE symbol='SPY'")[0]
    assert spy_prop["gpt_exit_explanation_status"] == "Not available; using rule-based exit reason"
    assert spy_prop["gpt_exit_confidence"] is None or spy_prop["gpt_exit_confidence"] == "Not called"

def test_setup_fingerprint_cooldown_and_revival(temp_storage, base_config):
    broker = MockBroker()
    service = TradingService(base_config, temp_storage, broker, "run-1")
    service.telegram = MagicMock()
    service.telegram.is_available.return_value = True
    service.ai.review = MagicMock(return_value={
        "summary": "AI summary",
        "risks": ["Risk"],
        "telegram_message": "AI msg",
        "caution_level": "medium",
        "should_block_for_reasoning_only": False,
        "reasoning_notes": "AI notes",
        "gpt_confidence": "High",
        "gpt_caution": "Low",
        "main_risk": "None",
        "supports_system_score": "yes",
        "reason": "Test"
    })

    # Mock compute_setup_key to return a static setup key so score delta revival can trigger
    service._compute_setup_key = MagicMock(return_value="SPY:buy:ENTRY:below_50:above_200:normal:score_80")

    # Mock _calculate_asset_selection_score to return different scores for Run 1 vs Run 3
    def mock_calc(symbol, *args, **kwargs):
        if service.run_id == "run-1" or service.run_id == "run-2":
            return 50.0
        return 85.0
    service._calculate_asset_selection_score = mock_calc

    with patch("app.service.evaluate_symbol") as mock_eval:
        def side_effect(sym, *args, **kwargs):
            if sym == "SPY":
                return Signal("ENTRY", "buy", "SPY", "trend up", 0.8, {"close": 100.0, "ma_50": 95.0, "volatility_20": 0.30})
            return Signal("HOLD", None, sym, "not SPY", 0.0, {})
        mock_eval.side_effect = side_effect

        service.scan()

        prop_1 = temp_storage.fetch_all("SELECT id, setup_key, status FROM trade_proposals WHERE symbol='SPY'")[0]
        setup_key = prop_1["setup_key"]

        # Mark the first proposal as approved and set it to 10 minutes ago so it enters cooldown and is not competing
        created_at_time = datetime.now(UTC) - timedelta(minutes=10)
        temp_storage.execute("UPDATE trade_proposals SET status='approved', created_at=? WHERE id=?", (created_at_time.isoformat(), prop_1["id"]))

        service.run_id = "run-2"
        service.scan()

        mem_2 = temp_storage.fetch_all("SELECT dedupe_status, dedupe_reason, cooldown_applied FROM market_memory WHERE symbol='SPY' AND run_id='run-2'")[0]
        assert mem_2["cooldown_applied"] == 1
        assert mem_2["dedupe_status"] == "suppressed"

        # Revival
        def side_effect_3(sym, *args, **kwargs):
            if sym == "SPY":
                return Signal("ENTRY", "buy", "SPY", "trend up", 0.99, {"close": 100.0, "ma_50": 95.0, "volatility_20": 0.10})
            return Signal("HOLD", None, sym, "not SPY", 0.0, {})
        mock_eval.side_effect = side_effect_3
        service.run_id = "run-3"
        service.scan()

        mem_3 = temp_storage.fetch_all("SELECT dedupe_status, revival_reason, cooldown_applied FROM market_memory WHERE symbol='SPY' AND run_id='run-3'")[0]
        assert mem_3["cooldown_applied"] == 0
        assert mem_3["dedupe_status"] == "allowed"
        assert "score" in mem_3["revival_reason"]
