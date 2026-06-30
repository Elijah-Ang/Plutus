import json
import uuid
import pytest
from datetime import datetime, UTC, timedelta
import pandas as pd
from unittest.mock import MagicMock, patch

from app.storage import Storage
from app.service import TradingService
from app.execution import ExecutionResult, Executor
from app.strategy_rule_based import Signal
from tests.test_dynamic_approval_and_validation import MockTelegramBot, MockBroker

def future_iso(minutes=15):
    return (datetime.now(UTC) + timedelta(minutes=minutes)).isoformat()

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
        "explicit_live_confirmation": False,
        "capabilities": {
            "live_trading_supported": False,
            "auto_execution_supported": False
        },
        "broker": "alpaca",
        "watchlist": ["SPY"],
        "approved_strategy_versions": ["rule_based_v1"],
        "storage": {
            "sqlite_path": "data/test_trading.db"
        },
        "risk": {
            "max_trades_per_day": 1,
            "max_daily_loss_pct": 2.0,
            "max_drawdown_pct": 5.0,
            "min_historical_bars": 50,
            "max_price_age_seconds": 120
        },
        "proposal_mode": {
            "type": "ranked_batch",
            "send_all_actionable_candidates": True,
            "combine_candidates_into_one_message": True,
            "require_individual_approval_per_trade": True,
            "allow_yes_all_for_paper": True,
            "yes_all_requires_each_trade_final_revalidation": True
        },
        "telegram": {
            "approval_enabled": True,
            "telegram_approval_listener_enabled": True,
            "approval_price_refresh_required": True,
            "approval_max_price_age_seconds": 120,
            "approval_max_price_move_bps": 25,
            "approval_max_price_move_hard_cap_bps": 75,
            "proposal_price_freshness_threshold_seconds": 60
        },
        "position_sizing": {
            "enabled": True,
            "mode": "risk_portfolio",
            "stage": "moderate_paper",
            "use_stage_dollar_cap": True,
            "stage_max_initial_notional": {
                "moderate_paper": 250
            },
            "stage_max_add_notional": {
                "moderate_paper": 100
            },
            "risk_per_trade_pct": 0.05,
            "max_trade_notional_pct_of_equity": 0.25,
            "max_position_notional_pct_of_equity": 2.0,
            "max_total_portfolio_exposure_pct": 6.0,
            "max_cluster_exposure_pct": 5.0,
            "min_cash_reserve_pct": 20.0,
            "max_cash_usage_pct": 10.0,
            "max_margin_usage_pct": 0.0,
            "base_paper_notional": 50,
            "add_size_multiplier": 0.5,
            "stop_model": {
                "method": "max_of_atr_or_technical",
                "atr_multiple": 2.0,
                "max_stop_pct": 8.0,
                "min_stop_pct": 1.0
            }
        },
        "portfolio_optimizer": {
            "max_same_symbol_exposure_pct": 5.0,
            "max_total_portfolio_exposure_pct": 15.0,
            "max_same_cluster_exposure_pct": 5.0
        }
    }

def test_proposal_blocked_if_stale_price_at_scan(temp_storage, base_config):
    broker = MockBroker()
    service = TradingService(base_config, temp_storage, broker, "test-run")
    service.telegram = MockTelegramBot()
    
    # 100s old price (stale)
    now = datetime.now(UTC)
    broker.price = 100.0
    broker.price_time = now - timedelta(seconds=100)
    broker.get_latest_price = MagicMock(return_value=type("T", (), {"price": 100.0, "timestamp": now - timedelta(seconds=100)})())
    
    # Mock strategy signal to return ENTRY buy with volatility_20 to bump score
    mock_signal = Signal(action="ENTRY", side="buy", symbol="SPY", reason="setup", confidence=95.0, indicators={"rsi": 30.0, "volatility_20": 0.15})
    
    # Dummy historical bars (at least 60 to satisfy min_historical_bars = 50)
    broker.get_historical_bars = MagicMock(return_value=pd.DataFrame({
        "open": [100.0] * 60, "high": [101.0] * 60, "low": [99.0] * 60, "close": [100.0] * 60, "volume": [1000000.0] * 60
    }))
    
    with patch("app.service.evaluate_symbol", return_value=mock_signal):
        with patch.object(service, '_get_symbol_cluster', return_value="Tech"):
            service.scan()
            
            # Print market memory content for diagnostics
            mem = temp_storage.fetch_all("SELECT symbol, price, signal, score, proposal_allowed, no_action_reason, dedupe_status, dedupe_reason FROM market_memory")
            print("\n--- MARKET MEMORY DIAGNOSTICS ---")
            for m in mem:
                print(dict(m))
            print("---------------------------------\n")
            
            # Verify proposal is blocked
            props = temp_storage.fetch_all("SELECT * FROM trade_proposals WHERE symbol='SPY'")
            assert len(props) == 0
            
            audits = temp_storage.fetch_all("SELECT * FROM audit_events WHERE event_type='proposal_blocked'")
            assert len(audits) == 1
            detail = json.loads(audits[0]["detail"])
            assert detail["symbol"] == "SPY"
            assert "stale Alpaca price" in detail["reasons"][0]

def test_proposal_created_if_fresh_price_at_scan(temp_storage, base_config):
    broker = MockBroker()
    service = TradingService(base_config, temp_storage, broker, "test-run")
    service.telegram = MockTelegramBot()
    
    # 10s old price (fresh)
    now = datetime.now(UTC)
    broker.price = 100.0
    broker.price_time = now - timedelta(seconds=10)
    broker.get_latest_price = MagicMock(return_value=type("T", (), {"price": 100.0, "timestamp": now - timedelta(seconds=10)})())
    
    mock_signal = Signal(action="ENTRY", side="buy", symbol="SPY", reason="setup", confidence=95.0, indicators={"rsi": 30.0, "volatility_20": 0.15})
    broker.get_historical_bars = MagicMock(return_value=pd.DataFrame({
        "open": [100.0] * 60, "high": [101.0] * 60, "low": [99.0] * 60, "close": [100.0] * 60, "volume": [1000000.0] * 60
    }))
    
    with patch("app.service.evaluate_symbol", return_value=mock_signal):
        with patch.object(service, '_get_symbol_cluster', return_value="Tech"):
            service.scan()
            
            # Verify proposal was created
            props = temp_storage.fetch_all("SELECT * FROM trade_proposals WHERE symbol='SPY'")
            assert len(props) == 1
            payload = json.loads(props[0]["payload"])
            assert payload["proposal_price"] == 100.0
            assert payload["proposal_price_source"] == "alpaca"
            assert payload["proposal_price_age_seconds_at_send"] is not None

def test_volatility_aware_price_movement_limits(temp_storage, base_config):
    broker = MockBroker()
    service = TradingService(base_config, temp_storage, broker, "test-run")
    service.telegram = MockTelegramBot()
    
    spy_proposal = {"stop_distance_pct": 1.5}
    limit_spy = service._calculate_volatility_aware_bps_limit(spy_proposal, base_bps=25.0, hard_cap_bps=75.0)
    assert limit_spy == 25.0
    
    amx_proposal = {"stop_distance_pct": 8.0}
    limit_amx = service._calculate_volatility_aware_bps_limit(amx_proposal, base_bps=25.0, hard_cap_bps=75.0)
    assert limit_amx == 75.0

def test_approval_time_price_refresh_and_invariant_capping(temp_storage, base_config):
    broker = MockBroker()
    service = TradingService(base_config, temp_storage, broker, "test-run")
    service.telegram = MockTelegramBot()
    
    proposal_id = str(uuid.uuid4())
    payload = {
        "id": proposal_id,
        "symbol": "ABBV",
        "side": "buy",
        "action": "add",
        "is_add": 1,
        "notional": 50.0,
        "latest_price": 250.0,
        "stop_distance_pct": 8.0,
        "score": 100,
        "volatility_regime": "normal"
    }
    
    temp_storage.execute(
        "INSERT INTO trade_proposals(id,run_id,symbol,side,notional,status,created_at,expires_at,payload) VALUES(?,?,?,?,?,?,?,?,?)",
        (proposal_id, "test-run", "ABBV", "buy", 50.0, "pending", future_iso(-1), future_iso(15), json.dumps(payload))
    )
    
    broker.price = 251.0
    broker.price_time = datetime.now(UTC)
    
    with patch.object(service, '_calculate_dynamic_size', return_value={
        "final_notional": 100.0,
        "suggested_shares": 0.40
    }):
        with patch.object(Executor, 'execute', return_value=ExecutionResult(submitted=True, status="submitted", client_order_id="ta-123", broker_response={"id": "order-123"})) as mock_exec:
            batch_row = {"telegram_message_id": 999, "batch_id": "batch-1", "expires_at": future_iso(15)}
            success, decision, reason = service._approve_batch_candidate(proposal_id, "user-1", "yes ABBV", batch_row)
            
            assert success is True
            assert decision == "submitted"
            
            called_proposal = mock_exec.call_args[0][0]
            assert called_proposal["notional"] == 50.0
            assert called_proposal["approved_notional"] == 50.0
            assert called_proposal["qty"] == 50.0 / 251.0
            
            assert len(service.telegram.messages) > 0
            assert "ADD TO WINNER" in service.telegram.messages[0]
            assert "Approved: $50.00" in service.telegram.messages[0]
            assert "Final: $50.00" in service.telegram.messages[0]

def test_price_moved_above_volatility_aware_tolerance_blocks_order(temp_storage, base_config):
    broker = MockBroker()
    service = TradingService(base_config, temp_storage, broker, "test-run")
    service.telegram = MockTelegramBot()
    
    proposal_id = str(uuid.uuid4())
    payload = {
        "id": proposal_id,
        "symbol": "SPY",
        "side": "buy",
        "action": "entry",
        "is_add": 0,
        "notional": 100.0,
        "latest_price": 500.0,
        "stop_distance_pct": 1.5,
        "score": 90,
        "volatility_regime": "normal"
    }
    temp_storage.execute(
        "INSERT INTO trade_proposals(id,run_id,symbol,side,notional,status,created_at,expires_at,payload) VALUES(?,?,?,?,?,?,?,?,?)",
        (proposal_id, "test-run", "SPY", "buy", 100.0, "pending", future_iso(-1), future_iso(15), json.dumps(payload))
    )
    
    broker.price = 502.0
    broker.price_time = datetime.now(UTC)
    
    batch_row = {"telegram_message_id": 999, "batch_id": "batch-1", "expires_at": future_iso(15)}
    success, decision, reason = service._approve_batch_candidate(proposal_id, "user-1", "yes SPY", batch_row)
    
    assert success is False
    assert decision == "blocked"
    assert "Price moved too much" in reason
    
    assert len(service.telegram.messages) > 0
    assert "Price moved too much" in service.telegram.messages[0]
    assert "limit 25.0 bps" in service.telegram.messages[0]

def test_missing_fresh_price_at_approval_blocks_order(temp_storage, base_config):
    broker = MockBroker()
    service = TradingService(base_config, temp_storage, broker, "test-run")
    service.telegram = MockTelegramBot()
    
    proposal_id = str(uuid.uuid4())
    payload = {
        "id": proposal_id,
        "symbol": "SPY",
        "side": "buy",
        "action": "entry",
        "is_add": 0,
        "notional": 100.0,
        "latest_price": 500.0,
        "stop_distance_pct": 2.0,
        "score": 90,
        "volatility_regime": "normal"
    }
    temp_storage.execute(
        "INSERT INTO trade_proposals(id,run_id,symbol,side,notional,status,created_at,expires_at,payload) VALUES(?,?,?,?,?,?,?,?,?)",
        (proposal_id, "test-run", "SPY", "buy", 100.0, "pending", future_iso(-1), future_iso(15), json.dumps(payload))
    )
    
    broker.price = 500.0
    broker.price_time = datetime.now(UTC) - timedelta(seconds=200)
    
    batch_row = {"telegram_message_id": 999, "batch_id": "batch-1", "expires_at": future_iso(15)}
    success, decision, reason = service._approve_batch_candidate(proposal_id, "user-1", "yes SPY", batch_row)
    
    assert success is False
    assert decision == "blocked"
    assert "Final validation could not get a fresh Alpaca price" in reason
    
    assert len(service.telegram.messages) > 0
    assert "Final validation could not get a fresh Alpaca price" in service.telegram.messages[0]
