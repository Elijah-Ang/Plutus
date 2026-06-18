from __future__ import annotations

from typing import Any

import pandas as pd


def normalize_bars(raw: Any, symbol: str | None = None) -> pd.DataFrame:
    frame = raw.copy() if isinstance(raw, pd.DataFrame) else pd.DataFrame(raw)
    if isinstance(frame.index, pd.MultiIndex) and symbol:
        try:
            frame = frame.xs(symbol)
        except KeyError:
            return pd.DataFrame()
    frame.columns = [str(c).lower() for c in frame.columns]
    return frame.sort_index()


def latest_snapshot(bars: pd.DataFrame) -> dict[str, Any]:
    if bars.empty:
        raise ValueError("No market bars")
    row = bars.iloc[-1]
    return {"latest_price": float(row["close"]), "volume": float(row["volume"]), "price_at": bars.index[-1]}
