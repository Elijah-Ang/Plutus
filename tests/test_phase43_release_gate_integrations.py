from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.execution import DurableExecutionStore, _winner_add_reservation_risk
from app.rotation_coordinator import RotationCoordinator, RotationState
from app.service import TradingService, _hydrate_proposal_row, _validated_authoritative_stop
from app.storage import Storage
from app.trend_management import TrendManagementEngine, TrendManagementInput
from app.winner_expansion import WinnerExpansionStore


def _storage(tmp_path) -> Storage:
    storage = Storage(tmp_path / "release-gates.sqlite3")
    storage.initialize()
    storage.apply_explicit_migrations()
    now = datetime.now(UTC).isoformat()
    storage.execute(
        """INSERT INTO position_lifecycles(
             id,symbol,side,state,opened_at,opening_quantity,current_quantity,average_entry_price,
             source,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        ("lifecycle-old", "OLD", "long", "active", now, 1.0, 1.0, 100.0, "test", now, now),
    )
    return storage


def test_nullable_additive_columns_do_not_erase_payload_provenance() -> None:
    hydrated = _hydrate_proposal_row({
        "id": "proposal-1",
        "status": "pending",
        "pyramiding_milestone_key": None,
        "winner_expansion_decision_id": None,
        "payload": json.dumps({
            "pyramiding_milestone_key": "milestone-1",
            "winner_expansion_decision_id": "decision-1",
        }),
    })

    assert hydrated["pyramiding_milestone_key"] == "milestone-1"
    assert hydrated["winner_expansion_decision_id"] == "decision-1"
    assert hydrated["status"] == "pending"


def test_winner_stop_gate_rejects_stale_or_incomplete_authority() -> None:
    now = datetime.now(UTC)
    config = {"require_authoritative_current_stop": True, "stop_freshness_seconds": 300}
    current = {
        "authoritative_protective_stop": 101.0,
        "protective_stop_as_of": now.isoformat(),
        "protective_stop_source": "trend",
        "protective_stop_formula_version": "trend-v1",
        "protective_stop_sequence": 2,
    }
    assert _validated_authoritative_stop(current, config, now=now) == (101.0, True)
    with pytest.raises(ValueError, match="stale"):
        _validated_authoritative_stop(
            {**current, "protective_stop_as_of": (now - timedelta(minutes=6)).isoformat()},
            config,
            now=now,
        )
    with pytest.raises(ValueError, match="provenance"):
        _validated_authoritative_stop({**current, "protective_stop_source": None}, config, now=now)


def test_risk_neutral_add_keeps_signed_delta_but_reserves_full_pending_leg() -> None:
    incremental, reservation = _winner_add_reservation_risk(
        {"incremental_risk": -10.0, "pending_add_stop_risk": 20.0},
        quantity=2.0,
        reference=110.0,
        stop_price=100.0,
    )
    assert incremental == -10.0
    assert reservation == 20.0
    with pytest.raises(ValueError, match="does not match"):
        _winner_add_reservation_risk(
            {"incremental_risk": -10.0, "pending_add_stop_risk": 20.0},
            quantity=2.0,
            reference=110.28,
            stop_price=100.0,
        )
    incremental, reservation = _winner_add_reservation_risk(
        {"incremental_risk": -10.0, "pending_add_stop_risk": 20.56},
        quantity=2.0,
        reference=110.28,
        stop_price=100.0,
    )
    assert incremental == -10.0
    assert reservation == pytest.approx(20.56)


def test_atomic_reservation_uses_limit_reference_and_never_exceeds_approval_ceiling(tmp_path) -> None:
    storage = _storage(tmp_path)
    proposal = {
        "id": "ceiling-bound", "proposal_id": "ceiling-bound", "symbol": "SPY",
        "side": "buy", "action": "entry", "trading_mode": "paper",
        "qty": 10.0, "notional": 1_000.0, "latest_price": 100.0,
        "limit_price": 100.25, "stop_price": 99.0,
        "approved_quantity_ceiling": 10.0,
        "approved_notional_ceiling": 1_000.0,
        "approved_stop_risk_ceiling": 12.5,
    }

    with pytest.raises(RuntimeError, match="approved notional ceiling"):
        DurableExecutionStore(storage).create_or_get_intent(
            proposal, run_id="run", source_type="proposal"
        )

    proposal["id"] = proposal["proposal_id"] = "ceiling-reduced"
    proposal["qty"] = 1_000.0 / 100.25
    intent = DurableExecutionStore(storage).create_or_get_intent(
        proposal, run_id="run", source_type="proposal"
    )
    assert intent["reserved_notional"] == pytest.approx(1_000.0)
    assert intent["requested_quantity"] <= intent["approved_quantity_ceiling"]


def _trend(stop: float, *, current_price: float = 125.0):
    return TrendManagementEngine().evaluate(TrendManagementInput(
        symbol="QQQ", position_lifecycle_id="life-1", current_price=current_price,
        average_entry_price=100.0, highest_price_since_entry=current_price + 1.0,
        current_protective_stop=stop, atr=2.0, current_r_multiple=2.0,
        peak_r_multiple=2.2, trend_strength=90.0, price_above_ma50=True,
        ma50_above_ma200=True, higher_highs_and_lows=True, market_regime="favorable",
        volatility_regime="normal", deployment_mode="OPPORTUNISTIC",
        execution_quality=0.9, account_health=0.9, account_drawdown_pct=0.5,
        position_age_days=5.0, as_of=datetime.now(UTC).isoformat(),
    ))


def test_authoritative_stop_update_is_atomic_and_stale_writer_cannot_loosen(tmp_path) -> None:
    storage = _storage(tmp_path)
    storage.execute(
        """INSERT INTO position_management_state(
             id,symbol,position_lifecycle_id,authoritative_protective_stop,protective_stop_as_of,
             protective_stop_source,protective_stop_formula_version,protective_stop_sequence,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        ("pm-1", "QQQ", "life-1", 105.0, datetime.now(UTC).isoformat(), "seed", "seed-v1", 1,
         datetime.now(UTC).isoformat(), datetime.now(UTC).isoformat()),
    )
    store = WinnerExpansionStore(storage)
    newer = _trend(105.0, current_price=130.0)
    stale = _trend(105.0, current_price=120.0)
    store.persist_authoritative_stop(
        newer, run_id="run", source="newer", stop_as_of=datetime.now(UTC).isoformat()
    )
    store.persist_authoritative_stop(
        stale, run_id="run", source="stale", stop_as_of=datetime.now(UTC).isoformat()
    )

    state = storage.fetch_all(
        "SELECT authoritative_protective_stop,protective_stop_sequence FROM position_management_state WHERE id='pm-1'"
    )[0]
    history = storage.fetch_all(
        "SELECT new_stop FROM position_stop_history WHERE position_lifecycle_id='life-1' ORDER BY stop_sequence"
    )
    assert state["authoritative_protective_stop"] == max(row["new_stop"] for row in history)
    assert state["authoritative_protective_stop"] >= newer.protective_stop
    assert state["protective_stop_sequence"] == len(history)


def test_stale_stop_writer_cannot_regress_exit_mode_and_same_stop_refreshes_age(tmp_path) -> None:
    storage = _storage(tmp_path)
    old = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    storage.execute(
        """INSERT INTO position_management_state(
             id,symbol,position_lifecycle_id,authoritative_protective_stop,protective_stop_as_of,
             protective_stop_source,protective_stop_formula_version,protective_stop_sequence,
             management_mode,trend_management_formula_version,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("pm-exit", "QQQ", "life-1", 130.0, old, "exit", "trend-v1", 4,
         "EXIT_REQUIRED", "trend-v1", old, old),
    )
    decision = _trend(105.0, current_price=131.0)
    refreshed = datetime.now(UTC).isoformat()
    WinnerExpansionStore(storage).persist_authoritative_stop(
        decision, run_id="run", source="same-stop-revalidation", stop_as_of=refreshed
    )

    state = storage.fetch_all(
        """SELECT authoritative_protective_stop,management_mode,protective_stop_as_of,
                  protective_stop_source FROM position_management_state WHERE id='pm-exit'"""
    )[0]
    assert state["authoritative_protective_stop"] == 130.0
    assert state["management_mode"] == "EXIT_REQUIRED"
    assert state["protective_stop_as_of"] == refreshed
    assert state["protective_stop_source"] == "same-stop-revalidation"


def _persist_registry_and_allocation(storage: Storage) -> None:
    now = datetime.now(UTC).isoformat()
    storage.execute(
        """INSERT INTO strategy_registry_snapshots(
             id,run_id,evaluated_at,registry_schema_version,registry_formula_version,
             configuration_version,config_hash,authorized_strategies_json,rejected_strategies_json,
             global_reasons_json,raw_inputs_json,evaluation_fingerprint,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("registry-1", "run-1", now, "schema", "formula", "config", "hash", "[]", "[]", "[]", "{}", "fp", now),
    )
    storage.execute(
        """INSERT INTO strategy_registry_decisions(
             id,snapshot_id,run_id,strategy_name,strategy_version,authorized,policy_state,
             reasons_json,reason,decision_json,raw_inputs_json,evidence_version,performance_version,
             policy_version,policy_schema_version,configuration_version,config_hash,decision_fingerprint,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("registry-decision", "registry-1", "run-1", "Rule", "rule_based_v2", 1, "ACTIVE",
         "[]", "authorized", "{}", "{}", "e", "p", "policy", "schema", "config", "hash", "decision-fp", now),
    )
    payload = {
        "authorized_strategies": ["rule_based_v2"],
        "registry_snapshot_id": "registry-1",
        "raw_replay_inputs": {"portfolio_snapshot": {
            "portfolio_equity": 10_000.0,
            "active_reservation_ids_by_strategy": {},
            "pending_proposal_claims_by_strategy": {},
        }},
        "strategy_sleeves": {
            "rule_based_v2": {
                "strategy_version": "rule_based_v2",
                "risk_unit": "pct_equity",
                "remaining_risk": 0.01,
                "remaining_notional": 10.0,
            }
        },
    }
    storage.execute(
        """INSERT INTO phase4_allocation_decisions(
             id,run_id,decided_at,mode,allocator_version,strategy_weights_json,cash_weight,
             fractional_kelly_ceiling,marginal_risk_json,component_risk_json,regime,drawdown_pct,
             uncertainty_penalty,data_quality,decision,reason,evidence_fingerprint,payload)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("allocation-1", "run-1", now, "ACTIVE_ADAPTIVE_PAPER", "allocator", "{}", 1.0,
         0.25, "{}", "{}", "normal", 0.0, 1.0, 1.0, "ALLOCATE", "test", "allocation-fp", json.dumps(payload)),
    )


def _sleeved_proposal(
    identifier: str, *, notional: float, notional_ceiling: float, risk_ceiling: float
) -> dict:
    return {
        "id": identifier, "proposal_id": identifier, "symbol": "SPY", "side": "buy",
        "action": "entry", "notional": notional, "latest_price": 100.0, "stop_price": 99.0,
        "trading_mode": "paper", "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        "strategy_version": "rule_based_v2", "strategy_registry_snapshot_id": "registry-1",
        "strategy_sleeve": "rule_based_v2", "sleeve_allocation_id": "allocation-1",
        "sleeve_notional_ceiling": notional_ceiling, "sleeve_stop_risk_ceiling": risk_ceiling,
    }


def test_atomic_sleeve_uses_persisted_authority_and_rejects_proposal_enlargement(tmp_path) -> None:
    storage = _storage(tmp_path)
    _persist_registry_and_allocation(storage)
    store = DurableExecutionStore(storage)

    with pytest.raises(RuntimeError, match="exceeds canonical persisted allocation"):
        store.create_or_get_intent(
            _sleeved_proposal(
                "inflated", notional=100.0,
                notional_ceiling=1_000_000.0, risk_ceiling=1_000_000.0,
            ),
            run_id="run-1", source_type="proposal",
        )

    intent = store.create_or_get_intent(
        _sleeved_proposal("bounded", notional=5.0, notional_ceiling=10.0, risk_ceiling=1.0),
        run_id="run-1", source_type="proposal",
    )
    assert intent["reserved_notional"] == pytest.approx(5.0)
    assert intent["reserved_stop_risk"] == pytest.approx(0.05)


def test_atomic_sleeve_coordinates_overlapping_allocation_ids(tmp_path) -> None:
    storage = _storage(tmp_path)
    _persist_registry_and_allocation(storage)
    payload = json.loads(storage.fetch_all(
        "SELECT payload FROM phase4_allocation_decisions WHERE id='allocation-1'"
    )[0]["payload"])
    payload["strategy_sleeves"]["rule_based_v2"]["remaining_notional"] = 200.0
    storage.execute(
        "UPDATE phase4_allocation_decisions SET payload=? WHERE id='allocation-1'",
        (json.dumps(payload),),
    )
    base = storage.fetch_all(
        "SELECT * FROM phase4_allocation_decisions WHERE id='allocation-1'"
    )[0]
    storage.execute(
        """INSERT INTO phase4_allocation_decisions(
             id,run_id,decided_at,mode,allocator_version,strategy_weights_json,cash_weight,
             fractional_kelly_ceiling,marginal_risk_json,component_risk_json,regime,drawdown_pct,
             uncertainty_penalty,data_quality,decision,reason,evidence_fingerprint,payload)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("allocation-2", base["run_id"], base["decided_at"], base["mode"], base["allocator_version"],
         base["strategy_weights_json"], base["cash_weight"], base["fractional_kelly_ceiling"],
         base["marginal_risk_json"], base["component_risk_json"], base["regime"], base["drawdown_pct"],
         base["uncertainty_penalty"], base["data_quality"], base["decision"], base["reason"],
         "allocation-fp-2", json.dumps(payload)),
    )
    store = DurableExecutionStore(storage)
    first = _sleeved_proposal("first", notional=60.0, notional_ceiling=200.0, risk_ceiling=1.0)
    store.create_or_get_intent(first, run_id="run-1", source_type="proposal")
    second = _sleeved_proposal("second", notional=60.0, notional_ceiling=200.0, risk_ceiling=1.0)
    second["symbol"] = "QQQ"
    second["sleeve_allocation_id"] = "allocation-2"
    with pytest.raises(RuntimeError, match="strategy sleeve stop-risk ceiling"):
        store.create_or_get_intent(second, run_id="run-1", source_type="proposal")


def test_atomic_sleeve_snapshot_ids_close_read_to_persist_race_without_double_count(tmp_path) -> None:
    storage = _storage(tmp_path)
    _persist_registry_and_allocation(storage)
    payload = json.loads(storage.fetch_all(
        "SELECT payload FROM phase4_allocation_decisions WHERE id='allocation-1'"
    )[0]["payload"])
    payload["strategy_sleeves"]["rule_based_v2"].update({
        "remaining_notional": 200.0, "remaining_risk": 1.0,
    })
    storage.execute(
        "UPDATE phase4_allocation_decisions SET payload=? WHERE id='allocation-1'",
        (json.dumps(payload),),
    )
    store = DurableExecutionStore(storage)
    first = store.create_or_get_intent(
        _sleeved_proposal("gap-first", notional=60.0, notional_ceiling=200.0, risk_ceiling=1.0),
        run_id="run-1", source_type="proposal",
    )

    stale_payload = json.loads(json.dumps(payload))
    stale_payload["raw_replay_inputs"]["portfolio_snapshot"]["active_reservation_ids_by_strategy"] = {}
    now = (datetime.now(UTC) + timedelta(seconds=1)).isoformat()
    base = storage.fetch_all(
        "SELECT * FROM phase4_allocation_decisions WHERE id='allocation-1'"
    )[0]
    storage.execute(
        """INSERT INTO phase4_allocation_decisions(
             id,run_id,decided_at,mode,allocator_version,strategy_weights_json,cash_weight,
             fractional_kelly_ceiling,marginal_risk_json,component_risk_json,regime,drawdown_pct,
             uncertainty_penalty,data_quality,decision,reason,evidence_fingerprint,payload)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("allocation-gap", base["run_id"], now, base["mode"], base["allocator_version"],
         base["strategy_weights_json"], base["cash_weight"], base["fractional_kelly_ceiling"],
         base["marginal_risk_json"], base["component_risk_json"], base["regime"], base["drawdown_pct"],
         base["uncertainty_penalty"], base["data_quality"], base["decision"], base["reason"],
         "allocation-gap-fp", json.dumps(stale_payload)),
    )
    second = _sleeved_proposal("gap-second", notional=60.0, notional_ceiling=200.0, risk_ceiling=1.0)
    second.update({"symbol": "QQQ", "sleeve_allocation_id": "allocation-gap"})
    with pytest.raises(RuntimeError, match="strategy sleeve stop-risk ceiling"):
        store.create_or_get_intent(second, run_id="run-1", source_type="proposal")

    accounted_payload = json.loads(json.dumps(stale_payload))
    accounted_payload["strategy_sleeves"]["rule_based_v2"].update({
        "remaining_notional": 140.0, "remaining_risk": 0.4,
    })
    first_reservation_id = storage.fetch_all(
        "SELECT id FROM risk_reservations WHERE intent_id=?", (first["id"],)
    )[0]["id"]
    accounted_payload["raw_replay_inputs"]["portfolio_snapshot"]["active_reservation_ids_by_strategy"] = {
        "rule_based_v2": [first_reservation_id]
    }
    storage.execute(
        "UPDATE phase4_allocation_decisions SET id=?,evidence_fingerprint=?,payload=? WHERE id='allocation-gap'",
        ("allocation-accounted", "allocation-accounted-fp", json.dumps(accounted_payload)),
    )
    accounted = _sleeved_proposal(
        "accounted-second", notional=40.0, notional_ceiling=140.0, risk_ceiling=0.4
    )
    accounted.update({"symbol": "IWM", "sleeve_allocation_id": "allocation-accounted"})
    intent = store.create_or_get_intent(accounted, run_id="run-1", source_type="proposal")
    assert intent["reserved_stop_risk"] == pytest.approx(0.4)


def test_pending_claim_identity_survives_conversion_to_reservation(tmp_path) -> None:
    storage = _storage(tmp_path)
    _persist_registry_and_allocation(storage)
    payload = json.loads(storage.fetch_all(
        "SELECT payload FROM phase4_allocation_decisions WHERE id='allocation-1'"
    )[0]["payload"])
    payload["strategy_sleeves"]["rule_based_v2"].update({
        "remaining_notional": 200.0, "remaining_risk": 0.01,
    })
    storage.execute(
        "UPDATE phase4_allocation_decisions SET payload=? WHERE id='allocation-1'",
        (json.dumps(payload),),
    )
    store = DurableExecutionStore(storage)
    converted = store.create_or_get_intent(
        _sleeved_proposal(
            "pending-a", notional=60.0, notional_ceiling=200.0, risk_ceiling=1.0
        ),
        run_id="run-1", source_type="proposal",
    )
    assert converted["reserved_stop_risk"] == pytest.approx(0.6)

    next_payload = json.loads(json.dumps(payload))
    next_payload["strategy_sleeves"]["rule_based_v2"].update({
        "remaining_notional": 40.0, "remaining_risk": 0.004,
    })
    next_payload["raw_replay_inputs"]["portfolio_snapshot"].update({
        "active_reservation_ids_by_strategy": {},
        "pending_proposal_claims_by_strategy": {
            "rule_based_v2": [{
                "proposal_id": "pending-a", "notional": 60.0, "stop_risk": 0.6,
            }],
        },
    })
    base = storage.fetch_all(
        "SELECT * FROM phase4_allocation_decisions WHERE id='allocation-1'"
    )[0]
    storage.execute(
        """INSERT INTO phase4_allocation_decisions(
             id,run_id,decided_at,mode,allocator_version,strategy_weights_json,cash_weight,
             fractional_kelly_ceiling,marginal_risk_json,component_risk_json,regime,drawdown_pct,
             uncertainty_penalty,data_quality,decision,reason,evidence_fingerprint,payload)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("allocation-after-pending", base["run_id"], datetime.now(UTC).isoformat(), base["mode"],
         base["allocator_version"], base["strategy_weights_json"], base["cash_weight"],
         base["fractional_kelly_ceiling"], base["marginal_risk_json"], base["component_risk_json"],
         base["regime"], base["drawdown_pct"], base["uncertainty_penalty"], base["data_quality"],
         base["decision"], base["reason"], "after-pending-fp", json.dumps(next_payload)),
    )
    candidate = _sleeved_proposal(
        "candidate-b", notional=40.0, notional_ceiling=40.0, risk_ceiling=0.4
    )
    candidate.update({"symbol": "QQQ", "sleeve_allocation_id": "allocation-after-pending"})
    intent = store.create_or_get_intent(candidate, run_id="run-1", source_type="proposal")
    assert intent["reserved_notional"] == pytest.approx(40.0)
    assert intent["reserved_stop_risk"] == pytest.approx(0.4)


def test_phase4_strategy_notional_uses_current_broker_mark_and_reconciles_lots(tmp_path) -> None:
    storage = _storage(tmp_path)
    now = datetime.now(UTC).isoformat()
    storage.execute(
        """INSERT INTO position_management_state(
             id,symbol,position_lifecycle_id,initial_stop_price,created_at,updated_at)
           VALUES(?,?,?,?,?,?)""",
        ("pm", "SPY", "life", 90.0, now, now),
    )
    storage.execute(
        """INSERT INTO position_lots(
             id,position_lifecycle_id,symbol,source_fill_event_key,opened_at,original_quantity,remaining_quantity,
             unit_cost,fees_allocated,source,provenance,confidence,entry_proposal_id,entry_intent_id,
             strategy_version,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("lot", "life", "SPY", "fill", now, 2.0, 2.0, 50.0, 0.0, "test", "{}", "high",
         "proposal", "order", "rule_based_v2", now, now),
    )
    service = TradingService.__new__(TradingService)
    service.storage = storage
    result = service._phase4_strategy_consumption(
        [SimpleNamespace(symbol="SPY", qty=2.0, current_price=120.0)], 10_000.0
    )

    assert result["complete"] is True
    assert result["notional_dollars"]["rule_based_v2"] == pytest.approx(240.0)
    assert result["risk_pct"]["rule_based_v2"] == pytest.approx(0.6)


def test_phase4_strategy_consumption_includes_pending_nonrotation_claims(tmp_path) -> None:
    storage = _storage(tmp_path)
    now = datetime.now(UTC).isoformat()
    storage.execute(
        """INSERT INTO trade_proposals(
             id,run_id,symbol,side,status,notional,created_at,expires_at,strategy_version,payload)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        ("pending", "run", "SPY", "buy", "pending", 100.0, now,
         (datetime.now(UTC) + timedelta(minutes=5)).isoformat(), "rule_based_v2",
         json.dumps({"qty": 1.0, "latest_price": 100.0, "stop_risk_dollars": 2.0,
                     "strategy_version": "rule_based_v2"})),
    )
    service = TradingService.__new__(TradingService)
    service.storage = storage
    result = service._phase4_strategy_consumption([], 10_000.0)
    assert result["complete"] is True
    assert result["notional_dollars"]["rule_based_v2"] == pytest.approx(100.0)
    assert result["risk_pct"]["rule_based_v2"] == pytest.approx(0.02)
    assert result["pending_proposal_claims_by_strategy"] == {
        "rule_based_v2": [{
            "proposal_id": "pending", "notional": 100.0, "stop_risk": 2.0,
        }]
    }


def _rotation(storage: Storage) -> tuple[RotationCoordinator, dict]:
    manager = RotationCoordinator(storage, config_hash="hash")
    evaluated_at = datetime.now(UTC)
    group = manager.create_group(
        run_id="run", expires_at=evaluated_at + timedelta(minutes=10), evaluation_time=evaluated_at,
        exit_legs=[{"proposal_id": "exit-1", "position_lifecycle_id": "lifecycle-old", "symbol": "OLD", "side": "sell", "quantity": 1, "estimated_notional": 100}],
        contingent_entries=[{"proposal_id": "buy-1", "candidate_key": "setup", "strategy_version": "rule_based_v2",
                             "symbol": "NEW", "side": "buy", "max_quantity": 1, "max_notional": 100,
                             "max_stop_risk": 5, "payload": {"action": "entry"}}],
    )
    return manager, group


def test_rotation_dedupes_logical_exit_and_enforces_group_ceiling_fingerprint(tmp_path) -> None:
    storage = _storage(tmp_path)
    manager, group = _rotation(storage)
    with pytest.raises(RuntimeError, match="conflicting active rotation"):
        evaluated_at = datetime.now(UTC)
        manager.create_group(
            run_id="run-2", expires_at=evaluated_at + timedelta(minutes=10), evaluation_time=evaluated_at,
            exit_legs=[{"proposal_id": "exit-2", "position_lifecycle_id": "lifecycle-old", "symbol": "OLD", "side": "sell", "quantity": 1, "estimated_notional": 100}],
            contingent_entries=[{"proposal_id": "buy-2", "candidate_key": "other", "strategy_version": "rule_based_v2",
                                 "symbol": "NEW2", "side": "buy", "max_quantity": 1, "max_notional": 100,
                                 "max_stop_risk": 5, "payload": {}}],
        )
    manager.approve(group["id"], approval_id="approval", sender_id="owner", command="APPROVE ROTATION")
    storage.execute(
        "UPDATE rotation_group_approvals SET status='exit_submitted',consumed_at=? WHERE group_id=?",
        (datetime.now(UTC).isoformat(), group["id"]),
    )
    assert manager.approval_is_current(group["id"])
    storage.execute(
        "UPDATE rotation_contingent_entries SET displayed_max_notional=displayed_max_notional+1 WHERE group_id=?",
        (group["id"],),
    )
    assert manager.approval_is_current(group["id"]) is False


def test_runtime_guard_rejects_missing_phase4_runtime_table(tmp_path) -> None:
    storage = _storage(tmp_path)
    storage.execute("DROP TABLE phase4_covariance_snapshots")
    with pytest.raises(RuntimeError, match="phase4_covariance_snapshots"):
        storage.require_runtime_schema(production=False)


@pytest.mark.parametrize("table", ["phase3_strategy_allocations", "phase3_strategy_states", "account_equity_watermarks"])
def test_runtime_guard_rejects_missing_phase3_runtime_authority(tmp_path, table) -> None:
    storage = _storage(tmp_path)
    storage.execute(f"DROP TABLE {table}")
    with pytest.raises(RuntimeError, match=table):
        storage.require_runtime_schema(production=False)
