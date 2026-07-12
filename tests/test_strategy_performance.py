from __future__ import annotations

import pytest

from app.formula_versions import ACCOUNTING_VERSION, EVIDENCE_VERSION
from app.lot_ledger import LotLedger
from app.research_validation import apply_phase1_schema
from app.storage import Storage
from app.strategy_performance import (
    PerformanceObservation,
    StrategyPerformanceEngine,
    average_loss_r,
    average_win_r,
    calculate_metrics,
    cost_drag_ratio,
    largest_symbol_profit_contribution,
    maximum_drawdown_r,
    payoff_ratio,
    positive_regime_ratio,
    positive_rolling_window_ratio,
    profit_factor,
    recent_expectancy_r,
    regime_metrics,
    score_components,
    shadow_paper_expectancy_divergence,
    top_five_profit_contribution,
    win_rate,
    worst_losing_streak,
)


def _obs(value: float, *, index: int = 0, evidence_class: str = "shadow_oos", symbol: str = "SPY", regime: str = "normal") -> PerformanceObservation:
    return PerformanceObservation(
        observation_id=f"o-{index}-{evidence_class}", source_id=f"source-{index}-{evidence_class}",
        strategy_version="rule_based_v2", symbol=symbol, evidence_class=evidence_class,
        entry_session=f"2026-01-{(index % 9) + 1:02d}T14:00:00+00:00",
        exit_session=f"2026-02-{(index % 9) + 1:02d}T14:00:00+00:00", regime=regime,
        r_multiple=value, gross_r=value + 0.05, net_pnl=value, gross_pnl=value + 0.05,
        evidence_version=EVIDENCE_VERSION, formula_version=ACCOUNTING_VERSION,
        attribution_confidence="shadow_deterministic" if evidence_class == "shadow_oos" else "verified",
    )


def _database(tmp_path) -> Storage:
    db = Storage(tmp_path / "performance.db")
    db.initialize()
    return db


def _engine_config() -> dict:
    return {
        "approved_strategy_versions": ["rule_based_v2"],
        "profitability_engine": {
            "enabled": True, "enforcement_enabled": False,
            "minimum_completed_samples": 100, "minimum_regimes": 2,
            "evidence_stale_after_days": 90,
        },
    }


def test_hand_calculated_expectancy_profit_factor_and_payoff_ratio():
    values = [2.0, -1.0, 1.0, -0.5]
    assert recent_expectancy_r(values, 20) == pytest.approx(0.375)
    assert profit_factor(values) == pytest.approx(2.0)
    assert win_rate(values) == pytest.approx(0.5)
    assert average_win_r(values) == pytest.approx(1.5)
    assert average_loss_r(values) == pytest.approx(-0.75)
    assert payoff_ratio(values) == pytest.approx(2.0)


def test_drawdown_losing_streak_and_rolling_windows():
    values = [1.0, -2.0, -1.0, 1.0, -3.0]
    assert maximum_drawdown_r(values) == pytest.approx(5.0)
    assert worst_losing_streak(values) == 2
    rolling = [1.0] * 20 + [-1.0] * 20
    assert positive_rolling_window_ratio(rolling, 20) == pytest.approx(10 / 21)


def test_regime_execution_and_concentration_divergence_metrics():
    rows = [_obs(1.0, index=0, regime="favorable"), _obs(-0.5, index=1, regime="defensive"), _obs(0.5, index=2, regime="favorable")]
    assert regime_metrics(rows)["favorable"] == {"count": 2, "expectancy_r": 0.75}
    assert positive_regime_ratio(rows) == pytest.approx(0.5)
    assert cost_drag_ratio(rows) == pytest.approx(0.15 / 1.15)
    assert top_five_profit_contribution(rows) == pytest.approx(1.5 / 1.5)
    assert largest_symbol_profit_contribution(rows) == pytest.approx(1.0)
    paper = [_obs(-0.25, index=3, evidence_class="actual_paper", symbol="QQQ")]
    assert shadow_paper_expectancy_divergence([*rows, *paper]) == pytest.approx(abs((1.0 - 0.5 + 0.5) / 3.0 - (-0.25)))
    execution = [
        {"submitted": True, "filled": True, "implementation_shortfall_bps": -4.0},
        {"submitted": True, "filled": False, "implementation_shortfall_bps": 12.0},
    ]
    metrics, _ = calculate_metrics(rows, execution_rows=execution, as_of="2026-02-10T00:00:00+00:00")
    assert metrics["submitted_order_fill_rate"] == pytest.approx(0.5)
    assert metrics["median_absolute_implementation_shortfall_bps"] == pytest.approx(8.0)


def test_exact_component_weights_and_score_penalties():
    metrics = {
        "expectancy_r": 0.25, "profit_factor": 1.75, "win_rate": 1.0,
        "maximum_drawdown_r": 0.0, "worst_losing_streak": 0,
        "recent_20_trade_expectancy_r": 0.25, "positive_rolling_20_window_ratio": 1.0,
        "positive_regime_ratio": 1.0, "worst_regime_expectancy_r": 0.25,
        "submitted_order_fill_rate": 1.0, "median_absolute_implementation_shortfall_bps": 0.0,
        "cost_drag_ratio": 0.0, "sample_count": 100, "evidence_recency_days": 0.0,
        "attribution_confidence": 1.0, "version_completeness": 1.0,
        "top_five_profit_contribution": 1.0, "largest_symbol_profit_contribution": 1.0,
        "shadow_paper_expectancy_divergence_r": 0.5,
    }
    components, quality, penalties = score_components(metrics, {"minimum_completed_samples": 100, "evidence_stale_after_days": 90})
    assert components == {"profitability": 30.0, "downside": 20.0, "stability": 15.0, "regime": 15.0, "execution": 10.0, "evidence": 10.0}
    assert penalties == {"concentration": 10.0, "divergence": 10.0}
    assert quality == pytest.approx(80.0)


@pytest.mark.parametrize("quality,expected", [(44.999, "RESEARCH_ONLY"), (45.0, "EXPLORATION"), (60.0, "THROTTLED"), (75.0, "ACTIVE")])
def test_quality_score_boundaries(tmp_path, monkeypatch, quality, expected):
    db = _database(tmp_path)
    observations = [_obs(0.5, index=i, regime="favorable" if i % 2 else "normal") for i in range(100)]
    monkeypatch.setattr("app.strategy_performance.score_components", lambda metrics, settings: ({"profitability": quality}, quality, {"concentration": 0.0, "divergence": 0.0}))
    engine = StrategyPerformanceEngine(db, _engine_config(), as_of="2026-02-10T00:00:00+00:00")
    monkeypatch.setattr(engine, "_shadow_observations", lambda: observations)
    monkeypatch.setattr(engine, "_actual_observations", lambda: [])
    assert engine.refresh_strategy("rule_based_v2").recommendation_state == expected


@pytest.mark.parametrize("count,expected", [(1, "RESEARCH_ONLY"), (20, "EXPLORATION"), (50, "THROTTLED"), (100, "ACTIVE")])
def test_maturity_state_ceilings(tmp_path, monkeypatch, count, expected):
    db = _database(tmp_path)
    observations = [_obs(0.5, index=i, regime="favorable" if i % 2 else "normal") for i in range(count)]
    monkeypatch.setattr("app.strategy_performance.score_components", lambda metrics, settings: ({"profitability": 100.0}, 100.0, {"concentration": 0.0, "divergence": 0.0}))
    engine = StrategyPerformanceEngine(db, _engine_config(), as_of="2026-02-10T00:00:00+00:00")
    monkeypatch.setattr(engine, "_shadow_observations", lambda: observations)
    monkeypatch.setattr(engine, "_actual_observations", lambda: [])
    assert engine.refresh_strategy("rule_based_v2").recommendation_state == expected


def test_fifo_partial_complete_sells_allocate_each_lot_and_count_one_lifecycle(tmp_path):
    db = _database(tmp_path)
    db.execute("INSERT INTO position_lifecycles(id,symbol,side,state,opened_at,closed_at,opening_quantity,current_quantity,source,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", ("lc", "SPY", "long", "closed", "2026-01-01T14:00:00+00:00", "2026-01-03T14:00:00+00:00", 3, 0, "test", "2026-01-01T14:00:00+00:00", "2026-01-03T14:00:00+00:00"))
    buy_a = {"id": "buy-a", "symbol": "SPY", "side": "buy", "position_lifecycle_id": "lc", "strategy_version": "rule_based_v2", "entry_regime": "normal", "entry_score": 71, "initial_risk_dollars": 4, "evidence_version": EVIDENCE_VERSION, "formula_version": ACCOUNTING_VERSION}
    buy_b = {**buy_a, "id": "buy-b", "initial_risk_dollars": 2}
    sell = {"id": "sell", "symbol": "SPY", "side": "sell", "position_lifecycle_id": "lc"}
    with db.connect() as conn:
        LotLedger.apply_fill_in_transaction(conn, intent=buy_a, broker_event_key="ba", delta_quantity=2, fill_price=10, occurred_at="2026-01-01T14:00:00+00:00", fees=0.20)
        LotLedger.apply_fill_in_transaction(conn, intent=buy_b, broker_event_key="bb", delta_quantity=1, fill_price=12, occurred_at="2026-01-02T14:00:00+00:00", fees=0.10)
        LotLedger.apply_fill_in_transaction(conn, intent=sell, broker_event_key="s1", delta_quantity=1, fill_price=11, occurred_at="2026-01-02T15:00:00+00:00", fees=0.10)
        LotLedger.apply_fill_in_transaction(conn, intent=sell, broker_event_key="s2", delta_quantity=2, fill_price=13, occurred_at="2026-01-03T14:00:00+00:00", fees=0.20)
    consumptions = db.fetch_all("SELECT * FROM lot_consumptions ORDER BY occurred_at,id")
    assert len(consumptions) == 3
    assert sum(row["allocated_buy_fees"] for row in consumptions) == pytest.approx(0.30)
    assert sum(row["allocated_sell_fees"] for row in consumptions) == pytest.approx(0.30)
    snapshot = StrategyPerformanceEngine(db, _engine_config(), as_of="2026-01-04T00:00:00+00:00").refresh_strategy("rule_based_v2")
    assert snapshot.metrics["trade_counts"] == {"shadow_oos": 0, "actual_paper": 1, "total": 1}
    assert db.fetch_all("SELECT COUNT(*) n FROM strategy_trade_records WHERE evidence_class='actual_paper' AND attribution_status='complete'")[0]["n"] == 1


def test_mixed_strategy_attribution_is_unavailable_and_excluded(tmp_path):
    db = _database(tmp_path)
    db.execute("INSERT INTO position_lifecycles(id,symbol,side,state,opened_at,closed_at,opening_quantity,current_quantity,source,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", ("mixed", "SPY", "long", "closed", "2026-01-01T14:00:00+00:00", "2026-01-03T14:00:00+00:00", 2, 0, "test", "2026-01-01T14:00:00+00:00", "2026-01-03T14:00:00+00:00"))
    for lot_id, strategy in (("l1", "rule_based_v2"), ("l2", "other_strategy")):
        db.execute("INSERT INTO position_lots(id,symbol,position_lifecycle_id,source_fill_event_key,opened_at,original_quantity,remaining_quantity,unit_cost,fees_allocated,source,provenance,confidence,strategy_version,initial_risk_dollars,evidence_version,formula_version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (lot_id, "SPY", "mixed", lot_id, "2026-01-01T14:00:00+00:00", 1, 0, 10, 0, "test", "test", "verified", strategy, 1, EVIDENCE_VERSION, ACCOUNTING_VERSION, "2026-01-01T14:00:00+00:00", "2026-01-01T14:00:00+00:00"))
        db.execute("INSERT INTO lot_consumptions(id,broker_event_key,sell_intent_id,position_lifecycle_id,lot_id,strategy_version,quantity,allocated_proceeds,allocated_cost_basis,allocated_buy_fees,allocated_sell_fees,realized_pnl,occurred_at,confidence,accounting_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (f"c-{lot_id}", f"sell-{lot_id}", "sell", "mixed", lot_id, strategy, 1, 12, 10, 0, 0, 2, "2026-01-03T14:00:00+00:00", "verified", ACCOUNTING_VERSION))
    snapshot = StrategyPerformanceEngine(db, _engine_config(), as_of="2026-01-04T00:00:00+00:00").refresh_strategy("rule_based_v2")
    assert snapshot.metrics["sample_count"] == 0
    assert db.fetch_all("SELECT attribution_status,reason FROM strategy_trade_records WHERE source_id='mixed'")[0]["attribution_status"] == "unavailable"


def test_shadow_filters_fixed_horizon_and_mixed_versions(tmp_path):
    db = _database(tmp_path)
    with db.connect() as conn:
        apply_phase1_schema(conn)
    db.execute("INSERT INTO research_opportunities(id,source_table,source_id,symbol,observed_at,direction,execution_type,entry_price,strategy_version,feature_version,universe_version,regime_version,eligibility_version,regime,split_label,provenance_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("opp1", "shadow_insights", "si1", "SPY", "2026-01-01T14:00:00+00:00", "long", "shadow_hypothetical", 100, "rule_based_v2", "f", "u", "r", "e", "normal", "out_of_sample", "{}", "2026-01-01T14:00:00+00:00"))
    db.execute("INSERT INTO research_opportunities(id,source_table,source_id,symbol,observed_at,direction,execution_type,entry_price,strategy_version,feature_version,universe_version,regime_version,eligibility_version,regime,split_label,provenance_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("opp2", "shadow_insights", "si2", "QQQ", "2026-01-01T14:00:00+00:00", "long", "shadow_hypothetical", 100, "rule_based_v2", "f", "u", "r", "e", "normal", "out_of_sample", "{}", "2026-01-01T14:00:00+00:00"))
    common = (EVIDENCE_VERSION, "fp", "2026-02-01T14:00:00+00:00")
    db.execute("INSERT INTO research_outcomes(id,opportunity_id,horizon_sessions,status,reason,maturity_session,exit_session,outcome_class,trade_path_gross_return,trade_path_cost_adjusted_return,gross_r_multiple,cost_adjusted_r_multiple,cost_model_version,cost_bps,calculation_version,input_fingerprint,calculated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("out1", "opp1", 20, "completed", "path", "2026-02-01", "2026-02-01T14:00:00+00:00", "executable_trade_path", .1, .08, .5, .4, "cost", 8, *common))
    db.execute("INSERT INTO research_outcomes(id,opportunity_id,horizon_sessions,status,reason,maturity_session,exit_session,outcome_class,trade_path_gross_return,trade_path_cost_adjusted_return,gross_r_multiple,cost_adjusted_r_multiple,cost_model_version,cost_bps,calculation_version,input_fingerprint,calculated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("out2", "opp2", 20, "completed", "fixed", "2026-02-01", "2026-02-01T14:00:00+00:00", "fixed_horizon_observation", .1, .08, .5, .4, "cost", 8, "old-version", "fp2", "2026-02-01T14:00:00+00:00"))
    snapshot = StrategyPerformanceEngine(db, _engine_config(), as_of="2026-02-02T00:00:00+00:00").refresh_strategy("rule_based_v2")
    assert snapshot.metrics["trade_counts"] == {"shadow_oos": 1, "actual_paper": 0, "total": 1}


def test_deterministic_replay_and_report_only_invariance(tmp_path):
    db = _database(tmp_path)
    rows_before = {table: db.fetch_all(f"SELECT COUNT(*) n FROM {table}")[0]["n"] for table in ("position_sizing_decisions", "trade_proposals", "approvals", "order_intents", "risk_reservations", "orders")}
    engine = StrategyPerformanceEngine(db, _engine_config(), as_of="2026-02-02T00:00:00+00:00")
    first = engine.refresh_strategy("rule_based_v2")
    second = engine.refresh_strategy("rule_based_v2")
    rows_after = {table: db.fetch_all(f"SELECT COUNT(*) n FROM {table}")[0]["n"] for table in rows_before}
    assert first.fingerprint == second.fingerprint
    assert rows_before == rows_after
    assert engine.latest_policy("rule_based_v2").enforcement_enabled is False
    assert "Enforcement: disabled" in engine.format_report("rule_based_v2")


def test_additive_migration_is_idempotent_and_required_for_runtime(tmp_path):
    db = _database(tmp_path)
    with pytest.raises(RuntimeError, match="Database migration required"):
        db.require_runtime_schema()
    db.apply_explicit_migrations()
    versions = db.schema_versions()
    assert "strategy_profitability_engine_v1" in versions
    schema = db.fetch_all("SELECT name,sql FROM sqlite_master WHERE type='table' AND name IN ('lot_consumptions','strategy_trade_records','strategy_performance_snapshots','strategy_policy_decisions') ORDER BY name")
    db.apply_explicit_migrations()
    assert schema == db.fetch_all("SELECT name,sql FROM sqlite_master WHERE type='table' AND name IN ('lot_consumptions','strategy_trade_records','strategy_performance_snapshots','strategy_policy_decisions') ORDER BY name")
    db.require_runtime_schema()
