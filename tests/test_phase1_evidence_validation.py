from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pytest

from app.research_validation import (
    BoundedBackfill,
    CanonicalOutcomeCalculator,
    CostModel,
    ExchangeSessions,
    Opportunity,
    PointInTimeSimulator,
    ResearchRepository,
    deterministic_regime,
    import_legacy_opportunities,
    render_evidence_report,
    score_calibration,
    update_service_outcomes,
    walk_forward_folds,
)
from app.storage import Storage


def bars(start: str, closes: list[float], *, highs=None, lows=None) -> pd.DataFrame:
    index = pd.bdate_range(start, periods=len(closes), tz="UTC")
    return pd.DataFrame(
        {
            "open": closes,
            "high": highs or [v + 1 for v in closes],
            "low": lows or [v - 1 for v in closes],
            "close": closes,
            "volume": [1_000_000] * len(closes),
        },
        index=index,
    )


def opportunity(**overrides) -> Opportunity:
    values = {
        "id": "opp-1",
        "symbol": "QQQ",
        "observed_at": datetime(2026, 1, 2, 15, tzinfo=UTC),
        "entry_price": 100.0,
        "direction": "long",
        "execution_type": "shadow_hypothetical",
        "strategy_version": "rule_based_v1",
        "stop_price": 95.0,
        "target_price": 110.0,
        "benchmark_entry_price": 500.0,
        "universe_version": "fixture-v1",
    }
    values.update(overrides)
    return Opportunity(**values)


def calculator() -> CanonicalOutcomeCalculator:
    return CanonicalOutcomeCalculator(
        ExchangeSessions(),
        CostModel("cost-v1", spread_bps=4, entry_slippage_bps=2, exit_slippage_bps=2, source="synthetic fixture"),
    )


def test_exchange_horizons_skip_weekends_and_holidays():
    calendar = ExchangeSessions()
    assert calendar.add_sessions(date(2026, 1, 16), 1) == date(2026, 1, 20)  # MLK Monday skipped
    assert calendar.add_sessions(date(2026, 4, 2), 1) == date(2026, 4, 6)  # Good Friday skipped


def test_maturing_is_based_on_exchange_sessions_not_calendar_days():
    result = calculator().calculate(
        opportunity(observed_at=datetime(2026, 1, 16, tzinfo=UTC)),
        bars("2026-01-20", [101]),
        bars("2026-01-20", [501]),
        as_of=datetime(2026, 1, 19, 23, tzinfo=UTC),
        horizons=(1,),
    )[0]
    assert result.status == "maturing"
    assert result.maturity_session == "2026-01-20"


def test_completed_outcome_has_cost_relative_excursions_and_r_multiple():
    asset = bars("2026-01-05", [102, 104, 106, 108, 109])
    spy = bars("2026-01-05", [500, 501, 502, 503, 504])
    result = calculator().calculate(opportunity(target_price=120.0), asset, spy, as_of=datetime(2026, 2, 1, tzinfo=UTC), horizons=(5,))[0]
    assert result.status == "completed"
    assert result.gross_return == pytest.approx(0.09)
    assert result.cost_adjusted_return == pytest.approx(0.0892)
    assert result.spy_relative_return is not None
    assert result.mfe is not None and result.mae is not None
    assert result.gross_r_multiple == pytest.approx(1.8)
    assert result.cost_bps == 8


def test_same_bar_stop_and_target_uses_conservative_ordering():
    asset = bars("2026-01-05", [100], highs=[111], lows=[94])
    result = calculator().calculate(opportunity(), asset, bars("2026-01-05", [500]), as_of=datetime(2026, 2, 1, tzinfo=UTC), horizons=(1,))[0]
    assert result.first_barrier == "stop"
    assert result.ordering_quality == "ambiguous_same_daily_bar_conservative_stop_first"
    assert result.stop_hit is True and result.target_hit is True
    assert result.gross_return == pytest.approx(-0.05)


def test_unknown_entry_and_missing_mature_bars_are_explicitly_unavailable():
    invalid = calculator().calculate(opportunity(entry_price=None), pd.DataFrame(), pd.DataFrame(), as_of=datetime(2026, 2, 1, tzinfo=UTC), horizons=(1,))[0]
    missing = calculator().calculate(opportunity(), pd.DataFrame(), pd.DataFrame(), as_of=datetime(2026, 2, 1, tzinfo=UTC), horizons=(1,))[0]
    assert (invalid.status, invalid.reason) == ("unavailable", "missing_or_invalid_entry_price")
    assert (missing.status, missing.reason) == ("unavailable", "asset_session_bars_missing")
    assert missing.gross_return is None


def test_regime_attribution_is_deterministic_and_past_only():
    history = bars("2025-01-01", list(range(100, 310)))
    assert deterministic_regime(history) == deterministic_regime(history.copy())
    future_changed = pd.concat([history, bars("2026-01-01", [1, 1, 1])])
    assert deterministic_regime(history) != deterministic_regime(future_changed)


def test_walk_forward_purge_and_embargo_prevent_overlap():
    rows = [{"id": str(i), "observed_at": datetime(2025, 1, 1, tzinfo=UTC) + timedelta(days=i)} for i in range(120)]
    folds = walk_forward_folds(rows, train_sessions=50, test_sessions=10, purge_sessions=20, embargo_sessions=2)
    assert folds
    for fold in folds:
        assert set(fold.train_ids).isdisjoint(fold.test_ids)
        assert datetime.fromisoformat(fold.test_start).date() > datetime.fromisoformat(fold.train_end).date() + timedelta(days=20)


def test_point_in_time_simulator_reuses_production_strategy(monkeypatch):
    seen_lengths: list[int] = []

    def fake_evaluate(symbol, history, **kwargs):
        from app.strategy_rule_based import Signal
        seen_lengths.append(len(history))
        return Signal("ENTRY", "buy", symbol, "fixture", 0.7, {"close": float(history.iloc[-1]["close"])})

    monkeypatch.setattr("app.research_validation.evaluate_symbol", fake_evaluate)
    frame = bars("2025-01-01", [100 + i / 10 for i in range(205)])
    results = PointInTimeSimulator(200).opportunities("QQQ", frame, frame, lambda symbol, day: (True, "universe-asof-v1"))
    assert len(results) == 5
    assert seen_lengths == [200, 201, 202, 203, 204]
    assert results[0]["entry_price"] == float(frame.iloc[200]["open"])


def test_schema_and_backfill_are_idempotent_resumable_and_duplicate_free(tmp_path):
    storage = Storage(tmp_path / "clone.db")
    storage.initialize()
    repository = ResearchRepository(storage.path)
    repository.migrate()
    repository.migrate()
    opps = [opportunity(id=f"opp-{i}", source_table="fixture", source_id=str(i)) for i in range(3)]
    for opp in opps:
        repository.upsert_opportunity(opp, provenance={"fixture": True})
    asset = bars("2026-01-05", [101] * 25)
    spy = bars("2026-01-05", [501] * 25)
    loader = lambda symbol: spy if symbol == "SPY" else asset
    runner = BoundedBackfill(repository, calculator(), loader, as_of=datetime(2026, 3, 1, tzinfo=UTC))
    first = runner.run(opps, limit=2, job_key="fixture")
    second = runner.run(opps, limit=2, job_key="fixture")
    third = runner.run(opps, limit=2, job_key="fixture")
    assert first["status"] == "partial"
    assert second["status"] == "completed"
    assert third["status"] == "completed"
    with repository.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM research_outcomes").fetchone()[0] == 9
        assert conn.execute("SELECT COUNT(*) FROM research_backfill_jobs").fetchone()[0] == 1


def test_legacy_import_keeps_actual_blocked_observation_and_shadow_distinct(tmp_path):
    storage = Storage(tmp_path / "clone.db")
    storage.initialize()
    now = datetime(2026, 1, 2, tzinfo=UTC).isoformat()
    base = (now, "run", "QQQ", "equity", "paper_tradable", "entry", "proposed", 1, "p1", 90.0, 100.0, now, now)
    storage.execute(
        """INSERT INTO performance_setups(id,timestamp,run_id,symbol,asset_class,tier,setup_type,action_decision,proposed,proposal_id,score,current_price,created_at,updated_at)
           VALUES('actual',?,?,?,?,?,?,?,?,?,?,?,?,?)""", base,
    )
    storage.execute("UPDATE performance_setups SET fill_id='fill-1',fill_price=101 WHERE id='actual'")
    for setup_id, tier, reason in (("blocked", "paper_tradable", "risk"), ("observe", "observation", None), ("shadow", "paper_tradable", None)):
        storage.execute(
            """INSERT INTO performance_setups(id,timestamp,run_id,symbol,asset_class,tier,setup_type,action_decision,proposed,score,current_price,not_proposed_reason,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,0,?,?,?, ?,?)""",
            (setup_id, now, "run", "QQQ", "equity", tier, "entry", "shadow_only", 80.0, 100.0, reason, now, now),
        )
        storage.execute(
            "INSERT INTO performance_outcomes(id,setup_id,run_id,symbol,actual_or_shadow,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (f"po-{setup_id}", setup_id, "run", "QQQ", "shadow", "pending_forward_returns", now, now),
        )
    repository = ResearchRepository(storage.path)
    imported = import_legacy_opportunities(storage, repository)
    kinds = {o.source_id: o.execution_type for o in imported if o.source_table == "performance_setups"}
    assert kinds == {"actual": "actual_fill", "blocked": "blocked_hypothetical", "observe": "observation_only", "shadow": "shadow_hypothetical"}


def test_score_calibration_and_report_always_show_sample_sizes():
    rows = [
        {"status": "completed", "split_label": "out_of_sample", "score": 90, "cost_adjusted_return": 0.01, "strategy_version": "v1", "regime": "up", "execution_type": "shadow"},
        {"status": "completed", "split_label": "out_of_sample", "score": 90, "cost_adjusted_return": -0.01, "strategy_version": "v1", "regime": "up", "execution_type": "shadow"},
    ]
    band = next(r for r in score_calibration(rows) if r["score_band"] == "90-100")
    assert band["n"] == 2 and band["observed_win_rate"] == 0.5
    report = render_evidence_report(
        rows,
        as_of=datetime(2026, 1, 1, tzinfo=UTC),
        cost_model=CostModel("v1", 4, 2, 2, source="fixture"),
        limitations=["fixture limitation"],
    )
    assert "| v1 | up | shadow | 2 |" in report
    assert "Strategy support after costs: **inconclusive**" in report


def test_full_day_future_revisions_do_not_change_one_session_label():
    first = bars("2026-01-05", [101])
    revised_future = pd.concat([first, bars("2026-01-06", [1, 500, 2])])
    spy = bars("2026-01-05", [500, 501, 502, 503])
    one = calculator().calculate(opportunity(), first, spy, as_of=datetime(2026, 2, 1, tzinfo=UTC), horizons=(1,))[0]
    two = calculator().calculate(opportunity(), revised_future, spy, as_of=datetime(2026, 2, 1, tzinfo=UTC), horizons=(1,))[0]
    assert one.gross_return == two.gross_return


def test_runtime_outcomes_use_current_run_and_cached_bars_without_provider_calls(tmp_path):
    storage = Storage(tmp_path / "runtime-clone.db")
    storage.initialize()
    observed = datetime(2026, 1, 2, 15, tzinfo=UTC).isoformat()
    for setup_id, run_id in (("current", "run-current"), ("historical", "run-old")):
        storage.execute(
            """INSERT INTO performance_setups(
               id,timestamp,run_id,symbol,asset_class,tier,setup_type,action_decision,
               proposed,score,current_price,signal_state,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                setup_id, observed, run_id, "QQQ", "equity", "paper_tradable", "entry",
                "shadow_only", 0, 80.0, 100.0,
                '{"side":"buy","strategy_version":"rule_based_v1","stop_price":95}',
                observed, observed,
            ),
        )

    class ForbiddenBroker:
        def get_historical_bars(self, *args, **kwargs):
            raise AssertionError("runtime Phase 1 must reuse scanner bars")

    qqq = bars("2026-01-05", [101 + i for i in range(25)])
    spy = bars("2026-01-05", [501 + i for i in range(25)])
    result = update_service_outcomes(
        storage,
        ForbiddenBroker(),
        now=datetime(2026, 2, 20, tzinfo=UTC),
        max_updates=25,
        run_id="run-current",
        bar_cache={"QQQ": qqq, "SPY": spy},
    )

    assert result["provider_calls"] == 0
    assert storage.fetch_all("SELECT source_id FROM research_opportunities") == [{"source_id": "current"}]
    outcomes = storage.fetch_all("SELECT status FROM research_outcomes")
    assert len(outcomes) == 3
    assert {row["status"] for row in outcomes} == {"completed"}
