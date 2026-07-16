from __future__ import annotations

import copy
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from app.formula_versions import (
    CONFIGURATION_SCHEMA_VERSION,
    EVIDENCE_VERSION,
    PROFITABILITY_RANKING_FORMULA_VERSION,
    PROFITABILITY_RANKING_SCHEMA_VERSION,
    STRATEGY_PERFORMANCE_SCHEMA_VERSION,
    STRATEGY_PERFORMANCE_VERSION,
    STRATEGY_POLICY_VERSION,
)
from app.profitability_ranking import (
    CandidateProfitabilityStore,
    ProfitabilityCandidateInput,
    ProfitabilityRankingError,
    calculate_candidate_profitability,
)
from app.approval_authority import authority_envelope
from app.storage import Storage
from app.service import TradingService
from app.strategy_rule_based import Signal
from app.strategy_performance import StrategyRiskPolicy, calculate_metrics
from app.utils import format_proposal_message, load_config


NOW = "2026-07-16T00:00:00+00:00"
CONFIG = load_config("config/config.yaml")
CONFIG_HASH = CONFIG["effective_config_hash"]


def policy(**overrides) -> StrategyRiskPolicy:
    metrics = {
        "gross_sample_count": 100,
        "gross_win_count": 55,
        "gross_loss_count": 45,
        "gross_flat_count": 0,
        "gross_win_rate": 0.55,
        "average_gross_win_r": 2.2,
        "average_gross_loss_r": -1.0,
        "median_absolute_implementation_shortfall_bps": 2.0,
        "average_holding_period_days": 5.0,
        "positive_regime_ratio": 0.75,
        "maximum_drawdown_r": 2.0,
    }
    metrics.update(overrides.pop("metrics", {}))
    values = {
        "strategy_version": "rule_based_v2",
        "state": "ACTIVE",
        "quality_score": 80.0,
        "reason": "current exact authority",
        "performance_snapshot_id": "snapshot-1",
        "enforcement_enabled": True,
        "performance_version": STRATEGY_PERFORMANCE_VERSION,
        "policy_version": STRATEGY_POLICY_VERSION,
        "fingerprint": "snapshot-fingerprint",
        "decided_at": "2026-07-15T23:59:00+00:00",
        "id": "policy-1",
        "schema_version": STRATEGY_PERFORMANCE_SCHEMA_VERSION,
        "metrics": metrics,
        "raw_inputs": {},
        "evidence_version": EVIDENCE_VERSION,
        "configuration_version": CONFIGURATION_SCHEMA_VERSION,
        "config_hash": CONFIG_HASH,
    }
    values.update(overrides)
    return StrategyRiskPolicy(**values)


def candidate(**overrides) -> ProfitabilityCandidateInput:
    values = {
        "candidate_id": "candidate-1",
        "run_id": "run-1",
        "asset_class": "etf",
        "symbol": "SPY",
        "action": "entry",
        "strategy_version": "rule_based_v2",
        "strategy_state": "ACTIVE",
        "setup_type": "trend continuation",
        "market_regime": "us_equities_normal",
        "volatility_regime": "normal",
        "liquidity_regime": "liquid",
        "trend_regime": "strong_uptrend",
        "breadth_regime": "not_available",
        "estimated_at": NOW,
        "quote_at": NOW,
        "quantity": D("10"),
        "entry_estimate": D("100"),
        "stop_price": D("95"),
        "bid_price": D("99.99"),
        "ask_price": D("100.01"),
        "average_dollar_volume": D("50000000"),
        "annualized_volatility": D("0.20"),
        "setup_score": D("88"),
        "symbol_exposure_pct": D("0"),
        "cluster_exposure_pct": D("0"),
        "maximum_symbol_exposure_pct": D("6"),
        "maximum_cluster_exposure_pct": D("15"),
        "performance_snapshot_id": "snapshot-1",
        "policy_decision_id": "policy-1",
        "configuration_version": CONFIGURATION_SCHEMA_VERSION,
        "config_hash": CONFIG_HASH,
        "formula_versions": CONFIG["formula_versions"],
    }
    values.update(overrides)
    return ProfitabilityCandidateInput(**values)


def install_authority(storage: Storage, exact_policy: StrategyRiskPolicy) -> None:
    import json

    storage.execute(
        """INSERT INTO strategy_performance_snapshots(
             id,strategy_version,as_of,performance_version,policy_version,
             schema_version,quality_score,recommendation_state,trade_counts_json,
             metrics_json,components_json,raw_inputs_json,evidence_recency_days,
             attribution_confidence,version_completeness,input_fingerprint,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            exact_policy.performance_snapshot_id,
            exact_policy.strategy_version,
            "2026-07-15T23:58:00+00:00",
            STRATEGY_PERFORMANCE_VERSION,
            STRATEGY_POLICY_VERSION,
            STRATEGY_PERFORMANCE_SCHEMA_VERSION,
            exact_policy.quality_score,
            exact_policy.state,
            "{}",
            json.dumps(exact_policy.metrics, sort_keys=True),
            "{}",
            "{}",
            0,
            1,
            1,
            exact_policy.fingerprint,
            "2026-07-15T23:58:00+00:00",
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
            exact_policy.id,
            exact_policy.strategy_version,
            exact_policy.decided_at,
            exact_policy.performance_snapshot_id,
            exact_policy.state,
            exact_policy.quality_score,
            exact_policy.reason,
            "{}",
            "{}",
            "{}",
            "{}",
            1,
            STRATEGY_PERFORMANCE_VERSION,
            STRATEGY_POLICY_VERSION,
            STRATEGY_PERFORMANCE_SCHEMA_VERSION,
            exact_policy.fingerprint,
            EVIDENCE_VERSION,
            CONFIGURATION_SCHEMA_VERSION,
            CONFIG_HASH,
        ),
    )


def test_strategy_metrics_expose_gross_edge_cost_drag_and_holding_period() -> None:
    from app.strategy_performance import PerformanceObservation

    rows = [
        PerformanceObservation(
            observation_id="1",
            gross_r=2.0,
            r_multiple=1.8,
            entry_session="2026-07-01T00:00:00+00:00",
            exit_session="2026-07-03T00:00:00+00:00",
        ),
        PerformanceObservation(
            observation_id="2",
            gross_r=-1.0,
            r_multiple=-1.1,
            entry_session="2026-07-04T00:00:00+00:00",
            exit_session="2026-07-07T00:00:00+00:00",
        ),
    ]
    metrics, raw = calculate_metrics(rows, as_of=NOW)
    assert metrics["gross_expectancy_r"] == 0.5
    assert metrics["gross_win_count"] == 1
    assert metrics["gross_loss_count"] == 1
    assert metrics["average_gross_win_r"] == 2.0
    assert metrics["average_gross_loss_r"] == -1.0
    assert metrics["average_cost_drag_r"] == pytest.approx(0.15)
    assert metrics["average_holding_period_days"] == 2.5
    assert raw["ordered_gross_r_curve"] == [2.0, -1.0]


def test_candidate_uses_shrunk_probability_and_conservative_lower_bound() -> None:
    decision = calculate_candidate_profitability(candidate(), policy(), CONFIG)
    metrics = decision.economics.metrics
    point = D(metrics["expected_win_probability"])
    lower = D(metrics["conservative_win_probability"])
    assert D("0.50") < point < D("0.55")
    assert D("0") < lower < point
    assert decision.profitability_eligible is True
    assert D(metrics["conservative_expected_net_r"]) > 0


def test_lower_bound_not_point_estimate_controls_profitability() -> None:
    immature = policy(
        metrics={
            "gross_sample_count": 10,
            "gross_win_count": 7,
            "gross_loss_count": 3,
            "average_gross_win_r": 1.2,
            "average_gross_loss_r": -1.0,
        }
    )
    decision = calculate_candidate_profitability(candidate(), immature, CONFIG)
    assert D(decision.economics.metrics["expected_net_r"]) > 0
    assert D(decision.economics.metrics["conservative_expected_net_r"]) < 0
    assert decision.profitability_eligible is False
    assert (
        "uncertainty_adjusted_net_edge_nonpositive_or_below_policy"
        in decision.rejection_reasons
    )


def test_flat_gross_outcomes_reduce_positive_outcome_probability() -> None:
    no_flats = calculate_candidate_profitability(
        candidate(),
        policy(
            metrics={
                "gross_sample_count": 100,
                "gross_win_count": 55,
                "gross_loss_count": 45,
                "gross_flat_count": 0,
            }
        ),
        CONFIG,
    )
    with_flats = calculate_candidate_profitability(
        candidate(),
        policy(
            metrics={
                "gross_sample_count": 120,
                "gross_win_count": 55,
                "gross_loss_count": 45,
                "gross_flat_count": 20,
            }
        ),
        CONFIG,
    )
    assert D(
        with_flats.economics.metrics["expected_win_probability"]
    ) < D(no_flats.economics.metrics["expected_win_probability"])


def test_high_setup_score_cannot_rescue_negative_after_cost_edge() -> None:
    weak = policy(
        metrics={
            "gross_win_count": 45,
            "gross_loss_count": 55,
            "average_gross_win_r": 1.2,
            "average_gross_loss_r": -1.0,
        }
    )
    decision = calculate_candidate_profitability(
        candidate(setup_score=D("99")), weak, CONFIG
    )
    assert decision.profitability_eligible is False
    assert D(decision.ranking_score) == 0
    assert D(decision.profitability_quality_score) >= 0


def test_cost_increase_reduces_net_r_quality_and_rank() -> None:
    cheap = calculate_candidate_profitability(candidate(), policy(), CONFIG)
    expensive = calculate_candidate_profitability(
        candidate(bid_price=D("99.70"), ask_price=D("100.30")),
        policy(),
        CONFIG,
    )
    assert D(expensive.economics.metrics["expected_total_cost"]) > D(
        cheap.economics.metrics["expected_total_cost"]
    )
    assert D(expensive.economics.metrics["expected_net_r"]) < D(
        cheap.economics.metrics["expected_net_r"]
    )
    assert D(expensive.profitability_quality_score) < D(
        cheap.profitability_quality_score
    )


def test_section_31_fee_uses_displayed_target_sale_value() -> None:
    decision = calculate_candidate_profitability(candidate(), policy(), CONFIG)
    target_sale_value = (
        D(decision.economics.metrics["target_price"])
        * D(decision.economics.metrics["proposed_quantity"])
    )
    model = CONFIG["profitability_engine"]["candidate_model"]
    expected = (
        target_sale_value * D(str(model["sec_sell_fee_rate"]))
        + min(
            D(decision.economics.metrics["proposed_quantity"])
            * D(str(model["finra_taf_per_share"])),
            D(str(model["finra_taf_max"])),
        )
        + D(decision.economics.metrics["proposed_quantity"])
        * D(str(model["cat_fee_per_share_per_side"]))
        * D("2")
    )
    assert D(decision.economics.costs["regulatory"]) == expected


def test_nominal_dollar_profit_does_not_dominate_normalized_rank_key() -> None:
    stronger_small = calculate_candidate_profitability(
        candidate(quantity=D("1"), symbol="IWM", candidate_id="small"),
        policy(metrics={"average_gross_win_r": 2.6}),
        CONFIG,
    )
    weaker_large = calculate_candidate_profitability(
        candidate(quantity=D("100"), symbol="SPY", candidate_id="large"),
        policy(metrics={"average_gross_win_r": 1.8}),
        CONFIG,
    )
    assert D(weaker_large.economics.metrics["expected_net_profit"]) > D(
        stronger_small.economics.metrics["expected_net_profit"]
    )
    assert D(
        stronger_small.economics.metrics["conservative_expected_net_r"]
    ) > D(weaker_large.economics.metrics["conservative_expected_net_r"])


def test_portfolio_concentration_reduces_marginal_contribution() -> None:
    diversified = calculate_candidate_profitability(candidate(), policy(), CONFIG)
    concentrated = calculate_candidate_profitability(
        candidate(symbol_exposure_pct=D("5.5"), cluster_exposure_pct=D("14")),
        policy(),
        CONFIG,
    )
    assert D(
        concentrated.economics.metrics["marginal_portfolio_contribution_r"]
    ) < D(
        diversified.economics.metrics["marginal_portfolio_contribution_r"]
    )
    assert D(
        concentrated.quality_components["portfolio_diversification"]
    ) < D(diversified.quality_components["portfolio_diversification"])
    assert abs(
        D(
            diversified.economics.metrics[
                "marginal_portfolio_contribution_r"
            ]
        )
        - D(
            diversified.economics.metrics["conservative_expected_net_r"]
        )
    ) < D("1e-24")


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("quantity", 10.0, "must use Decimal"),
        ("entry_estimate", "NaN", "finite"),
        ("bid_price", D("101"), "ask_price cannot be below"),
        ("quote_at", "2026-07-15T23:59:40+00:00", "stale"),
        ("config_hash", "bad", "SHA-256"),
        ("action", "exit", "unsupported"),
    ],
)
def test_malformed_candidate_inputs_fail_closed(field, value, message) -> None:
    with pytest.raises(ProfitabilityRankingError, match=message):
        calculate_candidate_profitability(
            candidate(**{field: value}), policy(), CONFIG
        )


def test_mismatched_policy_authority_fails_closed() -> None:
    with pytest.raises(ProfitabilityRankingError, match="does not match"):
        calculate_candidate_profitability(
            candidate(policy_decision_id="other"), policy(), CONFIG
        )


def test_deterministic_replay_and_symbol_tie_break_inputs() -> None:
    first = calculate_candidate_profitability(candidate(), policy(), CONFIG)
    second = calculate_candidate_profitability(candidate(), policy(), CONFIG)
    assert first == second
    assert first.decision_fingerprint == second.decision_fingerprint
    assert first.ranking_key == second.ranking_key
    assert first.formula_version == PROFITABILITY_RANKING_FORMULA_VERSION


def test_immutable_persistence_reload_and_tamper_detection(tmp_path) -> None:
    storage = Storage(tmp_path / "ranking.db")
    storage.initialize()
    exact_policy = policy()
    install_authority(storage, exact_policy)
    decision = calculate_candidate_profitability(
        candidate(), exact_policy, CONFIG
    )
    store = CandidateProfitabilityStore(storage)
    assert store.persist(decision) == decision.id
    assert store.persist(decision) == decision.id
    assert store.load_verified(decision.id) == decision
    assert len(storage.fetch_all("SELECT * FROM trade_economics_records")) == 1
    assert (
        len(storage.fetch_all("SELECT * FROM candidate_profitability_decisions"))
        == 1
    )
    storage.execute(
        """UPDATE candidate_profitability_decisions
           SET profitability_quality_score='999' WHERE id=?""",
        (decision.id,),
    )
    with pytest.raises(ProfitabilityRankingError, match="inconsistent"):
        store.load_verified(decision.id)


def test_exact_displayed_candidate_is_atomically_bound_to_proposal(
    tmp_path,
) -> None:
    storage = Storage(tmp_path / "proposal-ranking.db")
    storage.initialize()
    exact_policy = policy()
    install_authority(storage, exact_policy)
    decision = calculate_candidate_profitability(
        candidate(
            proposal_id="proposal-1",
            record_class="proposal_candidate",
            quantity=D("3.125"),
        ),
        exact_policy,
        CONFIG,
    )
    payload = {
        "candidate_id": "candidate-1",
        "config_hash": CONFIG_HASH,
        "formula_versions": CONFIG["formula_versions"],
        "performance_snapshot_id": exact_policy.performance_snapshot_id,
        "policy_decision_id": exact_policy.id,
        **decision.summary(),
    }
    service = TradingService(
        copy.deepcopy(CONFIG), storage, RankingBroker(), "run-1"
    )
    service._persist_proposal_with_profitability(
        """INSERT INTO trade_proposals(
             id,run_id,symbol,side,notional,status,created_at,expires_at,
             strategy_version,payload,performance_snapshot_id,
             policy_decision_id)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "proposal-1",
            "run-1",
            "SPY",
            "buy",
            312.5,
            "pending",
            NOW,
            "2026-07-16T00:10:00+00:00",
            "rule_based_v2",
            json.dumps(payload, sort_keys=True),
            exact_policy.performance_snapshot_id,
            exact_policy.id,
        ),
        decision,
    )

    proposal = storage.fetch_all(
        "SELECT * FROM trade_proposals WHERE id='proposal-1'"
    )[0]
    assert proposal["trade_economics_id"] == decision.economics.id
    assert decision.economics.candidate["proposal_id"] == "proposal-1"
    assert decision.economics.candidate["record_class"] == "proposal_candidate"
    assert decision.economics.metrics["proposed_quantity"] == "3.125"
    assert decision.economics.metrics["proposed_notional"] == "312.5"
    envelope = authority_envelope(proposal)
    assert envelope["trade_economics_id"] == decision.economics.id
    assert envelope["profitability_decision_id"] == decision.id


def test_failed_exact_candidate_binding_rolls_back_all_proposal_state(
    tmp_path,
) -> None:
    storage = Storage(tmp_path / "proposal-ranking-rollback.db")
    storage.initialize()
    exact_policy = policy()
    install_authority(storage, exact_policy)
    decision = calculate_candidate_profitability(
        candidate(
            proposal_id="proposal-1",
            record_class="proposal_candidate",
        ),
        exact_policy,
        CONFIG,
    )
    payload = {
        "candidate_id": "candidate-1",
        "config_hash": CONFIG_HASH,
        "formula_versions": CONFIG["formula_versions"],
        "performance_snapshot_id": exact_policy.performance_snapshot_id,
        "policy_decision_id": exact_policy.id,
        **decision.summary(),
    }
    service = TradingService(
        copy.deepcopy(CONFIG), storage, RankingBroker(), "run-1"
    )
    with pytest.raises(Exception, match="proposal authority"):
        service._persist_proposal_with_profitability(
            """INSERT INTO trade_proposals(
                 id,run_id,symbol,side,notional,status,created_at,expires_at,
                 strategy_version,payload,performance_snapshot_id,
                 policy_decision_id)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "proposal-1",
                "run-1",
                "QQQ",
                "buy",
                1000,
                "pending",
                NOW,
                "2026-07-16T00:10:00+00:00",
                "rule_based_v2",
                json.dumps(payload, sort_keys=True),
                exact_policy.performance_snapshot_id,
                exact_policy.id,
            ),
            decision,
        )
    assert storage.fetch_all("SELECT * FROM trade_proposals") == []
    assert storage.fetch_all("SELECT * FROM trade_economics_records") == []
    assert (
        storage.fetch_all("SELECT * FROM candidate_profitability_decisions")
        == []
    )


def test_stale_durable_strategy_authority_inserts_no_ranking_decision(tmp_path) -> None:
    storage = Storage(tmp_path / "ranking.db")
    storage.initialize()
    exact_policy = policy()
    install_authority(storage, exact_policy)
    decision = calculate_candidate_profitability(
        candidate(), exact_policy, CONFIG
    )
    storage.execute(
        "UPDATE strategy_policy_decisions SET config_hash=? WHERE id=?",
        ("f" * 64, exact_policy.id),
    )
    with pytest.raises(Exception, match="strategy policy authority"):
        CandidateProfitabilityStore(storage).persist(decision)
    assert (
        storage.fetch_all("SELECT * FROM candidate_profitability_decisions") == []
    )


def test_profitability_ranking_migration_is_additive_and_idempotent(tmp_path) -> None:
    storage = Storage(tmp_path / "migration.db")
    storage.initialize()
    storage.apply_explicit_migrations()
    first = storage.fetch_all(
        """SELECT name,sql FROM sqlite_master
           WHERE type IN ('table','index')
             AND (name='candidate_profitability_decisions'
                  OR name LIKE 'idx_profitability_decisions_%')
           ORDER BY name"""
    )
    storage.apply_explicit_migrations()
    second = storage.fetch_all(
        """SELECT name,sql FROM sqlite_master
           WHERE type IN ('table','index')
             AND (name='candidate_profitability_decisions'
                  OR name LIKE 'idx_profitability_decisions_%')
           ORDER BY name"""
    )
    assert first == second
    assert PROFITABILITY_RANKING_SCHEMA_VERSION in storage.schema_versions()
    storage.require_runtime_schema()


class RankingBroker:
    def get_latest_quote(self, symbol):
        now = datetime.now(UTC)
        if symbol == "SPY":
            return {
                "bid_price": 99.85,
                "ask_price": 100.15,
                "timestamp": now,
            }
        return {
            "bid_price": 99.99,
            "ask_price": 100.01,
            "timestamp": now,
        }


def service_candidate(symbol: str, score: float) -> dict:
    signal = Signal(
        "ENTRY",
        "buy",
        symbol,
        "trend continuation",
        0.9,
        {
            "close": 100.0,
            "ma_50": 95.0,
            "ma_200": 90.0,
            "volatility_20": 0.20,
        },
    )
    return {
        "symbol": symbol,
        "signal": signal,
        "signal_id": f"signal-{symbol}",
        "score": score,
        "final_notional": 1000.0,
        "suggested_shares": 10.0,
        "price": 100.0,
        "stop_price": 95.0,
        "stop_distance_dollars": 5.0,
        "stop_risk_dollars": 50.0,
        "risk_value": 50.0,
        "risk_unit": "stop_risk_dollars",
        "vol_20": 0.20,
        "volatility_regime": "normal",
        "average_dollar_volume": 50_000_000.0,
        "performance_snapshot_id": "snapshot-1",
        "policy_decision_id": "policy-1",
        "strategy_state": "ACTIVE",
        "strategy_quality_score": 80.0,
        "is_add": False,
        "is_observation": False,
    }


def service_snapshot() -> dict:
    now = datetime.now(UTC).isoformat()
    return {
        "portfolio_equity": 10_000.0,
        "single_exposures": {},
        "cluster_exposures": {},
        "as_of": now,
        "equity_as_of": now,
    }


def test_service_ranks_verified_after_cost_edge_before_raw_setup_score(
    tmp_path, monkeypatch
) -> None:
    storage = Storage(tmp_path / "service-ranking.db")
    storage.initialize()
    exact_policy = policy()
    install_authority(storage, exact_policy)
    config = copy.deepcopy(CONFIG)
    service = TradingService(config, storage, RankingBroker(), "run-1")
    monkeypatch.setattr(
        service, "_cycle_strategy_policy", lambda strategy: exact_policy
    )
    ranked = service._rank_candidates(
        [
            service_candidate("SPY", 99.0),
            service_candidate("QQQ", 80.0),
        ],
        service_snapshot(),
    )
    assert [row["symbol"] for row in ranked] == ["QQQ", "SPY"]
    assert D(
        ranked[0]["profitability_metrics"]["conservative_expected_net_r"]
    ) > D(ranked[1]["profitability_metrics"]["conservative_expected_net_r"])
    assert ranked[0]["profitability_decision_id"]
    assert len(
        storage.fetch_all("SELECT * FROM candidate_profitability_decisions")
    ) == 2
    assert storage.fetch_all("SELECT * FROM trade_proposals") == []
    assert storage.fetch_all("SELECT * FROM risk_reservations") == []


def test_service_stale_quote_fails_closed_without_candidate_or_order_state(
    tmp_path, monkeypatch
) -> None:
    storage = Storage(tmp_path / "service-stale.db")
    storage.initialize()
    exact_policy = policy()
    install_authority(storage, exact_policy)
    service = TradingService(
        copy.deepcopy(CONFIG), storage, RankingBroker(), "run-1"
    )
    monkeypatch.setattr(
        service, "_cycle_strategy_policy", lambda strategy: exact_policy
    )
    stale = service_candidate("SPY", 99.0)
    stale["profitability_quote"] = {
        "bid_price": 99.99,
        "ask_price": 100.01,
        "timestamp": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
    }
    assert service._rank_candidates([stale], service_snapshot()) == []
    assert storage.fetch_all("SELECT * FROM trade_economics_records") == []
    assert (
        storage.fetch_all("SELECT * FROM candidate_profitability_decisions") == []
    )
    assert storage.fetch_all("SELECT * FROM trade_proposals") == []
    assert storage.fetch_all("SELECT * FROM risk_reservations") == []


def test_service_forged_policy_metrics_cannot_override_durable_authority(
    tmp_path, monkeypatch
) -> None:
    storage = Storage(tmp_path / "service-forged-policy.db")
    storage.initialize()
    exact_policy = policy()
    install_authority(storage, exact_policy)
    forged_metrics = dict(exact_policy.metrics)
    forged_metrics.update(
        {
            "gross_win_count": 99,
            "gross_loss_count": 1,
            "average_gross_win_r": 4.0,
        }
    )
    forged = replace(exact_policy, metrics=forged_metrics)
    service = TradingService(
        copy.deepcopy(CONFIG), storage, RankingBroker(), "run-1"
    )
    monkeypatch.setattr(
        service, "_cycle_strategy_policy", lambda strategy: forged
    )
    with pytest.raises(
        ProfitabilityRankingError,
        match="differs from durable authority",
    ):
        service._candidate_profitability_decision(
            service_candidate("QQQ", 88.0),
            service_snapshot(),
        )
    assert storage.fetch_all("SELECT * FROM trade_economics_records") == []
    assert (
        storage.fetch_all("SELECT * FROM candidate_profitability_decisions")
        == []
    )


def test_service_recomputes_proposal_class_economics_from_final_size(
    tmp_path, monkeypatch
) -> None:
    storage = Storage(tmp_path / "service-final-economics.db")
    storage.initialize()
    exact_policy = policy()
    install_authority(storage, exact_policy)
    service = TradingService(
        copy.deepcopy(CONFIG), storage, RankingBroker(), "run-1"
    )
    monkeypatch.setattr(
        service, "_cycle_strategy_policy", lambda strategy: exact_policy
    )
    exact = service_candidate("QQQ", 88.0)
    exact["suggested_shares"] = 3.125
    exact["final_notional"] = 312.5
    decision = service._calculate_candidate_profitability_decision(
        exact,
        service_snapshot(),
        proposal_id="proposal-final",
        record_class="proposal_candidate",
    )
    assert decision.economics.candidate["proposal_id"] == "proposal-final"
    assert decision.economics.candidate["record_class"] == "proposal_candidate"
    assert decision.economics.metrics["proposed_quantity"] == "3.125"
    assert decision.economics.metrics["proposed_notional"] == "312.5"
    assert D(decision.score_context["diversification_factor"]) < D("1")
    assert storage.fetch_all("SELECT * FROM trade_economics_records") == []
    assert (
        storage.fetch_all("SELECT * FROM candidate_profitability_decisions")
        == []
    )


def test_service_canonicalizes_quantity_without_exceeding_notional_authority() -> None:
    proposal = {
        "side": "buy",
        "action": "entry",
        "proposal_price": 123.45,
        "qty": 8.10044552,
        "notional": 1000.0,
        "stop_distance_dollars": 5.25,
        "approved_quantity_ceiling": 8.10044552,
        "approved_stop_risk_ceiling": 100.0,
        "displayed_adaptive_ceiling": 1000.0,
    }
    TradingService._canonicalize_profitability_display_terms(proposal)
    quantity = D(str(proposal["qty"]))
    price = D(str(proposal["proposal_price"]))
    notional = D(str(proposal["notional"]))
    assert quantity == D("8.10044552")
    assert notional == quantity * price
    assert notional <= D("1000")
    assert D(str(proposal["stop_risk_dollars"])) == quantity * D("5.25")
    assert D(str(proposal["approved_quantity_ceiling"])) == quantity
    assert D(str(proposal["displayed_adaptive_ceiling"])) == notional


def test_profitability_components_are_visible_in_single_and_batch_displays(
    tmp_path,
) -> None:
    decision = calculate_candidate_profitability(candidate(), policy(), CONFIG)
    proposal = {
        "id": "proposal-1",
        "symbol": "SPY",
        "side": "buy",
        "action": "entry",
        "notional": 1000.0,
        "qty": 10.0,
        "expires_at": "2026-07-16T00:10:00+00:00",
        "expiry_minutes": 10,
        "score": 88.0,
        "strategy_state": "ACTIVE",
        "stop_price": 95.0,
        "stop_distance_pct": 5.0,
        "stop_distance_dollars": 5.0,
        "risk_budget": 50.0,
        "reason": "trend continuation",
        "selection_reason": "verified after-cost rank",
        "created_at": NOW,
        "price_change_pct": 0.0,
        "session_change_pct": 0.0,
        "indicators": {"volatility_20": 0.20},
        "gpt_called": False,
        **decision.summary(),
    }
    single = format_proposal_message(proposal, CONFIG)
    assert "Profitability (verified, after costs):" in single
    assert "Conservative net R:" in single
    assert "After-cost break-even win rate:" in single
    assert "The conservative estimate, not nominal dollar profit" in single

    storage = Storage(tmp_path / "display.db")
    storage.initialize()
    service = TradingService(
        copy.deepcopy(CONFIG), storage, RankingBroker(), "run-1"
    )
    batch = service._format_ranked_batch_message(
        [proposal],
        [],
        {
            "total_exposure_pct": 1.0,
            "open_risk_pct": 0.1,
            "buying_power": 9000.0,
            "portfolio_equity": 10000.0,
            "cash": 9000.0,
        },
    )
    assert "Profitability quality:" in batch
    assert "Expected / conservative net R:" in batch
    assert "Expected costs:" in batch


def test_sell_display_remains_independent_of_profitability_engine() -> None:
    proposal = {
        "symbol": "ABBV",
        "side": "sell",
        "action": "exit",
        "qty": 1.0,
        "notional": 100.0,
        "expires_at": "2026-07-16T00:10:00+00:00",
        "expiry_minutes": 10,
        "reason": "TIME_STOP_EXIT",
        "review": {},
    }
    message = format_proposal_message(proposal, CONFIG)
    assert "Profitability (verified, after costs):" not in message
    assert "Paper sell proposal" in message
    assert "Sell ABBV" in message
