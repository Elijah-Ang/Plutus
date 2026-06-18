import pytest

from app.broker_alpaca import AlpacaBroker


def test_live_broker_rejected_before_credentials():
    with pytest.raises(PermissionError):
        AlpacaBroker({"mode": "live", "live_enabled": False, "explicit_live_confirmation": False}, "x", "y")


def test_default_config_is_paper():
    from app.utils import load_config
    config = load_config()
    assert config["mode"] == "paper"
    assert config["live_enabled"] is False
    assert config["explicit_live_confirmation"] is False
