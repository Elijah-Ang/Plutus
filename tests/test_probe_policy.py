from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from app.configuration import ConfigurationError, validate_config
from app.execution import DurableExecutionStore
from app.formula_versions import ACCOUNTING_VERSION, EVIDENCE_VERSION, STRATEGY_POLICY_VERSION
from app.phase4_allocator import AdaptiveAllocator
from app.risk_engine import RiskEngine
from app.service import TradingService
from app.storage import Storage
from app.strategy_performance import PerformanceObservation, StrategyPerformanceEngine, state_risk_policy
from app.utils import format_proposal_message, load_config


def _db(tmp_path) -> Storage:
    storage = Storage(tmp_path / "probe.sqlite3")
    storage.initialize()
    return storage


def _observation(index: int, value: float = 0.5) -> PerformanceObservation:
    exit_session = datetime(2026, 3, 1, tzinfo=UTC) + timedelta(days=index)
    return PerformanceObservation(
        observation_id=f"probe-{index}", source_id=f"probe-source-{index}",
        strategy_version="rule_based_v2", symbol="SPY", evidence_class="shadow_oos",
        entry_session=(exit_session - timedelta(days=28)).isoformat(),
        exit_session=exit_session.isoformat(),
        regime="normal" if index % 2 else "favorable", r_multiple=value, gross_r=value + 0.05,
        evidence_version=EVIDENCE_VERSION, formula_version=ACCOUNTING_VERSION,
        attribution_confidence="shadow_deterministic",
    )


def _refresh_with(
    monkeypatch,
    storage,
    observations,
    quality=1.0,
    *,
    concentration=0.0,
    actual_observations=None,
    as_of=None,
):
    if as_of is None:
        all_rows = [*observations, *list(actual_observations or [])]
        as_of = (
            max(datetime.fromisoformat(row.exit_session) for row in all_rows)
            + timedelta(days=1)
        ).isoformat()
    engine = StrategyPerformanceEngine(storage, load_config(), as_of=as_of)
    monkeypatch.setattr(engine, "_shadow_observations", lambda: observations)
    monkeypatch.setattr(engine, "_actual_observations", lambda: list(actual_observations or []))
    monkeypatch.setattr(
        "app.strategy_performance.score_components",
        lambda metrics, settings: ({"profitability": quality}, quality, {"concentration": concentration, "divergence": 0.0}),
    )
    return engine.refresh_strategy("rule_based_v2"), engine


def test_probe_evidence_rule_and_transition_to_exploration(monkeypatch, tmp_path):
    storage = _db(tmp_path)
    probe, engine = _refresh_with(monkeypatch, storage, [_observation(i) for i in range(50)])
    assert probe.recommendation_state == "PROBE"
    policy = engine.latest_valid_policy("rule_based_v2")
    assert policy is not None
    assert probe.quality_score == 1.0
    assert policy.hard_gates["probe_evidence_eligible"] is True
    preliminary = probe.raw_inputs["probe_preliminary_evidence"]
    assert preliminary["inputs"]["shadow_oos_count"] == 50
    assert all(preliminary["results"].values())
    assert policy.maturity["probe"]["limits"] == {
        "stop_risk_pct": 0.03, "portfolio_heat_pct": 0.10, "gross_exposure_pct": 2.5,
        "max_active_count": 1, "minimum_setup_score": 85.0,
    }
    report = engine.format_report("rule_based_v2")
    assert "rule_based_v2: PROBE" in report
    assert "new entries only; no adds" in report

    exploration, _ = _refresh_with(monkeypatch, storage, [_observation(i) for i in range(100)], quality=50.0)
    assert exploration.recommendation_state == "EXPLORATION"


def test_incomplete_or_tampered_probe_policy_fails_closed(monkeypatch, tmp_path):
    storage = _db(tmp_path)
    _snapshot, engine = _refresh_with(monkeypatch, storage, [_observation(i) for i in range(50)])
    policy = engine.latest_valid_policy("rule_based_v2")
    assert policy is not None and policy.state == "PROBE"
    gates = dict(policy.hard_gates)
    gates["probe_preliminary_expectancy"] = False
    storage.execute("UPDATE strategy_policy_decisions SET hard_gates_json=? WHERE id=?", (json.dumps(gates), policy.id))
    assert engine.latest_valid_policy("rule_based_v2") is None
    assert engine.policy_by_id(policy.id) is None
    storage.execute("UPDATE strategy_policy_decisions SET hard_gates_json=? WHERE id=?", (json.dumps(policy.hard_gates), policy.id))
    snapshot_row = storage.fetch_all("SELECT raw_inputs_json FROM strategy_performance_snapshots WHERE id=?", (policy.performance_snapshot_id,))[0]
    raw_inputs = json.loads(snapshot_row["raw_inputs_json"])
    original_raw_inputs = json.dumps(raw_inputs)
    raw_inputs["probe_preliminary_evidence"]["inputs"]["expectancy_r"] = 999.0
    storage.execute(
        "UPDATE strategy_performance_snapshots SET raw_inputs_json=? WHERE id=?",
        (json.dumps(raw_inputs), policy.performance_snapshot_id),
    )
    assert engine.latest_valid_policy("rule_based_v2") is None
    storage.execute(
        "UPDATE strategy_performance_snapshots SET raw_inputs_json=? WHERE id=?",
        (original_raw_inputs, policy.performance_snapshot_id),
    )
    storage.execute(
        "UPDATE strategy_policy_decisions SET hard_gates_json=?,policy_version='strategy_policy_v2_1' WHERE id=?",
        (json.dumps(policy.hard_gates), policy.id),
    )
    assert engine.latest_valid_policy("rule_based_v2") is None


@pytest.mark.parametrize("count", [10, 99])
def test_probe_preliminary_gate_passes_at_10_and_99_without_quality_dependency(monkeypatch, tmp_path, count):
    snapshot, engine = _refresh_with(monkeypatch, _db(tmp_path), [_observation(i) for i in range(count)], quality=0.0)
    assert snapshot.quality_score == 0.0
    assert snapshot.recommendation_state == "PROBE"
    assert snapshot.raw_inputs["hard_gates"]["probe_evidence_eligible"] is True
    assert engine.latest_valid_policy("rule_based_v2") is not None


def test_probe_uses_real_preliminary_math_while_persisting_sub_85_quality(monkeypatch, tmp_path):
    storage = _db(tmp_path)
    values = [0.65, -0.30, 0.65, 0.65, -0.30] * 2
    observations = [dataclasses.replace(_observation(i, value), symbol=f"S{i}") for i, value in enumerate(values)]
    engine = StrategyPerformanceEngine(
        storage,
        load_config(),
        as_of=(datetime.fromisoformat(observations[-1].exit_session) + timedelta(days=1)).isoformat(),
    )
    monkeypatch.setattr(engine, "_shadow_observations", lambda: observations)
    monkeypatch.setattr(engine, "_actual_observations", lambda: [])
    snapshot = engine.refresh_strategy("rule_based_v2")
    assert snapshot.quality_score < 85.0
    assert snapshot.recommendation_state == "PROBE"
    preliminary = snapshot.raw_inputs["probe_preliminary_evidence"]
    assert preliminary["inputs"]["expectancy_r"] == pytest.approx(0.27)
    assert preliminary["inputs"]["profit_factor"] == pytest.approx(3.25)
    assert preliminary["inputs"]["concentration_penalty"] <= 5.0
    assert all(preliminary["results"].values())
    assert engine.latest_valid_policy("rule_based_v2") is not None


def test_probe_preliminary_gate_rejects_count_9_with_explicit_reason(monkeypatch, tmp_path):
    snapshot, _ = _refresh_with(monkeypatch, _db(tmp_path), [_observation(i) for i in range(9)], quality=100.0)
    assert snapshot.recommendation_state == "RESEARCH_ONLY"
    assert snapshot.raw_inputs["hard_gates"]["probe_preliminary_shadow_sample_range"] is False
    assert "shadow_sample_range" in snapshot.raw_inputs["maturity"]["binding_maturity_reason"]


def test_probe_preliminary_gate_exact_boundaries_pass():
    from app.strategy_performance import _probe_preliminary_results

    inputs = {
        "strategy_version": "rule_based_v2",
        "shadow_oos_count": 10,
        "minimum_shadow_oos_count": 10,
        "maximum_shadow_oos_count_exclusive": 100,
        "version_completeness": 1.0,
        "required_version_completeness": 1.0,
        "evidence_recency_days": 90.0,
        "maximum_evidence_recency_days": 90.0,
        "expectancy_r": 0.25,
        "minimum_expectancy_r": 0.25,
        "profit_factor": 1.75,
        "minimum_profit_factor": 1.75,
        "concentration_penalty": 5.0,
        "maximum_concentration_penalty": 5.0,
        "maximum_drawdown_r": 6.0,
        "maximum_allowed_drawdown_r": 6.0,
        "worst_losing_streak": 8,
        "maximum_allowed_losing_streak": 8,
        "actual_paper_count": 20,
        "divergence_gate_minimum_actual_paper_count": 20,
        "shadow_paper_expectancy_divergence_r": 1.5,
        "maximum_allowed_divergence_r": 1.5,
    }
    assert all(_probe_preliminary_results(inputs).values())


@pytest.mark.parametrize(
    ("case", "expected_gate"),
    [
        ("expectancy", "expectancy"),
        ("profit_factor", "profit_factor"),
        ("concentration", "concentration"),
        ("freshness", "evidence_fresh"),
        ("completeness", "version_complete"),
        ("drawdown", "drawdown"),
        ("streak", "losing_streak"),
        ("divergence", "divergence"),
    ],
)
def test_probe_preliminary_gate_failures_are_persisted_and_explicit(monkeypatch, tmp_path, case, expected_gate):
    count = 99 if case == "divergence" else 10
    observations = [_observation(i) for i in range(count)]
    actual = []
    kwargs = {"quality": 100.0}
    if case == "expectancy":
        observations = [_observation(i, 0.20) for i in range(count)]
    elif case == "profit_factor":
        values = [2.0, -2.0, 2.0, -2.0, 2.0, -2.0, 2.0, -2.0, 2.0, 2.0]
        observations = [_observation(i, value) for i, value in enumerate(values)]
    elif case == "concentration":
        kwargs["concentration"] = 5.01
    elif case == "freshness":
        kwargs["as_of"] = "2027-01-13T00:00:00+00:00"
    elif case == "completeness":
        observations[0] = dataclasses.replace(observations[0], evidence_version="old_evidence")
    elif case == "drawdown":
        values = [2.0] * 8 + [-4.0, -4.0]
        observations = [_observation(i, value) for i, value in enumerate(values)]
    elif case == "streak":
        values = [4.0] + [-0.1] * 9
        observations = [_observation(i, value) for i, value in enumerate(values)]
    elif case == "divergence":
        observations = [_observation(i, 2.0) for i in range(count)]
        actual = [dataclasses.replace(_observation(100 + i, 0.0), evidence_class="actual_paper") for i in range(20)]
        kwargs["actual_observations"] = actual
    snapshot, engine = _refresh_with(monkeypatch, _db(tmp_path), observations, **kwargs)
    results = snapshot.raw_inputs["probe_preliminary_evidence"]["results"]
    assert results[expected_gate] is False
    assert snapshot.raw_inputs["hard_gates"][f"probe_preliminary_{expected_gate}"] is False
    assert snapshot.recommendation_state == "RESEARCH_ONLY"
    assert expected_gate in snapshot.raw_inputs["maturity"]["binding_maturity_reason"]
    persisted = engine.latest_policy("rule_based_v2")
    assert persisted is not None
    assert expected_gate in persisted.reason


def test_probe_risk_mapping_is_entry_only_and_fixed():
    assert state_risk_policy("PROBE", initial_stop_risk_pct=0.20, add_stop_risk_pct=0.10, probe_stop_risk_pct=0.03, is_add=False)[:2] == (0.03, 0.15)
    assert state_risk_policy("PROBE", initial_stop_risk_pct=0.20, add_stop_risk_pct=0.10, probe_stop_risk_pct=0.03, is_add=True)[:2] == (0.0, 0.0)


def test_phase4_emits_distinct_probe_policy(monkeypatch, tmp_path):
    storage = _db(tmp_path)
    _snapshot, engine = _refresh_with(monkeypatch, storage, [_observation(i) for i in range(50)])
    policy = engine.latest_valid_policy("rule_based_v2")
    result = AdaptiveAllocator(storage, load_config(), "probe-allocation").run(
        regime="normal", drawdown_pct=0.0, strategy_policy_map={"rule_based_v2": policy},
        as_of="2026-07-14T08:00:00+00:00",
        portfolio_snapshot={"portfolio_equity": 100.0, "as_of": "2026-07-14T08:00:00+00:00", "equity_as_of": "2026-07-14T08:00:00+00:00"},
    )
    assert result["decision"] == "ALLOCATE_PROBE"
    assert result["allocation_class"] == "probe"
    assert result["probe_weights"] == {"rule_based_v2": 0.03}
    emitted = result["strategy_policies"]["rule_based_v2"]
    assert emitted["mode"] == "probe"
    assert emitted["entries_only"] is True and emitted["adds_allowed"] is False
    assert emitted["autonomous_execution_allowed"] is False
    row = storage.fetch_all("SELECT probe_allocation_json,binding_caps_json,strategy_policy_map_json FROM phase4_allocation_decisions WHERE id=?", (result["allocation_id"],))[0]
    assert json.loads(row["probe_allocation_json"]) == {"rule_based_v2": 0.03}
    assert json.loads(row["binding_caps_json"])["probe_max_active_count"] == 1
    assert json.loads(row["strategy_policy_map_json"])["rule_based_v2"]["state"] == "PROBE"
    state_row = storage.fetch_all("SELECT state,reason,state_version FROM phase4_strategy_states WHERE strategy_version='rule_based_v2'")[0]
    assert state_row["state"] == "PROBE"
    assert state_row["state_version"] == "phase4_strategy_state_v3_probe"


def test_probe_sizing_respects_score_stage_risk_and_active_count(monkeypatch, tmp_path):
    storage = _db(tmp_path)
    _snapshot, engine = _refresh_with(monkeypatch, storage, [_observation(i) for i in range(50)])
    policy = engine.latest_valid_policy("rule_based_v2")
    service = TradingService(load_config(), storage, None, "probe-sizing")
    service._strategy_policy_map = {"rule_based_v2": policy}
    service._authoritative_runtime_state = lambda force=False: {
        "positions": [], "account": {"equity": 100000, "cash": 90000, "buying_power": 360000},
        "loss_metrics": {"daily_loss_dollars": 0, "weekly_loss_dollars": 0, "daily_loss_confidence": "verified", "weekly_loss_confidence": "verified"},
    }
    bars = pd.DataFrame({"high": [101.0] * 250, "low": [99.0] * 250, "close": [100.0] * 250, "volume": [200000.0] * 250})
    account = {"portfolio_equity": 100000, "cash": 90000, "buying_power": 360000, "total_exposure_dollars": 0, "single_exposures": {}, "cluster_exposures": {}}
    weak = service._calculate_dynamic_size("SPY", 84.99, "normal", 100, bars, account, strategy_version="rule_based_v2")
    assert weak["final_notional"] == 0 and "score" in weak["blocked_reason"]
    probe = service._calculate_dynamic_size("SPY", 85, "normal", 100, bars, account, strategy_version="rule_based_v2")
    assert probe["phase4_mode"] == "probe"
    assert probe["permitted_stop_risk_pct"] == 0.03
    assert probe["final_notional"] > 250.0
    assert probe["stop_risk_dollars"] <= 30.0 + 1e-9
    assert probe["score_multiplier"] == 1.0
    add = service._calculate_dynamic_size("SPY", 99, "normal", 100, bars, account, is_add=True, strategy_version="rule_based_v2")
    assert add["final_notional"] == 0 and "adds are blocked" in add["blocked_reason"]

    storage.execute(
        "INSERT INTO trade_proposals(id,symbol,side,status,strategy_version,strategy_state,payload) VALUES(?,?,?,?,?,?,?)",
        ("existing-probe", "SPY", "buy", "filled", "rule_based_v2", "PROBE", "{}"),
    )
    storage.execute(
        """INSERT INTO position_lots(id,symbol,source_fill_event_key,opened_at,original_quantity,remaining_quantity,
           unit_cost,source,provenance,confidence,created_at,updated_at,strategy_version,entry_proposal_id,initial_risk_dollars)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("probe-lot", "SPY", "probe-fill", "2026-07-13T00:00:00+00:00", 1, 1, 100, "test", "test", "verified", "2026-07-13T00:00:00+00:00", "2026-07-13T00:00:00+00:00", "rule_based_v2", "existing-probe", 30),
    )
    blocked = service._calculate_dynamic_size("QQQ", 90, "normal", 100, bars, account, strategy_version="rule_based_v2")
    assert blocked["final_notional"] == 0 and "active position" in blocked["blocked_reason"]


def test_probe_configuration_is_strict():
    config = load_config()
    assert validate_config(config) == []
    assert "score_probe_threshold" not in config["profitability_engine"]
    config["phase4"]["probe_stop_risk_pct"] = 0.031
    with pytest.raises(ConfigurationError, match="probe stop risk"):
        validate_config(config)
    config = load_config()
    config["profitability_engine"]["probe_min_shadow_oos_samples"] = 9
    with pytest.raises(ConfigurationError, match="probe_min_shadow_oos_samples"):
        validate_config(config)


def test_final_risk_revalidation_repeats_probe_gates(safe_config, proposal, context):
    safe_config.update({
        "auto_execution_enabled": False,
        "phase3": {"risk_profile": {"minimum_average_dollar_volume": 10_000_000}},
        "phase4": {
            "require_manual_approval": True, "probe_min_setup_score": 85,
            "probe_max_active_count": 1, "probe_portfolio_heat_pct": 0.10,
            "probe_gross_exposure_pct": 2.5,
        },
    })
    proposal.update({
        "phase4_mode": "probe", "strategy_state": "PROBE", "strategy_policy_version": STRATEGY_POLICY_VERSION,
        "score": 85, "score_multiplier": 1.0, "average_dollar_volume": 20_000_000,
        "client_order_id": "probe-final", "notional": 250.0,
    })
    context.update({
        "approval_valid": True, "final_revalidation": True, "portfolio_equity": 100_000.0,
        "probe_projected_count": 1, "probe_projected_stop_risk": 30.0,
        "probe_projected_gross_notional": 250.0,
    })
    checks = {check.name: check.passed for check in RiskEngine(safe_config).evaluate(proposal, context, final=True).checks}
    assert all(checks[name] for name in checks if name.startswith("phase4_probe_"))

    proposal["score"] = 84.99
    context["probe_projected_count"] = 2
    failed = {check.name: check.passed for check in RiskEngine(safe_config).evaluate(proposal, context, final=True).checks}
    assert failed["phase4_probe_setup_score"] is False
    assert failed["phase4_probe_active_count"] is False

    message = format_proposal_message(proposal, load_config())
    assert "Strategy policy: PROBE" in message
    assert "PROBE controls: new entry only; no adds" in message


def test_probe_slot_limit_is_atomic_at_durable_reservation(tmp_path):
    storage = _db(tmp_path)
    now = datetime.now(UTC)
    limits = {
        "probe_max_active_count": 1,
        "probe_gross_notional_ceiling": 2_500.0,
        "probe_stop_risk_ceiling": 100.0,
    }

    def candidate(identifier: str, symbol: str) -> dict:
        storage.execute(
            "INSERT INTO trade_proposals(id,symbol,side,status,strategy_version,strategy_state,payload) VALUES(?,?,?,?,?,?,?)",
            (identifier, symbol, "buy", "approved", "rule_based_v2", "PROBE", "{}"),
        )
        return {
            "id": identifier, "proposal_id": identifier, "symbol": symbol, "side": "buy", "action": "entry",
            "notional": 250.0, "latest_price": 100.0, "stop_price": 99.0, "trading_mode": "paper",
            "expires_at": (now + timedelta(minutes=5)).isoformat(), "phase4_mode": "probe",
            "strategy_version": "rule_based_v2", "_reservation_limits": limits,
        }

    store = DurableExecutionStore(storage)
    first = store.create_or_get_intent(candidate("probe-one", "SPY"), run_id="probe", source_type="proposal")
    assert first["state"] == "reserved"
    with pytest.raises(RuntimeError, match="PROBE active-count ceiling"):
        store.create_or_get_intent(candidate("probe-two", "QQQ"), run_id="probe", source_type="proposal")
