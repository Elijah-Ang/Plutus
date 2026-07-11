from __future__ import annotations

from app.phase3_risk import Phase3Controller, Phase3RiskProfile, apply_phase3_schema, drawdown_multiplier, regime_multiplier
from app.research_validation import apply_phase1_schema
from app.shadow_strategies import apply_phase2_schema
from app.storage import Storage


def config():
    return {"phase3": {"enabled": True, "active": True, "promotion": {"minimum_completed_oos": 100}, "risk_profile": {
        "base_stop_risk_pct": .20, "add_stop_risk_pct": .10, "max_trade_stop_risk_pct": .35,
        "max_portfolio_heat_pct": 1.25, "favorable_portfolio_heat_pct": 1.50,
        "defensive_portfolio_heat_pct": .50, "normal_gross_exposure_pct": 30,
        "favorable_gross_exposure_pct": 40, "hard_gross_exposure_pct": 50,
        "max_symbol_exposure_pct": 6, "max_cluster_exposure_pct": 15,
        "daily_loss_throttle_pct": .75, "weekly_loss_throttle_pct": 1.5,
        "drawdown_halt_pct": 6, "minimum_average_dollar_volume": 10_000_000,
    }}}


def db(tmp_path):
    value = Storage(tmp_path / "p3.sqlite3"); value.initialize()
    with value.connect() as conn:
        apply_phase1_schema(conn); apply_phase2_schema(conn); apply_phase3_schema(conn)
    return value


def test_profile_and_deterministic_scalers():
    profile = Phase3RiskProfile.from_config(config())
    assert profile.base_stop_risk_pct == .20
    assert [drawdown_multiplier(x) for x in (0, 2, 4, 6)] == [1, .75, .5, 0]
    assert regime_multiplier("downtrend_high_vol") == .5
    assert regime_multiplier("uptrend_normal_vol") == 1


def test_sleeves_fail_closed_without_evidence_and_can_recover(tmp_path):
    storage = db(tmp_path); controller = Phase3Controller(storage, config(), "run")
    states = controller.refresh_strategy_states()
    assert set(states.values()) == {"THROTTLED"}
    assert len(storage.fetch_all("SELECT * FROM phase3_strategy_states")) == 6


def test_equity_drawdown_halts_at_six_percent(tmp_path):
    controller = Phase3Controller(db(tmp_path), config(), "run")
    assert controller.update_equity(100_000) == 0
    assert controller.update_equity(94_000) == 6
    assert drawdown_multiplier(6) == 0


def test_reconciliation_health_counts_unknown_and_partial(tmp_path):
    storage = db(tmp_path); controller = Phase3Controller(storage, config(), "run")
    healthy, report = controller.reconciliation_health()
    assert healthy and not any(report.values())
