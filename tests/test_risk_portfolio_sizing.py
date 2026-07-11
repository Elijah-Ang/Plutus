import pandas as pd

from app.service import TradingService


def _bars() -> pd.DataFrame:
    return pd.DataFrame({
        "open": [102.0] * 20,
        "high": [104.0] * 20,
        "low": [100.0] * 20,
        "close": [102.0] * 20,
        "volume": [10000.0] * 20,
    })


def _config() -> dict:
    return {
        "mode": "paper",
        "live_enabled": False,
        "phase3": {"enabled": False, "active": False},
        "phase4": {"enabled": False, "active": False},
        "position_sizing": {
            "enabled": True,
            "mode": "risk_portfolio",
            "stage": "moderate_paper",
            "use_stage_dollar_cap": False,
            "stage_max_initial_notional_usd": {"moderate_paper": 250.0},
            "stage_max_add_notional_usd": {"moderate_paper": 100.0},
            "risk_per_trade_pct": 0.05,
            "max_trade_notional_pct_equity": 1.0,
            "max_position_notional_pct_equity": 2.0,
            "max_total_portfolio_exposure_pct": 10.0,
            "max_cluster_exposure_pct": 10.0,
            "min_cash_reserve_pct": 10.0,
            "max_cash_usage_pct": 10.0,
            "default_paper_notional_usd": 250.0,
            "default_add_notional_usd": 100.0,
            "minimum_executable_notional_usd": 5.0,
            "add_size_multiplier": 0.5,
            "stop_model": {"atr_multiple": 2.0, "max_stop_pct": 8.0, "min_stop_pct": 1.0},
            "score_multiplier": {"65_74": 0.5, "75_84": 1.0, "85_94": 1.25, "95_100": 1.5},
            "volatility_multiplier": {"too_quiet": 0.75, "normal": 1.0, "elevated": 0.5, "high": 0.25, "extreme": 0.0},
        },
    }


def _snapshot(equity=100000.0, cash=90000.0, buying_power=360000.0) -> dict:
    return {
        "portfolio_equity": equity,
        "cash": cash,
        "buying_power": buying_power,
        "total_exposure_dollars": 0.0,
        "single_exposures": {},
        "cluster_exposures": {},
    }


def test_sizing_uses_validated_stop_and_no_temporary_fifty_dollar_cap():
    config = _config()
    result = TradingService(config, None, None, "test-run")._calculate_dynamic_size(
        "SPY", 90.0, "normal", 100.0, _bars(), _snapshot()
    )
    assert result["stop_validation_status"] == "validated"
    assert result["final_notional"] == 625.0
    assert result["absolute_cap"] == float("inf")
    assert result["final_notional"] > 50.0


def test_stage_caps_are_ceilings_for_initial_and_adds():
    config = _config()
    config["position_sizing"]["use_stage_dollar_cap"] = True
    config["position_sizing"]["stage"] = "smoke_test"
    config["position_sizing"]["stage_max_initial_notional_usd"] = {"smoke_test": 25.0}
    config["position_sizing"]["stage_max_add_notional_usd"] = {"smoke_test": 10.0}
    service = TradingService(config, None, None, "test-run")
    assert service._calculate_dynamic_size("SPY", 90.0, "normal", 100.0, _bars(), _snapshot())["final_notional"] == 25.0
    assert service._calculate_dynamic_size("SPY", 90.0, "normal", 100.0, _bars(), _snapshot(), is_add=True)["final_notional"] == 10.0


def test_constrained_notional_is_not_raised_to_the_minimum():
    config = _config()
    result = TradingService(config, None, None, "test-run")._calculate_dynamic_size(
        "SPY", 90.0, "normal", 100.0, _bars(), _snapshot(cash=2.0, buying_power=2.0)
    )
    assert result["final_notional"] == 0.0
    assert result["blocked_reason"] in {"no safe executable notional remains after ceiling constraints", "constrained notional is below the executable minimum"}


def test_missing_stop_evidence_blocks_even_with_other_account_inputs():
    config = _config()
    empty_bars = pd.DataFrame({"close": [100.0] * 250, "volume": [10000.0] * 250})
    result = TradingService(config, None, None, "test-run")._calculate_dynamic_size(
        "SPY", 90.0, "normal", 100.0, empty_bars, _snapshot()
    )
    assert result["final_notional"] == 0.0
    assert "stop evidence" in (result["blocked_reason"] or "")
