import os
import json
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
            "fills": 0,
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
    assert "Proposals: 0 | Orders: 0 | Fills: 0 | GPT calls: 0 | Expired: 0" in msg
    assert "Summary: QQQ is strongest, but no setup crossed the proposal threshold." in msg
    assert "No action needed." in msg
    assert "yes" not in msg.lower()
    assert "approve" not in msg.lower()


def test_digest_tier_snapshot_is_explicit_and_truncated():
    digest_data = {
        "market_open_status": "Open",
        "window_start": datetime(2026, 6, 22, 13, 30, 0, tzinfo=UTC),
        "window_end": datetime(2026, 6, 22, 14, 0, 0, tzinfo=UTC),
        "symbols_list": [],
        "tier_snapshot": {
            "static_paper_tradable": [
                {
                    "symbol": "SPY",
                    "score": 100.0,
                    "tradable": True,
                    "held": False,
                    "proposal_allowed": "blocked",
                    "proposal_block_reason": "broad-market cluster limit due DIA/IWM",
                }
            ],
            "dynamic_paper_tradable": [
                {
                    "symbol": "SMH",
                    "score": 91.0,
                    "tradable": True,
                    "held": False,
                    "proposal_allowed": "blocked",
                    "proposal_block_reason": "requires setup and RiskEngine pass",
                }
            ],
            "observation": [
                {
                    "symbol": f"OBS{idx}",
                    "score": 80.0 - idx,
                    "tradable": False,
                    "held": False,
                    "proposal_allowed": "no",
                    "proposal_block_reason": "observation only; needs paper-tradable promotion",
                }
                for idx in range(7)
            ],
            "research_candidate": [
                {
                    "symbol": "AAL",
                    "score": 58.0,
                    "tradable": False,
                    "held": False,
                    "proposal_allowed": "no",
                    "proposal_block_reason": "research candidate only; needs observation promotion first",
                }
            ],
        },
        "weakest_symbol": "AVGO",
        "weakest_score": 33.0,
        "weakest_classification": "No action suggested",
        "actions": {"proposals": 0, "orders": 0, "fills": 0, "gpt_calls": 0, "expired": 0},
        "summary": "No dynamic proposals/orders created.",
    }

    msg = format_digest_message(digest_data, {"mode": "paper"})

    assert "Static paper-tradable:" in msg
    assert "Dynamic paper-tradable:" in msg
    assert "Observation:" in msg
    assert "Research candidates:" in msg
    assert "* SPY — Tradable | Score 100 | Proposal blocked: broad-market cluster limit due DIA/IWM" in msg
    assert "* SMH — Tradable | Score 91 | Proposal blocked: requires setup and RiskEngine pass" in msg
    assert "* OBS0 — Not tradable | Score 80 | Proposal blocked: observation only; needs paper-tradable promotion" in msg
    assert "* AAL — Not tradable | Score 58 | Proposal blocked: research candidate only; needs observation promotion first" in msg
    assert "* Observation shown: top 6 of 7 by score" in msg
    assert "Actions:\n* Proposals 0 | Orders 0 | Fills 0 | GPT 0 | Expired 0" in msg

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


def test_digest_uses_authoritative_filled_state_over_stale_market_memory(temp_storage):
    config = {
        "mode": "paper",
        "live_enabled": False,
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
                "watchlist": ["IWM"],
                "observation_watchlist": [],
            }
        },
    }
    service = TradingService(config, temp_storage, MockBroker(), "run_id")
    service.telegram = MockTelegramBot()
    now = datetime.now(UTC)
    created_at = (now - timedelta(minutes=5)).isoformat()
    filled_at = (now - timedelta(minutes=1)).isoformat()

    temp_storage.execute(
        "INSERT INTO market_memory(run_id,market_profile,symbol,price,session_start_price,signal,score,classification,proposal_generated,no_action_reason,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("run1", "us_equities", "IWM", 298.878, 298.0, "ENTRY", 90.0, "Strong setup", 1, "proposal generated", created_at),
    )
    temp_storage.execute(
        "INSERT INTO trade_proposals(id,run_id,symbol,side,notional,status,created_at,expires_at,strategy_version,payload) VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("prop-iwm", "run1", "IWM", "buy", 15.0, "approved", created_at, (now + timedelta(minutes=10)).isoformat(), "rule_based_v1", json.dumps({"symbol": "IWM", "score": 90})),
    )
    temp_storage.execute(
        "INSERT INTO orders(id,run_id,proposal_id,broker_order_id,client_order_id,symbol,side,notional,qty,status,payload,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("order-iwm", "run1", "prop-iwm", "broker-iwm", "client-iwm", "IWM", "buy", 15.0, 0.050175614, "filled", "{}", created_at, filled_at),
    )
    temp_storage.execute(
        "INSERT INTO fills(run_id,order_id,qty,price,filled_at,payload,fill_notified_at,fill_notification_status,fill_notification_error) VALUES(?,?,?,?,?,?,?,?,?)",
        ("run1", "order-iwm", 0.050175614, 298.878, filled_at, "{}", None, None, None),
    )

    service.check_and_send_digest()

    msg = service.telegram.messages[0]
    assert "Status: Approved and filled — IWM paper buy filled" in msg
    assert "Proposal pending approval" not in msg
    assert "Summary: IWM was approved and filled during this window." in msg
    assert "Proposals: 1 | Orders: 1 | Fills: 1" in msg


def test_digest_counts_fill_with_space_separated_timestamp(temp_storage):
    config = {
        "mode": "paper",
        "live_enabled": False,
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
    created_at = (now - timedelta(minutes=5)).isoformat()
    filled_at_dt = now - timedelta(minutes=1)
    filled_at = filled_at_dt.strftime("%Y-%m-%d %H:%M:%S.%f+00:00")

    temp_storage.execute(
        "INSERT INTO market_memory(run_id,market_profile,symbol,price,session_start_price,signal,score,classification,proposal_generated,no_action_reason,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("run1", "us_equities", "DIA", 520.658, 519.0, "ENTRY", 100.0, "Very strong paper setup", 1, "proposal generated", created_at),
    )
    temp_storage.execute(
        "INSERT INTO trade_proposals(id,run_id,symbol,side,notional,status,created_at,expires_at,strategy_version,payload) VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("prop-dia", "run1", "DIA", "buy", 20.0, "approved", created_at, (now + timedelta(minutes=10)).isoformat(), "rule_based_v1", json.dumps({"symbol": "DIA", "score": 100})),
    )
    temp_storage.execute(
        "INSERT INTO orders(id,run_id,proposal_id,broker_order_id,client_order_id,symbol,side,notional,qty,status,payload,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("order-dia", "run1", "prop-dia", "broker-dia", "client-dia", "DIA", "buy", 20.0, 0.038400983, "filled", "{}", created_at, filled_at_dt.isoformat()),
    )
    temp_storage.execute(
        "INSERT INTO fills(run_id,order_id,qty,price,filled_at,payload,fill_notified_at,fill_notification_status,fill_notification_error) VALUES(?,?,?,?,?,?,?,?,?)",
        ("run1", "order-dia", 0.038400983, 520.658, filled_at, "{}", None, None, None),
    )

    service.check_and_send_digest()

    msg = service.telegram.messages[0]
    assert "Status: Approved and filled — DIA paper buy filled" in msg
    assert "Summary: DIA was approved and filled during this window." in msg
    assert "Proposals: 1 | Orders: 1 | Fills: 1" in msg


def test_digest_fill_count_ignores_fills_outside_the_window(temp_storage):
    config = {
        "mode": "paper",
        "live_enabled": False,
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
    created_at = (now - timedelta(minutes=5)).isoformat()
    filled_at = (now - timedelta(minutes=1)).isoformat()

    temp_storage.execute(
        "INSERT INTO market_memory(run_id,market_profile,symbol,price,session_start_price,signal,score,classification,proposal_generated,no_action_reason,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("run1", "us_equities", "DIA", 520.658, 519.0, "ENTRY", 100.0, "Very strong paper setup", 1, "proposal generated", created_at),
    )
    temp_storage.execute(
        "INSERT INTO trade_proposals(id,run_id,symbol,side,notional,status,created_at,expires_at,strategy_version,payload) VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("prop-dia", "run1", "DIA", "buy", 20.0, "approved", created_at, (now + timedelta(minutes=10)).isoformat(), "rule_based_v1", json.dumps({"symbol": "DIA", "score": 100})),
    )
    temp_storage.execute(
        "INSERT INTO orders(id,run_id,proposal_id,broker_order_id,client_order_id,symbol,side,notional,qty,status,payload,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("order-dia", "run1", "prop-dia", "broker-dia", "client-dia", "DIA", "buy", 20.0, 0.038400983, "filled", "{}", created_at, filled_at),
    )
    temp_storage.execute(
        "INSERT INTO fills(run_id,order_id,qty,price,filled_at,payload,fill_notified_at,fill_notification_status,fill_notification_error) VALUES(?,?,?,?,?,?,?,?,?)",
        ("run1", "order-dia", 0.038400983, 520.658, filled_at, "{}", None, None, None),
    )
    old_created_at = (now - timedelta(minutes=45)).isoformat()
    old_filled_at = (now - timedelta(minutes=40)).isoformat()
    temp_storage.execute(
        "INSERT INTO trade_proposals(id,run_id,symbol,side,notional,status,created_at,expires_at,strategy_version,payload) VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("prop-dia-old", "run0", "DIA", "buy", 5.0, "filled", old_created_at, (now - timedelta(minutes=35)).isoformat(), "rule_based_v1", json.dumps({"symbol": "DIA", "score": 90})),
    )
    temp_storage.execute(
        "INSERT INTO orders(id,run_id,proposal_id,broker_order_id,client_order_id,symbol,side,notional,qty,status,payload,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("order-dia-old", "run0", "prop-dia-old", "broker-dia-old", "client-dia-old", "DIA", "buy", 5.0, 0.01, "filled", "{}", old_created_at, old_filled_at),
    )
    temp_storage.execute(
        "INSERT INTO fills(run_id,order_id,qty,price,filled_at,payload,fill_notified_at,fill_notification_status,fill_notification_error) VALUES(?,?,?,?,?,?,?,?,?)",
        ("run0", "order-dia-old", 0.01, 500.0, old_filled_at, "{}", None, None, None),
    )

    service.check_and_send_digest()

    msg = service.telegram.messages[0]
    assert "Proposals: 1 | Orders: 1 | Fills: 1" in msg


def test_digest_delayed_fill_outside_window_does_not_count_as_window_fill(temp_storage):
    config = {
        "mode": "paper",
        "live_enabled": False,
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
                "watchlist": ["SPY"],
                "observation_watchlist": [],
            }
        },
    }
    service = TradingService(config, temp_storage, MockBroker(), "run_id")
    service.telegram = MockTelegramBot()
    now = datetime.now(UTC)
    market_memory_at = (now - timedelta(minutes=5)).isoformat()
    old_created_at = (now - timedelta(minutes=40)).isoformat()
    old_filled_at = (now - timedelta(minutes=35)).isoformat()
    refreshed_at = (now - timedelta(minutes=2)).isoformat()

    temp_storage.execute(
        "INSERT INTO market_memory(run_id,market_profile,symbol,price,session_start_price,signal,score,classification,proposal_generated,no_action_reason,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("run1", "us_equities", "SPY", 600.0, 600.0, "HOLD", 55.0, "Weak setup", 0, "no entry/exit signal", market_memory_at),
    )
    temp_storage.execute(
        "INSERT INTO trade_proposals(id,run_id,symbol,side,notional,status,created_at,expires_at,strategy_version,payload) VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("old-prop", "old-run", "SPY", "buy", 10.0, "filled", old_created_at, (now - timedelta(minutes=30)).isoformat(), "rule_based_v1", "{}"),
    )
    temp_storage.execute(
        "INSERT INTO orders(id,run_id,proposal_id,broker_order_id,client_order_id,symbol,side,notional,qty,status,payload,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("old-order", "old-run", "old-prop", "broker-spy", "client-spy", "SPY", "buy", 10.0, 0.01, "filled", "{}", old_created_at, refreshed_at),
    )
    temp_storage.execute(
        "INSERT INTO fills(run_id,order_id,qty,price,filled_at,payload,fill_notified_at,fill_notification_status,fill_notification_error) VALUES(?,?,?,?,?,?,?,?,?)",
        ("old-run", "old-order", 0.01, 600.0, old_filled_at, "{}", refreshed_at, "sent", None),
    )

    service.check_and_send_digest()

    msg = service.telegram.messages[0]
    assert "Approved and filled" not in msg
    assert "Proposals: 0 | Orders: 0 | Fills: 0" in msg
    assert "Summary:" in msg


def test_digest_does_not_treat_stale_approved_history_as_pending(temp_storage):
    config = {
        "mode": "paper",
        "live_enabled": False,
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
        "INSERT INTO market_memory(run_id,market_profile,symbol,price,session_start_price,signal,score,classification,proposal_generated,no_action_reason,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("run1", "us_equities", "DIA", 350.0, 350.0, "HOLD", 88.0, "Watch", 0, "no entry/exit signal", now.isoformat()),
    )
    temp_storage.execute(
        "INSERT INTO trade_proposals(id,run_id,symbol,side,notional,status,created_at,expires_at,strategy_version,payload) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            "old-dia",
            "old-run",
            "DIA",
            "buy",
            10.0,
            "approved",
            (now - timedelta(days=2)).isoformat(),
            (now - timedelta(days=2, minutes=-10)).isoformat(),
            "rule_based_v1",
            "{}",
        ),
    )

    service.check_and_send_digest()

    msg = service.telegram.messages[0]
    assert "pending approval" not in msg.lower()
    assert "DIA has an active proposal pending approval." not in msg


def test_digest_uses_authoritative_submitted_state_before_fill(temp_storage):
    config = {
        "mode": "paper",
        "live_enabled": False,
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
                "watchlist": ["SPY"],
                "observation_watchlist": [],
            }
        },
    }
    service = TradingService(config, temp_storage, MockBroker(), "run_id")
    service.telegram = MockTelegramBot()
    now = datetime.now(UTC)
    created_at = (now - timedelta(minutes=4)).isoformat()

    temp_storage.execute(
        "INSERT INTO market_memory(run_id,market_profile,symbol,price,session_start_price,signal,score,classification,proposal_generated,no_action_reason,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("run1", "us_equities", "SPY", 600.0, 599.0, "ENTRY", 88.0, "Strong setup", 1, "proposal generated", created_at),
    )
    temp_storage.execute(
        "INSERT INTO trade_proposals(id,run_id,symbol,side,notional,status,created_at,expires_at,strategy_version,payload) VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("prop-spy", "run1", "SPY", "buy", 10.0, "submitted", created_at, (now + timedelta(minutes=10)).isoformat(), "rule_based_v1", "{}"),
    )
    temp_storage.execute(
        "INSERT INTO orders(id,run_id,proposal_id,broker_order_id,client_order_id,symbol,side,notional,qty,status,payload,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("order-spy", "run1", "prop-spy", "broker-spy", "client-spy", "SPY", "buy", 10.0, 0.013596, "submitted", "{}", created_at, created_at),
    )

    service.check_and_send_digest()

    msg = service.telegram.messages[0]
    assert "Status: Approved — order submitted, awaiting fill" in msg
    assert "Proposal pending approval" not in msg
    assert "Summary: SPY was approved and submitted during this window." in msg


def test_digest_uses_authoritative_expired_state(temp_storage):
    config = {
        "mode": "paper",
        "live_enabled": False,
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
                "watchlist": ["XLV"],
                "observation_watchlist": [],
            }
        },
    }
    service = TradingService(config, temp_storage, MockBroker(), "run_id")
    service.telegram = MockTelegramBot()
    now = datetime.now(UTC)
    created_at = (now - timedelta(minutes=8)).isoformat()
    expired_at = (now - timedelta(minutes=2)).isoformat()

    temp_storage.execute(
        "INSERT INTO market_memory(run_id,market_profile,symbol,price,session_start_price,signal,score,classification,proposal_generated,no_action_reason,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("run1", "us_equities", "XLV", 140.0, 139.0, "ENTRY", 77.0, "Qualified setup", 1, "proposal generated", created_at),
    )
    temp_storage.execute(
        "INSERT INTO trade_proposals(id,run_id,symbol,side,notional,status,created_at,expires_at,strategy_version,payload) VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("prop-xlv", "run1", "XLV", "buy", 10.0, "expired", created_at, expired_at, "rule_based_v1", "{}"),
    )

    service.check_and_send_digest()

    msg = service.telegram.messages[0]
    assert "Status: Proposal expired — no order" in msg
    assert "Expired with no order: XLV." in msg


def test_digest_cluster_summary_names_held_symbols_without_duplicate_strongest(temp_storage):
    config = {
        "mode": "paper",
        "live_enabled": False,
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
                "watchlist": ["SPY", "DIA", "IWM", "XLE", "XLV", "XLY"],
                "observation_watchlist": [],
            }
        },
    }
    broker = MockBroker()
    broker.positions = [
        type("Pos", (), {"symbol": "DIA", "qty": 0.01, "avg_entry_price": 500.0, "current_price": 505.0, "market_value": 5.05})(),
        type("Pos", (), {"symbol": "IWM", "qty": 0.05, "avg_entry_price": 298.0, "current_price": 299.0, "market_value": 14.95})(),
    ]
    service = TradingService(config, temp_storage, broker, "run_id")
    service.telegram = MockTelegramBot()
    now = datetime.now(UTC).isoformat()

    rows = [
        ("SPY", 600.0, "ENTRY", 100.0, "Strongest setup", 0, "not actionable - pre-proposal risk check failed: same cluster positions limit"),
        ("DIA", 505.0, "HOLD", 82.0, "Qualified watch", 0, "no entry/exit signal"),
        ("IWM", 299.0, "HOLD", 81.0, "Qualified watch", 0, "no entry/exit signal"),
        ("XLE", 95.0, "HOLD", 79.0, "Qualified watch", 0, "no entry/exit signal"),
        ("XLV", 140.0, "HOLD", 78.0, "Qualified watch", 0, "no entry/exit signal"),
        ("XLY", 180.0, "HOLD", 77.0, "Qualified watch", 0, "no entry/exit signal"),
    ]
    for symbol, price, signal, score, classification, proposal_generated, no_action_reason in rows:
        temp_storage.execute(
            "INSERT INTO market_memory(run_id,market_profile,symbol,price,session_start_price,signal,score,classification,proposal_generated,no_action_reason,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("run1", "us_equities", symbol, price, price, signal, score, classification, proposal_generated, no_action_reason, now),
        )

    service.check_and_send_digest()

    msg = service.telegram.messages[0]
    assert "Status: Watch — broad-market cluster limit reached: existing DIA and IWM positions" in msg
    assert "Summary: SPY scored highest, but it was blocked by the broad-market cluster limit because DIA and IWM are already held." in msg
    assert "SPY was strongest and DIA, IWM, SPY" not in msg


def test_digest_tier_sorting_and_score_source_labels(temp_storage):
    config = {
        "mode": "paper",
        "live_enabled": False,
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
                "watchlist": ["SPY", "QQQ"],
                "observation_watchlist": ["AMGN"],
            }
        },
    }
    broker = MockBroker()
    service = TradingService(config, temp_storage, broker, "run_id")
    service.telegram = MockTelegramBot()
    now = datetime.now(UTC)

    # SPY: static paper-tradable, score = 45.5 in universe_symbols, but scanned with trade_score = 95.0
    temp_storage.execute(
        "INSERT INTO universe_symbols(id,symbol,tier,source,score,updated_at,created_at) VALUES(?,?,?,?,?,?,?)",
        ("u-spy", "SPY", "paper_tradable", "existing_static_watchlist", 45.5, now.isoformat(), now.isoformat())
    )
    temp_storage.execute(
        "INSERT INTO market_memory(run_id,market_profile,symbol,price,session_start_price,signal,score,classification,proposal_generated,no_action_reason,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("run1", "us_equities", "SPY", 400.0, 400.0, "HOLD", 95.0, "Qualified watch", 0, "no entry/exit signal", now.isoformat())
    )

    # QQQ: static paper-tradable, score = 45.5 in universe, scanned with trade_score = 88.0
    temp_storage.execute(
        "INSERT INTO universe_symbols(id,symbol,tier,source,score,updated_at,created_at) VALUES(?,?,?,?,?,?,?)",
        ("u-qqq", "QQQ", "paper_tradable", "existing_static_watchlist", 45.5, now.isoformat(), now.isoformat())
    )
    temp_storage.execute(
        "INSERT INTO market_memory(run_id,market_profile,symbol,price,session_start_price,signal,score,classification,proposal_generated,no_action_reason,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("run1", "us_equities", "QQQ", 500.0, 500.0, "HOLD", 88.0, "Qualified watch", 0, "no entry/exit signal", now.isoformat())
    )

    # AMGN: observation symbol, score = 93.0 in universe
    temp_storage.execute(
        "INSERT INTO universe_symbols(id,symbol,tier,source,score,updated_at,created_at) VALUES(?,?,?,?,?,?,?)",
        ("u-amgn", "AMGN", "observation", "dynamic_research", 93.0, now.isoformat(), now.isoformat())
    )

    # CFG: research candidate, score = 75.0 in universe
    temp_storage.execute(
        "INSERT INTO universe_symbols(id,symbol,tier,source,score,updated_at,created_at) VALUES(?,?,?,?,?,?,?)",
        ("u-cfg", "CFG", "research_candidate", "dynamic_research", 75.0, now.isoformat(), now.isoformat())
    )

    service.check_and_send_digest()

    assert len(service.telegram.messages) == 1
    msg = service.telegram.messages[0]

    # Verify score labeling and correct display values
    assert "* SPY — Tradable | Trade score 95 | Proposal blocked: no ENTRY signal" in msg
    assert "* QQQ — Tradable | Trade score 88 | Proposal blocked: no ENTRY signal" in msg
    assert "* AMGN — Not tradable | Research score 93 | Proposal blocked: needs paper-tradable promotion" in msg
    assert "* CFG — Not tradable | Research score 75 | Proposal blocked: needs observation promotion first" in msg

    # Verify tier headers in correct order
    idx_static = msg.index("Static paper-tradable:")
    idx_obs = msg.index("Observation:")
    idx_cfg = msg.index("Research candidates:")
    assert idx_static < idx_obs < idx_cfg

    # Verify no proposals or orders were created during formatting/sorting
    props = temp_storage.fetch_all("SELECT COUNT(*) as cnt FROM trade_proposals")[0]["cnt"]
    orders = temp_storage.fetch_all("SELECT COUNT(*) as cnt FROM orders")[0]["cnt"]
    assert props == 0
    assert orders == 0


def test_digest_eodhd_provider_status_reporting(temp_storage):
    config = {
        "mode": "paper",
        "live_enabled": False,
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
                "watchlist": ["SPY"],
                "observation_watchlist": [],
            }
        },
    }
    service = TradingService(config, temp_storage, MockBroker(), "run_id")
    service.telegram = MockTelegramBot()
    now = datetime.now(UTC)

    # Setup universe symbol so check_and_send_digest succeeds
    temp_storage.execute(
        "INSERT INTO universe_symbols(id,symbol,tier,source,score,updated_at,created_at) VALUES(?,?,?,?,?,?,?)",
        ("u-spy", "SPY", "paper_tradable", "existing_static_watchlist", 90.0, now.isoformat(), now.isoformat())
    )
    temp_storage.execute(
        "INSERT INTO market_memory(run_id,market_profile,symbol,price,session_start_price,signal,score,classification,proposal_generated,no_action_reason,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("run1", "us_equities", "SPY", 400.0, 400.0, "HOLD", 90.0, "Qualified watch", 0, "no entry/exit signal", now.isoformat())
    )

    # Case A: news rate limit
    temp_storage.execute("DELETE FROM data_provider_capabilities")
    future_cooldown = (now + timedelta(minutes=10)).isoformat()
    temp_storage.execute(
        "INSERT INTO data_provider_capabilities(id,run_id,provider,endpoint_name,available,plan_limited,disabled_until,last_error_category,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
        ("eodhd-news", "run_id", "eodhd", "news", 0, 0, future_cooldown, "rate_limited", now.isoformat())
    )
    temp_storage.execute(
        "INSERT INTO data_provider_capabilities(id,run_id,provider,endpoint_name,available,plan_limited,disabled_until,last_error_category,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
        ("eodhd-intraday", "run_id", "eodhd", "intraday_bars", 1, 0, None, None, now.isoformat())
    )
    temp_storage.execute(
        "INSERT INTO universe_research_runs(id,run_id,research_type,status,started_at,ended_at,symbols_considered,symbols_promoted,symbols_demoted,detail) VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("run-1", "run_id", "intraday_light_refresh", "completed", now.isoformat(), now.isoformat(), 1, 0, 0, "{}")
    )

    service.check_and_send_digest()
    msg = service.telegram.messages[-1]
    assert "* EODHD partial — optional news endpoint rate-limited" in msg

    # Case B: recovered from recent rate-limit
    temp_storage.execute("DELETE FROM telegram_digests")
    temp_storage.execute("DELETE FROM data_provider_capabilities")
    temp_storage.execute("DELETE FROM data_provider_health")
    temp_storage.execute(
        "INSERT INTO data_provider_capabilities(id,run_id,provider,endpoint_name,available,plan_limited,disabled_until,last_error_category,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
        ("eodhd-intraday", "run_id", "eodhd", "intraday_bars", 1, 0, None, None, now.isoformat())
    )
    # Insert a rate_limited log from 5 mins ago
    temp_storage.execute(
        "INSERT INTO data_provider_health(id,run_id,provider,status,checked_at) VALUES(?,?,?,?,?)",
        ("eodhd-h1", "run_id", "eodhd", "rate_limited", (now - timedelta(minutes=5)).isoformat())
    )

    service.check_and_send_digest()
    msg = service.telegram.messages[-1]
    assert "* EODHD recovered from recent rate-limit" in msg

    # Case C: plan-limited fundamentals
    temp_storage.execute("DELETE FROM telegram_digests")
    temp_storage.execute("DELETE FROM data_provider_capabilities")
    temp_storage.execute("DELETE FROM data_provider_health")
    temp_storage.execute(
        "INSERT INTO data_provider_capabilities(id,run_id,provider,endpoint_name,available,plan_limited,disabled_until,last_error_category,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
        ("eodhd-fundamentals", "run_id", "eodhd", "fundamentals", 0, 1, (now + timedelta(hours=10)).isoformat(), "forbidden", now.isoformat())
    )
    temp_storage.execute(
        "INSERT INTO data_provider_capabilities(id,run_id,provider,endpoint_name,available,plan_limited,disabled_until,last_error_category,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
        ("eodhd-intraday", "run_id", "eodhd", "intraday_bars", 1, 0, None, None, now.isoformat())
    )

    service.check_and_send_digest()
    msg = service.telegram.messages[-1]
    assert "* EODHD: fundamentals plan-limited" in msg

    # Case D: news cooldown active
    temp_storage.execute("DELETE FROM telegram_digests")
    temp_storage.execute("DELETE FROM data_provider_capabilities")
    temp_storage.execute("DELETE FROM data_provider_health")
    future_cooldown = (now + timedelta(minutes=10)).isoformat()
    temp_storage.execute(
        "INSERT INTO data_provider_capabilities(id,run_id,provider,endpoint_name,available,plan_limited,disabled_until,last_error_category,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
        ("eodhd-news", "run_id", "eodhd", "news", 0, 0, future_cooldown, "cooldown_active", now.isoformat())
    )
    temp_storage.execute(
        "INSERT INTO data_provider_capabilities(id,run_id,provider,endpoint_name,available,plan_limited,disabled_until,last_error_category,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
        ("eodhd-intraday", "run_id", "eodhd", "intraday_bars", 1, 0, None, None, now.isoformat())
    )
    temp_storage.execute(
        "INSERT INTO universe_research_runs(id,run_id,research_type,status,started_at,ended_at,symbols_considered,symbols_promoted,symbols_demoted,detail) VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("run-2", "run_id", "intraday_light_refresh", "completed", now.isoformat(), now.isoformat(), 1, 0, 0, "{}")
    )

    service.check_and_send_digest()
    msg = service.telegram.messages[-1]
    assert "* EODHD partial — intraday ok; news cooldown" in msg
