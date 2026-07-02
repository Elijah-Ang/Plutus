import pytest

from app.broker_alpaca import AlpacaBroker, AlpacaBrokerError
from app.utils import load_config


def test_live_broker_rejected_before_credentials():
    with pytest.raises(PermissionError):
        AlpacaBroker({"mode": "live", "live_enabled": False, "explicit_live_confirmation": False}, "x", "y")


def test_default_config_is_paper():
    config = load_config()
    assert config["mode"] == "paper"
    assert config["live_enabled"] is False
    assert config["explicit_live_confirmation"] is False


def test_alpaca_defaults_to_paper():
    # If mode is not specified, it should default to paper
    broker = AlpacaBroker({"live_enabled": False}, "dummy_key", "dummy_secret")
    assert broker.mode == "paper"


def test_live_trading_rejected_when_live_enabled_false():
    with pytest.raises(PermissionError):
        AlpacaBroker({"mode": "live", "live_enabled": False}, "dummy_key", "dummy_secret")


def test_live_order_submission_cannot_occur_by_accident():
    # Even if broker initialization is mocked or bypassed, submit_order guards must reject it
    broker = AlpacaBroker({"mode": "paper", "live_enabled": False}, "dummy_key", "dummy_secret")
    # Change mode to live manually to simulate accidental modification
    broker.mode = "live"
    with pytest.raises(PermissionError):
        broker.submit_order("QQQ", "buy", {"qty": 1})


def test_missing_alpaca_credentials_fail_safely(monkeypatch):
    import app.broker_alpaca
    monkeypatch.setattr(app.broker_alpaca, "get_secret", lambda name: None)
    with pytest.raises(RuntimeError) as exc_info:
        AlpacaBroker({"mode": "paper"}, api_key=None, secret_key=None)
    assert "Alpaca credentials are not configured" in str(exc_info.value)


def test_invalid_alpaca_credentials_fail_safely():
    broker = AlpacaBroker({"mode": "paper"}, "invalid_key", "invalid_secret")
    with pytest.raises(AlpacaBrokerError) as exc_info:
        broker.get_account()
    assert exc_info.value.category == "alpaca_auth_error"


def test_no_broker_secrets_are_logged():
    key = "super_secret_alpaca_key_12345"
    secret = "super_secret_alpaca_secret_67890"
    broker = AlpacaBroker({"mode": "paper"}, key, secret)
    with pytest.raises(AlpacaBrokerError) as exc_info:
        broker.get_account()
    
    err_str = str(exc_info.value)
    assert key not in err_str
    assert secret not in err_str
