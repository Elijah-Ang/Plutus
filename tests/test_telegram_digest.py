import os
import uuid
import pytest
from datetime import datetime, UTC, timezone, timedelta
import pandas as pd
from app.utils import format_digest_message, format_sgt
from app.storage import Storage
from app.service import TradingService
from app.risk_engine import RiskEngine
from app.reports import export_excel

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

def test_digest_config_defaults():
    config = {
        "digest": {
            "telegram_digest_enabled": True,
            "telegram_digest_interval_minutes": 30,
            "telegram_digest_market_hours_only": True,
            "telegram_digest_include_observation_symbols": True,
            "telegram_digest_max_symbols": 6,
            "telegram_digest_use_gpt": False,
            "telegram_digest_min_cycles_required": 2,
            "telegram_digest_send_when_market_closed": False
        }
    }
    assert config["digest"]["telegram_digest_enabled"] is True
    assert config["digest"]["telegram_digest_interval_minutes"] == 30

def test_digest_wording_and_structure():
    digest_data = {
        "market_open_status": "Open",
        "window_start": datetime(2026, 6, 22, 13, 30, 0, tzinfo=UTC),
        "window_end": datetime(2026, 6, 22, 14, 0, 0, tzinfo=UTC),
        "symbols_list": [
            {
                "symbol": "QQQ",
                "trade_score": 62.5,
                "trade_classification": "Weak setup, watch only",
                "price_change_30m": 0.42,
                "session_change": 0.71,
                "status": "Watch, no proposal"
            },
            {
                "symbol": "XLK",
                "trade_score": 55.0,
                "trade_classification": "Weak setup, watch only",
                "price_change_30m": 0.25,
                "session_change": 0.40,
                "status": "Watch"
            }
        ],
        "weakest_symbol": "SPY",
        "weakest_score": 17.5,
        "weakest_classification": "No action suggested",
        "actions": {
            "proposals": 0,
            "orders": 0,
            "gpt_calls": 0,
            "expired": 0
        },
        "summary": "QQQ is strongest, but no setup crossed the proposal threshold."
    }
    
    config = {"mode": "paper"}
    msg = format_digest_message(digest_data, config)
    
    assert "📊 30-min market digest" in msg
    assert "US market: Open" in msg
    assert "Window: 9:30 PM–10:00 PM SGT" in msg
    assert "Mode: Paper trading only" in msg
    assert "1. QQQ — Trade score 62.5, Weak setup, watch only" in msg
    assert "30-min: +0.42% | Session: +0.71%" in msg
    assert "Status: Watch, no proposal" in msg
    assert "Weakest: SPY — 17.5, No action suggested" in msg
    assert "Past 30 min actions:" in msg
    assert "Proposals: 0 | Orders: 0 | GPT calls: 0 | Expired: 0" in msg
    assert "Summary: QQQ is strongest, but no setup crossed the proposal threshold." in msg
    assert "No action needed." in msg
    assert "yes" not in msg.lower()
    assert "approve" not in msg.lower()

def test_digest_throttling_and_market_hours(temp_storage):
    config = {
        "mode": "paper",
        "digest": {
            "telegram_digest_enabled": True,
            "telegram_digest_interval_minutes": 30,
            "telegram_digest_market_hours_only": True,
            "telegram_digest_include_observation_symbols": True,
            "telegram_digest_max_symbols": 6,
            "telegram_digest_use_gpt": False,
            "telegram_digest_min_cycles_required": 2,
            "telegram_digest_send_when_market_closed": False
        },
        "market_profiles": {
            "us_equities": {
                "status": "active",
                "watchlist": ["SPY", "QQQ"],
                "observation_watchlist": []
            }
        }
    }
    broker = MockBroker()
    service = TradingService(config, temp_storage, broker, "run_id")
    service.telegram = MockTelegramBot()
    
    broker.open = False
    service.check_and_send_digest()
    assert len(service.telegram.messages) == 0
    
    broker.open = True
    service.check_and_send_digest()
    assert len(service.telegram.messages) == 0
    
    now = datetime.now(UTC)
    t1 = (now - timedelta(minutes=15)).isoformat()
    t2 = now.isoformat()
    
    temp_storage.execute(
        "INSERT INTO market_memory(run_id,market_profile,symbol,price,prev_price,price_change,price_change_pct,session_start_price,session_change,volatility,signal,score,classification,reason,proposal_allowed,gpt_called,created_at,asset_score,asset_classification) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("run1", "us_equities", "QQQ", 500.0, 500.0, 0.0, 0.0, 500.0, 0.0, 0.1, "HOLD", 62.5, "Weak setup, watch only", "None", 0, 0, t1, 74.0, "Moderate watch candidate")
    )
    temp_storage.execute(
        "INSERT INTO market_memory(run_id,market_profile,symbol,price,prev_price,price_change,price_change_pct,session_start_price,session_change,volatility,signal,score,classification,reason,proposal_allowed,gpt_called,created_at,asset_score,asset_classification) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("run1", "us_equities", "SPY", 400.0, 400.0, 0.0, 0.0, 400.0, 0.0, 0.1, "HOLD", 17.5, "No action suggested", "None", 0, 0, t1, 50.0, "Watch only")
    )
    
    temp_storage.execute(
        "INSERT INTO market_memory(run_id,market_profile,symbol,price,prev_price,price_change,price_change_pct,session_start_price,session_change,volatility,signal,score,classification,reason,proposal_allowed,gpt_called,created_at,asset_score,asset_classification) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("run2", "us_equities", "QQQ", 502.1, 500.0, 2.1, 0.42, 500.0, 2.1, 0.1, "HOLD", 62.5, "Weak setup, watch only", "None", 0, 0, t2, 74.0, "Moderate watch candidate")
    )
    temp_storage.execute(
        "INSERT INTO market_memory(run_id,market_profile,symbol,price,prev_price,price_change,price_change_pct,session_start_price,session_change,volatility,signal,score,classification,reason,proposal_allowed,gpt_called,created_at,asset_score,asset_classification) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("run2", "us_equities", "SPY", 399.8, 400.0, -0.2, -0.05, 400.0, -0.2, 0.1, "HOLD", 17.5, "No action suggested", "None", 0, 0, t2, 50.0, "Watch only")
    )
    
    service.check_and_send_digest()
    assert len(service.telegram.messages) == 1
    
    service.check_and_send_digest()
    assert len(service.telegram.messages) == 1
    
    digests = temp_storage.fetch_all("SELECT * FROM telegram_digests")
    assert len(digests) == 1
    assert digests[0]["status"] == "sent"
    assert "QQQ" in digests[0]["symbols"]

def test_excel_export_includes_digests(temp_storage, tmp_path):
    temp_storage.execute(
        "INSERT INTO telegram_digests(run_id,window_start,window_end,sent_at,symbols,summary_text,status) VALUES(?,?,?,?,?,?,?)",
        ("run_id", "t1", "t2", "t2", "QQQ, SPY", "Summary text", "sent")
    )
    config = {}
    out_file = tmp_path / "test_report.xlsx"
    export_excel(temp_storage, config, out_file)
    assert os.path.exists(out_file)


def test_pending_exit_blocker_names_symbol_in_risk_reason():
    now = datetime.now(UTC)
    config = {
        "mode": "paper",
        "live_enabled": False,
        "risk": {
            "max_price_age_seconds": 120,
            "min_historical_bars": 50,
            "max_trade_notional_paper": 50,
            "block_new_buys_when_any_position_open": False,
            "block_new_buys_after_buy_order_submitted_today": False,
        },
        "portfolio_behavior": {
            "block_new_buy_if_exit_pending": True,
            "max_open_positions": 5,
            "max_total_portfolio_exposure_pct": 6.0,
            "max_single_symbol_exposure_pct": 2.5,
        },
        "portfolio_optimizer": {
            "max_same_cluster_positions": 2,
            "max_same_cluster_exposure_pct": 5.0,
        },
        "market_profiles": {
            "us_equities": {
                "status": "active",
                "watchlist": ["SPY", "IWM", "DIA"],
                "observation_watchlist": [],
            }
        },
    }
    proposal = {
        "symbol": "SPY",
        "side": "buy",
        "action": "entry",
        "latest_price": 500.0,
        "price_at": now.isoformat(),
        "historical_bars": 100,
        "volume": 10000,
        "notional": 10.0,
        "asset_class": "equity",
    }
    context = {
        "now": now,
        "kill_switch": False,
        "power_connected": True,
        "internet_available": True,
        "database_writable": True,
        "broker_available": True,
        "telegram_available": True,
        "market_open": True,
        "open_positions": 0,
        "buy_trades_today": 0,
        "proposed_total_exposure_pct": 0.1,
        "proposed_symbol_exposure_pct": 0.1,
        "proposed_cluster_positions_count": 1,
        "proposed_cluster_exposure_pct": 0.1,
        "exit_pending": True,
        "exit_pending_reason": "DIA EXIT proposal pending",
        "max_emergency_exit_score": 0,
        "duplicate_order": False,
        "duplicate_position": False,
        "same_symbol_position": False,
        "uses_margin": False,
    }

    decision = RiskEngine(config).evaluate(proposal, context)

    assert not decision.passed
    assert "new buy blocked because DIA EXIT proposal pending" in decision.reasons


def test_stale_exit_proposal_is_reported_but_does_not_block(temp_storage):
    config = {"mode": "paper", "live_enabled": False}
    service = TradingService(config, temp_storage, MockBroker(), "run_id")
    now = datetime.now(UTC)
    temp_storage.execute(
        "INSERT INTO trade_proposals(id,run_id,symbol,side,notional,status,created_at,expires_at,strategy_version,payload) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            "old-exit",
            "old-run",
            "SPY",
            "sell",
            5.0,
            "approved",
            (now - timedelta(days=5)).isoformat(),
            (now - timedelta(days=5, minutes=-10)).isoformat(),
            "rule_based_v1",
            "{}",
        ),
    )

    blocker = service._exit_blocker_context([])

    assert blocker["active"] is False
    assert blocker["stale"] is True
    assert blocker["reason"] == "stale SPY exit flag ignored"


def test_digest_explains_exit_first_blocker_and_not_false_threshold(temp_storage):
    config = {
        "mode": "paper",
        "ai": {"ai_review_min_score": 65},
        "digest": {
            "telegram_digest_enabled": True,
            "telegram_digest_interval_minutes": 30,
            "telegram_digest_market_hours_only": False,
            "telegram_digest_include_observation_symbols": True,
            "telegram_digest_max_symbols": 6,
            "telegram_digest_use_gpt": False,
            "telegram_digest_min_cycles_required": 1,
            "telegram_digest_send_when_market_closed": True,
        },
        "market_profiles": {
            "us_equities": {
                "status": "active",
                "watchlist": ["SPY", "IWM", "DIA"],
                "observation_watchlist": ["XLV"],
            }
        },
    }
    service = TradingService(config, temp_storage, MockBroker(), "run_id")
    service.telegram = MockTelegramBot()
    now = datetime.now(UTC)
    rows = [
        ("run1", "SPY", 500.0, "ENTRY", 80.0, "Strong setup", 0, "blocked by risk checks; new buy blocked because DIA EXIT proposal pending"),
        ("run1", "IWM", 200.0, "ENTRY", 76.0, "Strong setup", 0, "blocked by risk checks; new buy blocked because DIA EXIT proposal pending"),
        ("run1", "DIA", 350.0, "HOLD", 90.0, "Strong watch", 0, "no entry/exit signal"),
        ("run1", "XLV", 100.0, "ENTRY", 72.0, "Observation setup", 0, "symbol not in active watchlist"),
    ]
    for run_id, symbol, price, signal, score, classification, proposal_generated, no_action_reason in rows:
        temp_storage.execute(
            "INSERT INTO market_memory(run_id,market_profile,symbol,price,session_start_price,signal,score,classification,proposal_generated,no_action_reason,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, "us_equities", symbol, price, price, signal, score, classification, proposal_generated, no_action_reason, now.isoformat()),
        )

    service.check_and_send_digest()

    assert len(service.telegram.messages) == 1
    msg = service.telegram.messages[0]
    assert "Exit-first blocker: DIA EXIT proposal pending" in msg
    assert "Status: Watch — New buy blocked — DIA EXIT proposal pending" in msg
    assert "DIA — Trade score 90.0" in msg
    assert "Status: Watch — no ENTRY signal" in msg
    assert "XLV — Trade score 72.0" in msg
    assert "Observation only — no proposal allowed" in msg
    assert "no setup crossed the proposal threshold" not in msg.lower()
    assert "No setup crossed the score threshold" not in msg


def test_digest_does_not_invent_pending_exit_from_low_warning(temp_storage):
    config = {
        "mode": "paper",
        "ai": {"ai_review_min_score": 65},
        "digest": {
            "telegram_digest_enabled": True,
            "telegram_digest_interval_minutes": 30,
            "telegram_digest_market_hours_only": False,
            "telegram_digest_include_observation_symbols": True,
            "telegram_digest_max_symbols": 6,
            "telegram_digest_use_gpt": False,
            "telegram_digest_min_cycles_required": 1,
            "telegram_digest_send_when_market_closed": True,
        },
        "market_profiles": {
            "us_equities": {
                "status": "active",
                "watchlist": ["DIA"],
                "observation_watchlist": [],
            }
        },
    }
    service = TradingService(config, temp_storage, MockBroker(), "run_id")
    service.telegram = MockTelegramBot()
    now = datetime.now(UTC)
    temp_storage.execute(
        "INSERT INTO market_memory(run_id,market_profile,symbol,price,session_start_price,signal,score,classification,proposal_generated,no_action_reason,emergency_exit_score,emergency_exit_triggered,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("run1", "us_equities", "DIA", 350.0, 350.0, "HOLD", 90.0, "Strong watch", 0, "no entry/exit signal", 10.0, 0, now.isoformat()),
    )

    service.check_and_send_digest()

    msg = service.telegram.messages[0]
    assert "Exit-first blocker:" not in msg
    assert "pending exit" not in msg.lower()
    assert "DIA crossed score threshold but had no ENTRY signal" in msg
