from __future__ import annotations

from app.phase4_allocator import AdaptiveAllocator, STRATEGIES, apply_phase4_schema
from app.storage import Storage
from app.utils import load_config


def test_no_oos_evidence_preserves_all_cash(tmp_path):
    storage=Storage(tmp_path/"p4.sqlite3"); storage.initialize(); cfg=load_config()
    result=AdaptiveAllocator(storage,cfg,"run-cash").run(regime="mixed_uncertain",drawdown_pct=0.0)
    assert result["decision"]=="PRESERVE_CASH"
    assert result["cash_weight"]==1.0
    assert set(result["weights"])==set(STRATEGIES)
    assert all(value==0 for value in result["weights"].values())
    assert {row["state"] for row in storage.fetch_all("SELECT state FROM phase4_strategy_states")}=={"THROTTLED"}


def test_fractional_kelly_is_bounded_and_phase3_authoritative():
    cfg=load_config()["phase4"]
    assert 0 < cfg["fractional_kelly"] <= .25
    assert cfg["full_kelly_allowed"] is False
    assert cfg["phase3_hard_limits_authoritative"] is True
    assert cfg["llm_trading_decisions"] is False


def test_covariance_and_all_stress_scenarios_are_persisted(tmp_path):
    storage=Storage(tmp_path/"p4.sqlite3"); storage.initialize()
    result=AdaptiveAllocator(storage,load_config(),"run-stress").run(regime="mixed_uncertain",drawdown_pct=1.0)
    assert storage.fetch_all("SELECT * FROM phase4_covariance_snapshots")
    scenarios={r["scenario"] for r in storage.fetch_all("SELECT * FROM phase4_stress_results WHERE allocation_id=?",(result["allocation_id"],))}
    assert scenarios=={"spy_down_3","spy_down_5","sector_down_7","volatility_doubles","two_atr_gap","correlations_to_one","largest_position_down_15"}
