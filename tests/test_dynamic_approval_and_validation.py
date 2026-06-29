import json
import uuid
import pytest
from datetime import datetime, UTC, timedelta
import pandas as pd
from unittest.mock import MagicMock, patch

from app.storage import Storage
from app.service import TradingService
from app.risk_engine import RiskEngine
from app.execution import Executor, ExecutionResult
from app.power import PowerStatus
from app.strategy_rule_based import Signal

class MockTelegramBot:
    def __init__(self):
        self.messages = []
        self.updates = []
    def send_message(self, text, chat_id=None):
        self.messages.append(text)
        return {"message_id": 123}
    def is_authorized(self, sender_id):
        return True
    def is_available(self, force=False):
        return True
    def get_updates(self, timeout=0, offset=None):
        updates = list(self.updates)
        self.updates.clear()
        return updates

class MockClock:
    def __init__(self, is_open=True):
        self.is_open = is_open
        self.timestamp = datetime.now(UTC)
        self.next_close = self.timestamp + timedelta(hours=2)

class MockBroker:
    def __init__(self):
        self.positions = []
        self.orders = []
        self.open = True
        self.price = 100.0
        self.price_time = datetime.now(UTC)
        self.clock = MockClock()
        self.submitted_orders = []

    def get_positions(self):
        return self.positions

    def get_open_orders(self):
        return self.orders

    def is_market_open(self):
        return self.open

    def get_latest_price(self, symbol):
        return type("T", (), {"price": self.price, "timestamp": self.price_time})()

    def get_historical_bars(self, symbol, timeframe, limit=250):
        return pd.DataFrame({"close": [self.price] * limit, "volume": [10000.0] * limit})

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
        order_info = {
            "symbol": symbol,
            "side": side,
            "order_args": order_args,
            "order_type": order_type,
            "limit_price": limit_price,
            "client_order_id": client_order_id
        }
        self.submitted_orders.append(order_info)
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
        "portfolio_execution_mode": "risk_budgeted",
        "watchlist": ["SPY", "QQQ"],
        "approved_strategy_versions": ["rule_based_v1"],
        "proposal_mode": {
            "type": "ranked_batch",
            "allow_yes_all_for_paper": True,
            "yes_all_requires_each_trade_final_revalidation": True
        },
        "risk": {
            "max_trade_notional_paper": 50,
            "max_trades_per_day": 10,
            "max_open_positions": 10,
            "min_historical_bars": 10,
            "max_price_age_seconds": 120,
            "allowed_order_types": ["market", "limit"],
            "stop_if_daily_loss_exceeds": 5,
            "stop_if_weekly_loss_exceeds": 10
        },
        "market_profiles": {
            "us_equities": {
                "name": "US Equities Night Market",
                "status": "active",
                "timezone": "America/New_York",
                "session_hours": "09:30-16:00",
                "execution_enabled": True,
                "proposals_enabled": True,
                "broker": "alpaca",
                "watchlist": ["SPY", "QQQ"],
                "observation_watchlist": []
            }
        },
        "telegram": {
            "approval_enabled": True,
            "telegram_approval_listener_enabled": True,
            "approval_price_refresh_required": True,
            "approval_max_price_age_seconds": 60,
            "approval_max_price_move_bps": 25,
            "allow_plain_yes_only_when_single_pending_proposal": True
        },
        "dynamic_universe": {
            "enabled": True,
            "paper_only": True
        }
    }

def helper_create_proposal(storage, symbol, approved_dynamic=False, universe_source="static", status="pending"):
    pid = str(uuid.uuid4())
    now = datetime.now(UTC)
    payload = {
        "id": pid,
        "symbol": symbol,
        "side": "buy",
        "action": "entry",
        "notional": 5.0,
        "latest_price": 100.0,
        "price_at": now.isoformat(),
        "historical_bars": 10,
        "volume": 1000,
        "price_gap_pct": 0,
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=10)).isoformat(),
        "strategy_version": "rule_based_v1",
        "reason": "trend passed",
        "order_type": "market",
        "asset_class": "equity",
        "approved_dynamic_paper_tradable": approved_dynamic,
        "universe_source": universe_source,
        "approved_market_profile": "us_equities",
        "client_order_id": f"mock-client-id-{pid}"
    }
    storage.execute(
        "INSERT INTO trade_proposals(id, run_id, symbol, side, notional, status, created_at, expires_at, strategy_version, payload) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (pid, "run-1", symbol, "buy", 5.0, status, now.isoformat(), (now + timedelta(minutes=10)).isoformat(), "rule_based_v1", json.dumps(payload))
    )
    return pid

def test_static_resolves_profile(temp_storage, base_config):
    # 1. Static paper-tradable proposal can resolve profile at final validation
    proposal = {
        "id": "p-static",
        "symbol": "SPY",
        "side": "buy",
        "action": "entry",
        "notional": 5.0,
        "latest_price": 100.0,
        "price_at": datetime.now(UTC).isoformat(),
        "historical_bars": 100,
        "volume": 1000,
        "price_gap_pct": 0,
        "created_at": datetime.now(UTC).isoformat(),
        "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
        "strategy_version": "rule_based_v1",
        "reason": "trend passed",
        "order_type": "market",
        "asset_class": "equity",
        "approved_dynamic_paper_tradable": False,
        "universe_source": "static",
        "approved_market_profile": "us_equities",
        "client_order_id": "test-static-client-id"
    }
    context = {
        "power_connected": True,
        "internet_available": True,
        "database_writable": True,
        "broker_available": True,
        "telegram_available": True,
        "market_open": True,
        "kill_switch": False,
        "open_positions": 0,
        "trades_today": 0,
        "duplicate_order": False,
        "same_symbol_position": False,
        "uses_margin": False,
        "daily_loss": 0.0,
        "weekly_loss": 0.0,
        "buying_power": 100000.0,
        "approval_valid": True,
        "proposed_total_exposure_pct": 0.0,
        "proposed_symbol_exposure_pct": 0.0,
        "proposed_cluster_positions_count": 0,
        "proposed_cluster_exposure_pct": 0.0,
        "exit_pending": False,
        "final_revalidation": True
    }
    decision = RiskEngine(base_config).evaluate(proposal, context, final=True)
    assert decision.passed

def test_static_missing_profile_blocked(temp_storage, base_config):
    # 2. Static symbol with missing profile is blocked
    # (Evaluating a symbol not in watchlist or observation_watchlist)
    proposal = {
        "id": "p-missing",
        "symbol": "MSFT",
        "side": "buy",
        "action": "entry",
        "notional": 5.0,
        "latest_price": 100.0,
        "price_at": datetime.now(UTC).isoformat(),
        "historical_bars": 100,
        "volume": 1000,
        "price_gap_pct": 0,
        "created_at": datetime.now(UTC).isoformat(),
        "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
        "strategy_version": "rule_based_v1",
        "reason": "trend passed",
        "order_type": "market",
        "asset_class": "equity",
        "approved_dynamic_paper_tradable": False,
        "universe_source": "static",
        "approved_market_profile": "us_equities",
        "client_order_id": "test-static-client-id"
    }
    context = {
        "power_connected": True,
        "internet_available": True,
        "database_writable": True,
        "broker_available": True,
        "telegram_available": True,
        "market_open": True,
        "kill_switch": False,
        "open_positions": 0,
        "trades_today": 0,
        "duplicate_order": False,
        "same_symbol_position": False,
        "uses_margin": False,
        "daily_loss": 0.0,
        "weekly_loss": 0.0,
        "buying_power": 100000.0,
        "approval_valid": True,
        "proposed_total_exposure_pct": 0.0,
        "proposed_symbol_exposure_pct": 0.0,
        "proposed_cluster_positions_count": 0,
        "proposed_cluster_exposure_pct": 0.0,
        "exit_pending": False,
        "final_revalidation": True
    }
    decision = RiskEngine(base_config).evaluate(proposal, context, final=True)
    assert not decision.passed
    assert "symbol not found in static or dynamic paper-tradable profiles" in decision.reasons

def test_static_stale_alpaca_blocked(temp_storage, base_config):
    # 3. Static symbol with stale Alpaca data is blocked
    proposal = {
        "id": "p-stale",
        "symbol": "SPY",
        "side": "buy",
        "action": "entry",
        "notional": 5.0,
        "latest_price": 100.0,
        "price_at": (datetime.now(UTC) - timedelta(minutes=5)).isoformat(), # Stale price_at
        "historical_bars": 100,
        "volume": 1000,
        "price_gap_pct": 0,
        "created_at": datetime.now(UTC).isoformat(),
        "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
        "strategy_version": "rule_based_v1",
        "reason": "trend passed",
        "order_type": "market",
        "asset_class": "equity",
        "approved_dynamic_paper_tradable": False,
        "universe_source": "static",
        "approved_market_profile": "us_equities",
        "client_order_id": "test-static-client-id"
    }
    context = {
        "power_connected": True,
        "internet_available": True,
        "database_writable": True,
        "broker_available": True,
        "telegram_available": True,
        "market_open": True,
        "kill_switch": False,
        "open_positions": 0,
        "trades_today": 0,
        "duplicate_order": False,
        "same_symbol_position": False,
        "uses_margin": False,
        "daily_loss": 0.0,
        "weekly_loss": 0.0,
        "buying_power": 100000.0,
        "approval_valid": True,
        "proposed_total_exposure_pct": 0.0,
        "proposed_symbol_exposure_pct": 0.0,
        "proposed_cluster_positions_count": 0,
        "proposed_cluster_exposure_pct": 0.0,
        "exit_pending": False,
        "final_revalidation": True
    }
    decision = RiskEngine(base_config).evaluate(proposal, context, final=True)
    assert not decision.passed
    assert "price timestamp must be fresh" in decision.reasons

def test_dynamic_resolves_profile(temp_storage, base_config):
    # 4. Dynamic paper-tradable proposal can resolve profile at final validation
    # 5. Dynamic paper-tradable proposal uses Alpaca trading data at final validation
    # 6. Dynamic paper-tradable proposal with fresh Alpaca data can pass profile resolution
    proposal = {
        "id": "p-dynamic",
        "symbol": "ARM",
        "side": "buy",
        "action": "entry",
        "notional": 5.0,
        "latest_price": 100.0,
        "price_at": datetime.now(UTC).isoformat(),
        "historical_bars": 100,
        "volume": 1000,
        "price_gap_pct": 0,
        "created_at": datetime.now(UTC).isoformat(),
        "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
        "strategy_version": "rule_based_v1",
        "reason": "trend passed",
        "order_type": "market",
        "asset_class": "equity",
        "approved_dynamic_paper_tradable": True,
        "universe_source": "dynamic",
        "approved_market_profile": "us_equities",
        "client_order_id": "test-dynamic-client-id"
    }
    context = {
        "power_connected": True,
        "internet_available": True,
        "database_writable": True,
        "broker_available": True,
        "telegram_available": True,
        "market_open": True,
        "kill_switch": False,
        "open_positions": 0,
        "trades_today": 0,
        "duplicate_order": False,
        "same_symbol_position": False,
        "uses_margin": False,
        "daily_loss": 0.0,
        "weekly_loss": 0.0,
        "buying_power": 100000.0,
        "approval_valid": True,
        "proposed_total_exposure_pct": 0.0,
        "proposed_symbol_exposure_pct": 0.0,
        "proposed_cluster_positions_count": 0,
        "proposed_cluster_exposure_pct": 0.0,
        "exit_pending": False,
        "final_revalidation": True,
        "universe_symbol_info": {
            "symbol": "ARM",
            "tier": "paper_tradable",
            "universe_lane": "alpaca_compatible_us",
            "alpaca_compatible": 1,
            "executable": 1
        },
        "active_dynamic_paper_tradable_symbols": ["ARM"]
    }
    decision = RiskEngine(base_config).evaluate(proposal, context, final=True)
    assert decision.passed

def test_dynamic_missing_from_active_blocked(temp_storage, base_config):
    # 7. Dynamic paper-tradable symbol missing from active dynamic profile set is blocked
    proposal = {
        "id": "p-dynamic",
        "symbol": "ARM",
        "side": "buy",
        "action": "entry",
        "notional": 5.0,
        "latest_price": 100.0,
        "price_at": datetime.now(UTC).isoformat(),
        "historical_bars": 100,
        "volume": 1000,
        "price_gap_pct": 0,
        "created_at": datetime.now(UTC).isoformat(),
        "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
        "strategy_version": "rule_based_v1",
        "reason": "trend passed",
        "order_type": "market",
        "asset_class": "equity",
        "approved_dynamic_paper_tradable": True,
        "universe_source": "dynamic",
        "approved_market_profile": "us_equities",
        "client_order_id": "test-dynamic-client-id"
    }
    context = {
        "power_connected": True,
        "internet_available": True,
        "database_writable": True,
        "broker_available": True,
        "telegram_available": True,
        "market_open": True,
        "kill_switch": False,
        "open_positions": 0,
        "trades_today": 0,
        "duplicate_order": False,
        "same_symbol_position": False,
        "uses_margin": False,
        "daily_loss": 0.0,
        "weekly_loss": 0.0,
        "buying_power": 100000.0,
        "approval_valid": True,
        "proposed_total_exposure_pct": 0.0,
        "proposed_symbol_exposure_pct": 0.0,
        "proposed_cluster_positions_count": 0,
        "proposed_cluster_exposure_pct": 0.0,
        "exit_pending": False,
        "final_revalidation": True,
        "universe_symbol_info": {
            "symbol": "ARM",
            "tier": "paper_tradable",
            "universe_lane": "alpaca_compatible_us",
            "alpaca_compatible": 1,
            "executable": 1
        },
        "active_dynamic_paper_tradable_symbols": [] # Missing from active set
    }
    decision = RiskEngine(base_config).evaluate(proposal, context, final=True)
    assert not decision.passed
    assert "dynamic symbol no longer paper-tradable at final validation" in decision.reasons

def test_dynamic_demoted_blocked(temp_storage, base_config):
    # 8. Dynamic symbol that was demoted after proposal is blocked at final validation
    proposal = {
        "id": "p-dynamic",
        "symbol": "ARM",
        "side": "buy",
        "action": "entry",
        "notional": 5.0,
        "latest_price": 100.0,
        "price_at": datetime.now(UTC).isoformat(),
        "historical_bars": 100,
        "volume": 1000,
        "price_gap_pct": 0,
        "created_at": datetime.now(UTC).isoformat(),
        "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
        "strategy_version": "rule_based_v1",
        "reason": "trend passed",
        "order_type": "market",
        "asset_class": "equity",
        "approved_dynamic_paper_tradable": True,
        "universe_source": "dynamic",
        "approved_market_profile": "us_equities",
        "client_order_id": "test-dynamic-client-id"
    }
    context = {
        "power_connected": True,
        "internet_available": True,
        "database_writable": True,
        "broker_available": True,
        "telegram_available": True,
        "market_open": True,
        "kill_switch": False,
        "open_positions": 0,
        "trades_today": 0,
        "duplicate_order": False,
        "same_symbol_position": False,
        "uses_margin": False,
        "daily_loss": 0.0,
        "weekly_loss": 0.0,
        "buying_power": 100000.0,
        "approval_valid": True,
        "proposed_total_exposure_pct": 0.0,
        "proposed_symbol_exposure_pct": 0.0,
        "proposed_cluster_positions_count": 0,
        "proposed_cluster_exposure_pct": 0.0,
        "exit_pending": False,
        "final_revalidation": True,
        "universe_symbol_info": {
            "symbol": "ARM",
            "tier": "research_candidate", # Demoted to research candidate
            "universe_lane": "alpaca_compatible_us",
            "alpaca_compatible": 1,
            "executable": 0
        },
        "active_dynamic_paper_tradable_symbols": ["ARM"]
    }
    decision = RiskEngine(base_config).evaluate(proposal, context, final=True)
    assert not decision.passed
    assert "dynamic symbol no longer paper-tradable at final validation" in decision.reasons

def test_dynamic_loses_alpaca_compatibility_blocked(temp_storage, base_config):
    # 9. Dynamic symbol that loses Alpaca compatibility after proposal is blocked
    proposal = {
        "id": "p-dynamic",
        "symbol": "ARM",
        "side": "buy",
        "action": "entry",
        "notional": 5.0,
        "latest_price": 100.0,
        "price_at": datetime.now(UTC).isoformat(),
        "historical_bars": 100,
        "volume": 1000,
        "price_gap_pct": 0,
        "created_at": datetime.now(UTC).isoformat(),
        "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
        "strategy_version": "rule_based_v1",
        "reason": "trend passed",
        "order_type": "market",
        "asset_class": "equity",
        "approved_dynamic_paper_tradable": True,
        "universe_source": "dynamic",
        "approved_market_profile": "us_equities",
        "client_order_id": "test-dynamic-client-id"
    }
    context = {
        "power_connected": True,
        "internet_available": True,
        "database_writable": True,
        "broker_available": True,
        "telegram_available": True,
        "market_open": True,
        "kill_switch": False,
        "open_positions": 0,
        "trades_today": 0,
        "duplicate_order": False,
        "same_symbol_position": False,
        "uses_margin": False,
        "daily_loss": 0.0,
        "weekly_loss": 0.0,
        "buying_power": 100000.0,
        "approval_valid": True,
        "proposed_total_exposure_pct": 0.0,
        "proposed_symbol_exposure_pct": 0.0,
        "proposed_cluster_positions_count": 0,
        "proposed_cluster_exposure_pct": 0.0,
        "exit_pending": False,
        "final_revalidation": True,
        "universe_symbol_info": {
            "symbol": "ARM",
            "tier": "paper_tradable",
            "universe_lane": "alpaca_compatible_us",
            "alpaca_compatible": 0, # Lost Alpaca compatibility
            "executable": 1
        },
        "active_dynamic_paper_tradable_symbols": ["ARM"]
    }
    decision = RiskEngine(base_config).evaluate(proposal, context, final=True)
    assert not decision.passed
    assert "dynamic symbol failed final Alpaca compatibility check" in decision.reasons

def test_dynamic_stale_alpaca_blocked(temp_storage, base_config):
    # 10. Dynamic symbol with stale Alpaca data is blocked
    proposal = {
        "id": "p-dynamic",
        "symbol": "ARM",
        "side": "buy",
        "action": "entry",
        "notional": 5.0,
        "latest_price": 100.0,
        "price_at": (datetime.now(UTC) - timedelta(minutes=5)).isoformat(), # Stale price_at
        "historical_bars": 100,
        "volume": 1000,
        "price_gap_pct": 0,
        "created_at": datetime.now(UTC).isoformat(),
        "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
        "strategy_version": "rule_based_v1",
        "reason": "trend passed",
        "order_type": "market",
        "asset_class": "equity",
        "approved_dynamic_paper_tradable": True,
        "universe_source": "dynamic",
        "approved_market_profile": "us_equities",
        "client_order_id": "test-dynamic-client-id"
    }
    context = {
        "power_connected": True,
        "internet_available": True,
        "database_writable": True,
        "broker_available": True,
        "telegram_available": True,
        "market_open": True,
        "kill_switch": False,
        "open_positions": 0,
        "trades_today": 0,
        "duplicate_order": False,
        "same_symbol_position": False,
        "uses_margin": False,
        "daily_loss": 0.0,
        "weekly_loss": 0.0,
        "buying_power": 100000.0,
        "approval_valid": True,
        "proposed_total_exposure_pct": 0.0,
        "proposed_symbol_exposure_pct": 0.0,
        "proposed_cluster_positions_count": 0,
        "proposed_cluster_exposure_pct": 0.0,
        "exit_pending": False,
        "final_revalidation": True,
        "universe_symbol_info": {
            "symbol": "ARM",
            "tier": "paper_tradable",
            "universe_lane": "alpaca_compatible_us",
            "alpaca_compatible": 1,
            "executable": 1
        },
        "active_dynamic_paper_tradable_symbols": ["ARM"]
    }
    decision = RiskEngine(base_config).evaluate(proposal, context, final=True)
    assert not decision.passed
    assert "dynamic symbol failed final Alpaca price freshness check" in decision.reasons

def test_research_candidate_blocked(temp_storage, base_config):
    # 11. Research candidate cannot pass final validation
    proposal = {
        "id": "p-research",
        "symbol": "XYZ",
        "side": "buy",
        "action": "entry",
        "notional": 5.0,
        "latest_price": 100.0,
        "price_at": datetime.now(UTC).isoformat(),
        "historical_bars": 100,
        "volume": 1000,
        "price_gap_pct": 0,
        "created_at": datetime.now(UTC).isoformat(),
        "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
        "strategy_version": "rule_based_v1",
        "reason": "trend passed",
        "order_type": "market",
        "asset_class": "equity",
        "approved_dynamic_paper_tradable": False,
        "universe_source": "dynamic",
        "approved_market_profile": "us_equities",
        "client_order_id": "test-dynamic-client-id"
    }
    context = {
        "power_connected": True,
        "internet_available": True,
        "database_writable": True,
        "broker_available": True,
        "telegram_available": True,
        "market_open": True,
        "kill_switch": False,
        "open_positions": 0,
        "trades_today": 0,
        "duplicate_order": False,
        "same_symbol_position": False,
        "uses_margin": False,
        "daily_loss": 0.0,
        "weekly_loss": 0.0,
        "buying_power": 100000.0,
        "approval_valid": True,
        "proposed_total_exposure_pct": 0.0,
        "proposed_symbol_exposure_pct": 0.0,
        "proposed_cluster_positions_count": 0,
        "proposed_cluster_exposure_pct": 0.0,
        "exit_pending": False,
        "final_revalidation": True,
        "universe_symbol_info": {
            "symbol": "XYZ",
            "tier": "research_candidate",
            "universe_lane": "alpaca_compatible_us",
            "alpaca_compatible": 1,
            "executable": 0
        }
    }
    decision = RiskEngine(base_config).evaluate(proposal, context, final=True)
    assert not decision.passed
    assert "research candidate cannot pass final validation" in decision.reasons

def test_observation_only_blocked(temp_storage, base_config):
    # 12. Observation-only symbol cannot pass final validation
    proposal = {
        "id": "p-obs",
        "symbol": "XYZ",
        "side": "buy",
        "action": "entry",
        "notional": 5.0,
        "latest_price": 100.0,
        "price_at": datetime.now(UTC).isoformat(),
        "historical_bars": 100,
        "volume": 1000,
        "price_gap_pct": 0,
        "created_at": datetime.now(UTC).isoformat(),
        "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
        "strategy_version": "rule_based_v1",
        "reason": "trend passed",
        "order_type": "market",
        "asset_class": "equity",
        "approved_dynamic_paper_tradable": False,
        "universe_source": "dynamic",
        "approved_market_profile": "us_equities",
        "client_order_id": "test-dynamic-client-id"
    }
    context = {
        "power_connected": True,
        "internet_available": True,
        "database_writable": True,
        "broker_available": True,
        "telegram_available": True,
        "market_open": True,
        "kill_switch": False,
        "open_positions": 0,
        "trades_today": 0,
        "duplicate_order": False,
        "same_symbol_position": False,
        "uses_margin": False,
        "daily_loss": 0.0,
        "weekly_loss": 0.0,
        "buying_power": 100000.0,
        "approval_valid": True,
        "proposed_total_exposure_pct": 0.0,
        "proposed_symbol_exposure_pct": 0.0,
        "proposed_cluster_positions_count": 0,
        "proposed_cluster_exposure_pct": 0.0,
        "exit_pending": False,
        "final_revalidation": True,
        "universe_symbol_info": {
            "symbol": "XYZ",
            "tier": "observation",
            "universe_lane": "alpaca_compatible_us",
            "alpaca_compatible": 1,
            "executable": 0,
            "observation_only": 1
        }
    }
    decision = RiskEngine(base_config).evaluate(proposal, context, final=True)
    assert not decision.passed
    assert "observation-only symbol cannot pass final validation" in decision.reasons

def test_global_research_only_blocked(temp_storage, base_config):
    # 13. Global research-only symbol cannot pass final validation
    proposal = {
        "id": "p-global",
        "symbol": "XYZ",
        "side": "buy",
        "action": "entry",
        "notional": 5.0,
        "latest_price": 100.0,
        "price_at": datetime.now(UTC).isoformat(),
        "historical_bars": 100,
        "volume": 1000,
        "price_gap_pct": 0,
        "created_at": datetime.now(UTC).isoformat(),
        "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
        "strategy_version": "rule_based_v1",
        "reason": "trend passed",
        "order_type": "market",
        "asset_class": "equity",
        "approved_dynamic_paper_tradable": False,
        "universe_source": "dynamic",
        "approved_market_profile": "us_equities",
        "client_order_id": "test-dynamic-client-id"
    }
    context = {
        "power_connected": True,
        "internet_available": True,
        "database_writable": True,
        "broker_available": True,
        "telegram_available": True,
        "market_open": True,
        "kill_switch": False,
        "open_positions": 0,
        "trades_today": 0,
        "duplicate_order": False,
        "same_symbol_position": False,
        "uses_margin": False,
        "daily_loss": 0.0,
        "weekly_loss": 0.0,
        "buying_power": 100000.0,
        "approval_valid": True,
        "proposed_total_exposure_pct": 0.0,
        "proposed_symbol_exposure_pct": 0.0,
        "proposed_cluster_positions_count": 0,
        "proposed_cluster_exposure_pct": 0.0,
        "exit_pending": False,
        "final_revalidation": True,
        "universe_symbol_info": {
            "symbol": "XYZ",
            "tier": "paper_tradable",
            "universe_lane": "global_research_only",
            "alpaca_compatible": 0,
            "executable": 0
        }
    }
    decision = RiskEngine(base_config).evaluate(proposal, context, final=True)
    assert not decision.passed
    assert "global research-only symbol cannot pass final validation" in decision.reasons

def test_unsupported_otc_blocked(temp_storage, base_config):
    # 14. Unsupported/OTC-like symbol cannot pass final validation
    proposal = {
        "id": "p-otc",
        "symbol": "XYZ",
        "side": "buy",
        "action": "entry",
        "notional": 5.0,
        "latest_price": 100.0,
        "price_at": datetime.now(UTC).isoformat(),
        "historical_bars": 100,
        "volume": 1000,
        "price_gap_pct": 0,
        "created_at": datetime.now(UTC).isoformat(),
        "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
        "strategy_version": "rule_based_v1",
        "reason": "trend passed",
        "order_type": "market",
        "asset_class": "equity",
        "approved_dynamic_paper_tradable": False,
        "universe_source": "dynamic",
        "approved_market_profile": "us_equities",
        "client_order_id": "test-dynamic-client-id"
    }
    context = {
        "power_connected": True,
        "internet_available": True,
        "database_writable": True,
        "broker_available": True,
        "telegram_available": True,
        "market_open": True,
        "kill_switch": False,
        "open_positions": 0,
        "trades_today": 0,
        "duplicate_order": False,
        "same_symbol_position": False,
        "uses_margin": False,
        "daily_loss": 0.0,
        "weekly_loss": 0.0,
        "buying_power": 100000.0,
        "approval_valid": True,
        "proposed_total_exposure_pct": 0.0,
        "proposed_symbol_exposure_pct": 0.0,
        "proposed_cluster_positions_count": 0,
        "proposed_cluster_exposure_pct": 0.0,
        "exit_pending": False,
        "final_revalidation": True,
        "universe_symbol_info": {
            "symbol": "XYZ",
            "tier": "paper_tradable",
            "universe_lane": "excluded_or_low_quality",
            "alpaca_compatible": 0,
            "executable": 0
        }
    }
    decision = RiskEngine(base_config).evaluate(proposal, context, final=True)
    assert not decision.passed
    assert "unsupported/OTC-like symbol cannot pass final validation" in decision.reasons

def test_approval_yes_abbv_resolves_abbv(temp_storage, base_config, monkeypatch):
    # 17. yes ABBV resolves only ABBV
    monkeypatch.setattr("app.service.get_power_status", lambda: PowerStatus(True, "test", "AC power connected", 100.0))
    monkeypatch.setattr("app.service.internet_available", lambda: True)

    broker = MockBroker()
    service = TradingService(base_config, temp_storage, broker, "run-test")
    service.telegram = MockTelegramBot()

    # Create batch
    bid = str(uuid.uuid4())
    now = datetime.now(UTC)
    temp_storage.execute(
        "INSERT INTO proposal_batches(id,run_id,telegram_message_id,status,created_at,expires_at,payload) VALUES(?,?,?,?,?,?,?)",
        (bid, "run-test", 184, "pending", now.isoformat(), (now + timedelta(minutes=10)).isoformat(), json.dumps({}))
    )

    p1 = helper_create_proposal(temp_storage, "ABBV", approved_dynamic=True, universe_source="dynamic")
    p2 = helper_create_proposal(temp_storage, "SPY")

    temp_storage.execute(
        "INSERT INTO proposal_batch_candidates(id,batch_id,proposal_id,telegram_message_id,candidate_symbol,candidate_side,candidate_action,candidate_status,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (str(uuid.uuid4()), bid, p1, "184", "ABBV", "buy", "entry", "pending", now.isoformat())
    )
    temp_storage.execute(
        "INSERT INTO proposal_batch_candidates(id,batch_id,proposal_id,telegram_message_id,candidate_symbol,candidate_side,candidate_action,candidate_status,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (str(uuid.uuid4()), bid, p2, "184", "SPY", "buy", "entry", "pending", now.isoformat())
    )

    # Insert into universe_symbols to pass validation
    temp_storage.execute(
        "INSERT INTO universe_symbols(id,symbol,tier,universe_lane,alpaca_compatible,executable,observation_only,score,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (str(uuid.uuid4()), "ABBV", "paper_tradable", "alpaca_compatible_us", 1, 1, 0, 90.0, now.isoformat(), now.isoformat())
    )

    # Mock dynamic active scan symbols
    monkeypatch.setattr(service, "_dynamic_universe_scan_symbols", lambda: (["ABBV"], []))

    handled = service._handle_batch_approval_command("yes ABBV", "7777", "yes", "ABBV", "184")
    assert handled is True

    # ABBV should be submitted
    assert temp_storage.fetch_all("SELECT status FROM trade_proposals WHERE id=?", (p1,))[0]["status"] == "submitted"
    # SPY should still be pending
    assert temp_storage.fetch_all("SELECT status FROM trade_proposals WHERE id=?", (p2,))[0]["status"] == "pending"

def test_approval_yes_all(temp_storage, base_config, monkeypatch):
    # 18. yes all remains paper-only and runs per-candidate final validation
    monkeypatch.setattr("app.service.get_power_status", lambda: PowerStatus(True, "test", "AC power connected", 100.0))
    monkeypatch.setattr("app.service.internet_available", lambda: True)

    broker = MockBroker()
    service = TradingService(base_config, temp_storage, broker, "run-test")
    service.telegram = MockTelegramBot()

    bid = str(uuid.uuid4())
    now = datetime.now(UTC)
    temp_storage.execute(
        "INSERT INTO proposal_batches(id,run_id,telegram_message_id,status,created_at,expires_at,payload) VALUES(?,?,?,?,?,?,?)",
        (bid, "run-test", 184, "pending", now.isoformat(), (now + timedelta(minutes=10)).isoformat(), json.dumps({}))
    )

    p1 = helper_create_proposal(temp_storage, "ABBV", approved_dynamic=True, universe_source="dynamic")
    p2 = helper_create_proposal(temp_storage, "SPY")

    temp_storage.execute(
        "INSERT INTO proposal_batch_candidates(id,batch_id,proposal_id,telegram_message_id,candidate_symbol,candidate_side,candidate_action,candidate_status,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (str(uuid.uuid4()), bid, p1, "184", "ABBV", "buy", "entry", "pending", now.isoformat())
    )
    temp_storage.execute(
        "INSERT INTO proposal_batch_candidates(id,batch_id,proposal_id,telegram_message_id,candidate_symbol,candidate_side,candidate_action,candidate_status,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (str(uuid.uuid4()), bid, p2, "184", "SPY", "buy", "entry", "pending", now.isoformat())
    )

    # ABBV is valid in universe_symbols, SPY is statically valid
    temp_storage.execute(
        "INSERT INTO universe_symbols(id,symbol,tier,universe_lane,alpaca_compatible,executable,observation_only,score,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (str(uuid.uuid4()), "ABBV", "paper_tradable", "alpaca_compatible_us", 1, 1, 0, 90.0, now.isoformat(), now.isoformat())
    )

    monkeypatch.setattr(service, "_dynamic_universe_scan_symbols", lambda: (["ABBV"], []))

    handled = service._handle_batch_approval_command("yes all", "7777", "yes", "ALL", "184")
    assert handled is True

    # Both should be submitted
    assert temp_storage.fetch_all("SELECT status FROM trade_proposals WHERE id=?", (p1,))[0]["status"] == "submitted"
    assert temp_storage.fetch_all("SELECT status FROM trade_proposals WHERE id=?", (p2,))[0]["status"] == "submitted"

def test_approval_plain_yes_ambiguous(temp_storage, base_config, monkeypatch):
    # 19. Plain yes remains ambiguous when multiple candidates are pending
    broker = MockBroker()
    service = TradingService(base_config, temp_storage, broker, "run-test")
    service.telegram = MockTelegramBot()

    bid = str(uuid.uuid4())
    now = datetime.now(UTC)
    temp_storage.execute(
        "INSERT INTO proposal_batches(id,run_id,telegram_message_id,status,created_at,expires_at,payload) VALUES(?,?,?,?,?,?,?)",
        (bid, "run-test", 184, "pending", now.isoformat(), (now + timedelta(minutes=10)).isoformat(), json.dumps({}))
    )

    p1 = helper_create_proposal(temp_storage, "ABBV", approved_dynamic=True, universe_source="dynamic")
    p2 = helper_create_proposal(temp_storage, "SPY")

    temp_storage.execute(
        "INSERT INTO proposal_batch_candidates(id,batch_id,proposal_id,telegram_message_id,candidate_symbol,candidate_side,candidate_action,candidate_status,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (str(uuid.uuid4()), bid, p1, "184", "ABBV", "buy", "entry", "pending", now.isoformat())
    )
    temp_storage.execute(
        "INSERT INTO proposal_batch_candidates(id,batch_id,proposal_id,telegram_message_id,candidate_symbol,candidate_side,candidate_action,candidate_status,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (str(uuid.uuid4()), bid, p2, "184", "SPY", "buy", "entry", "pending", now.isoformat())
    )

    # Let the service parse the message
    mock_update = {"update_id": 999, "message": {"text": "yes", "chat": {"id": 123}, "from": {"id": 7777}, "message_id": 999}}
    service.telegram.updates.append(mock_update)

    with patch.object(service.telegram, "is_authorized", return_value=True):
        service.process_telegram()

    # It should send a message about plain yes being ambiguous
    assert any("ambiguous" in msg for msg in service.telegram.messages)

def test_failed_validation_records_blocker_reason_and_prevents_duplicate_orders(temp_storage, base_config, monkeypatch):
    # 20. A candidate that fails final validation is marked handled or failed consistently
    # 21. Repeating yes ABBV after handling does not place a duplicate order
    # 22. Failed dynamic final validation records a clear final blocker reason
    monkeypatch.setattr("app.service.get_power_status", lambda: PowerStatus(True, "test", "AC power connected", 100.0))
    monkeypatch.setattr("app.service.internet_available", lambda: True)

    broker = MockBroker()
    service = TradingService(base_config, temp_storage, broker, "run-test")
    service.telegram = MockTelegramBot()

    bid = str(uuid.uuid4())
    now = datetime.now(UTC)
    temp_storage.execute(
        "INSERT INTO proposal_batches(id,run_id,telegram_message_id,status,created_at,expires_at,payload) VALUES(?,?,?,?,?,?,?)",
        (bid, "run-test", 184, "pending", now.isoformat(), (now + timedelta(minutes=10)).isoformat(), json.dumps({}))
    )

    p1 = helper_create_proposal(temp_storage, "ABBV", approved_dynamic=True, universe_source="dynamic")

    temp_storage.execute(
        "INSERT INTO proposal_batch_candidates(id,batch_id,proposal_id,telegram_message_id,candidate_symbol,candidate_side,candidate_action,candidate_status,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (str(uuid.uuid4()), bid, p1, "184", "ABBV", "buy", "entry", "pending", now.isoformat())
    )

    # Demote in universe_symbols to trigger failure
    temp_storage.execute(
        "INSERT INTO universe_symbols(id,symbol,tier,universe_lane,alpaca_compatible,executable,observation_only,score,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (str(uuid.uuid4()), "ABBV", "research_candidate", "alpaca_compatible_us", 1, 0, 0, 90.0, now.isoformat(), now.isoformat())
    )

    handled = service._handle_batch_approval_command("yes ABBV", "7777", "yes", "ABBV", "184")
    assert handled is True

    # Should be blocked
    assert temp_storage.fetch_all("SELECT status FROM trade_proposals WHERE id=?", (p1,))[0]["status"] == "blocked"
    
    # Check that final blocker reason was recorded in approvals table
    approval_row = temp_storage.fetch_all("SELECT final_block_reason FROM approvals WHERE proposal_id=?", (p1,))[0]
    assert "dynamic symbol no longer paper-tradable at final validation" in approval_row["final_block_reason"]

    # Try to approve again
    handled_again = service._handle_batch_approval_command("yes ABBV", "7777", "yes", "ABBV", "184")
    assert handled_again is True
    # The message should state it was already handled
    assert "already handled" in service.telegram.messages[-1]
