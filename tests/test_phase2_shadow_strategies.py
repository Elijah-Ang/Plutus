from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

from app.research_validation import apply_phase1_schema
from app.shadow_strategies import SHADOW_MODE, STRATEGY_VERSIONS, ShadowStrategyEngine, apply_phase2_schema
from app.storage import Storage
from app.formula_versions import EVIDENCE_VERSION


def bars(multiplier: float = 1.0, breakout: bool = False) -> pd.DataFrame:
    index = pd.date_range("2025-01-01", periods=250, freq="B", tz="UTC")
    close = np.linspace(80.0, 120.0 * multiplier, 250)
    if breakout:
        close[-20:-1] = np.linspace(108.0, 115.0, 19)
        close[-1] = 121.0
    return pd.DataFrame({
        "open": close * 0.999, "high": close * 1.005, "low": close * 0.995,
        "close": close, "volume": np.r_[np.full(249, 1_000_000.0), 1_500_000.0],
    }, index=index)


def storage(tmp_path) -> Storage:
    value = Storage(tmp_path / "phase2.sqlite3")
    value.initialize()
    with value.connect() as conn:
        apply_phase1_schema(conn)
        apply_phase2_schema(conn)
    return value


def test_five_versioned_sleeves_emit_immutable_standard_insights(tmp_path):
    db = storage(tmp_path)
    snapshots = [
        {"symbol": "SPY", "bars": bars(1.0), "universe_source": "static"},
        {"symbol": "XLK", "bars": bars(1.2, breakout=True), "universe_source": "static"},
        {"symbol": "XLF", "bars": bars(0.9), "universe_source": "static"},
    ]
    insights = ShadowStrategyEngine(db, "run-1").evaluate(snapshots, observed_at=datetime(2026, 1, 2, tzinfo=UTC))
    assert set(STRATEGY_VERSIONS).issubset({item.sleeve for item in insights})
    assert all(item.mode == SHADOW_MODE and item.outcome_engine_version == EVIDENCE_VERSION for item in insights)
    with pytest.raises(FrozenInstanceError):
        insights[0].score = 0  # type: ignore[misc]
    with pytest.raises(Exception, match="immutable"):
        db.execute("UPDATE shadow_insights SET score=0 WHERE id=?", (insights[0].id,))
    active = db.fetch_all("SELECT * FROM shadow_insights WHERE signal='active'")
    assert active
    assert int(db.fetch_all("SELECT COUNT(*) n FROM research_opportunities WHERE source_table='shadow_insights'")[0]["n"]) == len(active)
    assert int(db.fetch_all("SELECT COUNT(*) n FROM research_outcomes")[0]["n"]) == len(active) * 3


def test_shadow_writer_cannot_write_execution_surfaces(tmp_path):
    db = storage(tmp_path)
    protected = ("trade_proposals", "approvals", "risk_reservations", "order_intents", "orders")
    before = {table: db.fetch_all(f"SELECT COUNT(*) n FROM {table}")[0]["n"] for table in protected}
    ShadowStrategyEngine(db, "run-2").evaluate(
        [{"symbol": "SPY", "bars": bars()}, {"symbol": "XLK", "bars": bars(1.2, breakout=True)}],
        observed_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    after = {table: db.fetch_all(f"SELECT COUNT(*) n FROM {table}")[0]["n"] for table in protected}
    assert after == before


def test_overlap_portfolios_and_promotion_fail_closed(tmp_path):
    db = storage(tmp_path)
    ShadowStrategyEngine(db, "run-3").evaluate(
        [{"symbol": "SPY", "bars": bars()}, {"symbol": "XLK", "bars": bars(1.2, breakout=True)}],
        observed_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    assert db.fetch_all("SELECT * FROM shadow_overlap_observations")
    assert db.fetch_all("SELECT * FROM shadow_portfolio_observations")
    gates = db.fetch_all("SELECT * FROM shadow_promotion_assessments")
    assert len(gates) == 5 and {row["status"] for row in gates} == {"NOT_ELIGIBLE"}
