import numpy as np
import pandas as pd

from app.strategy_rule_based import evaluate_symbol


def test_trending_data_can_create_entry():
    close = np.linspace(100, 150, 250)
    bars = pd.DataFrame({"close": close, "volume": np.full(250, 1000)})
    signal = evaluate_symbol("QQQ", bars, maximum_volatility_20d=1)
    assert signal.action == "ENTRY"
    assert signal.strategy_version == "rule_based_v2"


def test_missing_long_history_and_ma200_fail_closed():
    close = np.linspace(100, 150, 199)
    bars = pd.DataFrame({"close": close, "volume": np.full(199, 1000)})
    signal = evaluate_symbol("QQQ", bars, maximum_volatility_20d=1)
    assert signal.action == "HOLD"
    assert "insufficient completed daily history" in signal.reason


def test_current_session_bar_is_not_used_for_signal():
    from datetime import UTC, datetime
    index = pd.date_range(end=datetime.now(UTC), periods=200, freq="D", tz="UTC")
    bars = pd.DataFrame({"close": np.linspace(100, 150, 200), "volume": np.full(200, 1000)}, index=index)
    signal = evaluate_symbol("QQQ", bars, maximum_volatility_20d=1)
    assert signal.action == "HOLD"
    assert "insufficient completed daily history" in signal.reason


def test_point_in_time_simulation_uses_runtime_decision_for_same_history():
    from app.research_validation import PointInTimeSimulator

    index = pd.date_range("2025-01-02", periods=205, freq="B", tz="UTC")
    close = np.linspace(100, 150, 205)
    bars = pd.DataFrame({"open": close, "high": close + 1, "low": close - 1, "close": close, "volume": np.full(205, 1000)}, index=index)
    opportunities = PointInTimeSimulator().opportunities(
        "QQQ", bars, bars, lambda symbol, day: (True, "test-universe"), maximum_volatility_20d=1.0
    )
    assert opportunities
    for opportunity in opportunities:
        end = list(index).index(pd.Timestamp(opportunity["observed_at"]))
        runtime_signal = evaluate_symbol("QQQ", bars.iloc[:end], market_open=True, maximum_volatility_20d=1.0)
        assert runtime_signal.action == "ENTRY"


def test_ml_shadow_cannot_execute():
    from app.strategy_ml_shadow import MLShadowStrategy
    try:
        MLShadowStrategy().submit_order()
    except PermissionError:
        pass
    else:
        raise AssertionError("shadow model must not execute")
