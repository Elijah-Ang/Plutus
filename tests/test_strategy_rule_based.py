import numpy as np
import pandas as pd

from app.strategy_rule_based import evaluate_symbol


def test_trending_data_can_create_entry():
    close = np.linspace(100, 150, 250)
    bars = pd.DataFrame({"close": close, "volume": np.full(250, 1000)})
    signal = evaluate_symbol("QQQ", bars, maximum_volatility_20d=1)
    assert signal.action == "ENTRY"
    assert signal.strategy_version == "rule_based_v1"


def test_ml_shadow_cannot_execute():
    from app.strategy_ml_shadow import MLShadowStrategy
    try:
        MLShadowStrategy().submit_order()
    except PermissionError:
        pass
    else:
        raise AssertionError("shadow model must not execute")
