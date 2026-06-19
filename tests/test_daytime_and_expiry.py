import json
import uuid
from datetime import datetime, UTC, timedelta
import pytest
import pandas as pd

from app.utils import load_config, format_proposal_message, format_sgt
from app.storage import Storage
from app.service import TradingService
from app.risk_engine import RiskEngine
from test_scoring_and_throttling import MockBroker, MockTelegramBot, temp_storage

def test_daytime_market_config_exists():
    config = load_config()
    profiles = config.get("market_profiles", {})
    assert "us_equities" in profiles
    assert "sgx_observation" in profiles
    assert "hkex_observation" in profiles
    
    assert profiles["us_equities"]["broker"] == "alpaca"
    assert profiles["us_equities"]["execution_enabled"] is True
    
    assert profiles["sgx_observation"]["broker"] == "none"
    assert profiles["sgx_observation"]["execution_enabled"] is False
    assert profiles["sgx_observation"]["proposals_enabled"] is False
    
    assert profiles["hkex_observation"]["broker"] == "none"
    assert profiles["hkex_observation"]["execution_enabled"] is False
    assert profiles["hkex_observation"]["proposals_enabled"] is False

def test_alpaca_cannot_be_assigned_to_sgx_hkex():
    # If a symbol is SGX/HKEX and the broker is set to alpaca, it must fail the RiskEngine checks
    config = load_config()
    # Force profile to simulate invalid config where alpaca is broker for SGX/HKEX
    config["market_profiles"]["sgx_observation"]["broker"] = "alpaca"
    config["market_profiles"]["sgx_observation"]["status"] = "active"
    
    proposal = {
        "symbol": "ES3.SI",
        "side": "buy",
        "notional": 5,
        "asset_class": "equity",
        "created_at": datetime.now(UTC).isoformat(),
        "expires_at": (datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
        "latest_price": 3.2,
        "historical_bars": 100,
        "volume": 50000,
        "strategy_version": "rule_based_v1",
        "reason": "mock reason"
    }
    
    engine = RiskEngine(config)
    context = {
        "now": datetime.now(UTC),
        "power_connected": True,
        "internet_available": True,
        "database_writable": True,
        "broker_available": True,
        "telegram_available": True,
        "market_open": True,
        "open_positions": 0,
        "trades_today": 0
    }
    decision = engine.evaluate(proposal, context)
    assert not decision.passed
    assert any("Alpaca cannot be assigned" in r for r in decision.reasons)

def test_sgx_hkex_profiles_do_not_create_proposals_or_execute():
    config = load_config()
    # Ensure they are disabled/observation_only
    assert config["market_profiles"]["sgx_observation"]["execution_enabled"] is False
    assert config["market_profiles"]["sgx_observation"]["proposals_enabled"] is False
    
    # Check proposal block at RiskEngine level
    proposal = {
        "symbol": "ES3.SI",
        "side": "buy",
        "notional": 5,
        "asset_class": "equity",
        "created_at": datetime.now(UTC).isoformat(),
        "expires_at": (datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
        "latest_price": 3.2,
        "historical_bars": 100,
        "volume": 50000,
        "strategy_version": "rule_based_v1",
        "reason": "mock reason"
    }
    
    engine = RiskEngine(config)
    context = {
        "now": datetime.now(UTC),
        "power_connected": True,
        "internet_available": True,
        "database_writable": True,
        "broker_available": True,
        "telegram_available": True,
        "market_open": True,
        "open_positions": 0,
        "trades_today": 0
    }
    decision = engine.evaluate(proposal, context)
    assert not decision.passed
    # Should fail active_profile or approved_universe or profile_execution_enabled
    assert any("is not active" in r or "not in active watchlist" in r or "execution disabled" in r for r in decision.reasons)

def test_missing_data_provider_audits_missing(temp_storage):
    config = load_config()
    # Enable SGX profile as active to run scanning cycle
    config["market_profiles"]["sgx_observation"]["status"] = "active"
    config["market_profiles"]["sgx_observation"]["broker"] = "none" # missing data provider
    
    broker = MockBroker()
    service = TradingService(config, temp_storage, broker, "test_run_id")
    service.telegram = MockTelegramBot()
    
    # Run scan
    service.scan()
    
    # Check that data_source_missing was audited
    audit_rows = temp_storage.fetch_all("SELECT * FROM audit_events WHERE event_type='data_source_missing'")
    assert len(audit_rows) > 0
    detail = json.loads(audit_rows[0]["detail"])
    assert detail["profile"] == "sgx_observation"
    assert detail["broker"] == "none"

def test_blocked_asset_classes_are_blocked():
    config = load_config()
    engine = RiskEngine(config)
    context = {
        "now": datetime.now(UTC),
        "power_connected": True,
        "internet_available": True,
        "database_writable": True,
        "broker_available": True,
        "telegram_available": True,
        "market_open": True,
        "open_positions": 0,
        "trades_today": 0
    }
    
    def check_blocked(symbol, asset_class):
        proposal = {
            "symbol": symbol,
            "side": "buy",
            "notional": 5,
            "asset_class": asset_class,
            "created_at": datetime.now(UTC).isoformat(),
            "expires_at": (datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
            "latest_price": 50.0,
            "historical_bars": 100,
            "volume": 10000,
            "strategy_version": "rule_based_v1",
            "reason": "Test"
        }
        dec = engine.evaluate(proposal, context)
        assert not dec.passed
        
    # Crypto (blocked)
    check_blocked("BTCUSD", "crypto")
    # Option (blocked)
    check_blocked("SPY_CALL", "option")
    # Forex / Futures (blocked)
    check_blocked("EURUSD", "forex")
    # Leveraged (blocked)
    check_blocked("TQQQ", "equity")

def test_expired_proposal_notification(temp_storage):
    config = load_config()
    broker = MockBroker()
    service = TradingService(config, temp_storage, broker, "test_run_id")
    service.telegram = MockTelegramBot()
    
    now = datetime.now(UTC)
    expires_at = now - timedelta(minutes=1)
    
    # Insert expired proposal into db
    temp_storage.execute(
        "INSERT INTO trade_proposals(id,run_id,signal_id,symbol,side,notional,status,created_at,expires_at,strategy_version,payload,expiry_notified) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        ("p_expired", "run1", "sig1", "SPY", "buy", 5.0, "expired", now.isoformat(), expires_at.isoformat(), "rule_based_v1", "{}", 0)
    )
    
    service.notify_expired_proposals()
    
    # Telegram must receive exactly one expired message
    assert len(service.telegram.messages) == 1
    assert "Proposal expired" in service.telegram.messages[0]
    assert "SPY" in service.telegram.messages[0]
    
    # The flag expiry_notified must be updated to 1
    row = temp_storage.fetch_all("SELECT expiry_notified FROM trade_proposals WHERE id='p_expired'")[0]
    assert row["expiry_notified"] == 1
    
    # Audit event must be logged
    audits = temp_storage.fetch_all("SELECT * FROM audit_events WHERE event_type='proposal_expiry_notified'")
    assert len(audits) == 1
    
    # Running again must not duplicate notification
    service.notify_expired_proposals()
    assert len(service.telegram.messages) == 1

def test_expired_proposal_cannot_execute():
    config = load_config()
    engine = RiskEngine(config)
    context = {
        "now": datetime.now(UTC),
        "power_connected": True,
        "internet_available": True,
        "database_writable": True,
        "broker_available": True,
        "telegram_available": True,
        "market_open": True,
        "open_positions": 0,
        "trades_today": 0
    }
    
    proposal = {
        "symbol": "SPY",
        "side": "buy",
        "notional": 5,
        "asset_class": "equity",
        "created_at": (datetime.now(UTC) - timedelta(minutes=30)).isoformat(),
        "expires_at": (datetime.now(UTC) - timedelta(minutes=15)).isoformat(), # expired
        "latest_price": 500.0,
        "historical_bars": 100,
        "volume": 10000,
        "strategy_version": "rule_based_v1",
        "reason": "Test"
    }
    
    dec = engine.evaluate(proposal, context)
    assert not dec.passed
    assert any("signal/proposal must be current" in r for r in dec.reasons)

def test_dynamic_expiry_volatility_rules(temp_storage):
    config = load_config()
    broker = MockBroker()
    service = TradingService(config, temp_storage, broker, "test_run_id")
    service.telegram = MockTelegramBot()
    
    # Verify volatility-based dynamic expiry calculations:
    # High volatility >= 0.20 -> high_vol_exp (5m)
    # Low volatility <= 0.12 -> low_vol_exp (20m)
    # Normal in-between -> default_exp (15m)
    
    # Mock get_power_status to return connected=True
    from app.service import evaluate_symbol, get_power_status
    from app.power import PowerStatus
    original_get_power = get_power_status
    import app.service
    app.service.get_power_status = lambda: PowerStatus(True, "mock", "AC power connected")
    
    from app.strategy_rule_based import Signal
    
    def run_scan_with_vol(vol_val):
        original_evaluate = evaluate_symbol
        def mock_evaluate(*args, **kwargs):
            return Signal("ENTRY", "buy", "SPY", "Reason", 0.8, {"volatility_20": vol_val})
        
        # Override evaluate_symbol temporarily
        import app.service
        app.service.evaluate_symbol = mock_evaluate
        try:
            service.scan()
        except Exception as e:
            import traceback
            traceback.print_exc()
            raise e
        finally:
            app.service.evaluate_symbol = original_evaluate
            
    # Clean signals table
    temp_storage.execute("DELETE FROM signals")
    temp_storage.execute("DELETE FROM trade_proposals")
    
    # 1. High Volatility (0.25)
    run_scan_with_vol(0.25)
    high_proposal = temp_storage.fetch_all("SELECT * FROM trade_proposals ORDER BY created_at DESC LIMIT 1")[0]
    payload = json.loads(high_proposal["payload"])
    assert payload["expiry_minutes"] == 5
    assert payload["volatility_class"] == "high"
    
    # 2. Low Volatility (0.10)
    run_scan_with_vol(0.10)
    low_proposal = temp_storage.fetch_all("SELECT * FROM trade_proposals ORDER BY created_at DESC LIMIT 1")[0]
    payload = json.loads(low_proposal["payload"])
    assert payload["expiry_minutes"] == 20
    assert payload["volatility_class"] == "low"
    
    # 3. Normal Volatility (0.15)
    run_scan_with_vol(0.15)
    norm_proposal = temp_storage.fetch_all("SELECT * FROM trade_proposals ORDER BY created_at DESC LIMIT 1")[0]
    payload = json.loads(norm_proposal["payload"])
    assert payload["expiry_minutes"] == 15
    assert payload["volatility_class"] == "normal"
    
    # Restore original function
    app.service.get_power_status = original_get_power

def test_telegram_proposal_formats_expiry_and_volatility():
    config = load_config()
    now_val = datetime.now(UTC)
    expiry_val = now_val + timedelta(minutes=15)
    expiry_sgt = format_sgt(expiry_val)
    
    proposal = {
        "symbol": "SPY",
        "side": "buy",
        "notional": 5,
        "score": 85.0,
        "classification": "Strong paper candidate",
        "reason": "Filters passed",
        "expires_at": expiry_val.isoformat(),
        "expiry_minutes": 15,
        "volatility_class": "normal"
    }
    
    # Normal Volatility Msg
    msg = format_proposal_message(proposal, config)
    assert "Time to decide: 15 minutes" in msg
    assert f"Expires: {expiry_sgt}" in msg
    assert "Urgency guidance: High priority/confidence signal." in msg
    
    # High Volatility Msg
    proposal["expiry_minutes"] = 5
    proposal["volatility_class"] = "high"
    msg_high = format_proposal_message(proposal, config)
    assert "Time to decide: 5 minutes because the market is moving quickly." in msg_high
    assert f"Expires: {expiry_sgt}" in msg_high
    
    # Low Volatility Msg
    proposal["expiry_minutes"] = 20
    proposal["volatility_class"] = "low"
    msg_low = format_proposal_message(proposal, config)
    assert "Time to decide: 20 minutes because conditions are relatively stable." in msg_low
    assert f"Expires: {expiry_sgt}" in msg_low

def test_auto_execution_disabled_by_default(temp_storage):
    config = load_config()
    assert config.get("auto_execution_enabled") is False
    
    broker = MockBroker()
    service = TradingService(config, temp_storage, broker, "test_run_id")
    
    proposal = {
        "symbol": "SPY",
        "side": "buy",
        "notional": 1,
        "score": 95.0,
        "classification": "Strong",
        "reason": "Test"
    }
    assert service._should_auto_execute(proposal) is False

def test_live_auto_execution_hard_blocked(temp_storage):
    config = load_config()
    # Force auto execution and live mode to check safety block
    config["auto_execution_enabled"] = True
    config["auto_execution_mode"] = "paper_high_confidence_only"
    config["mode"] = "live"
    config["live_enabled"] = True
    
    broker = MockBroker()
    service = TradingService(config, temp_storage, broker, "test_run_id")
    
    proposal = {
        "symbol": "SPY",
        "side": "buy",
        "notional": 1,
        "score": 95.0,
        "classification": "Strong",
        "reason": "Test"
    }
    # Must remain false because mode is live
    assert service._should_auto_execute(proposal) is False
