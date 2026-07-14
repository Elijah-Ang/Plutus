from __future__ import annotations

import inspect
import json

import pytest

from app.adaptive_conviction import AdaptiveConvictionEngine, apply_adaptive_conviction_schema
from app.configuration import ConfigurationError, validate_config
from app.service import TradingService
from app.storage import Storage
from app.utils import load_config
from scripts.replay_adaptive_conviction import replay_database


def _engine() -> AdaptiveConvictionEngine:
    return AdaptiveConvictionEngine(load_config())


def _inputs(**overrides):
    values = {
        "run_id": "run-1", "proposal_id": "proposal-1", "candidate_id": "candidate-1", "setup_id": "setup-1",
        "strategy_version": "rule_based_v2", "policy_decision_id": "policy-1", "performance_snapshot_id": "snapshot-1",
        "action": "entry", "side": "buy", "strategy_authorized": True, "strategy_policy_state": "ACTIVE",
        "strategy_stop_risk_cap_pct": 0.35,
        "evidence_quality": 1.0, "evidence_calibrated": True, "regime_alignment": 1.0,
        "account_drawdown_pct": 0.0, "daily_realized_loss_pct": 0.0, "weekly_realized_loss_pct": 0.0,
        "execution_quality": 1.0, "execution_integrity_ok": True, "reconciliation_ok": True,
        "current_portfolio_heat_pct": 0.0, "current_gross_exposure_pct": 0.0,
        "symbol_exposure_pct": 0.0, "cluster_exposure_pct": 0.0, "correlation_score": 0.0,
        "setup_score": 96.0, "stop_valid": True, "stop_geometry_quality": 1.0,
        "reward_to_risk": 3.0, "average_dollar_volume": 50_000_000.0, "quote_spread_bps": 0.0,
        "market_data_fresh": True, "risk_checks_passed": True, "deterioration_detected": False,
        "operational_stop_risk_pct": 0.20,
    }
    values.update(overrides)
    return values


@pytest.mark.parametrize(
    ("expected_class", "expected_mode", "overrides"),
    [
        ("REJECTED", "DEFENSIVE", {"stop_valid": False}),
        ("STANDARD", "NORMAL", {
            "evidence_calibrated": False, "evidence_quality": None, "regime_alignment": None,
            "account_drawdown_pct": None, "daily_realized_loss_pct": None, "weekly_realized_loss_pct": None,
            "execution_quality": None, "correlation_score": None, "reward_to_risk": None,
        }),
        ("STRONG", "NORMAL", {
            "setup_score": 86.0, "reward_to_risk": 1.6, "evidence_quality": 0.65, "regime_alignment": 0.65,
            "account_drawdown_pct": 2.1, "daily_realized_loss_pct": 0.20, "weekly_realized_loss_pct": 0.50,
            "execution_quality": 0.65, "stop_geometry_quality": 0.65, "average_dollar_volume": 32_500_000.0,
            "quote_spread_bps": 14.0, "symbol_exposure_pct": 1.0, "cluster_exposure_pct": 3.0, "correlation_score": 0.35,
        }),
        ("HIGH_CONVICTION", "OPPORTUNISTIC", {
            "setup_score": 92.0, "reward_to_risk": 2.2, "evidence_quality": 0.80, "regime_alignment": 0.80,
            "account_drawdown_pct": 1.2, "daily_realized_loss_pct": 0.10, "weekly_realized_loss_pct": 0.20,
            "execution_quality": 0.80, "stop_geometry_quality": 0.80, "average_dollar_volume": 40_000_000.0,
            "quote_spread_bps": 8.0, "symbol_exposure_pct": 1.0, "cluster_exposure_pct": 3.0, "correlation_score": 0.20,
        }),
        ("EXCEPTIONAL", "AGGRESSIVE", {}),
    ],
)
def test_opportunity_classes_and_deployment_modes(expected_class, expected_mode, overrides):
    decision = _engine().evaluate(_inputs(**overrides))
    assert decision is not None
    assert decision.opportunity_class == expected_class
    assert decision.deployment_mode == expected_mode


def test_raw_score_alone_cannot_expand_and_missing_data_is_not_aggressive():
    decision = _engine().evaluate(_inputs(
        setup_score=100.0, evidence_quality=None, evidence_calibrated=False, regime_alignment=None,
        account_drawdown_pct=None, daily_realized_loss_pct=None, weekly_realized_loss_pct=None,
        execution_quality=None, correlation_score=None, reward_to_risk=None, quote_spread_bps=None,
    ))
    assert decision is not None
    assert decision.opportunity_class == "STANDARD"
    assert decision.deployment_mode == "NORMAL"
    assert decision.confidence < 1.0


def test_multipliers_and_hard_ceiling_are_bounded():
    decision = _engine().evaluate(_inputs(base_strategy_risk_pct=9.0))
    assert decision is not None
    assert decision.base_strategy_risk_pct == 0.35
    assert 0.0 <= decision.opportunity_multiplier <= 1.25
    assert 0.75 <= decision.regime_multiplier <= 1.25
    assert 0.75 <= decision.account_health_multiplier <= 1.15
    assert 0.75 <= decision.execution_quality_multiplier <= 1.25
    assert 0.65 <= decision.diversification_multiplier <= 1.20
    assert decision.recommended_stop_risk_pct <= 0.35


@pytest.mark.parametrize(
    "overrides",
    [
        {"account_drawdown_pct": 5.5},
        {"daily_realized_loss_pct": 0.70},
        {"weekly_realized_loss_pct": 1.40},
        {"execution_integrity_ok": False},
        {"reconciliation_ok": False},
        {"execution_quality": 0.10},
        {"correlation_score": 0.95},
        {"symbol_exposure_pct": 5.9},
        {"cluster_exposure_pct": 14.9},
        {"deterioration_detected": True},
    ],
)
def test_degradation_integrity_correlation_and_concentration_never_expand(overrides):
    decision = _engine().evaluate(_inputs(**overrides))
    assert decision is not None
    assert decision.deployment_mode not in {"OPPORTUNISTIC", "AGGRESSIVE"}
    assert decision.recommended_stop_risk_pct <= 0.20


def test_probe_is_operationally_bounded_and_exit_bypasses_classification():
    probe = _engine().evaluate(_inputs(strategy_policy_state="PROBE", strategy_stop_risk_cap_pct=0.03, evidence_calibrated=False, operational_stop_risk_pct=0.03))
    assert probe is not None
    assert probe.operational_stop_risk_pct == 0.03
    assert probe.report_only is False
    assert probe.recommended_stop_risk_pct <= 0.03
    assert _engine().evaluate(_inputs(action="exit", side="sell")) is None


def test_persistence_and_replay_are_deterministic(tmp_path):
    storage = Storage(tmp_path / "adaptive.sqlite3")
    storage.initialize()
    engine = _engine()
    first = engine.evaluate(_inputs())
    second = engine.evaluate(_inputs())
    assert first is not None and second is not None
    assert first.decision_fingerprint == second.decision_fingerprint
    engine.persist(storage, first)
    engine.persist(storage, second)
    assert storage.fetch_all("SELECT COUNT(*) n FROM adaptive_conviction_operational_decisions")[0]["n"] == 1
    before = {table: storage.fetch_all(f"SELECT COUNT(*) n FROM {table}")[0]["n"] for table in ("trade_proposals", "approvals", "risk_reservations", "order_intents", "orders", "fills")}
    replay = engine.replay([_inputs(), _inputs(proposal_id="proposal-2")])
    after = {table: storage.fetch_all(f"SELECT COUNT(*) n FROM {table}")[0]["n"] for table in before}
    assert replay["trading_state_mutations"] == 0
    assert replay["contradictory_classifications"] == []
    assert before == after


def test_read_only_database_replay_does_not_mutate_trading_state(tmp_path):
    storage = Storage(tmp_path / "replay.sqlite3")
    storage.initialize()
    payload = {
        "action": "entry", "score": 90, "stop_validation_status": "validated", "stop_distance_pct": 2.0,
        "average_dollar_volume": 20_000_000, "proposal_price_age_seconds_at_send": 10,
    }
    storage.execute(
        """INSERT INTO trade_proposals(id,run_id,signal_id,symbol,side,notional,status,created_at,expires_at,strategy_version,payload,
           strategy_state,permitted_stop_risk_pct) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("p1", "r1", "s1", "SPY", "buy", 100.0, "pending", "2026-07-14T00:00:00+00:00", "2026-07-14T00:15:00+00:00", "rule_based_v2", json.dumps(payload), "PROBE", 0.03),
    )
    result = replay_database(storage.path)
    assert result["source_records"]["production_paper_proposals"] == 1
    assert result["trading_state_counts_before"] == result["trading_state_counts_after"]
    assert result["trading_state_mutations"] == 0


def test_schema_and_configuration_are_strict(tmp_path):
    storage = Storage(tmp_path / "schema.sqlite3")
    storage.initialize()
    columns = {row["name"] for row in storage.fetch_all("PRAGMA table_info(adaptive_conviction_operational_decisions)")}
    assert {"recommended_stop_risk_pct", "operational_stop_risk_pct", "raw_inputs_json", "operating_mode", "operational_enforced", "report_only"} <= columns
    config = load_config()
    config["adaptive_conviction"]["enforcement_enabled"] = False
    with pytest.raises(ConfigurationError):
        validate_config(config)


def test_operational_sizing_formula_does_not_reference_adaptive_conviction():
    sizing_source = inspect.getsource(TradingService._calculate_dynamic_size)
    final_validation_source = inspect.getsource(TradingService._execute_final_revalidation)
    assert "AdaptiveConviction" not in sizing_source
    assert "adaptive_conviction" not in sizing_source
    assert "AdaptiveConviction" not in final_validation_source
    assert "_record_adaptive_conviction" in final_validation_source
    assert "approved_notional" in final_validation_source
    assert "displayed_quantity" in final_validation_source
    assert "final_adaptive" in final_validation_source
