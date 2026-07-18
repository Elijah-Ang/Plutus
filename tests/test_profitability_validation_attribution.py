from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.formula_versions import (
    ACCOUNTING_VERSION,
    CONFIGURATION_SCHEMA_VERSION,
    EVIDENCE_VERSION,
    PROFITABILITY_VALIDATION_FORMULA_VERSION,
)
from app.execution import DurableExecutionStore
from app.configuration import ConfigurationError, validate_config
from app.profit_attribution import (
    AttributionLeg,
    ProfitAttributionError,
    ProfitAttributionInput,
    ProfitAttributionEngine,
    ProfitAttributionStore,
    calculate_profit_attribution,
)
from app.lot_ledger import LotLedger
from app.profitability_validation import (
    ProfitabilityValidationError,
    ProfitabilityValidationPolicy,
    ProfitabilityValidationStore,
    ValidationHypothesis,
    ValidationObservation,
    benjamini_hochberg,
    purged_walk_forward_folds,
    validate_profitability_family,
)
from app.storage import Storage
from app.reports import SHEETS
from app.strategy_performance import (
    PerformanceObservation,
    StrategyPerformanceEngine,
)
from app.utils import load_config


def _observation(
    index: int,
    value: str = "0.25",
    *,
    hypothesis: str = "strategy-a",
    horizon_days: int = 5,
) -> ValidationObservation:
    observed = datetime(2025, 1, 1, tzinfo=UTC) + timedelta(days=index)
    return ValidationObservation(
        id=f"{hypothesis}-{index}",
        hypothesis_id=hypothesis,
        strategy_version=hypothesis,
        observed_at=observed.isoformat(),
        outcome_end_at=(observed + timedelta(days=horizon_days)).isoformat(),
        net_r=value,
        evidence_class="shadow_oos",
        source_id=f"source-{hypothesis}-{index}",
    )


def _family(
    *,
    hypotheses: tuple[ValidationHypothesis, ...] | None = None,
    observations: tuple[ValidationObservation, ...] | None = None,
):
    hypotheses = hypotheses or (
        ValidationHypothesis("strategy-a", "strategy-a", "strategy-a"),
        ValidationHypothesis("strategy-b", "strategy-b", "strategy-b"),
        ValidationHypothesis("strategy-c", "strategy-c", "strategy-c"),
    )
    observations = observations or tuple(
        [
            *(_observation(i, "0.25", hypothesis="strategy-a") for i in range(70)),
            *(_observation(i, "0.15", hypothesis="strategy-b") for i in range(70)),
            *(_observation(i, "0.10", hypothesis="strategy-c") for i in range(20)),
        ]
    )
    return validate_profitability_family(
        family_key="configured-strategies",
        as_of="2026-01-01T00:00:00+00:00",
        hypotheses=hypotheses,
        observations=observations,
        policy=ProfitabilityValidationPolicy(
            minimum_samples=50,
            minimum_folds=2,
            minimum_train_observations=30,
            test_observations=10,
            embargo_periods=2,
            block_length=5,
            bootstrap_draws=200,
        ),
        configuration_version=CONFIGURATION_SCHEMA_VERSION,
        config_hash="config-hash",
        formula_versions={
            "profitability_validation": PROFITABILITY_VALIDATION_FORMULA_VERSION
        },
    )


def test_purged_walk_forward_removes_overlapping_labels_and_embargoes_groups():
    observations = tuple(_observation(i, horizon_days=8) for i in range(80))
    folds = purged_walk_forward_folds(
        observations,
        minimum_train_observations=20,
        test_observations=10,
        embargo_periods=2,
    )
    assert len(folds) >= 3
    by_id = {row.id: row.canonical() for row in observations}
    for fold in folds:
        test_start = datetime.fromisoformat(fold.test_start)
        assert fold.embargo_group_count == 2
        assert fold.purged_train_count > 0
        assert set(fold.train_ids).isdisjoint(fold.test_ids)
        assert all(
            datetime.fromisoformat(by_id[row_id]["outcome_end_at"]) < test_start
            for row_id in fold.train_ids
        )


def test_benjamini_hochberg_is_monotone_and_keeps_full_family():
    q_values, accepted = benjamini_hochberg(
        {"a": "0.01", "b": "0.04", "c": "0.03", "d": "0.20"},
        alpha="0.05",
    )
    assert accepted == {"a": True, "c": False, "b": False, "d": False}
    ordered = [
        q_values[key]
        for key in ("a", "c", "b", "d")
    ]
    assert ordered == sorted(ordered)


def test_family_validation_is_deterministic_and_insufficient_member_stays_in_fdr():
    first = _family()
    second = _family()
    assert first.family_fingerprint == second.family_fingerprint
    decisions = {row.hypothesis_id: row for row in first.decisions}
    assert decisions["strategy-a"].status == "validated"
    assert decisions["strategy-b"].status == "validated"
    assert decisions["strategy-c"].status == "insufficient"
    assert all(row.metrics["family_size"] == 3 for row in first.decisions)
    assert decisions["strategy-c"].bootstrap_p_value == "1"
    assert decisions["strategy-c"].fdr_accepted is False


def test_negative_mature_hypothesis_fails_closed():
    family = _family(
        hypotheses=(
            ValidationHypothesis("strategy-a", "strategy-a", "strategy-a"),
        ),
        observations=tuple(
            _observation(i, "-0.20", hypothesis="strategy-a")
            for i in range(70)
        ),
    )
    decision = family.decisions[0]
    assert decision.status == "failed"
    assert Decimal(decision.bootstrap_lower_net_r or "0") < 0
    assert decision.fdr_accepted is False


def test_parameter_neighborhood_instability_fails_narrow_variant():
    hypotheses = tuple(
        ValidationHypothesis(name, name, "trend-neighborhood")
        for name in ("trend-low", "trend-base", "trend-high")
    )
    observations = tuple(
        [
            *(
                _observation(i, "0.30", hypothesis="trend-base")
                for i in range(70)
            ),
            *(
                _observation(i, "-0.10", hypothesis="trend-low")
                for i in range(70)
            ),
            *(
                _observation(i, "-0.10", hypothesis="trend-high")
                for i in range(70)
            ),
        ]
    )
    family = _family(hypotheses=hypotheses, observations=observations)
    base = next(
        row for row in family.decisions if row.hypothesis_id == "trend-base"
    )
    assert base.status == "failed"
    assert base.parameter_stability_status == "failed_narrow_parameter_support"
    assert Decimal(base.parameter_stability_ratio) == Decimal("1") / Decimal("3")


def test_future_outcome_and_counterfactual_evidence_fail_closed():
    future = _observation(0)
    future = ValidationObservation(
        **{
            **future.__dict__,
            "outcome_end_at": "2027-01-01T00:00:00+00:00",
        }
    )
    with pytest.raises(ProfitabilityValidationError, match="future outcome"):
        _family(
            hypotheses=(
                ValidationHypothesis("strategy-a", "strategy-a", "strategy-a"),
            ),
            observations=(future,),
        )
    counterfactual = ValidationObservation(
        **{
            **_observation(0).__dict__,
            "evidence_class": "counterfactual",
        }
    )
    with pytest.raises(
        ProfitabilityValidationError, match="shadow_oos or actual_paper"
    ):
        counterfactual.canonical()


def test_validation_store_recomputes_and_detects_tampering(tmp_path):
    storage = Storage(tmp_path / "validation.db")
    storage.initialize()
    storage.apply_explicit_migrations()
    family = _family()
    store = ProfitabilityValidationStore(storage)
    store.persist(family)
    assert store.load_verified(family.id).family_fingerprint == family.family_fingerprint
    storage.execute(
        """UPDATE profitability_validation_decisions
           SET bootstrap_lower_net_r='999' WHERE family_id=?""",
        (family.id,),
    )
    with pytest.raises(
        ProfitabilityValidationError,
        match="bootstrap_lower_net_r",
    ):
        store.load_verified(family.id)


def test_validation_store_detects_persisted_fold_tampering(tmp_path):
    storage = Storage(tmp_path / "validation-fold.db")
    storage.initialize()
    storage.apply_explicit_migrations()
    family = _family()
    store = ProfitabilityValidationStore(storage)
    store.persist(family)
    storage.execute(
        """UPDATE profitability_validation_folds SET train_ids_json='[]'
           WHERE id=(SELECT id FROM profitability_validation_folds LIMIT 1)"""
    )
    with pytest.raises(
        ProfitabilityValidationError,
        match="validation fold column is inconsistent: train_ids_json",
    ):
        store.load_verified(family.id)


def test_validation_configuration_rejects_impossible_block_length():
    config = copy.deepcopy(load_config())
    config["profitability_validation"]["block_length"] = 51
    with pytest.raises(
        ConfigurationError,
        match="block_length cannot exceed minimum_samples",
    ):
        validate_config(config)


def _chronological_strategy_observations(
    value: float,
) -> list[PerformanceObservation]:
    rows = []
    start = datetime(2026, 1, 1, tzinfo=UTC)
    for index in range(100):
        entry = start + timedelta(days=index)
        exit_at = entry + timedelta(days=1)
        rows.append(
            PerformanceObservation(
                observation_id=f"strategy-{index}",
                source_id=f"source-{index}",
                strategy_version="rule_based_v2",
                symbol=("SPY", "QQQ", "IWM", "DIA")[index % 4],
                evidence_class="shadow_oos",
                entry_session=entry.isoformat(),
                exit_session=exit_at.isoformat(),
                regime="normal" if index % 2 else "defensive",
                r_multiple=value,
                gross_r=value + 0.05,
                net_pnl=value,
                gross_pnl=value + 0.05,
                attribution_confidence="shadow_deterministic",
                evidence_version=EVIDENCE_VERSION,
                formula_version=ACCOUNTING_VERSION,
            )
        )
    return rows


def _validation_engine(tmp_path, monkeypatch, value: float):
    storage = Storage(tmp_path / "strategy-validation.db")
    storage.initialize()
    config = load_config()
    engine = StrategyPerformanceEngine(
        storage,
        config,
        as_of="2026-04-15T00:00:00+00:00",
    )
    monkeypatch.setattr(
        engine,
        "_shadow_observations",
        lambda: _chronological_strategy_observations(value),
    )
    monkeypatch.setattr(engine, "_actual_observations", lambda: [])
    return storage, engine


def test_strategy_policy_progression_requires_verified_full_family(
    tmp_path, monkeypatch
):
    storage, engine = _validation_engine(tmp_path, monkeypatch, 0.5)
    snapshot = engine.refresh_strategy("rule_based_v2")
    assert snapshot.validation_status == "validated"
    assert snapshot.validation_family_id
    assert snapshot.validation_decision_id
    assert snapshot.recommendation_state != "SUSPENDED"
    family = ProfitabilityValidationStore(storage).load_verified(
        snapshot.validation_family_id
    )
    assert len(family.hypotheses) == 6
    policy = engine.latest_valid_policy("rule_based_v2")
    assert policy is not None
    assert policy.validation_fingerprint == snapshot.validation_fingerprint
    report = engine.format_report("rule_based_v2")
    assert "Validation: validated" in report
    assert "Attribution confidence:" in report


def test_mature_negative_strategy_is_suspended_by_validation(
    tmp_path, monkeypatch
):
    _storage, engine = _validation_engine(tmp_path, monkeypatch, -0.2)
    snapshot = engine.refresh_strategy("rule_based_v2")
    assert snapshot.validation_status == "failed"
    assert snapshot.recommendation_state == "SUSPENDED"


def test_tampered_or_incomplete_validation_family_invalidates_policy(
    tmp_path, monkeypatch
):
    storage, engine = _validation_engine(tmp_path, monkeypatch, 0.5)
    snapshot = engine.refresh_strategy("rule_based_v2")
    assert engine.latest_valid_policy("rule_based_v2") is not None
    storage.execute(
        "DELETE FROM profitability_validation_decisions WHERE family_id=? AND strategy_version<>?",
        (snapshot.validation_family_id, "rule_based_v2"),
    )
    assert engine.latest_valid_policy("rule_based_v2") is None


def _attribution_input(*, expected: bool = True) -> ProfitAttributionInput:
    expected_values = (
        {
            "expected_proposed_quantity": "10",
            "expected_entry_price": "100",
            "expected_gross_profit": "100",
            "expected_execution_cost": "6",
            "expected_holding_and_opportunity_cost": "2",
            "expected_uncertainty_cost": "2",
            "expected_net_profit": "90",
            "conservative_expected_net_profit": "60",
            "authority_status": "verified",
        }
        if expected
        else {"authority_status": "actual_only"}
    )
    return ProfitAttributionInput(
        position_lifecycle_id="lifecycle-1",
        symbol="SPY",
        strategy_version="strategy-a",
        opened_at="2026-01-01T14:00:00+00:00",
        closed_at="2026-01-10T14:00:00+00:00",
        initial_risk_dollars="50",
        legs=(
            AttributionLeg(
                id="leg-1",
                lot_id="lot-1",
                consumption_id="consumption-1",
                entry_proposal_id="proposal-1",
                entry_intent_id="buy-1",
                sell_intent_id="sell-1",
                trade_economics_id="economics-1" if expected else None,
                quantity="10",
                actual_entry_price="101",
                actual_exit_price="112",
                allocated_buy_fees="1",
                allocated_sell_fees="1",
                entry_final_ask="100.5" if expected else None,
                exit_final_bid="112.5" if expected else None,
                approval_delay_seconds="60" if expected else None,
                **expected_values,
            ),
        ),
    )


def _persist_actual_only_attribution(storage: Storage):
    storage.execute(
        """INSERT INTO position_lifecycles(
             id,symbol,side,state,opened_at,closed_at,opening_quantity,
             current_quantity,source,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "lifecycle-1",
            "SPY",
            "long",
            "closed",
            "2026-01-01T14:00:00+00:00",
            "2026-01-10T14:00:00+00:00",
            10,
            0,
            "test",
            "2026-01-01T14:00:00+00:00",
            "2026-01-10T14:00:00+00:00",
        ),
    )
    LotLedger(storage).set_coverage(
        effective_from="2025-12-01T00:00:00+00:00",
        confidence="verified",
        provenance="test",
    )
    buy = {
        "id": "buy-1",
        "proposal_id": "proposal-1",
        "symbol": "SPY",
        "side": "buy",
        "position_lifecycle_id": "lifecycle-1",
        "requested_quantity": 10,
        "strategy_version": "strategy-a",
        "initial_risk_dollars": 50,
        "evidence_version": EVIDENCE_VERSION,
        "formula_version": ACCOUNTING_VERSION,
    }
    sell = {
        "id": "sell-1",
        "symbol": "SPY",
        "side": "sell",
        "position_lifecycle_id": "lifecycle-1",
    }
    with storage.connect() as conn:
        LotLedger.apply_fill_in_transaction(
            conn,
            intent=buy,
            broker_event_key="buy-fill",
            delta_quantity=10,
            fill_price=101,
            occurred_at="2026-01-01T14:00:00+00:00",
            fees=1,
        )
        LotLedger.apply_fill_in_transaction(
            conn,
            intent=sell,
            broker_event_key="sell-fill",
            delta_quantity=10,
            fill_price=112,
            occurred_at="2026-01-10T14:00:00+00:00",
            fees=1,
        )
    lifecycle = storage.fetch_all(
        "SELECT * FROM position_lifecycles WHERE id='lifecycle-1'"
    )[0]
    return ProfitAttributionEngine(storage).refresh_lifecycle(lifecycle)


def test_profit_attribution_hand_reconciles_expected_and_realised_components():
    record = calculate_profit_attribution(_attribution_input())
    assert record.status == "complete"
    assert record.confidence == "verified"
    assert record.components["realized_gross_pnl"] == "110"
    assert record.components["realized_net_pnl"] == "108"
    assert record.components["actual_r_multiple"] == "2.16"
    assert record.components["reference_market_pnl"] == "125"
    assert record.components["combined_entry_timing_execution_drag"] == "10"
    assert record.components["approval_delay_price_drag"] == "5"
    assert record.components["entry_fill_slippage_drag"] == "5"
    assert record.components["exit_execution_drag"] == "5"
    assert record.components["expected_vs_realized_variance"] == "18"
    assert record.components["market_outcome_variance"] == "25"
    assert record.components["execution_cost_variance"] == "11"
    assert record.components["expected_noncash_reserve_release"] == "4"
    assert record.components["variance_reconciliation_residual"] == "0"
    assert record.components["reconciliation_residual"] == "0"


def test_actual_only_attribution_is_partial_and_never_invents_expected_values():
    record = calculate_profit_attribution(_attribution_input(expected=False))
    assert record.status == "partial"
    assert record.confidence == "verified_actual_only"
    assert record.components["realized_net_pnl"] == "108"
    assert record.components["expected_net_profit"] is None
    assert record.components["expected_vs_realized_variance"] is None
    assert record.components["reconciliation_residual"] == "0"


def test_signed_realized_adjustments_reconcile_without_becoming_execution_cost():
    original = _attribution_input()
    leg = original.legs[0]
    adjusted = ProfitAttributionInput(
        **{
            **original.__dict__,
            "legs": (
                AttributionLeg(
                    **{
                        **leg.__dict__,
                        "allocated_adjustments": "3",
                    }
                ),
            ),
        }
    )
    record = calculate_profit_attribution(adjusted)
    assert record.components["realized_adjustments"] == "3"
    assert record.components["realized_net_pnl"] == "111"
    assert record.components["expected_vs_realized_variance"] == "21"
    assert record.components["variance_reconciliation_residual"] == "0"
    assert record.components["reconciliation_residual"] == "0"


def test_partial_approval_delay_coverage_is_not_reported_as_complete():
    original = _attribution_input()
    leg = original.legs[0]
    legs = (
        AttributionLeg(
            **{
                **leg.__dict__,
                "id": "leg-a",
                "consumption_id": "consumption-a",
                "quantity": "5",
                "allocated_buy_fees": "0.5",
                "allocated_sell_fees": "0.5",
            }
        ),
        AttributionLeg(
            **{
                **leg.__dict__,
                "id": "leg-b",
                "consumption_id": "consumption-b",
                "quantity": "5",
                "allocated_buy_fees": "0.5",
                "allocated_sell_fees": "0.5",
                "approval_delay_seconds": None,
            }
        ),
    )
    record = calculate_profit_attribution(
        ProfitAttributionInput(**{**original.__dict__, "legs": legs})
    )
    assert record.components["approval_delay_coverage_quantity"] == "5"
    assert record.components["weighted_approval_delay_seconds"] is None
    assert record.components["reconciliation_residual"] == "0"


def test_profit_attribution_rejects_counterfactual_and_partial_economics():
    with pytest.raises(ProfitAttributionError, match="actual_paper"):
        calculate_profit_attribution(
            ProfitAttributionInput(
                **{
                    **_attribution_input().__dict__,
                    "evidence_class": "counterfactual",
                }
            )
        )
    leg = _attribution_input().legs[0]
    with pytest.raises(ProfitAttributionError, match="complete or entirely"):
        AttributionLeg(
            **{
                **leg.__dict__,
                "expected_net_profit": None,
            }
        ).canonical()
    with pytest.raises(ProfitAttributionError, match="trade economics ID"):
        AttributionLeg(
            **{
                **leg.__dict__,
                "trade_economics_id": None,
            }
        ).canonical()
    with pytest.raises(ProfitAttributionError, match="consumption IDs"):
        calculate_profit_attribution(
            ProfitAttributionInput(
                **{
                    **_attribution_input().__dict__,
                    "legs": (
                        leg,
                        AttributionLeg(
                            **{
                                **leg.__dict__,
                                "id": "leg-2",
                            }
                        ),
                    ),
                }
            )
        )


def test_profit_attribution_store_recomputes_and_detects_tampering(tmp_path):
    storage = Storage(tmp_path / "attribution.db")
    storage.initialize()
    storage.apply_explicit_migrations()
    record = calculate_profit_attribution(_attribution_input())
    store = ProfitAttributionStore(storage)
    with pytest.raises(
        ProfitAttributionError, match="lifecycle authority is missing"
    ):
        store.persist(record)
    assert storage.fetch_all("SELECT * FROM profit_attribution_records") == []
    record = _persist_actual_only_attribution(storage)
    assert store.load_verified(record.id).record_fingerprint == record.record_fingerprint
    storage.execute(
        "UPDATE lot_consumptions SET allocated_proceeds=999"
    )
    # REAL is a compatibility projection; canonical Decimal text is authority.
    assert store.load_verified(record.id).record_fingerprint == record.record_fingerprint
    storage.execute(
        "UPDATE lot_consumptions SET allocated_proceeds_decimal='999'"
    )
    with pytest.raises(
        ProfitAttributionError, match="leg values are inconsistent"
    ):
        store.load_verified(record.id)
    storage.execute(
        """UPDATE lot_consumptions
           SET allocated_proceeds=1120,allocated_proceeds_decimal='1120'"""
    )
    storage.execute(
        """UPDATE profit_attribution_records
           SET realized_net_pnl='999' WHERE id=?""",
        (record.id,),
    )
    with pytest.raises(
        ProfitAttributionError, match="realized_net_pnl"
    ):
        store.load_verified(record.id)


def test_closed_lifecycle_with_residual_lot_quantity_is_unavailable(tmp_path):
    storage = Storage(tmp_path / "residual-lot.db")
    storage.initialize()
    storage.execute(
        """INSERT INTO position_lifecycles(
             id,symbol,side,state,opened_at,closed_at,opening_quantity,
             current_quantity,source,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "residual",
            "SPY",
            "long",
            "closed",
            "2026-01-01T00:00:00+00:00",
            "2026-01-03T00:00:00+00:00",
            2,
            0,
            "test",
            "2026-01-01T00:00:00+00:00",
            "2026-01-03T00:00:00+00:00",
        ),
    )
    LotLedger(storage).set_coverage(
        effective_from="2025-12-01T00:00:00+00:00",
        confidence="verified",
        provenance="test",
    )
    buy = {
        "id": "buy",
        "symbol": "SPY",
        "side": "buy",
        "position_lifecycle_id": "residual",
        "requested_quantity": 2,
        "strategy_version": "rule_based_v2",
        "initial_risk_dollars": 2,
    }
    sell = {
        "id": "sell",
        "symbol": "SPY",
        "side": "sell",
        "position_lifecycle_id": "residual",
    }
    with storage.connect() as conn:
        LotLedger.apply_fill_in_transaction(
            conn,
            intent=buy,
            broker_event_key="buy-fill",
            delta_quantity=2,
            fill_price=10,
            occurred_at="2026-01-01T00:00:00+00:00",
        )
        LotLedger.apply_fill_in_transaction(
            conn,
            intent=sell,
            broker_event_key="sell-fill",
            delta_quantity=1,
            fill_price=12,
            occurred_at="2026-01-03T00:00:00+00:00",
        )
    lifecycle = storage.fetch_all(
        "SELECT * FROM position_lifecycles WHERE id='residual'"
    )[0]
    record = ProfitAttributionEngine(storage).refresh_lifecycle(lifecycle)
    assert record.status == "unavailable"
    assert record.reason == "closed lifecycle retains unconsumed attributed quantity"


def test_validation_and_attribution_audit_sheets_are_registered():
    names = {name for name, _query in SHEETS}
    assert {
        "Trade Economics",
        "Strategy Trade Records",
        "Strategy Scorecards",
        "Strategy Policies",
        "Validation Families",
        "Validation Decisions",
        "Validation Folds",
        "Profit Attribution",
    } <= names


def test_integrity_report_covers_validation_and_attribution(tmp_path):
    storage = Storage(tmp_path / "integrity.db")
    storage.initialize()
    storage.apply_explicit_migrations()
    report = DurableExecutionStore(storage).integrity_report()
    expected = {
        "orphaned_profitability_validation_decisions",
        "orphaned_profitability_validation_folds",
        "incomplete_profitability_validation_families",
        "strategy_validation_authority_mismatch",
        "orphaned_profit_attribution_records",
        "profit_attribution_reconciliation_mismatch",
        "strategy_trade_attribution_mismatch",
        "counterfactual_profitability_evidence",
    }
    assert expected <= set(report)
    assert all(report[name] == 0 for name in expected)


def test_integrity_report_counts_malformed_evidence_json_without_crashing(
    tmp_path,
):
    storage = Storage(tmp_path / "malformed-integrity.db")
    storage.initialize()
    storage.apply_explicit_migrations()
    family = _family()
    ProfitabilityValidationStore(storage).persist(family)
    attribution = _persist_actual_only_attribution(storage)
    storage.execute(
        "UPDATE profitability_validation_families SET observations_json='{' WHERE id=?",
        (family.id,),
    )
    storage.execute(
        "UPDATE profit_attribution_records SET components_json='{' WHERE id=?",
        (attribution.id,),
    )
    report = DurableExecutionStore(storage).integrity_report()
    assert report["incomplete_profitability_validation_families"] == 1
    assert report["counterfactual_profitability_evidence"] == 0
    assert report["profit_attribution_reconciliation_mismatch"] == 1
    storage.execute(
        """UPDATE profit_attribution_records
           SET components_json=?,reconciliation_residual='not-a-number'
           WHERE id=?""",
        (
            json.dumps(
                dict(attribution.components),
                sort_keys=True,
                separators=(",", ":"),
            ),
            attribution.id,
        ),
    )
    report = DurableExecutionStore(storage).integrity_report()
    assert report["profit_attribution_reconciliation_mismatch"] == 1
    storage.execute(
        "UPDATE profitability_validation_families SET observations_json='[\"bad\"]' WHERE id=?",
        (family.id,),
    )
    report = DurableExecutionStore(storage).integrity_report()
    assert report["counterfactual_profitability_evidence"] == 1
