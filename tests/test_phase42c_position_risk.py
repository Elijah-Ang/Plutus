from __future__ import annotations

import pytest

from app.position_risk import PositionRiskEngine, PositionRiskInput


def _input(**overrides) -> PositionRiskInput:
    values = {
        "symbol": "QQQ",
        "position_lifecycle_id": "life-qqq-1",
        "deployment_mode": "NORMAL",
        "current_shares": 10.0,
        "proposed_add_shares": 5.0,
        "current_market_price": 110.0,
        "proposed_add_price": 110.0,
        "current_protective_stop": 100.0,
        "proposed_tightened_stop": 105.0,
        "portfolio_equity": 100_000.0,
    }
    values.update(overrides)
    return PositionRiskInput(**values)


def test_normal_add_must_be_risk_neutral_and_persists_full_pre_post_math():
    decision = PositionRiskEngine().evaluate(_input())

    assert decision.eligible is True
    assert decision.pre_add_open_risk_gross == pytest.approx(100.0)
    assert decision.post_add_open_risk_gross == pytest.approx(75.0)
    assert decision.post_add_total_shares == 15.0
    assert decision.incremental_risk == pytest.approx(-25.0)
    assert decision.consumed_risk == 0.0
    assert decision.released_risk == pytest.approx(25.0)
    assert decision.raw_inputs["portfolio_risk_accounting"] == "replace_current_position_then_add_active_reservations"
    assert len(decision.decision_fingerprint) == 64


def test_normal_blocks_increased_total_position_risk_but_opportunistic_requires_explicit_allowance():
    normal = PositionRiskEngine().evaluate(_input(proposed_tightened_stop=101.0))
    missing_allowance = PositionRiskEngine().evaluate(
        _input(deployment_mode="OPPORTUNISTIC", proposed_tightened_stop=101.0)
    )
    opportunistic = PositionRiskEngine().evaluate(
        _input(
            deployment_mode="OPPORTUNISTIC",
            proposed_tightened_stop=101.0,
            mode_incremental_risk_allowance_pct=0.05,
        )
    )

    assert normal.eligible is False
    assert normal.incremental_risk == pytest.approx(35.0)
    assert "NORMAL allowance" in normal.blocking_reasons[0]
    assert missing_allowance.eligible is False
    assert any("explicit configured" in reason for reason in missing_allowance.blocking_reasons)
    assert opportunistic.eligible is True
    assert opportunistic.mode_incremental_risk_allowance_dollars == pytest.approx(50.0)


def test_defensive_downward_stop_hard_ceiling_and_reservation_heat_fail_closed():
    defensive = PositionRiskEngine().evaluate(_input(deployment_mode="DEFENSIVE"))
    downward = PositionRiskEngine().evaluate(
        _input(deployment_mode="OPPORTUNISTIC", proposed_tightened_stop=99.0, mode_incremental_risk_allowance_pct=0.20)
    )
    hard = PositionRiskEngine().evaluate(
        _input(
            deployment_mode="AGGRESSIVE",
            current_shares=20.0,
            proposed_add_shares=1.0,
            current_market_price=120.0,
            proposed_add_price=120.0,
            current_protective_stop=100.0,
            proposed_tightened_stop=100.0,
            mode_incremental_risk_allowance_pct=0.10,
        )
    )
    heat = PositionRiskEngine().evaluate(
        _input(active_reserved_stop_risk_dollars=1_700.0)
    )

    assert defensive.eligible is False
    assert defensive.binding_cap == "defensive_no_add"
    assert downward.eligible is False
    assert "downward" in " ".join(downward.blocking_reasons)
    assert hard.eligible is False
    assert "hard 0.35%" in " ".join(hard.blocking_reasons)
    assert heat.eligible is False
    assert "portfolio heat" in " ".join(heat.blocking_reasons)


def test_realized_profit_credit_is_separate_verified_and_cannot_manufacture_incremental_capacity():
    no_credit = PositionRiskEngine().evaluate(_input())
    unverified = PositionRiskEngine().evaluate(
        _input(
            realized_profit_credit_dollars=50.0,
            realized_profit_credit_eligible=True,
            realized_profit_credit_verified=False,
            realized_profit_evidence_id="lot-event-1",
        )
    )
    verified = PositionRiskEngine().evaluate(
        _input(
            realized_profit_credit_dollars=50.0,
            realized_profit_credit_eligible=True,
            realized_profit_credit_verified=True,
            realized_profit_evidence_id="lot-event-1",
        )
    )

    assert unverified.realized_profit_credit_applied == 0.0
    assert verified.realized_profit_credit_applied == 50.0
    assert verified.pre_add_open_risk_net == pytest.approx(50.0)
    assert verified.post_add_open_risk_net == pytest.approx(25.0)
    assert verified.incremental_risk == pytest.approx(no_credit.incremental_risk)


def test_existing_same_symbol_reservation_is_included_and_cannot_exceed_total_reservations():
    decision = PositionRiskEngine().evaluate(
        _input(
            same_symbol_reserved_stop_risk_dollars=25.0,
            active_reserved_stop_risk_dollars=40.0,
        )
    )
    assert decision.pre_position_commitment_risk == pytest.approx(125.0)
    assert decision.post_position_commitment_risk == pytest.approx(100.0)

    with pytest.raises(ValueError, match="cannot be below"):
        PositionRiskEngine().evaluate(
            _input(
                same_symbol_reserved_stop_risk_dollars=50.0,
                active_reserved_stop_risk_dollars=40.0,
            )
        )
