from __future__ import annotations

import random
from dataclasses import replace
from decimal import Decimal as D

import pytest

from app.formula_versions import (
    CONFIGURATION_SCHEMA_VERSION,
    EVIDENCE_VERSION,
    STRATEGY_PERFORMANCE_SCHEMA_VERSION,
    STRATEGY_PERFORMANCE_VERSION,
    STRATEGY_POLICY_VERSION,
    TRADE_ECONOMICS_FORMULA_VERSION,
    TRADE_ECONOMICS_SCHEMA_VERSION,
)
from app.storage import Storage
from app.trade_economics import (
    TradeEconomicsCosts,
    TradeEconomicsError,
    TradeEconomicsInput,
    TradeEconomicsPolicy,
    TradeEconomicsStore,
    calculate_trade_economics,
)


NOW = "2026-07-16T00:00:00+00:00"
CONFIG_HASH = "a" * 64
FORMULAS = {
    "evidence": EVIDENCE_VERSION,
    "strategy_performance": STRATEGY_PERFORMANCE_VERSION,
    "strategy_policy": STRATEGY_POLICY_VERSION,
    "trade_economics": TRADE_ECONOMICS_FORMULA_VERSION,
}


def _candidate(**overrides) -> TradeEconomicsInput:
    values = {
        "candidate_id": "candidate-1",
        "run_id": "run-1",
        "proposal_id": None,
        "record_class": "research_estimate",
        "asset_class": "equity",
        "symbol": "SPY",
        "side": "buy",
        "action": "entry",
        "request_basis": "quantity",
        "strategy_version": "rule_based_v2",
        "strategy_state": "ACTIVE",
        "setup_type": "trend_continuation",
        "market_regime": "normal",
        "volatility_regime": "normal",
        "liquidity_regime": "liquid",
        "trend_regime": "uptrend",
        "breadth_regime": "broad",
        "estimated_at": NOW,
        "quantity": D("10"),
        "proposed_notional": D("1000"),
        "entry_estimate": D("100"),
        "limit_price": D("101"),
        "stop_price": D("95"),
        "target_price": D("115"),
        "maximum_approved_loss": D("67"),
        "expected_win_probability": D("0.45"),
        "conservative_win_probability": D("0.40"),
        "expected_average_win": D("120"),
        "expected_average_loss": D("40"),
        "expected_holding_period_days": D("5"),
        "annualization_days": D("252"),
        "marginal_portfolio_contribution_r": D("0.1"),
        "performance_snapshot_id": "snapshot-1",
        "policy_decision_id": "policy-1",
        "evidence_version": EVIDENCE_VERSION,
        "configuration_version": CONFIGURATION_SCHEMA_VERSION,
        "config_hash": CONFIG_HASH,
        "formula_versions": FORMULAS,
        "cost_model_version": "equity-cost-v1",
        "estimation_model_version": "lower-bound-edge-v1",
    }
    values.update(overrides)
    return TradeEconomicsInput(**values)


def _costs(**overrides) -> TradeEconomicsCosts:
    values = {
        "spread": D("1"),
        "slippage": D("1"),
        "fees": D("0.5"),
        "regulatory": D("0.1"),
        "crypto_transaction": D("0"),
        "market_impact": D("0.5"),
        "implementation_shortfall": D("0.5"),
        "adverse_selection": D("0.4"),
        "rejected_or_missed_fill": D("0.2"),
        "opportunity": D("0.2"),
        "approval_delay": D("0.3"),
        "holding": D("0.1"),
        "model_uncertainty": D("0.1"),
        "estimation_uncertainty": D("0.1"),
        "worst_reasonable_additional_cost": D("2"),
    }
    values.update(overrides)
    return TradeEconomicsCosts(**values)


def _database(tmp_path) -> Storage:
    storage = Storage(tmp_path / "economics.db")
    storage.initialize()
    return storage


def _install_authority(storage: Storage, *, config_hash: str = CONFIG_HASH) -> None:
    storage.execute(
        """INSERT INTO strategy_performance_snapshots(
             id,strategy_version,as_of,performance_version,policy_version,
             schema_version,quality_score,recommendation_state,trade_counts_json,
             metrics_json,components_json,raw_inputs_json,evidence_recency_days,
             attribution_confidence,version_completeness,input_fingerprint,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "snapshot-1",
            "rule_based_v2",
            NOW,
            STRATEGY_PERFORMANCE_VERSION,
            STRATEGY_POLICY_VERSION,
            STRATEGY_PERFORMANCE_SCHEMA_VERSION,
            80,
            "ACTIVE",
            "{}",
            "{}",
            "{}",
            "{}",
            0,
            1,
            1,
            "snapshot-fingerprint",
            NOW,
        ),
    )
    storage.execute(
        """INSERT INTO strategy_policy_decisions(
             id,strategy_version,decided_at,performance_snapshot_id,state,
             quality_score,reason,hard_gates_json,maturity_json,components_json,
             raw_inputs_json,enforcement_enabled,performance_version,policy_version,
             schema_version,input_fingerprint,evidence_version,
             configuration_version,config_hash)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "policy-1",
            "rule_based_v2",
            NOW,
            "snapshot-1",
            "ACTIVE",
            80,
            "test authority",
            "{}",
            "{}",
            "{}",
            "{}",
            1,
            STRATEGY_PERFORMANCE_VERSION,
            STRATEGY_POLICY_VERSION,
            STRATEGY_PERFORMANCE_SCHEMA_VERSION,
            "snapshot-fingerprint",
            EVIDENCE_VERSION,
            CONFIGURATION_SCHEMA_VERSION,
            config_hash,
        ),
    )


def _install_proposal(
    storage: Storage,
    *,
    record=None,
    symbol: str = "SPY",
    candidate_id: str = "candidate-1",
) -> None:
    assert record is not None
    import json

    payload = json.dumps(
        {
            "candidate_id": candidate_id,
            "config_hash": CONFIG_HASH,
            "performance_snapshot_id": "snapshot-1",
            "policy_decision_id": "policy-1",
            "formula_versions": FORMULAS,
            "trade_economics_input_fingerprint": record.input_fingerprint,
        },
        sort_keys=True,
    )
    storage.execute(
        """INSERT INTO trade_proposals(
             id,run_id,symbol,side,notional,status,created_at,expires_at,
             strategy_version,payload,performance_snapshot_id,policy_decision_id)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "proposal-1",
            "run-1",
            symbol,
            "buy",
            1000,
            "pending",
            NOW,
            "2026-07-16T00:10:00+00:00",
            "rule_based_v2",
            payload,
            "snapshot-1",
            "policy-1",
        ),
    )


def test_hand_calculated_complete_trade_economics() -> None:
    record = calculate_trade_economics(_candidate(), _costs())
    metrics = record.metrics
    assert metrics["expected_gross_upside"] == "150"
    assert metrics["expected_downside"] == "50"
    assert metrics["gross_reward_to_risk"] == "3"
    assert metrics["expected_execution_cost"] == "4.5"
    assert metrics["expected_holding_and_opportunity_cost"] == "0.3"
    assert metrics["expected_uncertainty_cost"] == "0.2"
    assert metrics["expected_total_cost"] == "5"
    assert metrics["expected_gross_profit"] == "32"
    assert metrics["expected_net_profit"] == "27"
    assert metrics["conservative_expected_net_profit"] == "19"
    assert metrics["expected_net_r"] == "0.54"
    assert metrics["conservative_expected_net_r"] == "0.38"
    assert metrics["break_even_win_probability_after_costs"] == "0.28125"
    assert metrics["expected_capital_efficiency"] == "0.027"
    assert metrics["expected_annualized_capital_efficiency"] == "1.3608"
    assert metrics["expected_profit_per_day"] == "5.4"
    assert metrics["expected_r_per_day"] == "0.108"
    assert metrics["cost_to_gross_edge_ratio"] == "0.15625"
    assert metrics["worst_reasonable_loss"] == "67"
    assert record.profitability_eligible is True
    assert record.rejection_reasons == ()


def test_after_cost_break_even_probability_matches_independent_decimal_recomputation() -> None:
    record = calculate_trade_economics(_candidate(), _costs())
    independent = (D("40") + D("5")) / (D("120") + D("40"))
    assert D(record.metrics["break_even_win_probability_after_costs"]) == independent
    assert (
        independent * D("120")
        - (D("1") - independent) * D("40")
        - D("5")
        == 0
    )


def test_nonpositive_and_uncertainty_overwhelmed_edge_is_rejected_not_hidden() -> None:
    record = calculate_trade_economics(
        _candidate(
            expected_win_probability=D("0.20"),
            conservative_win_probability=D("0.10"),
        ),
        _costs(),
    )
    assert record.profitability_eligible is False
    assert "nonpositive_expected_gross_edge" in record.rejection_reasons
    assert (
        "uncertainty_adjusted_net_edge_nonpositive_or_below_policy"
        in record.rejection_reasons
    )
    assert D(record.metrics["expected_net_profit"]) < 0


def test_cost_burden_and_break_even_policy_are_explicit_rejection_reasons() -> None:
    high_cost = _costs(spread=D("2"), worst_reasonable_additional_cost=D("2"))
    candidate = _candidate(
        maximum_approved_loss=D("68"),
        expected_win_probability=D("0.90"),
        conservative_win_probability=D("0.85"),
        expected_average_win=D("20"),
        expected_average_loss=D("40"),
    )
    record = calculate_trade_economics(candidate, high_cost)
    assert record.metrics["expected_total_cost"] == "6"
    assert D(record.metrics["break_even_win_probability_after_costs"]) > D("0.75")
    assert "break_even_win_probability_exceeds_policy" in record.rejection_reasons

    excessive_cost = calculate_trade_economics(
        _candidate(maximum_approved_loss=D("82")),
        _costs(spread=D("16")),
    )
    assert D(excessive_cost.metrics["cost_to_gross_edge_ratio"]) > D("0.50")
    assert "cost_consumes_excessive_gross_edge" in excessive_cost.rejection_reasons


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("quantity", D("0"), "quantity must be positive"),
        ("quantity", 10.0, "must use Decimal"),
        ("quantity", "NaN", "must be finite"),
        ("proposed_notional", "Infinity", "must be finite"),
        ("expected_win_probability", D("1.01"), "must be at most"),
        ("conservative_win_probability", D("-0.01"), "must be at least"),
        ("config_hash", "not-a-digest", "SHA-256"),
        ("configuration_version", "old", "not current"),
        ("evidence_version", "old", "not current"),
        ("side", "sell", "long-only BUY"),
        ("action", "exit", "unsupported"),
        ("estimated_at", "2026-07-16T00:00:00", "timezone"),
    ],
)
def test_malformed_candidate_values_fail_closed(field, value, message) -> None:
    with pytest.raises(TradeEconomicsError, match=message):
        calculate_trade_economics(_candidate(**{field: value}), _costs())


@pytest.mark.parametrize(
    "candidate",
    [
        _candidate(stop_price=D("100")),
        _candidate(target_price=D("100")),
        _candidate(limit_price=D("99")),
        _candidate(limit_price=D("115")),
        _candidate(proposed_notional=D("999.99")),
        _candidate(expected_average_win=D("151")),
        _candidate(expected_average_loss=D("51")),
        _candidate(maximum_approved_loss=D("66.99")),
        _candidate(
            conservative_win_probability=D("0.46"),
            expected_win_probability=D("0.45"),
        ),
    ],
)
def test_internally_inconsistent_economics_fail_closed(candidate) -> None:
    with pytest.raises(TradeEconomicsError):
        calculate_trade_economics(candidate, _costs())


def test_negative_or_binary_float_costs_fail_closed() -> None:
    with pytest.raises(TradeEconomicsError, match="at least"):
        calculate_trade_economics(_candidate(), _costs(spread=D("-0.01")))
    with pytest.raises(TradeEconomicsError, match="must use Decimal"):
        calculate_trade_economics(_candidate(), _costs(spread=0.1))


def test_formula_identity_is_required_and_current() -> None:
    missing = dict(FORMULAS)
    missing.pop("trade_economics")
    with pytest.raises(TradeEconomicsError, match="formula_versions.trade_economics"):
        calculate_trade_economics(
            _candidate(formula_versions=missing),
            _costs(),
        )
    wrong = {**FORMULAS, "strategy_performance": "old"}
    with pytest.raises(
        TradeEconomicsError, match="formula_versions.strategy_performance"
    ):
        calculate_trade_economics(
            _candidate(formula_versions=wrong),
            _costs(),
        )


def test_deterministic_replay_and_decimal_only_payload() -> None:
    first = calculate_trade_economics(_candidate(), _costs())
    second = calculate_trade_economics(_candidate(), _costs())
    assert first == second
    assert first.record_fingerprint == second.record_fingerprint
    assert first.id == second.id
    assert all(
        value is None or isinstance(value, str)
        for value in first.metrics.values()
    )
    equivalent = calculate_trade_economics(
        _candidate(quantity=D("10.0"), proposed_notional=D("1000.00")),
        _costs(spread=D("1.00")),
    )
    assert equivalent.record_fingerprint == first.record_fingerprint


def test_seeded_property_sweep_matches_independent_expectancy_equations() -> None:
    randomizer = random.Random(94107)
    for index in range(250):
        quantity = D(randomizer.randint(1, 100)) / D("10")
        entry = D(randomizer.randint(1000, 5000)) / D("10")
        stop_distance = D(randomizer.randint(10, 100)) / D("10")
        target_distance = D(randomizer.randint(20, 300)) / D("10")
        stop = entry - stop_distance
        target = entry + target_distance
        limit = entry + D(randomizer.randint(0, 5)) / D("10")
        downside = quantity * stop_distance
        upside = quantity * target_distance
        average_win = upside * D("0.8")
        average_loss = downside * D("0.8")
        probability = D(randomizer.randint(35, 80)) / D("100")
        conservative = max(D("0"), probability - D("0.05"))
        costs = _costs(
            spread=D("0.01"),
            slippage=D("0.01"),
            fees=D("0"),
            regulatory=D("0"),
            market_impact=D("0"),
            implementation_shortfall=D("0"),
            adverse_selection=D("0"),
            rejected_or_missed_fill=D("0"),
            opportunity=D("0"),
            approval_delay=D("0"),
            holding=D("0"),
            model_uncertainty=D("0"),
            estimation_uncertainty=D("0"),
            worst_reasonable_additional_cost=D("0"),
        )
        total_cost = D("0.02")
        maximum_loss = quantity * (limit - stop) + total_cost
        candidate = _candidate(
            candidate_id=f"candidate-{index}",
            quantity=quantity,
            proposed_notional=quantity * entry,
            entry_estimate=entry,
            limit_price=limit,
            stop_price=stop,
            target_price=target,
            maximum_approved_loss=maximum_loss,
            expected_win_probability=probability,
            conservative_win_probability=conservative,
            expected_average_win=average_win,
            expected_average_loss=average_loss,
        )
        record = calculate_trade_economics(candidate, costs)
        independent_gross = (
            probability * average_win
            - (D("1") - probability) * average_loss
        )
        independent_net = independent_gross - total_cost
        assert D(record.metrics["expected_gross_profit"]) == independent_gross
        assert D(record.metrics["expected_net_profit"]) == independent_net
        assert D(record.metrics["expected_net_r"]) == independent_net / downside
        assert (
            D(record.metrics["break_even_win_probability_after_costs"])
            == (average_loss + total_cost) / (average_win + average_loss)
        )


def test_immutable_persistence_replay_and_verified_reload(tmp_path) -> None:
    storage = _database(tmp_path)
    _install_authority(storage)
    store = TradeEconomicsStore(storage)
    record = calculate_trade_economics(_candidate(), _costs())
    assert store.persist(record) == record.id
    assert store.persist(record) == record.id
    assert store.load_verified(record.id) == record
    rows = storage.fetch_all("SELECT * FROM trade_economics_records")
    assert len(rows) == 1
    assert rows[0]["proposed_notional"] == "1000"
    assert rows[0]["expected_net_profit"] == "27"
    assert rows[0]["formula_version"] == TRADE_ECONOMICS_FORMULA_VERSION
    assert rows[0]["schema_version"] == TRADE_ECONOMICS_SCHEMA_VERSION


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("expected_net_profit", "999"),
        ("economics_json", "{}"),
        ("input_fingerprint", "0" * 64),
        ("record_fingerprint", "f" * 64),
        ("config_hash", "b" * 64),
    ],
)
def test_corrupted_persisted_economics_fails_closed(tmp_path, column, value) -> None:
    storage = _database(tmp_path)
    _install_authority(storage)
    store = TradeEconomicsStore(storage)
    record = calculate_trade_economics(_candidate(), _costs())
    store.persist(record)
    storage.execute(
        f'UPDATE trade_economics_records SET "{column}"=? WHERE id=?',
        (value, record.id),
    )
    with pytest.raises(TradeEconomicsError, match="inconsistent"):
        store.load_verified(record.id)


def test_exact_proposal_is_bound_to_economics_in_same_transaction(tmp_path) -> None:
    storage = _database(tmp_path)
    _install_authority(storage)
    record = calculate_trade_economics(
        _candidate(
            proposal_id="proposal-1",
            record_class="proposal_candidate",
        ),
        _costs(),
    )
    _install_proposal(storage, record=record)
    TradeEconomicsStore(storage).persist(record)
    proposal = storage.fetch_all(
        "SELECT trade_economics_id FROM trade_proposals WHERE id='proposal-1'"
    )[0]
    assert proposal["trade_economics_id"] == record.id


def test_mismatched_proposal_authority_inserts_nothing(tmp_path) -> None:
    storage = _database(tmp_path)
    _install_authority(storage)
    record = calculate_trade_economics(
        _candidate(
            proposal_id="proposal-1",
            record_class="proposal_candidate",
        ),
        _costs(),
    )
    _install_proposal(storage, record=record, symbol="QQQ")
    with pytest.raises(TradeEconomicsError, match="proposal authority"):
        TradeEconomicsStore(storage).persist(record)
    assert storage.fetch_all("SELECT * FROM trade_economics_records") == []
    assert (
        storage.fetch_all(
            "SELECT trade_economics_id FROM trade_proposals WHERE id='proposal-1'"
        )[0]["trade_economics_id"]
        is None
    )


def test_stale_policy_authority_inserts_nothing(tmp_path) -> None:
    storage = _database(tmp_path)
    _install_authority(storage, config_hash="b" * 64)
    record = calculate_trade_economics(_candidate(), _costs())
    with pytest.raises(TradeEconomicsError, match="strategy policy authority"):
        TradeEconomicsStore(storage).persist(record)
    assert storage.fetch_all("SELECT * FROM trade_economics_records") == []


def test_future_strategy_evidence_cannot_be_attached_to_earlier_candidate(
    tmp_path,
) -> None:
    storage = _database(tmp_path)
    _install_authority(storage)
    storage.execute(
        "UPDATE strategy_performance_snapshots SET as_of=? WHERE id='snapshot-1'",
        ("2026-07-17T00:00:00+00:00",),
    )
    record = calculate_trade_economics(_candidate(), _costs())
    with pytest.raises(TradeEconomicsError, match="future information"):
        TradeEconomicsStore(storage).persist(record)
    assert storage.fetch_all("SELECT * FROM trade_economics_records") == []


def test_trade_economics_migration_is_additive_idempotent_and_runtime_required(
    tmp_path,
) -> None:
    storage = _database(tmp_path)
    with pytest.raises(RuntimeError, match="Database migration required"):
        storage.require_runtime_schema()
    storage.apply_explicit_migrations()
    first = storage.fetch_all(
        """SELECT name,sql FROM sqlite_master
           WHERE type IN ('table','index')
             AND (name='trade_economics_records'
                  OR name LIKE 'idx_trade_economics_%')
           ORDER BY name"""
    )
    storage.apply_explicit_migrations()
    second = storage.fetch_all(
        """SELECT name,sql FROM sqlite_master
           WHERE type IN ('table','index')
             AND (name='trade_economics_records'
                  OR name LIKE 'idx_trade_economics_%')
           ORDER BY name"""
    )
    assert first == second
    assert TRADE_ECONOMICS_SCHEMA_VERSION in storage.schema_versions()
    storage.require_runtime_schema()
