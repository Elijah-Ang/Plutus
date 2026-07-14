from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from app.phase4_allocator import (
    AdaptiveAllocator,
    STRATEGIES,
    apply_phase4_schema,
    candidate_allocation_rank,
    operational_risk_budget_multiplier,
)
from app.formula_versions import EVIDENCE_VERSION
from app.storage import Storage
from app.utils import load_config


def _outcome(storage, strategy, value, regime="normal", index=0, calculated_at=None, execution_type="actual_fill", source_table="performance_setups", provenance=None):
    now = datetime.now(UTC).isoformat()
    opportunity_id = f"op-{strategy}-{index}"
    storage.execute("""INSERT INTO research_opportunities(
        id,source_table,source_id,symbol,observed_at,direction,execution_type,entry_price,stop_price,target_price,
        benchmark_entry_price,actual_exit_price,strategy_version,score,score_version,feature_version,feature_snapshot_json,
        universe_version,universe_snapshot_json,regime,regime_version,eligibility_version,blocker,blocker_version,ai_gate,
        ai_gate_version,split_label,provenance_json,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (opportunity_id,source_table,"source-" + opportunity_id,"SPY",now,"long",execution_type,100,98,104,100,101,
         strategy,70,"score_v1","features_v1",json.dumps({}),"universe_v1",json.dumps({}),regime,"regime_v1",
         "shadow_v1",None,None,"not_used",None,"out_of_sample",json.dumps(provenance if provenance is not None else {"fill_id": "fill-" + opportunity_id}),now))
    outcome_id = f"out-{strategy}-{index}"
    storage.execute("""INSERT INTO research_outcomes(
        id,opportunity_id,horizon_sessions,status,reason,maturity_session,exit_session,gross_return,spy_return,
        spy_relative_return,cost_adjusted_return,mfe,mae,gross_r_multiple,cost_adjusted_r_multiple,stop_hit,target_hit,
        first_barrier,ordering_quality,cost_model_version,calculation_version,input_fingerprint,calculated_at,error_category)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (outcome_id,opportunity_id,20,"completed","test outcome", "2026-07-01",f"2026-07-{index + 1:02d}",value,value / 2,
         value / 2,value,0.02,-0.01,value,value,0,1,"target","good","cost_v1",EVIDENCE_VERSION,f"fp-{outcome_id}",
         calculated_at or now,None))


def test_healthy_immature_strategy_gets_bounded_exploration(tmp_path):
    storage=Storage(tmp_path/"p4.sqlite3"); storage.initialize(); cfg=load_config()
    result=AdaptiveAllocator(storage,cfg,"run-cash").run(regime="mixed_uncertain",drawdown_pct=0.0)
    assert result["decision"]=="ALLOCATE_EXPLORATION"
    assert result["cash_weight"]==1.0
    assert set(result["weights"])==set(STRATEGIES)
    assert all(value==0 for value in result["weights"].values())
    assert result["exploration_heat_pct"] <= .25
    assert result["exploration_weights"][STRATEGIES[0]] == .05
    assert all(value <= .05 for value in result["exploration_weights"].values())
    assert all(policy["kelly_used"] is False for policy in result["strategy_policies"].values() if policy["mode"] == "exploration")
    assert {row["state"] for row in storage.fetch_all("SELECT state FROM phase4_strategy_states")}=={"EXPLORATION"}
    assert all(json.loads(row["payload"])["evidence_class"] == "insufficient" for row in storage.fetch_all("SELECT payload FROM phase4_strategy_estimates"))


def test_negative_immature_evidence_is_suspended_not_explored(tmp_path):
    storage=Storage(tmp_path/"p4-negative.sqlite3"); storage.initialize(); cfg=load_config()
    _outcome(storage, STRATEGIES[0], -.01, index=1)
    result=AdaptiveAllocator(storage,cfg,"run-negative").run(regime="normal",drawdown_pct=0.0)
    estimate=result["estimates"][STRATEGIES[0]]
    assert estimate.state == "SUSPENDED"
    assert estimate.evidence_class == "negative"
    assert STRATEGIES[0] not in result["exploration_weights"]
    persisted=storage.fetch_all("SELECT state,payload FROM phase4_strategy_states WHERE strategy_version=?",(STRATEGIES[0],))[0]
    assert persisted["state"] == "SUSPENDED"
    assert json.loads(persisted["payload"])["evidence_class"] == "negative"


def test_qualified_strategy_uses_adaptive_allocation(tmp_path):
    storage=Storage(tmp_path/"p4-qualified.sqlite3"); storage.initialize(); cfg=load_config()
    for index in range(100):
        _outcome(storage, STRATEGIES[0], .02 + (index % 5) * .0001, regime="normal" if index % 2 else "favorable", index=index)
    result=AdaptiveAllocator(storage,cfg,"run-qualified").run(regime="normal",drawdown_pct=0.0)
    estimate=result["estimates"][STRATEGIES[0]]
    assert estimate.state == "ACTIVE"
    assert estimate.evidence_class == "qualified"
    policy=result["strategy_policies"][STRATEGIES[0]]
    assert policy["mode"] == "adaptive"
    assert policy["kelly_used"] is False
    assert policy["kelly_diagnostic_only"] is True
    assert policy["score_sizing_used"] is False
    assert result["weights"][STRATEGIES[0]] > 0


def test_reliable_current_regime_performance_is_an_operational_weight_input(tmp_path):
    storage=Storage(tmp_path/"p4-regime.sqlite3"); storage.initialize(); cfg=load_config()
    for index in range(100):
        _outcome(storage, STRATEGIES[0], .02 + (index % 3) * .0001,
                 regime="normal" if index < 20 else "favorable", index=index)
    result=AdaptiveAllocator(storage,cfg,"run-regime").run(regime="normal",drawdown_pct=0.0)
    regime_metric=result["strategy_policies"][STRATEGIES[0]]["current_regime_performance"]
    assert regime_metric["reliable"] is True
    assert regime_metric["sample_n"] == 20
    assert regime_metric["conservative_expected_return"] > 0
    assert result["weights"][STRATEGIES[0]] > 0


def test_phase4_exploration_defaults_are_bounded():
    cfg=load_config()["phase4"]
    assert cfg["exploration_heat_pct"] == .25
    assert cfg["exploration_stop_risk_pct"] == .05
    assert cfg["max_exploration_stop_risk_pct"] == .10
    assert cfg["exploration_gross_exposure_pct"] == 7.5
    assert cfg["require_manual_approval"] is True


def test_fractional_kelly_is_bounded_and_phase3_authoritative():
    cfg=load_config()["phase4"]
    assert 0 < cfg["fractional_kelly"] <= .25
    assert cfg["full_kelly_allowed"] is False
    assert cfg["phase3_hard_limits_authoritative"] is True
    assert cfg["llm_trading_decisions"] is False
    assert cfg["uncalibrated_score_sizing"] is False
    assert cfg["operational_kelly_enabled"] is False
    assert cfg["operational_allocation_mode"] == "bounded_evidence_aware"


def test_phase4_allocation_fractions_are_dimensionally_separate_from_stop_risk():
    cfg = load_config()
    phase4 = cfg["phase4"]
    phase3 = cfg["phase3"]["risk_profile"]
    assert operational_risk_budget_multiplier(0.35, 0.35) == 1.0
    assert operational_risk_budget_multiplier(0.175, 0.35) == 0.5
    assert operational_risk_budget_multiplier(0.0, 0.35) == 0.0
    assert phase4["max_allocated_risk_fraction"] == 0.75
    assert phase4["max_stress_loss"] == 0.05
    assert phase3["max_trade_stop_risk_pct"] == 0.35
    assert phase3["base_stop_risk_pct"] * operational_risk_budget_multiplier(0.35, 0.35) == 0.20
    assert phase3["base_stop_risk_pct"] * operational_risk_budget_multiplier(0.175, 0.35) == 0.10
    with pytest.raises(ValueError):
        operational_risk_budget_multiplier(0.35, 35.0)


def test_candidate_ranking_is_operationally_evidence_aware_and_deterministic():
    strong = {
        "setup_score": 94, "evidence_quality": 92, "regime": "favorable",
        "execution_fill_rate": .98, "execution_shortfall_bps": 2,
        "conservative_expected_return": .04, "uncertainty": .05,
        "deterioration_score": 0, "symbol_exposure_pct": 0,
        "cluster_exposure_pct": 0, "stop_risk_pct": .20,
    }
    weak = {
        **strong, "conservative_expected_return": -.01, "uncertainty": .80,
        "deterioration_score": .70, "symbol_exposure_pct": 5.5,
        "cluster_exposure_pct": 14.0,
    }
    first = candidate_allocation_rank(strong)
    assert first == candidate_allocation_rank(strong)
    assert first["ranking_score"] > candidate_allocation_rank(weak)["ranking_score"]


def test_covariance_overlap_reduces_rank_and_low_correlation_capacity_expands_rank():
    inputs = {
        "setup_score": 90, "evidence_quality": 85, "regime": "normal",
        "execution_fill_rate": .90, "execution_shortfall_bps": 5,
        "conservative_expected_return": .02, "uncertainty": .10,
        "deterioration_score": 0, "stop_risk_pct": .20,
    }
    diversified = candidate_allocation_rank({**inputs, "symbol_exposure_pct": 0, "cluster_exposure_pct": 0})
    overlapping = candidate_allocation_rank({**inputs, "symbol_exposure_pct": 5.5, "cluster_exposure_pct": 14.0})
    assert diversified["diversification_score"] > overlapping["diversification_score"]
    assert diversified["ranking_score"] > overlapping["ranking_score"]


def test_shadow_evidence_never_becomes_operational_allocation(tmp_path):
    storage=Storage(tmp_path/"p4-shadow.sqlite3"); storage.initialize(); cfg=load_config()
    for index in range(100):
        _outcome(storage, STRATEGIES[1], .02 + (index % 5) * .0001, regime="normal" if index % 2 else "favorable", index=index,
                 execution_type="shadow_hypothetical", source_table="shadow_insights", provenance={"shadow_id": f"shadow-{index}"})
    result=AdaptiveAllocator(storage,cfg,"run-shadow").run(regime="normal",drawdown_pct=0.0)
    assert result["estimates"][STRATEGIES[1]].state == "ACTIVE"
    assert result["weights"][STRATEGIES[1]] == 0
    assert result["strategy_policies"][STRATEGIES[1]]["mode"] == "research_only"
    assert result["strategy_policies"][STRATEGIES[1]]["operationally_executable"] is False


def test_negative_shadow_evidence_keeps_auditable_state_without_allocation(tmp_path):
    storage=Storage(tmp_path/"p4-shadow-negative.sqlite3"); storage.initialize(); cfg=load_config()
    for index in range(100):
        _outcome(storage, STRATEGIES[1], -.02, regime="normal" if index % 2 else "favorable", index=index,
                 execution_type="shadow_hypothetical", source_table="shadow_insights", provenance={"shadow_id": f"shadow-{index}"})
    result=AdaptiveAllocator(storage,cfg,"run-shadow-negative").run(regime="normal",drawdown_pct=0.0)
    assert result["estimates"][STRATEGIES[1]].state == "SUSPENDED"
    assert result["estimates"][STRATEGIES[1]].evidence_class == "negative"
    assert result["weights"][STRATEGIES[1]] == 0
    assert result["strategy_policies"][STRATEGIES[1]]["mode"] == "research_only"


def test_covariance_and_all_stress_scenarios_are_persisted(tmp_path):
    storage=Storage(tmp_path/"p4.sqlite3"); storage.initialize()
    result=AdaptiveAllocator(storage,load_config(),"run-stress").run(regime="mixed_uncertain",drawdown_pct=1.0)
    assert storage.fetch_all("SELECT * FROM phase4_covariance_snapshots")
    scenarios={r["scenario"] for r in storage.fetch_all("SELECT * FROM phase4_stress_results WHERE allocation_id=?",(result["allocation_id"],))}
    assert scenarios=={"spy_down_3","spy_down_5","sector_down_7","volatility_doubles","two_atr_gap","correlations_to_one","largest_position_down_15"}
