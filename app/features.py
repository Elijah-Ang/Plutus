from __future__ import annotations

import numpy as np
import pandas as pd


def build_features(bars: pd.DataFrame) -> pd.DataFrame:
    close = bars["close"].astype(float)
    volume = bars["volume"].astype(float)
    result = pd.DataFrame(index=bars.index)
    result["return_1"] = close.pct_change(1)
    result["return_5"] = close.pct_change(5)
    result["return_20"] = close.pct_change(20)
    result["ma_20"] = close.rolling(20).mean()
    result["ma_50"] = close.rolling(50).mean()
    result["ma_200"] = close.rolling(200).mean()
    result["distance_ma20"] = close / result["ma_20"] - 1
    result["distance_ma50"] = close / result["ma_50"] - 1
    result["distance_ma200"] = close / result["ma_200"] - 1
    result["volatility_10"] = result["return_1"].rolling(10).std() * np.sqrt(252)
    result["volatility_20"] = result["return_1"].rolling(20).std() * np.sqrt(252)
    result["volume_change"] = volume.pct_change(5)
    result["close"] = close
    result["volume"] = volume
    return result.replace([np.inf, -np.inf], np.nan)
