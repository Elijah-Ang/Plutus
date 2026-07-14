from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json

import pytest

from app.execution import DurableExecutionStore
from app.order_state import OrderState
from app.phase4_allocator import (
    AdaptiveAllocator,
    allocate_candidates_to_sleeves,
    normalize_risk_to_dollars,
)
from app.rotation_coordinator import RotationCoordinator
from app.service import TradingService
from app.storage import Storage
from app.trend_management import (
    TrendManagementEngine,
    TrendManagementInput,
    validate_trend_decision_invariants,
)
from app.utils import load_config


AS_OF = "2026-07-14T08:00:00+00:00"


def _storage(tmp_path, name: str = "audit.sqlite3") -> Storage:
    storage = Storage(tmp_path / name)
    storage.initialize()
    return storage


def _seed_active_position(storage: Storage, *, lifecycle_id: str = "life-spy", quantity: float = 10.0) -> None:
    now = AS_OF
    storage.execute(
        """INSERT INTO position_lifecycles(
             id,symbol,side,state,opened_at,opening_quantity,current_quantity,average_entry_price,
             source,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (lifecycle_id, "SPY", "long", "active", now, quantity, quantity, 100.0, "test", now, now),
    )
    storage.execute(
        """INSERT INTO position_management_state(
             id,symbol,position_lifecycle_id,avg_entry_price,quantity,highest_price_since_entry,
             max_unrealized_profit_pct,take_profit_level_1_hit,take_profit_level_2_hit,
             take_profit_level_3_hit,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("state-spy", "SPY", lifecycle_id, 100.0, quantity, 110.0, 10.0, 0, 0, 0, now, now),
    )


def _take_profit_proposal(identifier: str, quantity: float) -> dict:
    return {
        "id": identifier,
        "proposal_id": identifier,
        "symbol": "SPY",
        "side": "sell",
        "action": "exit",
        "qty": quantity,
        "latest_price": 110.0,
        "position_lifecycle_id": "life-spy",
        "position_management_decision_type": "TAKE_PROFIT_PARTIAL",
        "position_management_decision": {"take_profit_level": 1},
        "trading_mode": "paper",
    }


def test_take_profit_progress_is_fill_only_idempotent_and_restart_safe(tmp_path) -> None:
    storage = _storage(tmp_path)
    _seed_active_position(storage)
    service = TradingService({"mode": "paper", "live_enabled": False}, storage, None, "run")
    proposal = _take_profit_proposal("tp-1", 4.0)

    for status in ("rejected", "expired", "blocked", "superseded", "submitted", "cancelled"):
        service._mark_position_management_proposal_handled(
            {"id": proposal["id"], "symbol": "SPY", "payload": json.dumps(proposal)},
            status,
        )
    assert storage.fetch_all("SELECT take_profit_level_1_hit FROM position_management_state")[0]["take_profit_level_1_hit"] == 0

    store = DurableExecutionStore(storage)
    intent = store.create_or_get_intent(proposal, run_id="run", source_type="proposal")
    milestone = storage.fetch_all("SELECT * FROM take_profit_milestones")[0]
    assert (milestone["cumulative_filled_quantity"], milestone["completed_fraction"], milestone["status"]) == (0.0, 0.0, "pending_fill")
    store.transition(intent["id"], OrderState.SUBMITTING, event_type="test")
    store.transition(intent["id"], OrderState.SUBMITTED, event_type="test")
    store.record_fill(intent["id"], cumulative_quantity=1.5, fill_price=109.0, broker_event_key="tp-fill-1")
    store.record_fill(intent["id"], cumulative_quantity=1.5, fill_price=109.0, broker_event_key="tp-fill-1")
    store.transition(intent["id"], OrderState.CANCELLED, event_type="cancel_remainder")

    partial = storage.fetch_all("SELECT * FROM take_profit_milestones")[0]
    assert partial["cumulative_filled_quantity"] == pytest.approx(1.5)
    assert partial["completed_fraction"] == pytest.approx(0.375)
    assert partial["status"] == "partially_filled"
    assert storage.fetch_all("SELECT COUNT(*) n FROM take_profit_milestone_fill_links")[0]["n"] == 1
    assert storage.fetch_all("SELECT status FROM take_profit_milestone_actions")[0]["status"] == "partially_filled_cancelled"

    restarted = DurableExecutionStore(storage)
    remainder = restarted.create_or_get_intent(
        _take_profit_proposal("tp-2", 2.5), run_id="run-restart", source_type="proposal"
    )
    restarted.transition(remainder["id"], OrderState.SUBMITTING, event_type="test")
    restarted.record_fill(remainder["id"], cumulative_quantity=2.5, fill_price=108.0, broker_event_key="tp-fill-2")
    restarted.record_fill(remainder["id"], cumulative_quantity=2.5, fill_price=108.0, broker_event_key="tp-fill-2")
    complete = storage.fetch_all("SELECT * FROM take_profit_milestones")[0]
    assert (complete["cumulative_filled_quantity"], complete["completed_fraction"], complete["status"]) == (4.0, 1.0, "filled")
    assert storage.fetch_all("SELECT take_profit_level_1_hit FROM position_management_state")[0]["take_profit_level_1_hit"] == 1


def test_only_equivalent_pending_buys_are_superseded(tmp_path) -> None:
    storage = _storage(tmp_path)
    service = TradingService({"mode": "paper", "live_enabled": False}, storage, None, "run")
    expiry = (datetime.now(UTC) + timedelta(minutes=15)).isoformat()
    now = datetime.now(UTC).isoformat()
    rows = (
        ("same", "SPY", '{"action":"entry","setup_key":"setup-a","strategy_version":"rule_based_v2"}'),
        ("unrelated", "DIA", '{"action":"entry","setup_key":"setup-b","strategy_version":"rule_based_v2"}'),
    )
    for identifier, symbol, payload in rows:
        storage.execute(
            "INSERT INTO trade_proposals(id,run_id,symbol,side,status,created_at,expires_at,payload) VALUES(?,?,?,?,?,?,?,?)",
            (identifier, "run", symbol, "buy", "pending", now, expiry, payload),
        )
    superseded = service._supersede_equivalent_pending_buys({
        "id": "submitted", "symbol": "SPY", "side": "buy", "action": "entry",
        "setup_key": "setup-a", "strategy_version": "rule_based_v2",
    })
    assert superseded == ["same"]
    assert storage.fetch_all("SELECT status FROM trade_proposals WHERE id='unrelated'")[0]["status"] == "pending"


def test_initial_r_requires_current_lifecycle_fill_provenance(tmp_path) -> None:
    storage = _storage(tmp_path)
    _seed_active_position(storage)
    storage.execute(
        """INSERT INTO trade_proposals(id,run_id,symbol,side,status,created_at,expires_at,payload)
           VALUES('old-approved','old','SPY','buy','approved',?,?,?)""",
        (AS_OF, "2027-01-01T00:00:00+00:00", '{"stop_price":80,"latest_price":90}'),
    )
    service = TradingService({"mode": "paper", "live_enabled": False}, storage, None, "run")
    missing = service._initial_risk_seed_for_position("SPY")
    assert missing["initial_risk_per_share"] is None
    assert missing["r_multiple_unavailable_reason"] == "r_multiple_unavailable_current_lifecycle_entry_fill_missing"

    store = DurableExecutionStore(storage)
    entry = store.create_or_get_intent({
        "id": "current-entry", "proposal_id": "current-entry", "symbol": "SPY", "side": "buy",
        "action": "entry", "qty": 10.0, "latest_price": 100.0, "stop_price": 95.0,
        "position_lifecycle_id": "life-spy", "trading_mode": "paper",
    }, run_id="run", source_type="proposal")
    store.transition(entry["id"], OrderState.SUBMITTING, event_type="test")
    store.record_fill(entry["id"], cumulative_quantity=10.0, fill_price=100.0, broker_event_key="current-entry-fill")
    seeded = service._initial_risk_seed_for_position("SPY")
    assert seeded["position_lifecycle_id"] == "life-spy"
    assert seeded["entry_order_intent_id"] == entry["id"]
    assert seeded["entry_price_for_r"] == 100.0
    assert seeded["initial_stop_price"] == 95.0
    assert seeded["initial_risk_per_share"] == 5.0
    assert seeded["initial_risk_reconstruction_source"] == "linked_order_intent"


def test_phase4_risk_units_are_explicit_canonical_and_fail_closed() -> None:
    converted = normalize_risk_to_dollars(
        0.20, "pct_equity", conversion_equity=100_000.0,
        conversion_equity_as_of=AS_OF, evaluation_time=AS_OF,
    )
    assert converted["risk_value"] == pytest.approx(200.0)
    assert converted["risk_unit"] == "stop_risk_dollars"
    with pytest.raises(ValueError, match="stale"):
        normalize_risk_to_dollars(
            0.20, "pct_equity", conversion_equity=100_000.0,
            conversion_equity_as_of="2026-07-14T07:00:00+00:00", evaluation_time=AS_OF,
        )
    with pytest.raises(ValueError, match="unsupported"):
        normalize_risk_to_dollars(
            200, "", conversion_equity=100_000.0,
            conversion_equity_as_of=AS_OF, evaluation_time=AS_OF,
        )
    for bad_value, bad_unit in ((0.0, "stop_risk_dollars"), (-1.0, "stop_risk_dollars"),
                                (float("nan"), "stop_risk_dollars"), (float("inf"), "stop_risk_dollars"),
                                (1.0, "basis_points")):
        with pytest.raises(ValueError):
            normalize_risk_to_dollars(
                bad_value, bad_unit, conversion_equity=100_000.0,
                conversion_equity_as_of=AS_OF, evaluation_time=AS_OF,
            )
    with pytest.raises(ValueError, match="equity"):
        normalize_risk_to_dollars(
            0.20, "pct_equity", conversion_equity=None,
            conversion_equity_as_of=AS_OF, evaluation_time=AS_OF,
        )

    result = allocate_candidates_to_sleeves(
        [{"candidate_id": "missing-unit", "strategy_version": "s", "symbol": "SPY", "side": "buy", "risk_value": 0.20}],
        {"s": {"remaining_risk": 250.0, "risk_unit": "stop_risk_dollars"}},
        global_available_risk=250.0,
        global_risk_unit="stop_risk_dollars",
        conversion_equity=100_000.0, conversion_equity_as_of=AS_OF, evaluation_time=AS_OF,
    )
    assert result["decisions"][0]["decision"] == "REJECT"
    assert result["allocated_risk"] == 0.0


def test_final_trend_mode_rebuilds_all_dependent_flags_and_validator_rejects_contradictions() -> None:
    decision = TrendManagementEngine().evaluate(TrendManagementInput(
        symbol="SPY", position_lifecycle_id="life-spy", current_price=118.0,
        average_entry_price=100.0, highest_price_since_entry=120.0,
        current_protective_stop=105.0, atr=2.0, current_r_multiple=2.0,
        peak_r_multiple=2.2, trend_strength=90.0, price_above_ma50=True,
        ma50_above_ma200=True, higher_highs_and_lows=True, market_regime="favorable",
        volatility_regime="normal", deployment_mode="OPPORTUNISTIC",
        execution_quality=0.9, account_health=0.9, account_drawdown_pct=0.5,
        position_age_days=5.0, emergency_exit=True, as_of=AS_OF,
    ))
    assert decision.mode == "EXIT_REQUIRED"
    assert decision.allow_pyramiding is False
    assert decision.defer_fixed_profit_target is False
    assert decision.retained_runner_fraction == 0.0
    assert decision.recommended_partial_exit_fraction == 1.0
    with pytest.raises(ValueError, match="contradict"):
        validate_trend_decision_invariants(replace(decision, allow_pyramiding=True))


def test_terminal_rotation_is_not_reused_and_active_conflict_is_blocked(tmp_path) -> None:
    storage = _storage(tmp_path)
    _seed_active_position(storage)
    manager = RotationCoordinator(storage, config_hash="config")
    exits = [{
        "proposal_id": "exit-1", "position_lifecycle_id": "life-spy", "symbol": "SPY",
        "side": "sell", "quantity": 2.0, "estimated_notional": 200.0,
    }]
    entries = [{
        "proposal_id": "entry-1", "candidate_key": "candidate-1", "strategy_version": "rule_based_v2",
        "symbol": "QQQ", "side": "buy", "max_quantity": 1.0, "max_notional": 100.0,
        "max_stop_risk": 5.0, "payload": {},
    }]
    first = manager.create_group(
        run_id="run-1", exit_legs=exits, contingent_entries=entries,
        evaluation_time=AS_OF, expires_at="2026-07-14T08:15:00+00:00",
    )
    storage.execute("UPDATE rotation_groups SET state='completed' WHERE id=?", (first["id"],))
    second = manager.create_group(
        run_id="run-2", exit_legs=exits, contingent_entries=entries,
        evaluation_time="2026-07-14T08:01:00+00:00", expires_at="2026-07-14T08:16:00+00:00",
    )
    assert second["id"] != first["id"]
    with pytest.raises(RuntimeError, match="conflicting active rotation"):
        manager.create_group(
            run_id="run-3", exit_legs=[{**exits[0], "proposal_id": "exit-2"}],
            contingent_entries=[{**entries[0], "proposal_id": "entry-2", "candidate_key": "candidate-2"}],
            evaluation_time="2026-07-14T08:02:00+00:00", expires_at="2026-07-14T08:17:00+00:00",
        )


def test_phase4_replay_is_stable_for_same_as_of_and_rejects_missing_time(tmp_path) -> None:
    config = load_config()
    snapshot = {"portfolio_equity": 100_000.0, "as_of": AS_OF, "equity_as_of": AS_OF}
    first = AdaptiveAllocator(_storage(tmp_path, "first.sqlite3"), config, "same-run").run(
        regime="normal", drawdown_pct=0.0, portfolio_snapshot=snapshot, as_of=AS_OF,
    )
    second = AdaptiveAllocator(_storage(tmp_path, "second.sqlite3"), config, "same-run").run(
        regime="normal", drawdown_pct=0.0, portfolio_snapshot=snapshot, as_of=AS_OF,
    )
    assert first["allocation_id"] == second["allocation_id"]
    assert first["decision"] == second["decision"]
    assert first["strategy_sleeves"] == second["strategy_sleeves"]
    with pytest.raises(ValueError, match="as_of"):
        AdaptiveAllocator(_storage(tmp_path, "missing.sqlite3"), config, "missing").run(
            regime="normal", drawdown_pct=0.0, portfolio_snapshot=snapshot,
        )
