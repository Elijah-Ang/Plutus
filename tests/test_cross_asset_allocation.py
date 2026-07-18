from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.configuration import effective_config_hash, validate_config
from app.cross_asset_allocation import (
    CrossAssetAllocationError,
    CrossAssetAllocationStore,
    CrossAssetCandidate,
    CrossAssetPortfolioSnapshot,
    optimize_cross_asset_allocation,
)
from app.execution import DurableExecutionStore
from app.formula_versions import (
    CROSS_ASSET_ALLOCATION_FORMULA_VERSION,
    CROSS_ASSET_ALLOCATION_SCHEMA_VERSION,
)
from app.storage import Storage
from app.utils import load_config


D = Decimal
AS_OF = "2026-07-18T11:00:00+00:00"


def _config(**updates):
    config = deepcopy(load_config())
    config["cross_asset_allocation"].update(updates)
    config["effective_config_hash"] = effective_config_hash(config)
    return config


def _candidate(config, candidate_id="equity-1", **overrides):
    notional = D(str(overrides.pop("proposed_notional", "1000")))
    risk = D(str(overrides.pop("stop_risk_dollars", "100")))
    economic_risk = D(str(overrides.pop("economic_risk_dollars", risk)))
    profit = D(str(overrides.pop("expected_net_profit", "25")))
    holding = D(str(overrides.pop("expected_holding_days", "10")))
    expected_r = overrides.pop("expected_net_r", profit / economic_risk)
    capital_efficiency = overrides.pop(
        "expected_capital_efficiency", profit / notional
    )
    r_per_day = overrides.pop("expected_r_per_day", D(str(expected_r)) / holding)
    conservative = overrides.pop("conservative_expected_net_r", "0.18")
    marginal = overrides.pop("marginal_portfolio_contribution_r", "0.15")
    values = {
        "candidate_id": candidate_id,
        "source_type": "candidate_profitability_decision",
        "source_id": f"source-{candidate_id}",
        "source_fingerprint": "1" * 64,
        "source_authoritative": True,
        "run_id": "run-cross-asset",
        "asset_class": "equity",
        "symbol": candidate_id.upper(),
        "cluster": "us_equity",
        "strategy_version": "rule_based_v2",
        "strategy_state": "ACTIVE",
        "action": "entry",
        "execution_lane": "operational_paper",
        "evidence_as_of": AS_OF,
        "proposed_notional": str(notional),
        "economic_risk_dollars": str(economic_risk),
        "stop_risk_dollars": str(risk),
        "expected_net_profit": str(profit),
        "expected_net_r": str(expected_r),
        "conservative_expected_net_r": str(conservative),
        "expected_capital_efficiency": str(capital_efficiency),
        "expected_r_per_day": str(r_per_day),
        "marginal_portfolio_contribution_r": str(marginal),
        "probability_positive_return": "0.55",
        "probability_severe_loss": "0.10",
        "uncertainty": "0.10",
        "cost_to_gross_edge_ratio": "0.20",
        "expected_holding_days": str(holding),
        "annualized_volatility": "0.25",
        "liquidity_notional": "20000000",
        "correlation_to_portfolio": "0.20",
        "marginal_drawdown_r": "0.10",
        "current_position": False,
        "conflict_free": True,
        "profitability_eligible": True,
        "config_hash": config["effective_config_hash"],
        "formula_versions": {
            "trade_economics": config["formula_versions"]["trade_economics"],
            "profitability_ranking": config["formula_versions"]["profitability_ranking"],
            "cross_asset_allocation": config["formula_versions"]["cross_asset_allocation"],
        },
    }
    values.update(overrides)
    return CrossAssetCandidate(**values)


def _portfolio(config, **overrides):
    values = {
        "snapshot_id": "portfolio-1",
        "snapshot_fingerprint": "2" * 64,
        "authoritative": True,
        "paper_account_id_hash": "3" * 64,
        "as_of": AS_OF,
        "equity": "100000",
        "cash": "80000",
        "buying_power": "80000",
        "gross_exposure": "10000",
        "stop_heat": "100",
        "daily_loss_pct": "0",
        "weekly_loss_pct": "0",
        "drawdown_pct": "0",
        "portfolio_annualized_volatility": "0.12",
        "position_count": 1,
        "asset_class_position_count": {"equity": 1, "crypto": 0},
        "symbol_exposure": {"HELD": "10000"},
        "cluster_exposure": {"us_equity": "10000", "crypto_major": "0"},
        "asset_class_exposure": {"equity": "10000", "crypto": "0"},
        "asset_class_stop_heat": {"equity": "100", "crypto": "0"},
        "strategy_stop_heat": {"rule_based_v2": "100"},
        "kill_switch_active": False,
        "loss_evidence_fresh": True,
        "database_healthy": True,
        "internet_healthy": True,
        "power_healthy": True,
        "broker_healthy": True,
        "config_hash": config["effective_config_hash"],
    }
    values.update(overrides)
    return CrossAssetPortfolioSnapshot(**values)


def _plan(config, candidates, portfolio=None):
    return optimize_cross_asset_allocation(
        run_id="run-cross-asset",
        candidates=candidates,
        portfolio=portfolio or _portfolio(config),
        config=config,
        as_of=AS_OF,
    )


def test_exact_configuration_remains_paper_manual_and_advisory_only():
    config = load_config()
    validate_config(config)
    assert config["mode"] == "paper"
    assert config["live_enabled"] is False
    assert config["auto_execution_enabled"] is False
    assert config["auto_execution_mode"] == "manual_only"
    assert config["cross_asset_allocation"]["mode"] == "research_advisory"
    assert config["cross_asset_allocation"]["produce_order_authority"] is False
    assert config["crypto"]["mode"] == "research_only"
    assert config["crypto"]["paper_trading_enabled"] is False


def test_cross_asset_ranking_is_deterministic_and_input_order_independent():
    config = _config()
    equity = _candidate(config, "SPY")
    crypto = _candidate(
        config,
        "BTC-USD",
        asset_class="crypto",
        symbol="BTC/USD",
        cluster="crypto_major",
        execution_lane="research_only",
        source_type="crypto_profitability_research",
        strategy_version="crypto_trend_v1",
        liquidity_notional="5000000",
    )
    first = _plan(config, [equity, crypto])
    second = _plan(config, [crypto, equity])
    assert first.plan_fingerprint == second.plan_fingerprint
    assert first.decisions == second.decisions
    assert first.execution_authorized is False


def test_larger_nominal_profit_does_not_outrank_better_normalized_candidate():
    config = _config()
    large_weak = _candidate(
        config,
        "LARGE",
        proposed_notional="10000",
        stop_risk_dollars="1000",
        expected_net_profit="100",
        expected_holding_days="20",
        conservative_expected_net_r="0.05",
        marginal_portfolio_contribution_r="0.03",
        uncertainty="0.40",
        correlation_to_portfolio="0.80",
    )
    small_strong = _candidate(
        config,
        "SMALL",
        proposed_notional="1000",
        stop_risk_dollars="100",
        expected_net_profit="30",
        expected_holding_days="5",
        conservative_expected_net_r="0.22",
        marginal_portfolio_contribution_r="0.20",
        uncertainty="0.05",
        correlation_to_portfolio="0.10",
    )
    decisions = _plan(config, [large_weak, small_strong]).decisions
    assert decisions[0]["candidate_id"] == "SMALL"
    assert D(decisions[0]["ranking_score"]) > D(decisions[1]["ranking_score"])


def test_crypto_research_candidate_is_bounded_by_crypto_sleeve_and_never_authority():
    config = _config()
    crypto = _candidate(
        config,
        "BTC",
        asset_class="crypto",
        symbol="BTC/USD",
        cluster="crypto_major",
        execution_lane="research_only",
        source_type="crypto_profitability_research",
        strategy_version="crypto_trend_v1",
        action="add",
        current_position=True,
        proposed_notional="2000",
        stop_risk_dollars="20",
        expected_net_profit="50",
        expected_holding_days="10",
        conservative_expected_net_r="2.0",
        marginal_portfolio_contribution_r="0.15",
        liquidity_notional="5000000",
    )
    portfolio = _portfolio(
        config,
        gross_exposure="10500",
        position_count=2,
        asset_class_position_count={"equity": 1, "crypto": 1},
        symbol_exposure={"HELD": "10000", "BTC/USD": "500"},
        asset_class_exposure={"equity": "10000", "crypto": "500"},
        cluster_exposure={"us_equity": "10000", "crypto_major": "500"},
    )
    decision = _plan(config, [crypto], portfolio).decisions[0]
    assert decision["decision"] == "ALLOCATE_RESEARCH_ONLY_PARTIAL"
    assert decision["allocated_notional"] == "500"
    assert decision["order_authority"] is False


@pytest.mark.parametrize(
    ("portfolio_updates", "reason"),
    [
        ({"gross_exposure": "49999", "symbol_exposure": {"HELD": "49999"}, "cluster_exposure": {"us_equity": "49999", "crypto_major": "0"}, "asset_class_exposure": {"equity": "49999", "crypto": "0"}}, "remaining_capacity_below_minimum"),
        ({"stop_heat": "1749.9", "asset_class_stop_heat": {"equity": "1749.9", "crypto": "0"}, "strategy_stop_heat": {"rule_based_v2": "1749.9"}}, "remaining_capacity_below_minimum"),
        ({"symbol_exposure": {"SPY": "5999", "HELD": "4001"}}, "remaining_capacity_below_minimum"),
        ({"gross_exposure": "14999", "symbol_exposure": {"HELD": "14999"}, "cluster_exposure": {"us_equity": "14999", "crypto_major": "0"}, "asset_class_exposure": {"equity": "14999", "crypto": "0"}}, "remaining_capacity_below_minimum"),
        ({"stop_heat": "612.4", "asset_class_stop_heat": {"equity": "612.4", "crypto": "0"}, "strategy_stop_heat": {"rule_based_v2": "612.4"}}, "remaining_capacity_below_minimum"),
        ({"cash": "20001", "buying_power": "80000"}, "remaining_capacity_below_minimum"),
    ],
)
def test_every_portfolio_capacity_ceiling_fails_closed(portfolio_updates, reason):
    config = _config()
    held = "SPY" in portfolio_updates.get("symbol_exposure", {})
    if held:
        portfolio_updates = {
            "position_count": 2,
            "asset_class_position_count": {"equity": 2, "crypto": 0},
            **portfolio_updates,
        }
    decision = _plan(
        config,
        [
            _candidate(
                config,
                "SPY",
                symbol="SPY",
                action="add" if held else "entry",
                current_position=held,
            )
        ],
        _portfolio(config, **portfolio_updates),
    ).decisions[0]
    assert decision["decision"] == "REJECT"
    assert reason in decision["rejection_reasons"]


def test_position_limits_are_asset_specific_and_same_symbol_is_counted_once():
    config = _config()
    full = _portfolio(
        config,
        position_count=3,
        asset_class_position_count={"equity": 3, "crypto": 0},
        symbol_exposure={"HELD-1": "3000", "HELD-2": "3000", "HELD-3": "4000"},
    )
    rejected = _plan(config, [_candidate(config, "SPY", symbol="SPY")], full)
    assert "maximum_equity_positions_reached" in rejected.decisions[0]["rejection_reasons"]

    first = _candidate(config, "SPY-1", symbol="SPY")
    second = _candidate(config, "SPY-2", symbol="SPY")
    available = _portfolio(
        config,
        position_count=2,
        asset_class_position_count={"equity": 2, "crypto": 0},
        symbol_exposure={"HELD-1": "5000", "HELD-2": "5000"},
    )
    plan = _plan(config, [first, second], available)
    assert plan.summary["position_count_after"] == 3
    assert plan.summary["asset_class_position_count_after"]["equity"] == 3


def test_crypto_trade_heat_and_exploration_heat_cannot_exceed_existing_controls():
    config = _config()
    crypto = _candidate(
        config,
        "BTC",
        asset_class="crypto",
        symbol="BTC/USD",
        cluster="crypto_major",
        execution_lane="research_only",
        source_type="crypto_profitability_research",
        strategy_version="crypto_trend_v1",
        proposed_notional="2000",
        stop_risk_dollars="20",
        expected_net_profit="50",
        expected_holding_days="10",
        conservative_expected_net_r="2.0",
        marginal_portfolio_contribution_r="0.15",
        liquidity_notional="5000000",
    )
    crypto_decision = _plan(config, [crypto]).decisions[0]
    assert crypto_decision["allocated_notional"] == "1000"
    assert crypto_decision["allocated_stop_risk"] == "10"
    assert "trade_stop_risk" in crypto_decision["binding_constraints"]

    exploration = _candidate(
        config,
        "EXPLORE",
        strategy_state="EXPLORATION",
        strategy_version="exploration_v1",
        proposed_notional="2000",
        stop_risk_dollars="200",
        expected_net_profit="50",
        expected_holding_days="10",
        conservative_expected_net_r="0.20",
        marginal_portfolio_contribution_r="0.15",
    )
    exploration_decision = _plan(config, [exploration]).decisions[0]
    assert exploration_decision["allocated_notional"] == "1000"
    assert exploration_decision["allocated_stop_risk"] == "100"
    assert "exploration_stop_heat" in exploration_decision["binding_constraints"]


def test_authoritative_portfolio_totals_must_reconcile():
    config = _config()
    with pytest.raises(CrossAssetAllocationError, match="symbol exposure"):
        _plan(
            config,
            [_candidate(config, "SPY")],
            _portfolio(config, symbol_exposure={"HELD": "9999"}),
        )
    with pytest.raises(CrossAssetAllocationError, match="position counts"):
        _plan(
            config,
            [_candidate(config, "SPY")],
            _portfolio(config, asset_class_position_count={"equity": 0, "crypto": 0}),
        )


@pytest.mark.parametrize(
    ("portfolio_updates", "blocker"),
    [
        ({"kill_switch_active": True}, "kill_switch_active"),
        ({"loss_evidence_fresh": False}, "loss_evidence_stale"),
        ({"database_healthy": False}, "database_unhealthy"),
        ({"internet_healthy": False}, "internet_unhealthy"),
        ({"power_healthy": False}, "power_unhealthy"),
        ({"broker_healthy": False}, "broker_unhealthy"),
        ({"daily_loss_pct": "0.75"}, "daily_loss_halt"),
        ({"weekly_loss_pct": "1.50"}, "weekly_loss_halt"),
        ({"drawdown_pct": "6.0"}, "drawdown_halt"),
    ],
)
def test_critical_controls_block_every_allocation(portfolio_updates, blocker):
    config = _config()
    plan = _plan(
        config, [_candidate(config, "SPY")], _portfolio(config, **portfolio_updates)
    )
    assert plan.decisions[0]["decision"] == "REJECT"
    assert blocker in plan.decisions[0]["rejection_reasons"]
    assert plan.summary["keep_cash"] is True


def test_drawdown_throttle_reduces_portfolio_capacity_before_halt():
    config = _config()
    portfolio = _portfolio(
        config,
        drawdown_pct="2.1",
        gross_exposure="24950",
        symbol_exposure={"HELD": "24950"},
        cluster_exposure={"other": "24950", "us_equity": "0", "crypto_major": "0"},
        asset_class_exposure={"equity": "24950", "crypto": "0"},
    )
    decision = _plan(config, [_candidate(config, "SPY")], portfolio).decisions[0]
    assert decision["decision"] == "ALLOCATE_ADVISORY_PARTIAL"
    assert decision["allocated_notional"] == "50"


@pytest.mark.parametrize(
    ("candidate_updates", "reason"),
    [
        ({"source_authoritative": False}, "source_not_authoritative"),
        ({"profitability_eligible": False}, "profitability_ineligible"),
        ({"conflict_free": False}, "order_or_position_conflict"),
        ({"strategy_state": "SUSPENDED"}, "strategy_not_allocatable"),
        ({"cost_to_gross_edge_ratio": "0.51"}, "execution_cost_burden_exceeds_policy"),
        ({"liquidity_notional": "9999999"}, "liquidity_below_policy"),
        ({"marginal_drawdown_r": "1.01"}, "marginal_drawdown_exceeds_policy"),
        ({"annualized_volatility": "0.46"}, "annualized_volatility_exceeds_policy"),
    ],
)
def test_candidate_authority_and_quality_fail_closed(candidate_updates, reason):
    config = _config()
    decision = _plan(config, [_candidate(config, "SPY", **candidate_updates)]).decisions[0]
    assert decision["decision"] == "REJECT"
    assert reason in decision["rejection_reasons"]


def test_nonpositive_edge_keeps_cash():
    config = _config()
    candidate = _candidate(
        config,
        "SPY",
        expected_net_profit="-5",
        conservative_expected_net_r="-0.10",
        marginal_portfolio_contribution_r="-0.10",
    )
    plan = _plan(config, [candidate])
    assert plan.summary["keep_cash"] is True
    assert "nonpositive_expected_net_profit" in plan.decisions[0]["rejection_reasons"]


def test_plan_reports_conservative_volatility_drawdown_and_liquidity_diagnostics():
    config = _config()
    plan = _plan(config, [_candidate(config, "SPY")])
    summary = plan.summary
    assert D(summary["portfolio_annualized_volatility_upper_bound"]) > D("0.12")
    assert D(summary["portfolio_variance_upper_bound"]) > D("0.12") ** 2
    assert D(summary["expected_marginal_drawdown_dollars"]) > 0
    assert D(summary["maximum_liquidity_utilization"]) == D("0.00005")


def test_allocated_portfolio_preserves_all_dimensional_invariants():
    config = _config()
    candidates = [
        _candidate(config, f"EQ-{index}", symbol=f"EQ{index}", cluster=f"cluster-{index}")
        for index in range(4)
    ]
    candidates.append(
        _candidate(
            config,
            "BTC",
            asset_class="crypto",
            symbol="BTC/USD",
            cluster="crypto_major",
            execution_lane="research_only",
            source_type="crypto_profitability_research",
            strategy_version="crypto_trend_v1",
            proposed_notional="2000",
            stop_risk_dollars="10",
            expected_net_profit="25",
            expected_holding_days="10",
            conservative_expected_net_r="2.0",
            marginal_portfolio_contribution_r="0.15",
            liquidity_notional="5000000",
        )
    )
    plan = _plan(config, candidates)
    summary = plan.summary
    assert D(summary["gross_exposure_after"]) <= D("50000")
    assert D(summary["stop_heat_after"]) <= D("1750")
    assert D(summary["asset_class_exposure_after"]["crypto"]) <= D("1000")
    assert D(summary["asset_class_stop_heat_after"]["crypto"]) <= D("50")
    assert summary["position_count_after"] <= 5
    assert summary["asset_class_position_count_after"]["equity"] <= 3
    assert summary["asset_class_position_count_after"]["crypto"] <= 2
    assert sum(map(D, summary["asset_class_exposure_after"].values())) == D(
        summary["gross_exposure_after"]
    )
    assert sum(map(D, summary["asset_class_stop_heat_after"].values())) == D(
        summary["stop_heat_after"]
    )
    for decision in plan.decisions:
        assert D(decision["allocated_notional"]) <= D(decision["requested_notional"])
        assert D(decision["allocated_stop_risk"]) <= D(
            decision["requested_stop_risk"]
        )
        assert D(decision["allocated_economic_risk"]) <= D(
            decision["requested_economic_risk"]
        )
        assert decision["order_authority"] is False


def test_inconsistent_economic_identity_is_rejected():
    config = _config()
    candidate = _candidate(config, "SPY", expected_net_r="0.99")
    with pytest.raises(CrossAssetAllocationError, match="inconsistent"):
        _plan(config, [candidate])


def test_cost_inclusive_stop_risk_is_separate_from_economic_r_denominator():
    config = _config()
    candidate = _candidate(
        config,
        "SPY",
        economic_risk_dollars="80",
        stop_risk_dollars="100",
        expected_net_profit="20",
        conservative_expected_net_r="0.18",
        marginal_portfolio_contribution_r="0.15",
    )
    decision = _plan(config, [candidate]).decisions[0]
    assert decision["requested_economic_risk"] == "80"
    assert decision["requested_stop_risk"] == "100"
    assert decision["allocated_economic_risk"] == "80"
    assert decision["allocated_stop_risk"] == "100"
    assert decision["conservative_expected_profit_contribution"] == "14.4"

    understated = _candidate(
        config,
        "BAD",
        economic_risk_dollars="101",
        stop_risk_dollars="100",
    )
    with pytest.raises(CrossAssetAllocationError, match="stop risk"):
        _plan(config, [understated])


def test_binary_float_and_nonfinite_values_fail_closed():
    config = _config()
    with pytest.raises(CrossAssetAllocationError, match="Decimal"):
        _plan(config, [_candidate(config, "SPY", uncertainty=0.1)])
    with pytest.raises(CrossAssetAllocationError, match="finite"):
        _plan(config, [_candidate(config, "SPY", uncertainty="NaN")])


def test_crypto_cannot_escape_research_only_lane():
    config = _config()
    candidate = _candidate(
        config,
        "BTC",
        asset_class="crypto",
        symbol="BTC/USD",
        cluster="crypto_major",
        execution_lane="operational_paper",
    )
    with pytest.raises(CrossAssetAllocationError, match="research_only"):
        _plan(config, [candidate])


def test_stale_evidence_wrong_config_and_formula_fail_closed():
    config = _config()
    stale = _candidate(
        config,
        "SPY",
        evidence_as_of=(datetime.fromisoformat(AS_OF) - timedelta(seconds=301)).isoformat(),
    )
    with pytest.raises(CrossAssetAllocationError, match="stale"):
        _plan(config, [stale])
    with pytest.raises(CrossAssetAllocationError, match="configuration identity"):
        _plan(config, [_candidate(config, "SPY", config_hash="f" * 64)])
    formulas = dict(_candidate(config, "SPY").formula_versions)
    formulas["cross_asset_allocation"] = "old"
    with pytest.raises(CrossAssetAllocationError, match="formula_versions"):
        _plan(config, [_candidate(config, "SPY", formula_versions=formulas)])


def test_stale_or_malformed_portfolio_authority_fails_closed():
    config = _config()
    stale_time = (datetime.fromisoformat(AS_OF) - timedelta(seconds=301)).isoformat()
    with pytest.raises(CrossAssetAllocationError, match="portfolio snapshot is stale"):
        _plan(
            config,
            [_candidate(config, "SPY")],
            _portfolio(config, as_of=stale_time),
        )
    with pytest.raises(CrossAssetAllocationError, match="SHA-256"):
        _plan(
            config,
            [_candidate(config, "SPY")],
            _portfolio(config, paper_account_id_hash="paper-account"),
        )


def test_mutated_policy_without_recomputed_hash_fails_closed():
    config = _config()
    config["cross_asset_allocation"]["maximum_crypto_exposure_pct"] = 50.0
    with pytest.raises(CrossAssetAllocationError, match="configuration hash"):
        _plan(config, [_candidate(config, "SPY")])


def test_fractional_position_limit_is_rejected_not_truncated():
    config = _config(maximum_positions=1.5)
    candidate = _candidate(config, "SPY")
    with pytest.raises(CrossAssetAllocationError, match="positive integer"):
        _plan(config, [candidate])


def test_duplicate_candidate_or_source_authority_fails_closed():
    config = _config()
    first = _candidate(config, "SPY")
    with pytest.raises(CrossAssetAllocationError, match="candidate_id"):
        _plan(config, [first, first])
    second = _candidate(config, "QQQ", source_id=first.source_id)
    with pytest.raises(CrossAssetAllocationError, match="source authority"):
        _plan(config, [first, second])


def test_store_is_immutable_idempotent_and_independently_recomputed(tmp_path):
    config = _config()
    storage = Storage(tmp_path / "cross-asset.sqlite3")
    storage.apply_explicit_migrations()
    store = CrossAssetAllocationStore(storage, config)
    candidate = _candidate(config, "SPY")
    portfolio = _portfolio(config)
    first = store.create(
        run_id="run-cross-asset", candidates=[candidate], portfolio=portfolio, as_of=AS_OF
    )
    second = store.create(
        run_id="run-cross-asset", candidates=[candidate], portfolio=portfolio, as_of=AS_OF
    )
    assert first == second
    assert storage.fetch_all("SELECT COUNT(*) n FROM cross_asset_allocation_plans")[0]["n"] == 1
    assert CROSS_ASSET_ALLOCATION_SCHEMA_VERSION in storage.schema_versions()
    assert store.load_verified(first.id, now=datetime.fromisoformat(AS_OF)) == first


def test_concurrent_replay_creates_one_immutable_plan(tmp_path):
    config = _config()
    storage = Storage(tmp_path / "cross-asset-concurrent.sqlite3")
    storage.apply_explicit_migrations()
    store = CrossAssetAllocationStore(storage, config)
    candidate = _candidate(config, "SPY")
    portfolio = _portfolio(config)

    def create():
        return store.create(
            run_id="run-cross-asset",
            candidates=[candidate],
            portfolio=portfolio,
            as_of=AS_OF,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        plans = list(executor.map(lambda _: create(), range(2)))
    assert plans[0] == plans[1]
    assert storage.fetch_all("SELECT COUNT(*) n FROM cross_asset_allocation_plans")[0]["n"] == 1


def test_tampered_plan_and_regenerated_local_fingerprints_still_fail(tmp_path):
    config = _config()
    storage = Storage(tmp_path / "tamper.sqlite3")
    storage.initialize()
    store = CrossAssetAllocationStore(storage, config)
    plan = store.create(
        run_id="run-cross-asset",
        candidates=[_candidate(config, "SPY")],
        portfolio=_portfolio(config),
        as_of=AS_OF,
    )
    row = storage.fetch_all(
        "SELECT plan_json FROM cross_asset_allocation_plans WHERE id=?", (plan.id,)
    )[0]
    payload = json.loads(row["plan_json"])
    payload["decisions"][0]["allocated_notional"] = "99999"
    from app.cross_asset_allocation import _canonical_json, _fingerprint

    body = dict(payload)
    body.pop("id")
    regenerated = _fingerprint(body)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        storage.execute(
            "UPDATE cross_asset_allocation_plans SET plan_json=? WHERE id=?",
            (_canonical_json(payload), plan.id),
        )
    storage.execute("DROP TRIGGER trg_cross_asset_plan_immutable_update")
    storage.execute(
        "UPDATE cross_asset_allocation_plans SET plan_json=?,plan_fingerprint=? WHERE id=?",
        (_canonical_json(payload), regenerated, plan.id),
    )
    with pytest.raises(CrossAssetAllocationError, match="identity mismatch"):
        store.load_verified(plan.id, now=datetime.fromisoformat(AS_OF))


def test_integrity_counter_counts_malformed_plan_json_without_raising(tmp_path):
    config = _config()
    storage = Storage(tmp_path / "malformed-plan.sqlite3")
    storage.initialize()
    plan = CrossAssetAllocationStore(storage, config).create(
        run_id="run-cross-asset",
        candidates=[_candidate(config, "SPY")],
        portfolio=_portfolio(config),
        as_of=AS_OF,
    )
    storage.execute("DROP TRIGGER trg_cross_asset_plan_immutable_update")
    storage.execute(
        "UPDATE cross_asset_allocation_plans SET plan_json='{' WHERE id=?",
        (plan.id,),
    )
    report = DurableExecutionStore(storage).integrity_report()
    assert report["invalid_cross_asset_allocation_plan"] == 1


def test_expired_plan_fails_closed(tmp_path):
    config = _config()
    storage = Storage(tmp_path / "expired.sqlite3")
    storage.initialize()
    store = CrossAssetAllocationStore(storage, config)
    plan = store.create(
        run_id="run-cross-asset",
        candidates=[_candidate(config, "SPY")],
        portfolio=_portfolio(config),
        as_of=AS_OF,
    )
    with pytest.raises(CrossAssetAllocationError, match="expired"):
        store.load_verified(
            plan.id, now=datetime.fromisoformat(AS_OF).astimezone(UTC) + timedelta(seconds=301)
        )


def test_plan_expiry_cannot_outlive_near_expiry_candidate_evidence():
    config = _config()
    evaluation = datetime.fromisoformat(AS_OF).astimezone(UTC)
    evidence_time = evaluation - timedelta(seconds=299)
    plan = _plan(
        config,
        [_candidate(config, "SPY", evidence_as_of=evidence_time.isoformat())],
    )
    assert datetime.fromisoformat(plan.expires_at) == evaluation + timedelta(seconds=1)


def test_self_consistent_config_cannot_widen_code_level_hard_limits():
    config = _config(maximum_crypto_exposure_pct=2.0)
    with pytest.raises(CrossAssetAllocationError, match="at most 1"):
        _plan(config, [_candidate(config, "SPY")])


def test_candidate_run_and_current_position_must_match_portfolio_authority():
    config = _config()
    with pytest.raises(CrossAssetAllocationError, match="run identity"):
        _plan(config, [_candidate(config, "SPY", run_id="other-run")])
    with pytest.raises(CrossAssetAllocationError, match="current-position"):
        _plan(config, [_candidate(config, "SPY", current_position=True)])


def test_portfolio_symbol_keys_cannot_alias_after_canonicalization():
    config = _config()
    with pytest.raises(CrossAssetAllocationError, match="duplicate canonical key HELD"):
        _plan(
            config,
            [_candidate(config, "SPY")],
            _portfolio(
                config,
                symbol_exposure={"held": "5000", "HELD": "5000"},
            ),
        )


def test_advisory_plan_creates_no_execution_state_and_integrity_is_zero(tmp_path):
    config = _config()
    storage = Storage(tmp_path / "no-execution.sqlite3")
    storage.initialize()
    CrossAssetAllocationStore(storage, config).create(
        run_id="run-cross-asset",
        candidates=[_candidate(config, "SPY")],
        portfolio=_portfolio(config),
        as_of=AS_OF,
    )
    assert storage.fetch_all("SELECT COUNT(*) n FROM trade_proposals")[0]["n"] == 0
    assert storage.fetch_all("SELECT COUNT(*) n FROM order_intents")[0]["n"] == 0
    assert storage.fetch_all("SELECT COUNT(*) n FROM risk_reservations")[0]["n"] == 0
    assert all(value == 0 for value in DurableExecutionStore(storage).integrity_report().values())
    assert (
        storage.fetch_all(
            "SELECT execution_authorized FROM cross_asset_allocation_plans"
        )[0]["execution_authorized"]
        == 0
    )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        storage.execute(
            "UPDATE cross_asset_allocation_plans SET execution_authorized=1"
        )


def test_migrations_are_idempotent(tmp_path):
    storage = Storage(tmp_path / "migration.sqlite3")
    storage.apply_explicit_migrations()
    first = storage.fetch_all(
        "SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
    )
    versions = storage.schema_versions()
    storage.apply_explicit_migrations()
    assert storage.fetch_all(
        "SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
    ) == first
    assert storage.schema_versions() == versions
    assert CROSS_ASSET_ALLOCATION_SCHEMA_VERSION in versions


def test_version_constants_are_bound_to_configuration():
    config = load_config()
    assert (
        config["formula_versions"]["cross_asset_allocation"]
        == CROSS_ASSET_ALLOCATION_FORMULA_VERSION
    )
    assert (
        config["cross_asset_allocation"]["schema_version"]
        == CROSS_ASSET_ALLOCATION_SCHEMA_VERSION
    )
