from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from app.position_risk import PositionRiskInput
from app.storage import Storage
from app.trend_management import TrendManagementEngine, TrendManagementInput
from app.winner_expansion import (
    MilestoneIdentity,
    WinnerExpansionEngine,
    WinnerExpansionInput,
    WinnerExpansionStore,
    apply_winner_expansion_schema,
)


def _trend(**overrides):
    values = {
        "symbol": "QQQ",
        "position_lifecycle_id": "life-qqq-1",
        "current_price": 110.0,
        "average_entry_price": 100.0,
        "highest_price_since_entry": 116.0,
        "current_protective_stop": 100.0,
        "atr": 2.0,
        "current_r_multiple": 2.0,
        "peak_r_multiple": 2.0,
        "trend_strength": 90.0,
        "price_above_ma50": True,
        "ma50_above_ma200": True,
        "higher_highs_and_lows": True,
        "market_regime": "favorable",
        "volatility_regime": "normal",
        "deployment_mode": "OPPORTUNISTIC",
        "execution_quality": 0.90,
        "account_health": 0.90,
        "account_drawdown_pct": 0.5,
        "position_age_days": 5.0,
        "as_of": "2026-07-14T01:00:00+00:00",
    }
    values.update(overrides)
    return TrendManagementEngine().evaluate(TrendManagementInput(**values))


def _milestone(**overrides):
    values = {
        "symbol": "QQQ",
        "position_lifecycle_id": "life-qqq-1",
        "current_r_multiple": 2.0,
        "price_advance_since_prior_entry_pct": 6.0,
        "stop_advance_r": 1.0,
        "prior_filled_adds": 0,
        "trend_mode": "TREND_PYRAMID",
    }
    values.update(overrides)
    return MilestoneIdentity.build(**values)


def _winner_input(**overrides):
    trend = overrides.pop("trend_decision", _trend())
    risk = overrides.pop(
        "risk_input",
        PositionRiskInput(
            symbol="QQQ",
            position_lifecycle_id="life-qqq-1",
            deployment_mode="OPPORTUNISTIC",
            current_shares=10.0,
            proposed_add_shares=5.0,
            current_market_price=110.0,
            proposed_add_price=110.0,
            current_protective_stop=100.0,
            proposed_tightened_stop=trend.protective_stop,
            portfolio_equity=100_000.0,
            mode_incremental_risk_allowance_pct=0.05,
        ),
    )
    values = {
        "risk_input": risk,
        "trend_decision": trend,
        "milestone": _milestone(),
        "average_entry_price": 100.0,
        "strategy_state": "ACTIVE",
        "policy_adds_allowed": True,
        "regime_supports_add": True,
        "setup_score": 90.0,
        "minimum_setup_score": 85.0,
        "score_improvement": 5.0,
        "minimum_score_improvement": 0.0,
        "exit_or_deterioration_warning": False,
        "reconciliation_ok": True,
        "integrity_ok": True,
        "quote_current": True,
        "spread_ok": True,
        "liquidity_ok": True,
        "stop_current": True,
        "milestone_available": True,
        "adaptive_conviction_authorized": True,
        "adaptive_sizing_authorized": True,
        "phase3_validation_passed": True,
        "phase3_validation_stage": "proposal",
    }
    values.update(overrides)
    return WinnerExpansionInput(**values)


def _storage(tmp_path) -> Storage:
    storage = Storage(tmp_path / "phase42c.sqlite3")
    storage.initialize()
    with storage.connect() as conn:
        apply_winner_expansion_schema(conn)
    return storage


def test_winner_expansion_requires_all_policy_trend_milestone_and_risk_gates():
    decision = WinnerExpansionEngine().evaluate(_winner_input())

    assert decision.eligible is True
    assert decision.trend_mode == "TREND_PYRAMID"
    assert decision.pre_add_open_risk == pytest.approx(100.0)
    assert decision.post_add_open_risk < decision.pre_add_open_risk
    assert decision.incremental_risk < 0
    assert decision.required_protective_stop == pytest.approx(decision.risk_decision.proposed_tightened_stop)
    assert decision.proposed_add_notional == pytest.approx(550.0)


def test_normal_mode_authorizes_only_a_risk_neutral_winner_add():
    trend = _trend(deployment_mode="NORMAL")
    risk = replace(
        _winner_input().risk_input,
        deployment_mode="NORMAL",
        proposed_tightened_stop=trend.protective_stop,
        mode_incremental_risk_allowance_pct=0.0,
    )

    decision = WinnerExpansionEngine().evaluate(
        _winner_input(trend_decision=trend, risk_input=risk)
    )

    assert decision.eligible is True
    assert decision.deployment_mode == "NORMAL"
    assert decision.incremental_risk <= 0.0
    assert decision.risk_decision.consumed_risk == pytest.approx(0.0)


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"strategy_state": "PROBE"}, "PROBE"),
        ({"milestone_available": False}, "milestone"),
        ({"stop_current": False}, "protective stop"),
        ({"adaptive_conviction_authorized": False}, "Adaptive Conviction"),
        ({"phase3_validation_passed": False}, "Phase 3"),
    ],
)
def test_winner_expansion_fail_closed_gates(updates, message):
    decision = WinnerExpansionEngine().evaluate(_winner_input(**updates))
    assert decision.eligible is False
    assert message in " ".join(decision.blocking_reasons)


def test_averaging_down_and_inconsistent_stop_are_blocked():
    base = _winner_input()
    averaging_down = replace(base.risk_input, proposed_add_price=99.0)
    inconsistent_stop = replace(base.risk_input, proposed_tightened_stop=base.risk_input.proposed_tightened_stop - 1.0)

    down_decision = WinnerExpansionEngine().evaluate(_winner_input(risk_input=averaging_down))
    stop_decision = WinnerExpansionEngine().evaluate(_winner_input(risk_input=inconsistent_stop))

    assert down_decision.eligible is False
    assert "average down" in " ".join(down_decision.blocking_reasons)
    assert stop_decision.eligible is False
    assert "disagree" in " ".join(stop_decision.blocking_reasons)


def test_milestone_claim_is_deduplicated_across_restart_and_fill_is_permanent(tmp_path):
    storage = _storage(tmp_path)
    identity = _milestone()
    store = WinnerExpansionStore(storage)
    first = store.claim_milestone(identity, run_id="run-1", proposal_id="proposal-1")
    duplicate = WinnerExpansionStore(storage).claim_milestone(
        identity, run_id="run-2", proposal_id="proposal-2"
    )

    assert first.accepted is True
    assert duplicate.accepted is False
    assert duplicate.milestone_id == first.milestone_id
    assert duplicate.reason == "milestone already has an active ADD action"

    store.transition_milestone(identity.position_lifecycle_id, identity.milestone_key, "APPROVED", approval_id="approval-1")
    store.transition_milestone(identity.position_lifecycle_id, identity.milestone_key, "SUBMITTED", intent_id="intent-1")
    store.transition_milestone(identity.position_lifecycle_id, identity.milestone_key, "PARTIALLY_FILLED", order_id="order-1")
    filled = store.transition_milestone(identity.position_lifecycle_id, identity.milestone_key, "FILLED")
    after_fill = WinnerExpansionStore(storage).claim_milestone(
        identity, run_id="run-3", proposal_id="proposal-3", allow_retry=True, max_retries=3
    )

    assert filled["status"] == "FILLED"
    assert filled["completed_at"] is not None
    assert after_fill.accepted is False
    assert "permanently consumed" in after_fill.reason
    assert len(storage.fetch_all("SELECT * FROM pyramiding_milestones")) == 1


def test_rejected_milestone_retry_requires_authority_limit_and_cooldown(tmp_path):
    storage = _storage(tmp_path)
    identity = _milestone()
    store = WinnerExpansionStore(storage)
    start = datetime(2026, 7, 14, 1, 0, tzinfo=UTC)
    retry_at = start + timedelta(minutes=15)
    first = store.claim_milestone(
        identity,
        run_id="run-1",
        proposal_id="proposal-1",
        max_retries=1,
        now=start.isoformat(),
    )
    store.transition_milestone(
        identity.position_lifecycle_id,
        identity.milestone_key,
        "REJECTED",
        terminal_reason="manual rejection",
        retry_after=retry_at.isoformat(),
        now=(start + timedelta(minutes=1)).isoformat(),
    )

    no_authority = store.claim_milestone(
        identity, run_id="run-2", proposal_id="proposal-2", max_retries=1,
        now=(start + timedelta(minutes=20)).isoformat(),
    )
    too_early = store.claim_milestone(
        identity, run_id="run-2", proposal_id="proposal-2", max_retries=1,
        allow_retry=True, now=(start + timedelta(minutes=10)).isoformat(),
    )
    retried = WinnerExpansionStore(storage).claim_milestone(
        identity, run_id="run-2", proposal_id="proposal-2", max_retries=1,
        allow_retry=True, now=(start + timedelta(minutes=20)).isoformat(),
    )

    assert first.accepted is True
    assert no_authority.accepted is False
    assert too_early.accepted is False
    assert "cooldown" in too_early.reason
    assert retried.accepted is True
    assert retried.generation == 2
    assert retried.retry_count == 1

    store.transition_milestone(
        identity.position_lifecycle_id, identity.milestone_key, "REJECTED",
        terminal_reason="manual rejection again", now=(start + timedelta(minutes=21)).isoformat(),
    )
    exhausted = store.claim_milestone(
        identity, run_id="run-3", proposal_id="proposal-3", max_retries=1,
        allow_retry=True, now=(start + timedelta(minutes=30)).isoformat(),
    )
    assert exhausted.accepted is False
    assert "retry limit" in exhausted.reason


def test_stop_history_is_idempotent_monotonic_and_restart_safe(tmp_path):
    storage = _storage(tmp_path)
    store = WinnerExpansionStore(storage)
    first_decision = _trend()
    first_id = store.persist_stop(
        first_decision, run_id="run-1", source="proposal", stop_as_of="2026-07-14T01:00:00+00:00"
    )
    same_id = WinnerExpansionStore(storage).persist_stop(
        first_decision, run_id="run-2", source="proposal", stop_as_of="2026-07-14T01:00:00+00:00"
    )
    higher_decision = _trend(
        current_price=120.0,
        highest_price_since_entry=122.0,
        current_protective_stop=first_decision.protective_stop,
    )
    second_id = WinnerExpansionStore(storage).persist_stop(
        higher_decision, run_id="run-2", source="final_revalidation", stop_as_of="2026-07-14T01:05:00+00:00"
    )

    assert same_id == first_id
    assert second_id != first_id
    rows = storage.fetch_all(
        "SELECT * FROM position_stop_history ORDER BY stop_sequence"
    )
    assert [row["stop_sequence"] for row in rows] == [1, 2]
    assert rows[1]["new_stop"] >= rows[0]["new_stop"]

    forged_downward = replace(
        higher_decision,
        protective_stop=first_decision.protective_stop - 1.0,
        decision_fingerprint="forged-downward-stop",
    )
    with pytest.raises(ValueError, match="downward"):
        store.persist_stop(
            forged_downward, run_id="run-3", source="stale_scan", stop_as_of="2026-07-14T01:10:00+00:00"
        )


def test_schema_and_decision_persistence_are_complete_and_idempotent(tmp_path):
    storage = _storage(tmp_path)
    tables = {
        row["name"]
        for row in storage.fetch_all("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {
        "position_stop_history",
        "pyramiding_milestones",
        "add_risk_decisions",
        "trend_management_decisions",
    } <= tables

    winner = WinnerExpansionEngine().evaluate(_winner_input())
    store = WinnerExpansionStore(storage)
    trend_id = store.persist_trend_decision(_winner_input().trend_decision, run_id="run-1")
    same_trend_id = store.persist_trend_decision(_winner_input().trend_decision, run_id="run-2")
    risk_id = store.persist_add_risk_decision(winner, run_id="run-1", proposal_id="proposal-1")
    same_risk_id = store.persist_add_risk_decision(winner, run_id="run-2", proposal_id="proposal-1")

    assert trend_id == same_trend_id
    assert risk_id == same_risk_id
    risk_row = storage.fetch_all("SELECT * FROM add_risk_decisions")[0]
    assert risk_row["pre_add_shares"] == 10.0
    assert risk_row["post_add_total_shares"] == 15.0
    assert risk_row["proposed_tightened_stop"] == pytest.approx(winner.required_protective_stop)
    assert risk_row["formula_version"] == winner.risk_decision.formula_version
