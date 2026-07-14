import json
import uuid
import time
import pytest
from datetime import datetime, UTC, timedelta
from unittest.mock import MagicMock, patch
import pandas as pd

from app.utils import load_config
from app.storage import Storage
from app.service import TradingService
from app.strategy_rule_based import Signal
from tests.test_combined_upgrades import MockBroker, MockClock

@pytest.fixture
def temp_storage(tmp_path):
    db_path = tmp_path / "test_trading_agent_portfolio.db"
    storage = Storage(db_path)
    storage.initialize()
    return storage

@pytest.fixture
def base_config():
    config = load_config()
    config["mode"] = "paper"
    config["live_enabled"] = False

    # Portfolio intelligence config
    config["portfolio_behavior"] = {
        "max_open_positions": 3,
        "max_new_buy_orders_per_day": 3,
        "max_new_buy_proposals_per_cycle": 1,
        "max_pending_buy_proposals": 1,
        "allow_add_to_existing_position": True,
        "block_averaging_down": True,
        "max_adds_per_symbol_per_day": 1,
        "max_total_adds_per_symbol": 2,
        "max_total_portfolio_exposure_pct": 6.0,
        "max_single_symbol_exposure_pct": 2.5,
        "max_correlated_us_equity_exposure_pct": 5.0,
        "block_new_buy_if_exit_pending": True,
        "block_new_buy_if_emergency_exit_score_above": 40
    }

    config["position_sizing"] = {
        "enabled": True,
        "mode": "risk_portfolio",
        "stage": "moderate_paper",
        "use_stage_dollar_cap": True,
        "stage_max_initial_notional_usd": {"moderate_paper": 250.0},
        "stage_max_add_notional_usd": {"moderate_paper": 100.0},
        "default_paper_notional_usd": 10.0,
        "default_add_notional_usd": 10.0,
        "minimum_executable_notional_usd": 5.0,
        "risk_per_trade_pct": 0.05,
        "max_trade_notional_pct_equity": 10.0,
        "max_position_notional_pct_equity": 10.0,
        "max_total_portfolio_exposure_pct": 50.0,
        "max_cluster_exposure_pct": 50.0,
        "min_cash_reserve_pct": 10.0,
        "max_cash_usage_pct": 50.0,
        "add_size_multiplier": 0.5,
        "stop_model": {
            "method": "max_of_atr_or_technical",
            "atr_multiple": 2.0,
            "technical_stop": "below_ma50_or_recent_swing_low",
            "max_stop_pct": 8.0,
            "min_stop_pct": 1.0
        },
        "score_multiplier": {
            "65_74": 0.5,
            "75_84": 1.0,
            "85_94": 1.5,
            "95_100": 2.0
        },
        "volatility_multiplier": {
            "too_quiet": 0.75,
            "normal": 1.0,
            "elevated": 0.5,
            "high": 0.0,
            "extreme": 0.0
        }
    }

    config["phase3"]["enabled"] = False
    config["phase3"]["active"] = False
    config["phase4"]["enabled"] = False
    config["phase4"]["active"] = False

    config["portfolio_optimizer"] = {
        "clusters": {
            "us_broad_market": ["SPY", "DIA", "IWM"],
            "us_growth_tech": ["QQQ", "XLK"]
        },
        "max_same_cluster_positions": 2,
        "max_same_cluster_exposure_pct": 5.0
    }

    return config

def test_calculate_dynamic_size_enabled(temp_storage, base_config):
    broker = MockBroker()
    service = TradingService(base_config, temp_storage, broker, "run-1")

    # Mock snapshot with $10,000 equity and no exposure
    snapshot = {
        "portfolio_equity": 10000.0,
        "cash": 10000.0,
        "buying_power": 10000.0,
        "total_exposure_dollars": 0.0,
        "single_exposures": {},
        "cluster_exposures": {}
    }

    # 20 bars, high/low/close to yield ATR of 1.0
    bars = pd.DataFrame({
        "high": [101.0] * 20,
        "low": [100.0] * 20,
        "close": [100.5] * 20,
        "volatility_20": [0.15] * 20
    })

    # Score 90 -> score mult 1.5, normal vol -> vol mult 1.0
    # Base paper notional 10.0 * 1.5 = 15.0 notional
    res = service._calculate_dynamic_size("SPY", 90.0, "normal", 100.0, bars, snapshot)

    assert res["final_notional"] == 250.0
    assert res["score_multiplier"] == 1.5
    assert res["volatility_multiplier"] == 1.0
    assert res["stop_model_used"] == "atr"
    assert res["risk_based_shares"] > 0
    assert res["score_adjusted_notional"] == 375.0

def test_calculate_dynamic_size_volatility_blocks(temp_storage, base_config):
    broker = MockBroker()
    service = TradingService(base_config, temp_storage, broker, "run-1")

    snapshot = {
        "portfolio_equity": 10000.0,
        "total_exposure_dollars": 0.0,
        "single_exposures": {},
        "cluster_exposures": {}
    }
    bars = pd.DataFrame({
        "high": [101.0] * 20,
        "low": [100.0] * 20,
        "close": [100.5] * 20,
        "volatility_20": [0.40] * 20
    })

    # High volatility regime -> vol mult 0.0 -> final_notional 0.0
    res = service._calculate_dynamic_size("SPY", 90.0, "high", 100.0, bars, snapshot)
    assert res["final_notional"] == 0.0
    assert res["volatility_multiplier"] == 0.0

def test_get_exposure_snapshot(temp_storage, base_config):
    broker = MockBroker()
    # Mock positions in Alpaca: SPY with market value $100 (1.0% of $10,000 equity)
    broker.positions = [
        type("P", (), {"symbol": "SPY", "market_value": 100.0, "qty": 1.0, "avg_entry_price": 100.0, "unrealized_pl": 0.0})()
    ]

    service = TradingService(base_config, temp_storage, broker, "run-1")
    snapshot = service._get_exposure_snapshot(broker.get_positions(), broker.get_account())

    assert snapshot["portfolio_equity"] == 1000000.0 # From MockBroker default
    assert snapshot["total_exposure_dollars"] == 100.0
    assert snapshot["single_exposures"]["SPY"] == (100.0 / 1000000.0) * 100
    assert snapshot["cluster_exposures"]["us_broad_market"] == (100.0 / 1000000.0) * 100

def test_candidate_ranking(base_config, temp_storage):
    broker = MockBroker()
    service = TradingService(base_config, temp_storage, broker, "run-1")

    snapshot = {
        "portfolio_equity": 10000.0,
        "total_exposure_dollars": 0.0,
        "single_exposures": {},
        "cluster_exposures": {
            "us_broad_market": 4.0 # Elevated exposure in us_broad_market
        },
        "as_of": "2026-07-14T08:00:00+00:00",
        "equity_as_of": "2026-07-14T08:00:00+00:00",
        "cluster_counts": {
            "us_broad_market": 1
        }
    }

    candidates = [
        {"symbol": "SPY", "score": 90.0, "final_notional": 15.0, "price": 100.0, "stop_distance_dollars": 5.0, "is_observation": False},
        {"symbol": "QQQ", "score": 85.0, "final_notional": 10.0, "price": 100.0, "stop_distance_dollars": 5.0, "is_observation": False}
    ]

    ranked = service._rank_candidates(candidates, snapshot)

    # QQQ has no cluster exposure currently (0.0), so its portfolio_fit_score is 100.
    # SPY is in us_broad_market which has 4% exposure, so portfolio_fit is 100 - 40 = 60.
    # QQQ should rank higher or rank correctly based on ranking_score formula.
    assert len(ranked) == 2
    assert ranked[0]["final_candidate_rank"] == 1

def test_pyramiding_eligibility(temp_storage, base_config):
    base_config.setdefault("position_management", {})["enabled"] = False
    base_config.setdefault("risk", {})["require_gpt_review_for_buy_proposals"] = False
    broker = MockBroker()
    # We hold SPY at avg_entry 95.0, current price is 100.0 (profitable!)
    broker.positions = [
        type("P", (), {"symbol": "SPY", "market_value": 100.0, "qty": 1.0, "avg_entry_price": 95.0, "unrealized_pl": 5.0})()
    ]

    # Insert entry trade proposal so we can add to it (we set action inside payload)
    temp_storage.execute(
        "INSERT INTO trade_proposals(id,run_id,symbol,side,status,created_at,expires_at,payload) VALUES(?,?,?,?,?,?,?,?)",
        ("prop-1", "run-1", "SPY", "buy", "filled", "2026-06-22T12:00:00", "2026-06-22T12:05:00", json.dumps({"score": 80.0, "action": "entry"}))
    )
    # Phase 0 projected-open-risk checks fail closed unless the held lifecycle has
    # an auditable initial stop rather than silently treating its risk as zero.
    temp_storage.execute(
        "INSERT INTO position_management_state(id,symbol,avg_entry_price,quantity,initial_stop_price,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
        ("pm-spy", "SPY", 95.0, 1.0, 90.0, "2026-06-22T12:00:00", "2026-06-22T12:00:00"),
    )

    service = TradingService(base_config, temp_storage, broker, "run-2")
    service.telegram = MagicMock()
    service.telegram.is_available.return_value = True

    # Mock evaluate_symbol to return entry signal for SPY with score 90 (improvement of +10)
    with patch("app.service.evaluate_symbol") as mock_eval:
        def side_effect(sym, *args, **kwargs):
            if sym == "SPY":
                return Signal("ENTRY", "buy", "SPY", "trend filters passed and volatility normal", 0.9, {"close": 100.0, "ma_50": 95.0, "ma_200": 90.0, "volatility_20": 0.1})
            return Signal("HOLD", None, sym, "not SPY", 0.0, {})
        mock_eval.side_effect = side_effect

        service.scan()

    # Check that a BUY ADD proposal was created (since it's added, the proposal action field in payload is "add")
    props = temp_storage.fetch_all("SELECT payload, side, notional, status FROM trade_proposals WHERE symbol='SPY' AND status='pending'")
    assert len(props) == 1
    p_load = json.loads(props[0]["payload"])
    assert p_load["action"] == "add"
    assert p_load["initial_stop_price"] is not None
    assert p_load["initial_risk_per_share"] is not None
    sizing = temp_storage.fetch_all("SELECT initial_stop_price, initial_risk_per_share, candidate_id, proposal_id FROM position_sizing_decisions WHERE symbol='SPY'")
    assert len(sizing) == 1
    assert sizing[0]["initial_stop_price"] is not None
    assert sizing[0]["initial_risk_per_share"] is not None

def test_shadow_trades_creation(temp_storage, base_config):
    broker = MockBroker()
    service = TradingService(base_config, temp_storage, broker, "run-1")
    service.telegram = MagicMock()
    service.telegram.is_available.return_value = True

    # Sleep mode is active, so standard proposal is suppressed
    temp_storage.execute("INSERT OR REPLACE INTO control_state(key, value) VALUES('sleep_mode_active', '1')")

    with patch("app.service.evaluate_symbol") as mock_eval:
        def side_effect(sym, *args, **kwargs):
            if sym == "SPY":
                return Signal("ENTRY", "buy", "SPY", "trend up", 0.8, {"close": 100.0, "ma_50": 95.0, "ma_200": 90.0, "volatility_20": 0.15})
            return Signal("HOLD", None, sym, "not SPY", 0.0, {})
        mock_eval.side_effect = side_effect

        service.scan()

    # Standard proposals should be 0 because of sleep mode
    props = temp_storage.fetch_all("SELECT * FROM trade_proposals WHERE status='pending'")
    assert len(props) == 0

    # But a shadow trade should be recorded in shadow_trades table!
    shadows = temp_storage.fetch_all("SELECT * FROM shadow_trades WHERE symbol='SPY'")
    assert len(shadows) == 1
    assert "suppressed" in shadows[0]["reason_not_executed"]

def test_final_revalidation(temp_storage, base_config):
    broker = MockBroker()
    service = TradingService(base_config, temp_storage, broker, "run-1")
    service.telegram = MagicMock()
    service.telegram.is_authorized = MagicMock(return_value=True)
    service.telegram.allowed_user_id = "123456789"

    # Stored pending proposal
    now_iso = datetime.now(UTC).isoformat()
    expires_iso = (datetime.now(UTC) + timedelta(minutes=10)).isoformat()
    temp_storage.execute(
        "INSERT INTO trade_proposals(id,run_id,symbol,side,status,created_at,expires_at,payload,current_price) VALUES(?,?,?,?,?,?,?,?,?)",
        ("prop-2", "run-1", "SPY", "buy", "pending", now_iso, expires_iso, json.dumps({"latest_price": 100.0, "action": "entry"}), 100.0)
    )

    # Mock Telegram to return the user's YES message (we set date to +10s relative to start to prevent bootstrap stale check)
    update = {
        "update_id": 10002,
        "message": {
            "message_id": 20002,
            "from": {"id": "123456789"},
            "chat": {"id": "123456789"},
            "date": int(time.time()) + 10,
            "text": "yes"
        }
    }
    service.telegram.get_updates.side_effect = [[update], []]

    # Mock broker price to 105.0 (5% move up, exceeding slippage limit)
    broker.price = 105.0

    service.process_telegram() # triggers execute_approved_proposals path

    # A pre-submission validation block is a local approval decision, not a broker
    # order and not an order intent, so reconciliation must never look it up.
    orders = temp_storage.fetch_all("SELECT * FROM orders")
    assert orders == []
    assert temp_storage.fetch_all("SELECT * FROM order_intents") == []
    assert len(broker.orders) == 0

    # The approval status should be updated to blocked
    app_status = temp_storage.fetch_all("SELECT final_order_decision, final_block_reason FROM approvals WHERE proposal_id='prop-2'")[0]
    assert app_status["final_order_decision"] == "blocked"
    assert "Price moved" in app_status["final_block_reason"]


def test_new_buy_persists_initial_risk_fields(temp_storage, base_config):
    base_config.setdefault("risk", {})["require_gpt_review_for_buy_proposals"] = False
    broker = MockBroker()
    service = TradingService(base_config, temp_storage, broker, "run-buy")
    service.telegram = MagicMock()
    service.telegram.is_available.return_value = True

    with patch("app.service.evaluate_symbol") as mock_eval:
        def side_effect(sym, *args, **kwargs):
            if sym == "SPY":
                return Signal("ENTRY", "buy", "SPY", "trend filters passed and volatility normal", 0.9, {"close": 100.0, "ma_50": 95.0, "ma_200": 90.0, "volatility_20": 0.1})
            return Signal("HOLD", None, sym, "not SPY", 0.0, {})
        mock_eval.side_effect = side_effect
        service.scan()

    proposal = temp_storage.fetch_all("SELECT payload FROM trade_proposals WHERE symbol='SPY' AND status='pending'")[0]
    payload = json.loads(proposal["payload"])
    assert payload["initial_stop_price"] is not None
    assert payload["initial_risk_per_share"] is not None
    assert payload["entry_price_for_r"] is not None
    assert payload["r_multiple_unavailable_reason"] is None
    sizing = temp_storage.fetch_all("SELECT initial_stop_price, initial_risk_per_share, entry_price_for_r, r_multiple_unavailable_reason FROM position_sizing_decisions WHERE symbol='SPY'")[0]
    assert sizing["initial_stop_price"] is not None
    assert sizing["initial_risk_per_share"] is not None
    assert sizing["entry_price_for_r"] is not None
    assert sizing["r_multiple_unavailable_reason"] is None
