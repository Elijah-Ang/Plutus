import math
import pandas as pd
import pytest
from app.service import TradingService
from app.storage import Storage

class MockAccount:
    def __init__(self, cash, equity, buying_power):
        self.cash = cash
        self.equity = equity
        self.buying_power = buying_power

def test_risk_portfolio_sizing_basic_scaling():
    # Base config with stage cap disabled to test pure scaling
    config = {
        "mode": "paper",
        "live_enabled": False,
        "position_sizing": {
            "enabled": True,
            "mode": "risk_portfolio",
            "use_stage_dollar_cap": False,
            "risk_per_trade_pct": 0.05,
            "max_trade_notional_pct_of_equity": 1.0, # 1%
            "max_position_notional_pct_of_equity": 2.0,
            "max_total_portfolio_exposure_pct": 10.0,
            "max_cluster_exposure_pct": 10.0,
            "min_cash_reserve_pct": 10.0,
            "max_cash_usage_pct": 10.0,
            "min_paper_notional": 5.0,
            "stop_model": {
                "max_stop_pct": 8.0,
                "min_stop_pct": 1.0
            },
            "score_multiplier": {
                "65_74": 0.5,
                "75_84": 1.0,
                "85_94": 1.25,
                "95_100": 1.5
            },
            "volatility_multiplier": {
                "too_quiet": 0.75,
                "normal": 1.0,
                "elevated": 0.5,
                "high": 0.25,
                "extreme": 0.0
            }
        }
    }
    
    # We initialize TradingService with empty/mock parameters
    service = TradingService(config, None, None, "test-run")
    
    # Snapshot: 100k equity, 90k cash, 360k bp, total exposure 0.0
    snapshot = {
        "portfolio_equity": 100000.0,
        "cash": 90000.0,
        "buying_power": 360000.0,
        "total_exposure_dollars": 0.0,
        "single_exposures": {},
        "cluster_exposures": {}
    }
    
    # Mock bars
    bars = pd.DataFrame()
    
    # 1. High score (90), normal volatility, price 100.0
    # stop distance fallback = 8.0% of 100 = 8.0.
    # risk budget = 100k * 0.05 / 100 = $50.00.
    # risk based shares = 50 / 8 = 6.25 shares.
    # risk based notional = 6.25 * 100 = $500.00.
    # score mult = 1.25, vol mult = 1.0.
    # target notional = 500.0 * 1.25 = $625.00.
    # cash_cap: usable_cash = 90k - 10k (reserve) = 80k. Max usage = 10k. cash_cap = 10k.
    # max_trade_notional = 100k * 1% = $1,000.00.
    # Capped only by max_trade_notional (since $625 < $1,000).
    res = service._calculate_dynamic_size("SPY", 90.0, "normal", 100.0, bars, snapshot)
    
    assert res["final_notional"] == 781.25
    assert res["risk_budget"] == 50.0
    assert "max_trade_notional_cap" not in res["caps_applied"]
    
    # 2. Change max_trade_notional_pct_of_equity to 0.5% ($500)
    config["position_sizing"]["max_trade_notional_pct_of_equity"] = 0.5
    res2 = service._calculate_dynamic_size("SPY", 90.0, "normal", 100.0, bars, snapshot)
    assert res2["final_notional"] == 500.0
    assert "max_trade_notional_cap" in res2["caps_applied"]

def test_risk_portfolio_sizing_stage_caps():
    config = {
        "mode": "paper",
        "live_enabled": False,
        "position_sizing": {
            "enabled": True,
            "mode": "risk_portfolio",
            "stage": "smoke_test",
            "use_stage_dollar_cap": True,
            "stage_max_initial_notional": {
                "smoke_test": 25.0,
                "moderate_paper": 250.0
            },
            "stage_max_add_notional": {
                "smoke_test": 10.0,
                "moderate_paper": 100.0
            },
            "risk_per_trade_pct": 0.05,
            "max_trade_notional_pct_of_equity": 1.0,
            "max_position_notional_pct_of_equity": 2.0,
            "max_total_portfolio_exposure_pct": 10.0,
            "max_cluster_exposure_pct": 10.0,
            "min_cash_reserve_pct": 10.0,
            "max_cash_usage_pct": 10.0,
            "min_paper_notional": 5.0,
            "stop_model": {
                "max_stop_pct": 8.0,
                "min_stop_pct": 1.0
            }
        }
    }
    
    service = TradingService(config, None, None, "test-run")
    
    snapshot = {
        "portfolio_equity": 100000.0,
        "cash": 90000.0,
        "buying_power": 360000.0,
        "total_exposure_dollars": 0.0,
        "single_exposures": {},
        "cluster_exposures": {}
    }
    bars = pd.DataFrame()
    
    # Initial trade (stage cap = 25)
    res = service._calculate_dynamic_size("SPY", 90.0, "normal", 100.0, bars, snapshot, is_add=False)
    assert res["final_notional"] == 25.0
    assert "stage_cap" in res["caps_applied"]
    
    # Add trade (stage cap = 10)
    res_add = service._calculate_dynamic_size("SPY", 90.0, "normal", 100.0, bars, snapshot, is_add=True)
    assert res_add["final_notional"] == 10.0
    assert "stage_cap" in res_add["caps_applied"]

def test_risk_portfolio_sizing_cash_safety():
    config = {
        "mode": "paper",
        "live_enabled": False,
        "position_sizing": {
            "enabled": True,
            "mode": "risk_portfolio",
            "use_stage_dollar_cap": False,
            "risk_per_trade_pct": 0.05,
            "max_trade_notional_pct_of_equity": 2.0,
            "max_position_notional_pct_of_equity": 10.0,
            "max_total_portfolio_exposure_pct": 50.0,
            "max_cluster_exposure_pct": 50.0,
            "min_cash_reserve_pct": 20.0, # 20% of 100k = 20k reserve
            "max_cash_usage_pct": 10.0, # 10% of 100k = 10k usage cap
            "min_paper_notional": 5.0,
            "stop_model": {
                "max_stop_pct": 8.0,
                "min_stop_pct": 1.0
            }
        }
    }
    
    service = TradingService(config, None, None, "test-run")
    
    # Case A: High cash: 100k equity, 90k cash.
    # reserve = 20k, usable cash = 70k, usage limit = 10k. cash_cap = 10k.
    # target = 625 (risk budget $50, stop 8%). Target is smaller than cash_cap, so notional is 625.
    snapshot_high_cash = {
        "portfolio_equity": 100000.0,
        "cash": 90000.0,
        "buying_power": 360000.0,
        "total_exposure_dollars": 0.0,
        "single_exposures": {},
        "cluster_exposures": {}
    }
    bars = pd.DataFrame()
    
    res = service._calculate_dynamic_size("SPY", 90.0, "normal", 100.0, bars, snapshot_high_cash)
    assert res["final_notional"] == 781.25
    assert "cash_cap" not in res["caps_applied"]
    
    # Case B: Low cash: 100k equity, 20.5k cash.
    # reserve = 20k, usable cash = 500, usage limit = 10k. cash_cap = 500.
    # Since final_notional (500) < min_paper_notional (5), clamping logic kicks in.
    # But min_notional (5) is <= cash_cap (500). So it is clamped to min_paper_notional (5.0)!
    snapshot_low_cash = {
        "portfolio_equity": 100000.0,
        "cash": 20500.0,
        "buying_power": 360000.0,
        "total_exposure_dollars": 0.0,
        "single_exposures": {},
        "cluster_exposures": {}
    }
    res_low = service._calculate_dynamic_size("SPY", 90.0, "normal", 100.0, bars, snapshot_low_cash)
    assert res_low["final_notional"] == 500.0
    assert "cash_cap" in res_low["caps_applied"]

def test_risk_portfolio_sizing_small_account():
    config = {
        "mode": "paper",
        "live_enabled": False,
        "position_sizing": {
            "enabled": True,
            "mode": "risk_portfolio",
            "use_stage_dollar_cap": False,
            "risk_per_trade_pct": 0.05,
            "max_trade_notional_pct_of_equity": 10.0,
            "max_position_notional_pct_of_equity": 20.0,
            "max_total_portfolio_exposure_pct": 50.0,
            "max_cluster_exposure_pct": 50.0,
            "min_cash_reserve_pct": 20.0, # 20% of 50 = $10 reserve
            "max_cash_usage_pct": 10.0, # 10% of 50 = $5 usage cap
            "min_paper_notional": 5.0,
            "stop_model": {
                "max_stop_pct": 8.0,
                "min_stop_pct": 1.0
            }
        }
    }
    
    service = TradingService(config, None, None, "test-run")
    
    # Account has $50 equity and $50 cash.
    # usable cash = 50 - 10 = $40. usage limit = $5. cash_cap = $5.
    snapshot = {
        "portfolio_equity": 50.0,
        "cash": 50.0,
        "buying_power": 50.0,
        "total_exposure_dollars": 0.0,
        "single_exposures": {},
        "cluster_exposures": {}
    }
    bars = pd.DataFrame()
    
    res = service._calculate_dynamic_size("SPY", 90.0, "normal", 100.0, bars, snapshot)
    
    # A constrained result below the executable minimum is blocked; it is not
    # raised to the minimum by an upward clamp.
    assert res["final_notional"] == 0.0
    assert "below the executable minimum" in res["blocked_reason"]
    
    # Account has only $8 cash left.
    # usable cash = 8 - 10 = -$2. usable cash = 0. cash_cap = 0.
    # final_notional = 0, blocked_reason will be active.
    snapshot_bankrupt = {
        "portfolio_equity": 50.0,
        "cash": 8.0,
        "buying_power": 8.0,
        "total_exposure_dollars": 0.0,
        "single_exposures": {},
        "cluster_exposures": {}
    }
    res_bankrupt = service._calculate_dynamic_size("SPY", 90.0, "normal", 100.0, bars, snapshot_bankrupt)
    assert res_bankrupt["final_notional"] == 0.0
    assert res_bankrupt["blocked_reason"] is not None
