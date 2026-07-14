from __future__ import annotations

import inspect
import json
from datetime import UTC, datetime, timedelta

import pytest

from app.adaptive_sizing import (
    AdaptiveSizingEngine,
    CANONICAL_CEILING_ORDER,
    evidence_report,
)
from app.configuration import ConfigurationError, validate_config
from app.execution import DurableExecutionStore
from app.service import TradingService
from app.storage import Storage
from app.utils import format_proposal_message, load_config
from scripts.trace_adaptive_operational_proposal import build_trace
from scripts.adaptive_sizing_evidence import build_report


def _engine() -> AdaptiveSizingEngine:
    return AdaptiveSizingEngine(load_config())


def _inputs(**overrides):
    ceilings = {name: 1_000_000.0 for name in CANONICAL_CEILING_ORDER}
    values = {
        "stage": "proposal", "run_id": "run-1", "proposal_id": "proposal-1", "candidate_id": "candidate-1",
        "setup_id": "setup-1", "approval_id": None, "strategy_version": "rule_based_v2", "policy_id": "policy-1",
        "action": "entry", "side": "buy", "authoritative_equity": 100_000.0, "authoritative_cash": 50_000.0,
        "authoritative_buying_power": 50_000.0, "entry_price": 100.0, "stop_price": 95.0, "stop_distance_dollars": 5.0,
        "adaptive_conviction": {
            "decision_id": "conviction-1", "recommended_stop_risk_pct": 0.20, "confidence": 0.90,
            "missing_inputs": [], "portfolio_heat_target_pct": 1.75, "gross_exposure_target_pct": 50.0,
        },
        "operational_sizing": {
            "score_adjusted_notional": 90.0, "final_notional": 80.0, "suggested_shares": 0.8,
            "stop_risk_dollars": 4.0, "minimum_executable_notional": 1.0, "sizing_caps": ceilings,
            "blocked_reason": None,
        },
        "current_portfolio_heat_pct": 0.20, "current_gross_exposure_pct": 10.0,
        "current_symbol_exposure_pct": 1.0, "current_cluster_exposure_pct": 2.0,
        "active_reservations": {"notional": 0.0, "stop_risk": 0.0},
        "integrity": {"pending_buy_exposure_unknown": False, "reconciliation_checked": True},
        "hard_limits_pct": {"portfolio_heat": 1.25, "gross_exposure": 50.0, "symbol_exposure": 6.0, "cluster_exposure": 15.0},
    }
    values.update(overrides)
    return values


@pytest.mark.parametrize(
    ("direction", "operational", "cap", "risk_pct"),
    [("INCREASE", 80.0, 100.0, 0.20), ("UNCHANGED", 100.0, 100.0, 0.20),
     ("REDUCE", 120.0, 100.0, 0.20), ("REJECT", 80.0, 100.0, 0.0)],
)
def test_all_four_comparison_directions(direction, operational, cap, risk_pct):
    inputs = _inputs()
    inputs["adaptive_conviction"]["recommended_stop_risk_pct"] = risk_pct
    inputs["operational_sizing"]["final_notional"] = operational
    inputs["operational_sizing"]["suggested_shares"] = operational / 100.0
    inputs["operational_sizing"]["sizing_caps"]["cash"] = cap
    decision = _engine().evaluate(inputs)
    assert decision is not None
    assert decision.comparison_direction == direction


def test_stop_risk_to_notional_conversion_reuses_canonical_helper():
    decision = _engine().evaluate(_inputs())
    assert decision is not None
    assert decision.conviction_stop_risk_dollars == 200.0
    assert decision.adaptive_requested_notional == 4_000.0
    assert decision.adaptive_constrained_stop_risk_dollars == 200.0


def test_every_operational_adaptive_ceiling_can_bind():
    base = _engine().evaluate(_inputs())
    assert base is not None
    canonical = [name for name in base.ceilings if name not in {"deployment_mode_heat", "deployment_mode_gross"}]
    for cap_name in canonical:
        inputs = _inputs()
        inputs["operational_sizing"]["sizing_caps"][cap_name] = 50.0
        decision = _engine().evaluate(inputs)
        assert decision is not None
        assert decision.adaptive_constrained_notional == 50.0, cap_name
        assert decision.binding_adaptive_cap == cap_name
    heat = _inputs()
    heat["current_portfolio_heat_pct"] = 0.0
    heat["adaptive_conviction"]["portfolio_heat_target_pct"] = 0.0025
    assert _engine().evaluate(heat).binding_adaptive_cap == "deployment_mode_heat"
    gross = _inputs()
    gross["current_gross_exposure_pct"] = 0.0
    gross["adaptive_conviction"]["gross_exposure_target_pct"] = 0.05
    assert _engine().evaluate(gross).binding_adaptive_cap == "deployment_mode_gross"


def test_fixed_stage_caps_are_not_operational_ceilings():
    inputs = _inputs()
    inputs["operational_sizing"]["sizing_caps"]["stage"] = 100.0
    decision = _engine().evaluate(inputs)
    assert decision is not None
    assert "stage" not in decision.ceilings
    assert decision.adaptive_constrained_notional > 250.0


def test_active_conviction_can_expand_above_old_baseline_stop_risk_to_hard_envelope():
    inputs = _inputs(strategy_policy_state="ACTIVE")
    inputs["adaptive_conviction"]["recommended_stop_risk_pct"] = 0.35
    inputs["operational_sizing"]["sizing_caps"]["stop_risk"] = 4_000.0
    decision = _engine().evaluate(inputs)
    assert decision is not None
    assert decision.adaptive_constrained_notional == 7_000.0
    assert decision.adaptive_constrained_stop_risk_pct == 0.35
    assert decision.adaptive_constrained_notional > inputs["operational_sizing"]["sizing_caps"]["stop_risk"]


def test_stop_risk_percentage_dollars_quantity_and_notional_units_at_extremes():
    inputs = _inputs(strategy_policy_state="ACTIVE")
    inputs["adaptive_conviction"]["recommended_stop_risk_pct"] = 0.35
    inputs["operational_sizing"]["sizing_caps"] = {"cash": 1_000_000.0, "buying_power": 1_000_000.0}
    decision = _engine().evaluate(inputs)
    assert decision is not None
    assert decision.conviction_stop_risk_dollars == 350.0
    assert decision.adaptive_requested_notional == 7_000.0
    assert decision.adaptive_quantity == 70.0
    assert decision.adaptive_constrained_stop_risk_pct == 0.35


@pytest.mark.parametrize(
    ("recomputed", "blocked", "outcome", "future"),
    [(100.0, False, "STAYED_EQUAL", 100.0), (60.0, False, "REDUCED", 60.0),
     (0.0, True, "BECAME_BLOCKED", 0.0), (140.0, False, "INCREASE_CONSTRAINED_BY_DISPLAYED_CEILING", 100.0)],
)
def test_final_handoff_is_one_way_and_records_drift(recomputed, blocked, outcome, future):
    inputs = _inputs(stage="final_revalidation", approval_id="approval-1", displayed_adaptive_ceiling=100.0,
                     proposal_adaptive_notional=100.0, final_revalidation_blocked=blocked)
    inputs["operational_sizing"]["sizing_caps"]["cash"] = recomputed
    if recomputed == 0:
        inputs["operational_sizing"]["blocked_reason"] = "current authoritative ceiling is zero"
    decision = _engine().evaluate(inputs)
    assert decision is not None
    assert decision.final_revalidation_outcome == outcome
    assert decision.future_activation_notional == future
    assert decision.future_activation_notional <= decision.displayed_adaptive_ceiling
    assert decision.proposal_to_approval_drift_dollars == recomputed - 100.0


def test_final_quantity_and_stop_risk_cannot_exceed_displayed_approval():
    inputs = _inputs(
        stage="final_revalidation", approval_id="approval-1",
        displayed_adaptive_ceiling=10_000.0, proposal_adaptive_notional=4_000.0,
        displayed_quantity_ceiling=12.0, displayed_stop_risk_dollars=50.0,
    )
    decision = _engine().evaluate(inputs)
    assert decision is not None
    assert decision.adaptive_constrained_notional == 1_000.0
    assert decision.final_operational_quantity == 10.0
    assert decision.adaptive_constrained_stop_risk_dollars == 50.0
    assert decision.binding_adaptive_cap == "displayed_stop_risk"


def test_missing_and_degraded_inputs_reject_without_exception():
    decision = _engine().evaluate(_inputs(authoritative_equity=None, entry_price=None, stop_distance_dollars=None))
    assert decision is not None
    assert decision.comparison_direction == "REJECT"
    assert {"authoritative_equity", "entry_price", "stop_distance_dollars"} <= set(decision.missing_inputs)
    assert decision.confidence < 0.90


def test_add_is_compared_without_enabling_new_add_behavior_and_exits_bypass():
    add = _engine().evaluate(_inputs(action="add"))
    assert add is not None and add.action == "add" and add.report_only is False
    assert _engine().evaluate(_inputs(action="exit", side="sell")) is None
    source = inspect.getsource(TradingService._record_adaptive_sizing)
    assert "Executor" not in source and "order_intents" not in source and "risk_reservations" not in source


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("enabled", False), ("mode", "shadow_only"), ("operational_enforcement", False),
        ("allow_order_size_change", False), ("formula_version", "unsupported"),
        ("schema_version", "unsupported"),
    ],
)
def test_configuration_strictly_rejects_non_operational_modes(key, value):
    config = load_config()
    config["adaptive_sizing"][key] = value
    with pytest.raises(ConfigurationError):
        validate_config(config)
    with pytest.raises(ValueError):
        AdaptiveSizingEngine(config)


def test_probe_caps_remain_unchanged_and_fixed_stage_caps_are_disabled():
    config = load_config()
    assert config["phase4"]["probe_stop_risk_pct"] == 0.03
    assert config["phase4"]["probe_portfolio_heat_pct"] == 0.10
    assert config["phase4"]["probe_gross_exposure_pct"] == 2.5
    assert config["position_sizing"]["use_stage_dollar_cap"] is False
    assert config["position_sizing"]["stage"] == "adaptive_operational_paper"
    sizing_source = inspect.getsource(TradingService._calculate_dynamic_size)
    assert "AdaptiveSizing" not in sizing_source and "adaptive_sizing" not in sizing_source


def test_deterministic_persistence_reporting_and_no_trading_state_mutation(tmp_path):
    storage = Storage(tmp_path / "adaptive-sizing.sqlite3")
    storage.initialize()
    engine = _engine()
    first = engine.evaluate(_inputs())
    second = engine.evaluate(_inputs())
    assert first is not None and second is not None
    assert first.decision_fingerprint == second.decision_fingerprint
    tables = ("trade_proposals", "approvals", "risk_reservations", "order_intents", "orders", "fills")
    before = {table: storage.fetch_all(f"SELECT COUNT(*) n FROM {table}")[0]["n"] for table in tables}
    engine.persist(storage, first)
    engine.persist(storage, second)
    assert storage.fetch_all("SELECT COUNT(*) n FROM adaptive_sizing_operational_decisions")[0]["n"] == 1
    report = build_report(storage.path)
    after = {table: storage.fetch_all(f"SELECT COUNT(*) n FROM {table}")[0]["n"] for table in before}
    assert before == after
    assert report["trading_state_mutations"] == 0
    assert report["total_decisions"] == 1
    assert "operational paper" in engine.format_report(storage)


def test_service_persistence_hook_does_not_mutate_operational_proposal(tmp_path):
    storage = Storage(tmp_path / "service-hook.sqlite3")
    storage.initialize()
    service = object.__new__(TradingService)
    service.config = load_config()
    service.storage = storage
    service.run_id = "run-1"
    proposal = {
        "id": "proposal-1", "run_id": "run-1", "signal_id": "candidate-1", "setup_key": "setup-1",
        "strategy_version": "rule_based_v2", "policy_decision_id": "policy-1", "side": "buy", "action": "entry",
        "notional": 80.0, "qty": 0.8, "latest_price": 100.0, "stop_price": 95.0, "stop_distance_dollars": 5.0,
        "strategy_state": "ACTIVE", "strategy_policy_version": "strategy_policy_v2_2_probe",
    }
    sizing = _inputs()["operational_sizing"] | {"stop_distance_dollars": 5.0, "stop_price": 95.0, "strategy_state": "ACTIVE"}
    portfolio = {
        "portfolio_equity": 100_000.0, "cash": 50_000.0, "buying_power": 50_000.0,
        "proposed_total_exposure_pct": 10.08, "proposed_symbol_exposure_pct": 1.08,
        "proposed_cluster_exposure_pct": 2.08, "held_open_stop_risk": 200.0,
        "active_reserved_stop_risk": 0.0, "pending_buy_stop_risk": 0.0,
        "active_reserved_exposure": 0.0, "pending_buy_notional": 0.0,
        "pending_buy_exposure_unknown": False,
    }
    before = dict(proposal)
    summary = service._record_adaptive_sizing(
        proposal, sizing, portfolio,
        {"decision_id": "conviction-1", "recommended_stop_risk_pct": 0.20, "confidence": 0.9, "missing_inputs": [], "portfolio_heat_target_pct": 1.75, "gross_exposure_target_pct": 50.0},
        stage="proposal",
    )
    assert summary is not None
    assert proposal == before
    assert storage.fetch_all("SELECT COUNT(*) n FROM adaptive_sizing_operational_decisions")[0]["n"] == 1
    assert storage.fetch_all("SELECT COUNT(*) n FROM risk_reservations")[0]["n"] == 0
    assert storage.fetch_all("SELECT COUNT(*) n FROM order_intents")[0]["n"] == 0
    assert storage.fetch_all("SELECT COUNT(*) n FROM orders")[0]["n"] == 0


def test_durable_reservation_uses_exact_adaptive_operational_size_and_risk(tmp_path):
    storage = Storage(tmp_path / "adaptive-reservation.sqlite3")
    storage.initialize()
    decision = _engine().evaluate(_inputs(strategy_policy_state="ACTIVE"))
    assert decision is not None
    proposal = {
        "id": "proposal-adaptive", "run_id": "run-1", "symbol": "SPY",
        "side": "buy", "action": "entry", "mode": "paper",
        "notional": decision.final_operational_notional,
        "qty": decision.final_operational_quantity,
        "approved_notional_ceiling": decision.displayed_adaptive_ceiling,
        "approved_quantity_ceiling": decision.final_operational_quantity,
        "latest_price": 100.0, "stop_price": 95.0,
        "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
    }
    intent = DurableExecutionStore(storage).create_or_get_intent(
        proposal, run_id="run-1", source_type="telegram_approval",
    )
    reservation = storage.fetch_all("SELECT * FROM risk_reservations WHERE intent_id=?", (intent["id"],))[0]
    assert reservation["initial_notional"] == decision.final_operational_notional
    assert reservation["initial_stop_risk"] == decision.final_operational_quantity * 5.0
    assert intent["requested_quantity"] <= intent["approved_quantity_ceiling"]
    assert intent["requested_notional"] <= intent["approved_notional_ceiling"]


def test_final_revalidation_capacity_removes_displayed_proposal_not_stale_baseline(tmp_path):
    storage = Storage(tmp_path / "adaptive-final-capacity.sqlite3")
    storage.initialize()
    service = object.__new__(TradingService)
    service.config = load_config()
    service.storage = storage
    service.run_id = "run-final"
    proposal = {
        "id": "proposal-final", "run_id": "run-final", "signal_id": "candidate-1",
        "setup_key": "setup-1", "strategy_version": "rule_based_v2", "side": "buy",
        "action": "entry", "notional": 4_000.0, "qty": 40.0, "approved_quantity_ceiling": 40.0,
        "approved_stop_risk_ceiling": 200.0, "latest_price": 100.0,
        "stop_price": 95.0, "stop_distance_dollars": 5.0, "strategy_state": "ACTIVE",
    }
    sizing = _inputs()["operational_sizing"] | {
        "final_notional": 2_000.0, "suggested_shares": 20.0,
        "stop_distance_dollars": 5.0, "stop_price": 95.0, "strategy_state": "ACTIVE",
    }
    portfolio = {
        "portfolio_equity": 100_000.0, "cash": 50_000.0, "buying_power": 50_000.0,
        "proposed_total_exposure_pct": 14.0, "proposed_symbol_exposure_pct": 5.0,
        "proposed_cluster_exposure_pct": 6.0, "held_open_stop_risk": 100.0,
        "active_reserved_stop_risk": 0.0, "pending_buy_stop_risk": 0.0,
        "active_reserved_exposure": 0.0, "pending_buy_notional": 0.0,
        "pending_buy_exposure_unknown": False,
    }
    summary = service._record_adaptive_sizing(
        proposal, sizing, portfolio,
        {"decision_id": "conviction-final", "recommended_stop_risk_pct": .20,
         "confidence": .9, "missing_inputs": [], "portfolio_heat_target_pct": 1.25,
         "gross_exposure_target_pct": 30.0},
        stage="final_revalidation", approval_id="approval-final",
        displayed_adaptive_ceiling=4_000.0, proposal_adaptive_notional=4_000.0,
    )
    assert summary is not None
    row = storage.fetch_all("SELECT raw_inputs_json FROM adaptive_sizing_operational_decisions")[0]
    raw = json.loads(row["raw_inputs_json"])
    assert raw["current_gross_exposure_pct"] == 10.0
    assert raw["current_symbol_exposure_pct"] == 1.0
    assert raw["current_cluster_exposure_pct"] == 2.0


def test_schema_contains_versions_raw_inputs_and_one_way_operational_contract(tmp_path):
    storage = Storage(tmp_path / "schema.sqlite3")
    storage.initialize()
    columns = {row["name"] for row in storage.fetch_all("PRAGMA table_info(adaptive_sizing_operational_decisions)")}
    assert {
        "stage", "adaptive_conviction_decision_id", "operational_constrained_notional", "adaptive_constrained_notional",
        "displayed_adaptive_ceiling", "future_activation_notional", "final_revalidation_outcome", "ceilings_json",
        "raw_inputs_json", "evidence_version", "formula_version", "schema_version", "configuration_version",
        "decision_fingerprint", "operating_mode", "operational_enforced", "final_operational_notional", "final_operational_quantity", "report_only",
    } <= columns


def test_proposal_message_labels_operational_adaptive_size():
    proposal = {
        "symbol": "SPY", "side": "buy", "action": "entry", "notional": 80.0, "qty": 0.8,
        "latest_price": 100.0, "score": 90.0, "reason": "trend passed", "expires_at": "2026-07-14T01:00:00+00:00",
        "strategy_state": "ACTIVE", "stop_price": 95.0, "stop_distance_pct": 5.0,
        "stop_distance_dollars": 5.0, "stop_risk_dollars": 4.0,
        "adaptive_conviction": {"deployment_mode": "OPPORTUNISTIC", "opportunity_class": "HIGH_CONVICTION",
                                "recommended_stop_risk_pct": 0.30, "portfolio_heat_target_pct": 1.50,
                                "gross_exposure_target_pct": 40.0, "binding_cap": "portfolio_heat"},
        "adaptive_sizing": {"operational_notional": 80.0, "operational_quantity": 0.8, "adaptive_notional": 100.0,
                            "adaptive_quantity": 1.0, "stop_risk_dollars": 4.0, "comparison_direction": "INCREASE",
                            "binding_adaptive_cap": "portfolio", "reason": "strong evidence with remaining capacity"},
    }
    message = format_proposal_message(proposal, load_config())
    assert "Strategy authorization: ACTIVE" in message
    assert "OPPORTUNISTIC/HIGH_CONVICTION" in message
    assert "Adaptive Sizing (operational paper)" in message
    assert "actual proposed $80.00" in message
    assert "stop risk 0.3000% ($4.00)" in message
    assert "Stop price: $95.00" in message
    assert "binding portfolio" in message
    assert "Reason: strong evidence with remaining capacity" in message
    assert "maximum that approval can submit" in message
    assert "report-only" not in message and "shadow" not in message and "future" not in message


def test_evidence_report_uses_persisted_rows_only(tmp_path):
    storage = Storage(tmp_path / "evidence.sqlite3")
    storage.initialize()
    decision = _engine().evaluate(_inputs())
    assert decision is not None
    _engine().persist(storage, decision)
    with storage.connect() as conn:
        report = evidence_report(conn)
    assert report["complete_counts"] == {"proposal": 1}
    assert report["comparison_directions"] == {"INCREASE": 1}
    assert report["trading_state_mutations"] == 0


def test_first_natural_proposal_trace_is_read_only_while_pending(tmp_path):
    storage = Storage(tmp_path / "trace.sqlite3")
    storage.initialize()
    report = build_trace(storage.path, after="2026-07-14T00:00:00+00:00")
    assert report["status"] == "pending_first_natural_operational_buy_or_add"
    assert report["trading_state_mutations"] == 0
    assert report["trading_state_counts_before"] == report["trading_state_counts_after"]
