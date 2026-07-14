from __future__ import annotations

import json

import pandas as pd

from app.phase3_risk import Phase3Controller
from app.phase4_allocator import AdaptiveAllocator, STRATEGIES
from app.service import TradingService
from app.storage import Storage
from app.strategy_performance import (
    StrategyPerformanceEngine,
    more_conservative_state,
    state_risk_policy,
)
from app.utils import load_config


def _db(tmp_path):
    db = Storage(tmp_path / "policy.sqlite3")
    db.initialize()
    return db


def _policy(db, state: str):
    cfg = load_config()
    engine = StrategyPerformanceEngine(db, cfg, as_of="2026-07-12T00:00:00+00:00")
    engine.refresh_strategy("rule_based_v2")
    snapshot = db.fetch_all("SELECT * FROM strategy_performance_snapshots WHERE strategy_version='rule_based_v2'")[0]
    decision = db.fetch_all("SELECT * FROM strategy_policy_decisions WHERE strategy_version='rule_based_v2'")[0]
    metrics = json.loads(snapshot["metrics_json"])
    metrics.update({
        "sample_count": 100,
        "regime_metrics": {"normal": {"count": 50, "expectancy_r": 0.1}, "favorable": {"count": 50, "expectancy_r": 0.1}},
        "evidence_recency_days": 0,
        "version_completeness": 1,
    })
    gates = {name: True for name in (
        "evidence_present", "minimum_sample", "minimum_regimes", "evidence_fresh", "version_complete",
        "positive_expectancy", "drawdown_within_hard_limit", "losing_streak_within_hard_limit", "divergence_within_hard_limit",
    )}
    db.execute(
        "UPDATE strategy_performance_snapshots SET metrics_json=?,recommendation_state=?,evidence_recency_days=0,version_completeness=1 WHERE id=?",
        (json.dumps(metrics), state, snapshot["id"]),
    )
    db.execute(
        "UPDATE strategy_policy_decisions SET state=?,quality_score=80,hard_gates_json=?,maturity_json=? WHERE id=?",
        (state, json.dumps(gates), json.dumps({"sample_count": 100, "regime_count": 2}), decision["id"]),
    )
    return cfg, engine.latest_valid_policy("rule_based_v2")


def test_exact_state_to_risk_mapping_and_conservative_merge():
    assert state_risk_policy("EXPLORATION", initial_stop_risk_pct=.20, add_stop_risk_pct=.10, is_add=False)[:2] == (.05, .25)
    assert state_risk_policy("EXPLORATION", initial_stop_risk_pct=.20, add_stop_risk_pct=.10, is_add=True)[:2] == (0.0, 0.0)
    assert state_risk_policy("THROTTLED", initial_stop_risk_pct=.20, add_stop_risk_pct=.10, is_add=False)[:2] == (.10, .50)
    assert state_risk_policy("THROTTLED", initial_stop_risk_pct=.20, add_stop_risk_pct=.10, is_add=True)[:2] == (.05, .50)
    assert state_risk_policy("ACTIVE", initial_stop_risk_pct=.20, add_stop_risk_pct=.10, is_add=False)[:2] == (.20, 1.0)
    assert state_risk_policy("ACTIVE", initial_stop_risk_pct=.20, add_stop_risk_pct=.10, is_add=True)[:2] == (.10, 1.0)
    assert more_conservative_state("ACTIVE", "THROTTLED") == "THROTTLED"
    assert more_conservative_state("EXPLORATION", "SUSPENDED") == "SUSPENDED"


def test_empty_current_scorecard_is_persisted_research_only_and_phase3_mirrors_it(tmp_path):
    db = _db(tmp_path)
    cfg = load_config()
    engine = StrategyPerformanceEngine(db, cfg, as_of="2026-07-14T08:00:00+00:00")
    engine.refresh_all()
    policy = engine.latest_valid_policy("rule_based_v2")
    assert policy is not None and policy.state == "RESEARCH_ONLY"
    states = Phase3Controller(db, cfg, "policy-run").refresh_strategy_states()
    row = db.fetch_all("SELECT state,reason,payload FROM phase3_strategy_states WHERE strategy_version='rule_based_v2'")[0]
    assert states["rule_based_v2"] == "RESEARCH_ONLY"
    assert "latest strategy performance policy" not in row["reason"]
    assert json.loads(row["payload"])["policy_decision_id"] == policy.id


def test_phase4_operational_state_comes_from_persisted_policy_and_shadow_stays_research_only(tmp_path):
    db = _db(tmp_path)
    cfg, policy = _policy(db, "ACTIVE")
    policy_map = {"rule_based_v2": policy}
    result = AdaptiveAllocator(db, cfg, "policy-run").run(
        regime="normal", drawdown_pct=0.0, strategy_policy_map=policy_map,
        as_of="2026-07-14T08:00:00+00:00",
        portfolio_snapshot={"portfolio_equity": 100.0, "as_of": "2026-07-14T08:00:00+00:00", "equity_as_of": "2026-07-14T08:00:00+00:00"},
    )
    assert result["weights"]["rule_based_v2"] > 0
    assert result["strategy_policies"]["rule_based_v2"]["state"] == "ACTIVE"
    active_policy = result["strategy_policies"]["rule_based_v2"]
    assert active_policy["risk_budget_multiplier"] == active_policy["allocation_weight"] / cfg["phase4"]["max_strategy_weight"]
    assert 0 < active_policy["allocation_weight"] <= 0.175
    assert result["strategy_policies"][STRATEGIES[1]]["state"] == "RESEARCH_ONLY"
    assert result["weights"][STRATEGIES[1]] == 0
    assert all(not value["kelly_used"] for value in result["strategy_policies"].values())
    row = db.fetch_all("SELECT strategy_policy_map_json,strategy_policy_version FROM phase4_allocation_decisions WHERE id=?", (result["allocation_id"],))[0]
    assert json.loads(row["strategy_policy_map_json"])["rule_based_v2"]["state"] == "ACTIVE"
    assert row["strategy_policy_version"]


def test_missing_build2_policy_fails_closed_for_sizing_but_legacy_fixtures_remain_compatible(tmp_path):
    db = _db(tmp_path)
    cfg = load_config()
    service = TradingService(cfg, db, None, "policy-run")
    service._authoritative_runtime_state = lambda force=False: {
        "positions": [], "account": {"equity": 100000, "cash": 90000, "buying_power": 360000},
        "loss_metrics": {"daily_loss_dollars": 0, "weekly_loss_dollars": 0, "daily_loss_confidence": "verified", "weekly_loss_confidence": "verified"},
    }
    bars = pd.DataFrame({"high": [101.0] * 250, "low": [99.0] * 250, "close": [100.0] * 250, "volume": [200000.0] * 250})
    snapshot = {"portfolio_equity": 100000, "cash": 90000, "buying_power": 360000, "total_exposure_dollars": 0, "single_exposures": {}, "cluster_exposures": {}}
    result = service._calculate_dynamic_size("SPY", 90, "normal", 100, bars, snapshot, strategy_version="rule_based_v2")
    assert result["final_notional"] == 0
    assert "policy" in result["blocked_reason"]


def test_policy_sizing_budget_is_not_score_scaled(tmp_path):
    db = _db(tmp_path)
    cfg, _policy_row = _policy(db, "THROTTLED")
    service = TradingService(cfg, db, None, "policy-run")
    service._authoritative_runtime_state = lambda force=False: {
        "positions": [], "account": {"equity": 100000, "cash": 90000, "buying_power": 360000},
        "loss_metrics": {"daily_loss_dollars": 0, "weekly_loss_dollars": 0, "daily_loss_confidence": "verified", "weekly_loss_confidence": "verified"},
    }
    bars = pd.DataFrame({"high": [101.0] * 250, "low": [99.0] * 250, "close": [100.0] * 250, "volume": [200000.0] * 250})
    snapshot = {"portfolio_equity": 100000, "cash": 90000, "buying_power": 360000, "total_exposure_dollars": 0, "single_exposures": {}, "cluster_exposures": {}}
    initial = service._calculate_dynamic_size("SPY", 65, "normal", 100, bars, snapshot, strategy_version="rule_based_v2")
    add = service._calculate_dynamic_size("SPY", 99, "normal", 100, bars, snapshot, is_add=True, strategy_version="rule_based_v2")
    assert initial["permitted_stop_risk_pct"] == .10
    assert add["permitted_stop_risk_pct"] == .05
    assert initial["strategy_risk_multiplier"] == .50
    assert initial["strategy_quality_score"] == 80
    assert initial["final_notional"] <= 100000 * .10 / 100 / (initial["stop_distance_dollars"] / 100)
    assert add["final_notional"] <= 100000 * .05 / 100 / (add["stop_distance_dollars"] / 100)


def test_active_phase4_evidence_weight_reduces_actual_candidate_risk_budget(tmp_path):
    db = _db(tmp_path)
    cfg, _policy_row = _policy(db, "ACTIVE")
    service = TradingService(cfg, db, None, "policy-run")
    service._authoritative_runtime_state = lambda force=False: {
        "positions": [], "account": {"equity": 100000, "cash": 90000, "buying_power": 360000},
        "loss_metrics": {"daily_loss_dollars": 0, "weekly_loss_dollars": 0, "daily_loss_confidence": "verified", "weekly_loss_confidence": "verified"},
    }
    bars = pd.DataFrame({"high": [101.0] * 250, "low": [99.0] * 250, "close": [100.0] * 250, "volume": [200000.0] * 250})
    snapshot = {"portfolio_equity": 100000, "cash": 90000, "buying_power": 360000, "total_exposure_dollars": 0, "single_exposures": {}, "cluster_exposures": {}}
    result = service._calculate_dynamic_size("SPY", 90, "normal", 100, bars, snapshot, strategy_version="rule_based_v2")
    assert result["phase4_mode"] == "adaptive"
    phase4_policy = service._phase4_allocation_cache["strategy_policies"]["rule_based_v2"]
    expected_risk_pct = 0.20 * phase4_policy["risk_budget_multiplier"]
    assert result["risk_budget_dollars"] == 100000.0 * expected_risk_pct / 100.0
    assert result["final_notional"] <= result["risk_budget_dollars"] / result["stop_distance_dollars"] * 100.0
