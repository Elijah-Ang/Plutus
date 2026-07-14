from __future__ import annotations

import pytest

from app.trend_management import (
    PositionManagementMode,
    TrendManagementEngine,
    TrendManagementInput,
)


def _input(**overrides) -> TrendManagementInput:
    values = {
        "symbol": "QQQ",
        "position_lifecycle_id": "life-qqq-1",
        "current_price": 118.0,
        "average_entry_price": 100.0,
        "highest_price_since_entry": 120.0,
        "current_protective_stop": 105.0,
        "atr": 2.0,
        "current_r_multiple": 2.0,
        "peak_r_multiple": 2.2,
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
    return TrendManagementInput(**values)


def test_strong_favorable_trend_enters_pyramid_mode_with_wider_atr_trail():
    decision = TrendManagementEngine().evaluate(_input())

    assert decision.mode == PositionManagementMode.TREND_PYRAMID.value
    assert decision.allow_pyramiding is True
    assert decision.defer_fixed_profit_target is True
    assert decision.recommended_partial_exit_fraction == 0.0
    assert decision.effective_atr_multiplier == pytest.approx(3.0)
    assert decision.protective_stop == pytest.approx(114.0)
    assert decision.stop_monotonic is True


def test_normal_mode_classifies_mature_strong_trend_for_risk_neutral_pyramiding():
    decision = TrendManagementEngine().evaluate(_input(deployment_mode="NORMAL"))

    assert decision.mode == PositionManagementMode.TREND_PYRAMID.value
    assert decision.allow_pyramiding is True
    assert decision.recommended_partial_exit_fraction == pytest.approx(0.0)
    assert decision.retained_runner_fraction == pytest.approx(1.0)


def test_defensive_and_profit_protect_modes_tighten_deterministically():
    defensive = TrendManagementEngine().evaluate(_input(deployment_mode="DEFENSIVE"))
    profit_protect = TrendManagementEngine().evaluate(
        _input(current_r_multiple=1.0, peak_r_multiple=3.0)
    )

    assert defensive.mode == PositionManagementMode.DEFENSIVE_HARVEST.value
    assert defensive.effective_atr_multiplier == pytest.approx(1.5)
    assert defensive.protective_stop > 114.0
    assert profit_protect.mode == PositionManagementMode.PROFIT_PROTECT.value
    assert profit_protect.recommended_partial_exit_fraction == pytest.approx(0.50)
    assert profit_protect.effective_atr_multiplier == pytest.approx(0.75)


def test_exit_precedence_and_terminal_exit_transition():
    emergency = TrendManagementEngine().evaluate(_input(emergency_exit=True))
    terminal = TrendManagementEngine().evaluate(
        _input(previous_mode="EXIT_REQUIRED", emergency_exit=False)
    )

    assert emergency.mode == PositionManagementMode.EXIT_REQUIRED.value
    assert emergency.recommended_partial_exit_fraction == 1.0
    assert terminal.mode == PositionManagementMode.EXIT_REQUIRED.value
    assert "terminal" in terminal.transition_reason


def test_recovery_from_profit_protect_cannot_jump_directly_to_pyramid():
    decision = TrendManagementEngine().evaluate(
        _input(previous_mode="PROFIT_PROTECT")
    )

    assert decision.classified_mode == PositionManagementMode.TREND_PYRAMID.value
    assert decision.mode == PositionManagementMode.STANDARD_SCALE_OUT.value
    assert decision.transition == "PROFIT_PROTECT->STANDARD_SCALE_OUT"


def test_stale_or_looser_stop_calculation_never_reduces_persisted_stop():
    decision = TrendManagementEngine().evaluate(
        _input(
            current_price=125.0,
            highest_price_since_entry=126.0,
            current_protective_stop=124.5,
            trend_strength=60.0,
            higher_highs_and_lows=False,
        )
    )

    assert decision.mode == PositionManagementMode.STANDARD_SCALE_OUT.value
    assert decision.calculated_stop_candidate < 124.5
    assert decision.protective_stop == pytest.approx(124.5)
    assert decision.stop_changed is False
    assert decision.stop_monotonic is True
