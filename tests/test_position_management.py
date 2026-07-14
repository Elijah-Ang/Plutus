from __future__ import annotations

import json
from datetime import UTC, datetime

import pandas as pd

from app.position_management import PositionManagementEngine
from app.service import TradingService
from app.storage import Storage
from app.utils import format_proposal_message
from app.reports import SHEETS


def bars(close: float = 104.0, high: float | None = None, low: float | None = None, rows: int = 250) -> pd.DataFrame:
    high = high if high is not None else close + 1
    low = low if low is not None else close - 1
    return pd.DataFrame({
        "open": [close] * rows,
        "high": [high] * rows,
        "low": [low] * rows,
        "close": [close] * rows,
        "volume": [10000.0] * rows,
        "volatility_20": [0.15] * rows,
    })


def config() -> dict:
    return {
        "mode": "paper",
        "live_enabled": False,
        "position_management": {
            "enabled": True,
            "profit_taking": {
                "enabled": True,
                "level_1_r": 1.5,
                "level_1_sell_fraction": 0.25,
                "level_2_r": 2.0,
                "level_2_sell_fraction": 0.33,
                "level_3_r": 3.0,
                "level_3_sell_fraction": 0.50,
                "fallback_level_1_profit_pct": 2.0,
                "fallback_level_2_profit_pct": 3.0,
                "fallback_level_3_profit_pct": 5.0,
                "minimum_notional_to_sell": 1.0,
            },
            "profit_protection": {
                "enabled": True,
                "activate_at_r": 1.0,
                "fallback_activate_at_profit_pct": 2.0,
                "giveback_warning_ratio": 0.40,
                "giveback_exit_ratio": 0.55,
                "min_peak_profit_pct": 2.0,
                "protect_sell_fraction": 0.25,
                "exit_sell_fraction": 0.50,
            },
            "trailing_stop": {
                "enabled": True,
                "activate_at_r": 1.0,
                "fallback_activate_at_profit_pct": 2.0,
                "atr_multiplier": 2.0,
                "fallback_trailing_pct": 1.5,
                "sell_fraction": 0.50,
            },
            "healthy_pullback_add": {
                "enabled": True,
                "minimum_unrealized_profit_pct": 0.5,
                "minimum_trade_score": 85,
                "minimum_score_improvement": 5,
                "max_emergency_exit_score": 40,
                "max_profit_giveback_ratio": 0.35,
                "max_pullback_atr_multiple": 1.5,
                "fallback_max_pullback_pct": 1.0,
                "suggested_add_notional": 10.0,
            },
        },
        "position_sizing": {"max_add_paper_notional": 10.0},
    }


def classify(**overrides):
    cfg = overrides.pop("config", config())
    params = {
        "symbol": "SPY",
        "current_price": 104.0,
        "avg_entry_price": 100.0,
        "quantity": 2.0,
        "bars": bars(104.0),
        "previous_state": None,
        "initial_stop_price": 98.0,
        "trade_score": 80.0,
        "score_improvement": 0.0,
        "emergency_exit_score": 0.0,
        "normal_exit_signal": False,
        "volatility_regime": "normal",
        "has_open_order": False,
        "now": datetime.now(UTC),
    }
    params.update(overrides)
    return PositionManagementEngine(cfg).with_previous_state(params.get("previous_state")).classify(**params)


def test_position_state_calculations_and_r_multiple():
    decision = classify(current_price=104.0, avg_entry_price=100.0, initial_stop_price=98.0, previous_state={"highest_price_since_entry": 105.0})

    assert round(decision.unrealized_profit_pct, 2) == 4.0
    assert round(decision.drawdown_from_entry_pct, 2) == 0.0
    assert round(decision.drawdown_from_peak_pct, 2) == -0.95
    assert decision.highest_price_since_entry == 105.0
    assert round(decision.max_unrealized_profit_pct, 2) == 5.0
    assert round(decision.pullback_from_peak_pct, 2) == 0.95
    assert round(decision.profit_giveback_ratio, 2) == 0.20
    assert round(decision.current_r_multiple, 2) == 2.0


def test_drawdown_from_entry_computed_for_losing_position():
    cfg = config()
    cfg["position_management"]["profit_protection"]["enabled"] = False
    cfg["position_management"]["profit_taking"]["enabled"] = False
    cfg["position_management"]["trailing_stop"]["enabled"] = False
    decision = classify(current_price=96.0, avg_entry_price=100.0, initial_stop_price=None, previous_state={"highest_price_since_entry": 101.0}, config=cfg)

    assert round(decision.unrealized_profit_pct, 2) == -4.0
    assert round(decision.drawdown_from_entry_pct, 2) == -4.0
    assert round(decision.drawdown_from_peak_pct, 2) == -4.95


def test_drawdown_from_peak_computed_after_winner_gives_back():
    cfg = config()
    cfg["position_management"]["profit_protection"]["enabled"] = False
    cfg["position_management"]["profit_taking"]["enabled"] = False
    cfg["position_management"]["trailing_stop"]["enabled"] = False
    decision = classify(current_price=104.0, avg_entry_price=100.0, initial_stop_price=None, previous_state={"highest_price_since_entry": 110.0}, config=cfg)

    assert round(decision.drawdown_from_entry_pct, 2) == 0.0
    assert round(decision.drawdown_from_peak_pct, 2) == -5.45


def test_trailing_stop_only_moves_upward():
    cfg = config()
    cfg["position_management"]["profit_protection"]["enabled"] = False
    cfg["position_management"]["profit_taking"]["enabled"] = False
    decision = classify(
        current_price=104.0,
        previous_state={"highest_price_since_entry": 110.0, "trailing_stop_price": 105.0},
        initial_stop_price=None,
        config=cfg,
    )

    assert decision.decision_type == "TRAILING_STOP_EXIT"
    assert decision.trailing_stop_price >= 105.0


def test_fallback_take_profit_level_one_only_once():
    first = classify(current_price=102.5, previous_state={"highest_price_since_entry": 102.5}, initial_stop_price=None)
    second = classify(
        current_price=102.5,
        previous_state={"highest_price_since_entry": 102.5, "take_profit_level_1_hit": 1},
        initial_stop_price=None,
    )

    assert first.decision_type == "TAKE_PROFIT_PARTIAL"
    assert first.take_profit_level == 1
    assert first.current_r_multiple is None
    assert second.decision_type == "HOLD"


def test_no_tiny_take_profit_below_minimum_notional():
    decision = classify(current_price=103.0, quantity=0.01, previous_state={"highest_price_since_entry": 103.0}, initial_stop_price=None)

    assert decision.decision_type == "TAKE_PROFIT_PARTIAL"
    assert decision.is_actionable is False
    assert "below minimum sell notional" in decision.reason


def test_decision_priority_emergency_and_risk_exit_override_profit():
    emergency = classify(current_price=110.0, emergency_exit_score=90.0)
    risk_exit = classify(current_price=110.0, normal_exit_signal=True)

    assert emergency.decision_type == "EMERGENCY_EXIT"
    assert risk_exit.decision_type == "NORMAL_RISK_EXIT"


def test_profit_protection_overrides_take_profit_and_add():
    decision = classify(
        current_price=102.0,
        previous_state={"highest_price_since_entry": 106.0, "profit_protection_active": 1},
        initial_stop_price=None,
        trade_score=90,
        score_improvement=10,
    )

    assert decision.decision_type == "PROFIT_PROTECT_EXIT"
    assert decision.priority < 4


def test_trailing_stop_breach_creates_exit():
    cfg = config()
    cfg["position_management"]["profit_protection"]["enabled"] = False
    cfg["position_management"]["profit_taking"]["enabled"] = False
    decision = classify(current_price=103.0, previous_state={"highest_price_since_entry": 110.0, "trailing_stop_price": 105.0}, initial_stop_price=None, config=cfg)

    assert decision.decision_type == "TRAILING_STOP_EXIT"
    assert decision.action == "sell"
    assert decision.is_actionable is True


def test_trailing_stop_does_not_trigger_before_real_gain():
    cfg = config()
    cfg["position_management"]["profit_protection"]["enabled"] = False
    cfg["position_management"]["profit_taking"]["enabled"] = False
    decision = classify(current_price=99.0, avg_entry_price=100.0, previous_state={"highest_price_since_entry": 100.0, "trailing_stop_price": 98.5}, initial_stop_price=None, config=cfg)

    assert decision.decision_type != "TRAILING_STOP_EXIT"
    assert decision.action != "sell"


def test_time_stop_review_creates_exit_candidate_after_stale_hold():
    cfg = config()
    cfg["position_management"]["profit_protection"]["enabled"] = False
    cfg["position_management"]["profit_taking"]["enabled"] = False
    cfg["position_management"]["trailing_stop"]["enabled"] = False
    cfg["position_management"]["healthy_pullback_add"]["enabled"] = False
    cfg["position_management"]["time_stop"] = {
        "enabled": True,
        "min_hold_cycles_before_time_stop": 12,
        "min_hold_days_before_time_stop": 3.0,
        "max_unrealized_gain_pct": 0.5,
        "max_peak_gain_pct": 1.0,
        "weak_trade_score_below": 60,
        "deteriorating_score_delta_below": -5,
        "proposal_enabled": True,
        "sell_fraction": 1.0,
    }

    decision = classify(
        current_price=100.2,
        avg_entry_price=100.0,
        previous_state={"highest_price_since_entry": 100.7},
        initial_stop_price=None,
        trade_score=55.0,
        score_improvement=-6.0,
        position_age_cycles=15,
        position_age_days=4.0,
        config=cfg,
    )

    assert decision.decision_type == "TIME_STOP_EXIT"
    assert decision.action == "sell"
    assert decision.is_actionable is True
    assert decision.exit_review_needed is True


def test_healthy_pullback_add_requires_winner_and_trend():
    decision = classify(
        current_price=101.2,
        bars=bars(100.8),
        previous_state={"highest_price_since_entry": 101.5},
        initial_stop_price=None,
        trade_score=90.0,
        score_improvement=6.0,
    )
    losing = classify(current_price=99.0, previous_state={"highest_price_since_entry": 101.5}, initial_stop_price=None, trade_score=95.0, score_improvement=10.0)
    trap = classify(current_price=101.2, bars=bars(110.0), previous_state={"highest_price_since_entry": 101.5}, initial_stop_price=None, trade_score=95.0, score_improvement=10.0)

    assert decision.decision_type == "HEALTHY_PULLBACK_ADD"
    assert decision.dip_trap_classification == "healthy_pullback"
    assert losing.decision_type == "HOLD"
    assert "not sufficiently profitable" in losing.reason
    assert trap.decision_type == "HOLD"
    assert "price is below MA50" in trap.reason


def test_position_management_tables_and_report_sheets_exist(tmp_path):
    storage = Storage(tmp_path / "pm.db")
    storage.initialize()
    tables = {r["name"] for r in storage.fetch_all("SELECT name FROM sqlite_master WHERE type='table'")}
    sheet_names = {s[0] for s in SHEETS}

    assert "position_management_state" in tables
    assert "position_management_decisions" in tables
    assert "profit_exit_events" in tables
    assert "Position Management State" in sheet_names
    assert "Healthy Pullback Adds" in sheet_names
    assert "Exit Review Status" in sheet_names
    assert "Position Drawdown Metrics" in sheet_names


class Broker:
    def __init__(self, positions=None, orders=None):
        self.positions = positions or []
        self.orders = orders or []

    def get_positions(self):
        return self.positions

    def get_open_orders(self):
        return self.orders


def test_final_revalidation_blocks_profit_exit_if_position_gone(tmp_path):
    storage = Storage(tmp_path / "pm_final.db")
    storage.initialize()
    service = TradingService(config(), storage, Broker([]), "run")
    proposal = {"symbol": "SPY", "side": "sell", "qty": 1.0, "latest_price": 100.0, "position_management_decision_type": "TAKE_PROFIT_PARTIAL"}

    assert service._final_revalidate_position_management(proposal, 100.0) == "position no longer exists"


def test_take_profit_proposal_status_does_not_mark_level_state(tmp_path):
    storage = Storage(tmp_path / "pm_handled.db")
    storage.initialize()
    service = TradingService(config(), storage, Broker([]), "run")
    now = datetime.now(UTC).isoformat()
    storage.execute(
        """INSERT INTO position_management_state(
            id,symbol,avg_entry_price,quantity,highest_price_since_entry,max_unrealized_profit_pct,
            take_profit_level_1_hit,take_profit_level_2_hit,take_profit_level_3_hit,updated_at,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        ("state-1", "SPY", 100.0, 1.0, 103.0, 3.0, 0, 0, 0, now, now),
    )
    payload = {
        "position_management_decision_type": "TAKE_PROFIT_PARTIAL",
        "position_management_decision": {"take_profit_level": 1},
    }
    row = {"id": "prop-1", "symbol": "SPY", "payload": json.dumps(payload)}

    service._mark_position_management_proposal_handled(row, "rejected")

    state = storage.fetch_all("SELECT take_profit_level_1_hit FROM position_management_state WHERE symbol='SPY'")[0]
    assert state["take_profit_level_1_hit"] == 0


def test_position_management_proposal_wording():
    msg = format_proposal_message(
        {
            "symbol": "SPY",
            "side": "sell",
            "qty": 0.5,
            "notional": 55.0,
            "expires_at": datetime.now(UTC).isoformat(),
            "reason": "level 1 profit target reached",
            "position_management_decision_type": "TAKE_PROFIT_PARTIAL",
            "position_management_sell_fraction": 0.25,
            "position_management_decision": {
                "unrealized_profit_pct": 3.4,
                "max_unrealized_profit_pct": 3.9,
                "current_r_multiple": 1.7,
                "suggested_sell_fraction": 0.25,
            },
        },
        {"mode": "paper", "live_enabled": False},
    )

    assert "Paper profit-taking proposal" in msg
    assert "Current gain: +3.40%" in msg
    assert "Suggested action: Sell 25%" in msg


def test_time_stop_proposal_wording():
    msg = format_proposal_message(
        {
            "symbol": "SPY",
            "side": "sell",
            "qty": 1.0,
            "notional": 100.0,
            "expires_at": datetime.now(UTC).isoformat(),
            "reason": "time stop review: no meaningful gain after hold period",
            "position_management_decision_type": "TIME_STOP_EXIT",
            "position_management_sell_fraction": 1.0,
            "position_management_decision": {
                "unrealized_profit_pct": 0.1,
                "max_unrealized_profit_pct": 0.5,
                "suggested_sell_fraction": 1.0,
            },
        },
        {"mode": "paper", "live_enabled": False},
    )

    assert "Paper time-stop exit proposal" in msg
    assert "Suggested action: Sell 100%" in msg


def test_r_multiple_falls_back_safely_when_stop_equals_entry():
    decision = classify(current_price=102.0, avg_entry_price=100.0, initial_stop_price=100.0)

    assert decision.current_r_multiple is None
    assert decision.decision_type in {"TAKE_PROFIT_PARTIAL", "HOLD"}
