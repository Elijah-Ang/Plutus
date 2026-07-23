from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from app.allocation_authority import (
    ALLOCATION_AUTHORITY_VERSION,
    allocation_authority_fingerprint,
    allocation_identity,
)
from app.rotation_coordinator import (
    RotationCoordinator,
    RotationState,
    apply_rotation_schema,
    parse_rotation_approval,
)
from app.execution import DurableExecutionStore, ExecutionResult
from app.approval_workflow import ApprovalWorkflowState, ApprovalWorkflowStore
from app.formula_versions import (
    CONFIGURATION_SCHEMA_VERSION,
    EVIDENCE_VERSION,
    PHASE4_ALLOCATION_VERSION,
    PHASE4_ALLOCATOR_VERSION,
    PHASE4_SCHEMA_VERSION,
    STRATEGY_PERFORMANCE_SCHEMA_VERSION,
    STRATEGY_PERFORMANCE_VERSION,
    STRATEGY_POLICY_VERSION,
    STRATEGY_EXECUTION_REGISTRY_FORMULA_VERSION,
    STRATEGY_EXECUTION_REGISTRY_SCHEMA_VERSION,
)
from app.order_state import OrderState
from app.service import TradingService
from app.storage import Storage
from app.strategy_execution_registry import StrategyExecutionRegistry, persist


CONFIG_HASH = "a" * 64


@pytest.fixture()
def coordinator(tmp_path):
    storage = Storage(tmp_path / "rotation.sqlite3")
    storage.initialize()
    with storage.connect() as conn:
        apply_rotation_schema(conn)
    now_dt = datetime.now(UTC)
    now = now_dt.isoformat()
    config = {
        "configuration_schema_version": CONFIGURATION_SCHEMA_VERSION,
        "effective_config_hash": CONFIG_HASH,
        "mode": "paper",
        "live_enabled": False,
        "auto_execution_enabled": False,
        "execution_capabilities": {"live_execution_enabled": False},
        "strategy_execution_registry": {
            "schema_version": STRATEGY_EXECUTION_REGISTRY_SCHEMA_VERSION,
            "formula_version": STRATEGY_EXECUTION_REGISTRY_FORMULA_VERSION,
            "mode": "paper_only",
            "required_configuration_version": CONFIGURATION_SCHEMA_VERSION,
            "required_evidence_version": EVIDENCE_VERSION,
            "required_performance_version": STRATEGY_PERFORMANCE_VERSION,
            "required_policy_version": STRATEGY_POLICY_VERSION,
            "required_policy_schema_version": STRATEGY_PERFORMANCE_SCHEMA_VERSION,
            "entries": {"rule_based_v2": {
                "strategy_name": "Rule",
                "strategy_version": "rule_based_v2",
                "implementation_id": "implementation:rule_based_v2",
                "implementation_version": "implementation_v1",
                "implementation_available": True,
                "execution_eligible": True,
                "paper_eligible": True,
                "live_eligible": False,
                "human_authorized": True,
                "config_authorized": True,
                "authorization_id": "rotation-test-paper-authority",
                "effective_at": "2026-01-01T00:00:00+00:00",
                "expires_at": "2027-01-01T00:00:00+00:00",
                "suspended": False,
            }},
        },
    }
    policy = {
        "id": "rotation-test-policy-decision",
        "strategy_version": "rule_based_v2",
        "state": "ACTIVE",
        "quality_score": 85.0,
        "reason": "test ACTIVE policy",
        "performance_snapshot_id": "rotation-test-performance-snapshot",
        "decided_at": now,
        "maturity": {"test": True},
        "metrics": {"quality_score": 85.0},
        "enforcement_enabled": True,
        "evidence_current": True,
        "evidence_version_complete": True,
        "evidence_version": EVIDENCE_VERSION,
        "performance_version": STRATEGY_PERFORMANCE_VERSION,
        "policy_version": STRATEGY_POLICY_VERSION,
        "schema_version": STRATEGY_PERFORMANCE_SCHEMA_VERSION,
        "configuration_version": CONFIGURATION_SCHEMA_VERSION,
        "config_hash": CONFIG_HASH,
        "suspended": False,
        "fingerprint": "rotation-test-policy",
    }
    authorities: dict[str, str] = {}
    for index, label in enumerate(("before", "after")):
        evaluated_at = (now_dt + timedelta(microseconds=index)).isoformat()
        evaluation = StrategyExecutionRegistry(
            config,
            available_implementations={
                "implementation:rule_based_v2": "implementation_v1"
            },
        ).evaluate({"rule_based_v2": policy}, as_of=evaluated_at)
        snapshot_id = str(persist(storage, "run-1", evaluation)["snapshot_id"])
        payload = {
            "schema_version": PHASE4_SCHEMA_VERSION,
            "allocation_authority_version": ALLOCATION_AUTHORITY_VERSION,
            "formula_version": PHASE4_ALLOCATION_VERSION,
            "config_hash": CONFIG_HASH,
            "registry_snapshot_id": snapshot_id,
            "authorized_strategies": ["rule_based_v2"],
            "registry_evaluation": evaluation.as_dict(),
            "strategy_order": ["rule_based_v2"],
            "evidence_versions": {"rule_based_v2": EVIDENCE_VERSION},
            "strategy_sleeves": {"rule_based_v2": {
                "strategy_version": "rule_based_v2",
                "risk_unit": "stop_risk_dollars",
                "remaining_risk": 25.0,
                "remaining_notional": 500.0,
            }},
            "raw_replay_inputs": {
                "as_of": evaluated_at,
                "regime": "normal",
                "drawdown_pct": 0.0,
                "portfolio_snapshot": {
                    "strategy_registry_snapshot_id": snapshot_id,
                },
                "registry": evaluation.as_dict(),
                "strategy_order": ["rule_based_v2"],
                "authorized_strategy_order": ["rule_based_v2"],
                "evidence_fingerprints": {},
                "covariance_inputs": {},
                "available_risk_inputs": {},
                "configuration_hash": CONFIG_HASH,
                "formula_version": PHASE4_ALLOCATION_VERSION,
            },
        }
        payload["allocation_authority_fingerprint"] = (
            allocation_authority_fingerprint(payload)
        )
        weights = {"rule_based_v2": 1.0}
        evidence_fingerprint, allocation_id = allocation_identity(
            "run-1", payload["raw_replay_inputs"], weights,
            payload["strategy_sleeves"], authority_payload=payload,
        )
        storage.execute(
            """INSERT INTO phase4_allocation_decisions(
                 id,run_id,decided_at,mode,allocator_version,strategy_weights_json,cash_weight,
                 fractional_kelly_ceiling,marginal_risk_json,component_risk_json,regime,drawdown_pct,
                 uncertainty_penalty,data_quality,decision,reason,evidence_versions_json,
                 evidence_fingerprint,formula_version,config_hash,payload)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (allocation_id, "run-1", evaluated_at, "ACTIVE_ADAPTIVE_PAPER",
             PHASE4_ALLOCATOR_VERSION, json.dumps(weights), 0.0, 0.25, "{}", "{}",
             "normal", 0.0, 0.0, 1.0, "ALLOCATE_ADAPTIVELY", "test",
             json.dumps(payload["evidence_versions"]), evidence_fingerprint,
             PHASE4_ALLOCATION_VERSION, CONFIG_HASH, json.dumps(payload)),
        )
        authorities[f"registry-{label}"] = snapshot_id
        authorities[f"allocation-{label}"] = allocation_id
    storage.execute(
        """INSERT INTO position_lifecycles(
             id,symbol,side,state,opened_at,opening_quantity,current_quantity,average_entry_price,
             source,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        ("lifecycle-old", "OLD", "long", "active", now, 10.0, 10.0, 100.0, "test", now, now),
    )
    manager = RotationCoordinator(storage, config_hash=CONFIG_HASH)
    manager._test_authority_ids = authorities
    return storage, manager


def _create(coordinator, *, entries=None, exits=None, minutes=30,
            registry_snapshot_id="registry-before", allocation_id="allocation-before"):
    _storage, manager = coordinator
    authority_ids = getattr(manager, "_test_authority_ids", {})
    registry_snapshot_id = authority_ids.get(
        registry_snapshot_id, registry_snapshot_id
    )
    allocation_id = authority_ids.get(allocation_id, allocation_id)
    evaluated_at = datetime.now(UTC)
    exit_rows = exits or [{
        "proposal_id": "exit-proposal",
        "symbol": "OLD",
        "side": "sell",
        "quantity": 10,
        "estimated_notional": 1000,
        "reason": "current exit required",
    }]
    exit_rows = [{**row, "position_lifecycle_id": row.get("position_lifecycle_id", "lifecycle-old")} for row in exit_rows]
    entry_rows = entries or [{
        "candidate_key": "rule_based_v2:NEW:entry:setup-1",
        "strategy_version": "rule_based_v2",
        "symbol": "NEW",
        "side": "buy",
        "max_quantity": 5,
        "max_notional": 500,
        "max_stop_risk": 25,
        "payload": {"shown_to_operator": True},
    }]
    entry_rows = [{**row, "proposal_id": row.get("proposal_id", "entry-proposal")} for row in entry_rows]
    return manager.create_group(
        run_id="run-1",
        exit_legs=exit_rows,
        contingent_entries=entry_rows,
        expires_at=evaluated_at + timedelta(minutes=minutes),
        evaluation_time=evaluated_at,
        registry_snapshot_id=registry_snapshot_id,
        allocation_id=allocation_id,
    )


def _approve_and_submit_exit(coordinator, group):
    _storage, manager = coordinator
    manager.approve(group["id"], approval_id="approval-1", sender_id="owner", command=f"APPROVE ROTATION {group['id'][:8]}")
    step = manager.steps(group["id"])[0]
    manager.record_exit_submitted(group["id"], step_id=step["id"], intent_id="exit-intent")
    return step


def test_rotation_command_requires_explicit_group_target(coordinator):
    _storage, manager = coordinator
    group = _create(coordinator)
    assert not parse_rotation_approval("yes", [group]).accepted
    assert not parse_rotation_approval("approve OLD", [group]).accepted
    parsed = parse_rotation_approval(f"APPROVE ROTATION {group['id'][:8]}", [group])
    assert parsed.accepted
    assert parsed.group_id == group["id"]


@pytest.mark.parametrize("mutation", [
    "exit_quantity", "exit_symbol", "exit_proposal", "exit_ordering", "lifecycle",
    "contingent_entry", "authority_ids", "expiry", "reply_target",
])
def test_group_display_binds_complete_workflow_before_any_exit_intent(coordinator, mutation):
    storage, manager = coordinator
    exits = [
        {"proposal_id": "exit-1", "position_lifecycle_id": "lifecycle-old", "symbol": "OLD",
         "side": "sell", "quantity": 3, "estimated_notional": 300, "reason": "risk one"},
        {"proposal_id": "exit-2", "position_lifecycle_id": "lifecycle-old", "symbol": "OLD2",
         "side": "sell", "quantity": 2, "estimated_notional": 200, "reason": "risk two"},
    ]
    group = _create(coordinator, exits=exits)
    manager.record_group_display(group["id"], "telegram-900")
    if mutation == "exit_quantity":
        storage.execute("UPDATE rotation_steps SET requested_quantity=requested_quantity+1 WHERE group_id=? AND sequence=0", (group["id"],))
    elif mutation == "exit_symbol":
        storage.execute("UPDATE rotation_steps SET symbol='MUTATED' WHERE group_id=? AND sequence=0", (group["id"],))
    elif mutation == "exit_proposal":
        storage.execute("UPDATE rotation_steps SET proposal_id='other' WHERE group_id=? AND sequence=0", (group["id"],))
    elif mutation == "exit_ordering":
        storage.execute("UPDATE rotation_steps SET sequence=99 WHERE group_id=? AND sequence=0", (group["id"],))
    elif mutation == "lifecycle":
        row = storage.fetch_all("SELECT id,payload FROM rotation_steps WHERE group_id=? ORDER BY sequence LIMIT 1", (group["id"],))[0]
        payload = json.loads(row["payload"])
        payload["position_lifecycle_id"] = "other-lifecycle"
        storage.execute("UPDATE rotation_steps SET payload=? WHERE id=?", (json.dumps(payload), row["id"]))
    elif mutation == "contingent_entry":
        storage.execute("UPDATE rotation_contingent_entries SET candidate_key='other-entry' WHERE group_id=?", (group["id"],))
    elif mutation == "authority_ids":
        storage.execute("UPDATE rotation_groups SET allocation_id='other-allocation' WHERE id=?", (group["id"],))
    elif mutation == "expiry":
        storage.execute("UPDATE rotation_groups SET expires_at=? WHERE id=?", ((datetime.now(UTC)-timedelta(seconds=1)).isoformat(), group["id"]))
    reply = "wrong-message" if mutation == "reply_target" else "telegram-900"
    try:
        result = manager.approve(
            group["id"], approval_id=f"approval-{mutation}", sender_id="owner",
            command=f"APPROVE ROTATION {group['id'][:8]}", reply_to_message_id=reply,
        )
        assert result["state"] != RotationState.APPROVED_EXIT_PENDING.value
    except RuntimeError:
        pass
    assert storage.fetch_all("SELECT COUNT(*) n FROM order_intents")[0]["n"] == 0
    assert storage.fetch_all("SELECT COUNT(*) n FROM risk_reservations")[0]["n"] == 0


def test_rotation_creation_requires_both_authority_ids(coordinator):
    with pytest.raises(ValueError, match="registry_snapshot_id and allocation_id"):
        _create(coordinator, registry_snapshot_id="")


@pytest.mark.parametrize("mutation", ["stale", "config_hash", "decision", "allocation"])
def test_rotation_approval_fails_closed_for_stale_or_mismatched_authority(coordinator, mutation):
    storage, manager = coordinator
    group = _create(coordinator)
    snapshot_id = manager._test_authority_ids["registry-before"]
    if mutation == "stale":
        storage.execute(
            "UPDATE strategy_registry_snapshots SET evaluated_at=? WHERE id=?",
            ((datetime.now(UTC) - timedelta(minutes=6)).isoformat(), snapshot_id),
        )
    elif mutation == "config_hash":
        storage.execute(
            "UPDATE strategy_registry_snapshots SET config_hash='wrong-hash' WHERE id=?",
            (snapshot_id,),
        )
    elif mutation == "decision":
        storage.execute(
            "UPDATE strategy_registry_decisions SET authorized=0 WHERE snapshot_id=?",
            (snapshot_id,),
        )
    else:
        allocation_id = manager._test_authority_ids["allocation-before"]
        payload = json.loads(storage.fetch_all(
            "SELECT payload FROM phase4_allocation_decisions WHERE id=?",
            (allocation_id,),
        )[0]["payload"])
        payload["strategy_sleeves"]["rule_based_v2"]["remaining_notional"] = 999999
        storage.execute(
            "UPDATE phase4_allocation_decisions SET payload=? WHERE id=?",
            (json.dumps(payload), allocation_id),
        )
    with pytest.raises(RuntimeError, match="rotation approval blocked"):
        manager.approve(group["id"], approval_id="authority-test", sender_id="owner", command="APPROVE ROTATION")
    assert manager.get_group(group["id"])["state"] == RotationState.CANCELLED.value


def test_rotation_approval_fails_closed_for_nonexistent_authority(coordinator):
    _storage, manager = coordinator
    group = _create(coordinator, registry_snapshot_id="missing-registry", allocation_id="missing-allocation")
    with pytest.raises(RuntimeError, match="rotation approval blocked"):
        manager.approve(group["id"], approval_id="missing-authority", sender_id="owner", command="APPROVE ROTATION")
    assert manager.get_group(group["id"])["state"] == RotationState.CANCELLED.value


def test_ambiguous_group_prefix_is_rejected():
    future = (datetime.now(UTC) + timedelta(minutes=10)).isoformat()
    rows = [
        {"id": "abcdef111111", "state": "pending_group_approval", "expires_at": future},
        {"id": "abcdef222222", "state": "pending_group_approval", "expires_at": future},
    ]
    assert parse_rotation_approval("approve rotation abcdef", rows).reason.endswith("ambiguous")


def test_group_creation_is_idempotent_and_never_reserves_expected_proceeds(coordinator):
    storage, manager = coordinator
    before_intents = storage.fetch_all("SELECT COUNT(*) n FROM order_intents")[0]["n"]
    before_reservations = storage.fetch_all("SELECT COUNT(*) n FROM risk_reservations")[0]["n"]
    first = _create(coordinator)
    second = _create(coordinator)
    assert first["id"] == second["id"]
    assert first["actual_released_notional"] == 0
    assert storage.fetch_all("SELECT COUNT(*) n FROM order_intents")[0]["n"] == before_intents
    assert storage.fetch_all("SELECT COUNT(*) n FROM risk_reservations")[0]["n"] == before_reservations
    assert manager.entries(first["id"])[0]["state"] == "contingent"


def test_full_fill_rotation_uses_only_reconciled_capacity_and_never_enlarges(coordinator):
    storage, manager = coordinator
    group = _create(coordinator)
    _approve_and_submit_exit(coordinator, group)
    manager.record_exit_fill(
        group["id"], intent_id="exit-intent", cumulative_quantity=10,
        cumulative_notional=900, released_risk=30, exit_complete=True,
    )
    manager.begin_reconciliation(group["id"])
    manager.record_reconciliation(group["id"], cash=800, buying_power=800, snapshot_fingerprint="snapshot-1")
    entry = manager.entries(group["id"])[0]
    result = manager.revalidate_entry(
        group["id"], entry["id"], candidate_key=entry["candidate_key"], price=100,
        requested_quantity=8, stop_risk_per_share=5, allocation_notional_cap=700,
        allocation_risk_cap=20, other_available_cash=600, minimum_notional=5,
        registry_snapshot_id=manager._test_authority_ids["registry-after"],
        allocation_id=manager._test_authority_ids["allocation-after"],
    )
    assert result.allowed
    assert result.final_quantity == 4
    assert result.final_notional == 400
    assert result.final_stop_risk == 20
    assert result.final_quantity <= entry["displayed_max_quantity"]
    assert result.final_notional <= entry["displayed_max_notional"]
    assert result.final_stop_risk <= entry["displayed_max_stop_risk"]
    assert storage.fetch_all("SELECT COUNT(*) n FROM risk_reservations")[0]["n"] == 0
    manager.record_entry_reserved(group["id"], entry["id"], intent_id="entry-intent")
    manager.record_entry_submitted(group["id"], entry["id"], intent_id="entry-intent")
    assert manager.get_group(group["id"])["state"] == RotationState.ENTRY_SUBMITTED.value
    with pytest.raises(RuntimeError, match="authoritative entry fill"):
        manager.complete(group["id"])


def test_partial_fill_caps_entry_at_actual_release_not_estimate(coordinator):
    _storage, manager = coordinator
    group = _create(coordinator)
    _approve_and_submit_exit(coordinator, group)
    manager.record_exit_fill(
        group["id"], intent_id="exit-intent", cumulative_quantity=2,
        cumulative_notional=175, released_risk=6, exit_complete=False,
    )
    manager.begin_reconciliation(group["id"])
    manager.record_reconciliation(group["id"], cash=1000, buying_power=1000, snapshot_fingerprint="partial-snapshot")
    entry = manager.entries(group["id"])[0]
    result = manager.revalidate_entry(
        group["id"], entry["id"], candidate_key=entry["candidate_key"], price=50,
        requested_quantity=10, stop_risk_per_share=1, allocation_notional_cap=500,
        allocation_risk_cap=25, other_available_cash=1000, minimum_notional=5,
        registry_snapshot_id=manager._test_authority_ids["registry-after"],
        allocation_id=manager._test_authority_ids["allocation-after"],
    )
    assert result.allowed
    assert result.final_notional == 175
    assert result.binding_cap == "actual_exit_release"


def test_later_exit_fill_is_accounted_without_enlarging_or_reviving_dependency(coordinator):
    storage, manager = coordinator
    group = _create(coordinator)
    _approve_and_submit_exit(coordinator, group)
    manager.record_exit_fill(
        group["id"], intent_id="exit-intent", cumulative_quantity=2,
        cumulative_notional=175, released_risk=6, exit_complete=False,
    )
    manager.begin_reconciliation(group["id"])
    manager.record_reconciliation(
        group["id"], cash=1000, buying_power=1000, snapshot_fingerprint="partial-snapshot"
    )
    entry = manager.entries(group["id"])[0]
    manager.revalidate_entry(
        group["id"], entry["id"], candidate_key=entry["candidate_key"], price=50,
        requested_quantity=1, stop_risk_per_share=1, allocation_notional_cap=500,
        allocation_risk_cap=25, other_available_cash=1000, minimum_notional=5,
        registry_snapshot_id=manager._test_authority_ids["registry-after"],
        allocation_id=manager._test_authority_ids["allocation-after"],
    )
    before = manager.get_group(group["id"])
    assert before["state"] == RotationState.ENTRY_REVALIDATING.value
    manager.record_exit_fill(
        group["id"], intent_id="exit-intent", cumulative_quantity=10,
        cumulative_notional=900, released_risk=30, exit_complete=True,
    )
    after = manager.get_group(group["id"])
    assert after["state"] == RotationState.ENTRY_REVALIDATING.value
    assert after["actual_released_notional"] == pytest.approx(900.0)
    event = storage.fetch_all(
        "SELECT event_type,safe_detail FROM rotation_events WHERE group_id=? AND event_type='late_exit_fill_reconciled'",
        (group["id"],),
    )
    assert len(event) == 1


def test_partial_fill_recovery_can_link_every_approved_exit_leg(coordinator):
    _storage, manager = coordinator
    group = _create(coordinator, exits=[
        {"proposal_id": "exit-a", "symbol": "OLD", "side": "sell", "quantity": 5, "estimated_notional": 500},
            {"proposal_id": "exit-b", "symbol": "OLD", "side": "sell", "quantity": 5, "estimated_notional": 500},
    ])
    manager.approve(group["id"], approval_id="approval", sender_id="owner", command="APPROVE ROTATION")
    first, second = manager.steps(group["id"])
    manager.record_exit_submitted(group["id"], step_id=first["id"], intent_id="intent-a")
    manager.record_exit_fill(
        group["id"], intent_id="intent-a", cumulative_quantity=1,
        cumulative_notional=100, released_risk=2, exit_complete=False,
    )
    manager.record_exit_submitted(group["id"], step_id=second["id"], intent_id="intent-b")
    linked = manager.steps(group["id"])
    assert {row["intent_id"] for row in linked} == {"intent-a", "intent-b"}
    assert manager.get_group(group["id"])["state"] == RotationState.EXIT_PARTIALLY_FILLED.value


def test_reconciliation_cannot_skip_authoritative_fill(coordinator):
    _storage, manager = coordinator
    group = _create(coordinator)
    _approve_and_submit_exit(coordinator, group)
    with pytest.raises(RuntimeError):
        manager.record_reconciliation(group["id"], cash=1000, buying_power=1000, snapshot_fingerprint="bad")


@pytest.mark.parametrize("reason", ["exit rejected", "exit cancelled", "broker error", "exit expired"])
def test_exit_failure_terminates_dependent_entries(coordinator, reason):
    _storage, manager = coordinator
    group = _create(coordinator)
    _approve_and_submit_exit(coordinator, group)
    terminal = manager.fail_exit(group["id"], reason=reason)
    assert terminal["state"] == RotationState.EXIT_FAILED.value
    assert terminal["terminal_reason"] == reason
    assert manager.entries(group["id"])[0]["state"] == "blocked"


def test_material_candidate_change_requires_new_approval(coordinator):
    _storage, manager = coordinator
    group = _create(coordinator)
    _approve_and_submit_exit(coordinator, group)
    manager.record_exit_fill(group["id"], intent_id="exit-intent", cumulative_quantity=1,
                             cumulative_notional=100, released_risk=5, exit_complete=True)
    manager.begin_reconciliation(group["id"])
    manager.record_reconciliation(group["id"], cash=100, buying_power=100,
                                  snapshot_fingerprint="snapshot")
    entry = manager.entries(group["id"])[0]
    result = manager.revalidate_entry(
        group["id"], entry["id"], candidate_key="different-logical-action", price=100,
        requested_quantity=1, stop_risk_per_share=5, allocation_notional_cap=100,
        allocation_risk_cap=5, other_available_cash=100, minimum_notional=5,
        registry_snapshot_id=manager._test_authority_ids["registry-after"],
        allocation_id=manager._test_authority_ids["allocation-after"],
    )
    assert not result.allowed
    assert manager.get_group(group["id"])["state"] == RotationState.ENTRY_BLOCKED.value


def test_duplicate_fill_event_and_notification_are_one_shot(coordinator):
    storage, manager = coordinator
    group = _create(coordinator)
    _approve_and_submit_exit(coordinator, group)
    for _ in range(2):
        manager.record_exit_fill(group["id"], intent_id="exit-intent", cumulative_quantity=2,
                                 cumulative_notional=200, released_risk=4, exit_complete=False)
    events = storage.fetch_all(
        "SELECT event_key FROM rotation_events WHERE group_id=? AND event_type='exit_partially_filled'",
        (group["id"],),
    )
    assert len(events) == 1
    assert manager.claim_notification(group["id"], events[0]["event_key"])
    assert not manager.claim_notification(group["id"], events[0]["event_key"])
    manager.mark_notification_sent(group["id"], events[0]["event_key"])


def test_restart_recovery_never_submits_and_resumes_reconciliation(coordinator):
    storage, manager = coordinator
    group = _create(coordinator)
    _approve_and_submit_exit(coordinator, group)
    manager.record_exit_fill(group["id"], intent_id="exit-intent", cumulative_quantity=2,
                             cumulative_notional=200, released_risk=4, exit_complete=False)
    restarted = RotationCoordinator(storage, config_hash=CONFIG_HASH)
    action = next(row for row in restarted.recovery_actions() if row["group_id"] == group["id"])
    assert action["action"] == "reconcile_exit_only"
    assert action["broker_submission_allowed"] is False


def test_expired_pending_group_is_terminal_and_not_revived(coordinator):
    storage, manager = coordinator
    group = _create(coordinator)
    storage.execute(
        "UPDATE rotation_groups SET expires_at=? WHERE id=?",
        ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), group["id"]),
    )
    assert manager.expire_stale() == 1
    assert manager.get_group(group["id"])["state"] == RotationState.EXPIRED.value
    assert manager.transition(group["id"], RotationState.EXPIRED, reason="again")["state"] == RotationState.EXPIRED.value


def test_expired_partially_submitted_group_never_authorizes_missing_exit_leg(coordinator):
    storage, manager = coordinator
    group = _create(coordinator, exits=[
        {"proposal_id": "exit-a", "symbol": "OLD", "side": "sell", "quantity": 5, "estimated_notional": 500},
        {"proposal_id": "exit-b", "symbol": "OLDER", "side": "sell", "quantity": 5, "estimated_notional": 500},
    ])
    manager.approve(group["id"], approval_id="approval", sender_id="owner", command="APPROVE ROTATION")
    first, second = manager.steps(group["id"])
    manager.record_exit_submitted(group["id"], step_id=first["id"], intent_id="existing-intent")
    storage.execute(
        "UPDATE rotation_groups SET expires_at=? WHERE id=?",
        ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), group["id"]),
    )

    assert manager.expire_stale() == 1
    assert manager.get_group(group["id"])["state"] == RotationState.EXPIRED.value
    assert manager.steps(group["id"])[1]["intent_id"] is None
    assert manager.recovery_actions()[0]["action"] == "await_manual_or_exit_action"


def test_terminal_partial_exit_quiesces_but_keeps_accounted_fill(coordinator):
    storage, manager = coordinator
    group = _create(coordinator)
    manager.approve(group["id"], approval_id="approval", sender_id="owner", command="APPROVE ROTATION")
    proposal = {
        "id": "exit-proposal", "proposal_id": "exit-proposal", "symbol": "OLD",
        "side": "sell", "action": "exit", "qty": 10.0, "latest_price": 100.0,
        "trading_mode": "paper",
    }
    durable = DurableExecutionStore(storage)
    intent = durable.create_or_get_intent(proposal, run_id="run", source_type="proposal")
    step = manager.steps(group["id"])[0]
    manager.record_exit_submitted(group["id"], step_id=step["id"], intent_id=intent["id"])
    durable.transition(intent["id"], OrderState.SUBMITTING, event_type="test")
    durable.transition(intent["id"], OrderState.SUBMITTED, event_type="test")
    durable.record_fill(intent["id"], cumulative_quantity=2.0, fill_price=100.0, broker_event_key="partial")
    durable.transition(intent["id"], OrderState.CANCELLED, event_type="test")
    manager.record_exit_fill(
        group["id"], intent_id=intent["id"], cumulative_quantity=2.0,
        cumulative_notional=200.0, released_risk=4.0, exit_complete=False,
    )
    manager.fail_exit(group["id"], reason="partial exit cancelled")
    manager.record_exit_terminal(group["id"], intent_id=intent["id"], terminal_state="cancelled")

    assert manager.get_group(group["id"])["actual_released_notional"] == pytest.approx(200.0)
    assert manager.steps(group["id"])[0]["state"] == "terminal_cancelled"
    assert manager.recovery_actions() == []


def test_recovered_exit_submission_lifecycle_event_is_notified(coordinator):
    storage, manager = coordinator
    group = _create(coordinator)
    _approve_and_submit_exit(coordinator, group)

    class Telegram:
        def __init__(self): self.messages = []
        def send_message(self, message): self.messages.append(message)

    service = TradingService.__new__(TradingService)
    service.storage = storage
    service.config = {"effective_config_hash": CONFIG_HASH}
    service.telegram = Telegram()
    service._send_rotation_lifecycle_events()

    assert any("exit submitted" in message.lower() for message in service.telegram.messages)
    assert storage.fetch_all(
        "SELECT notification_sent_at FROM rotation_events WHERE group_id=? AND event_type='exit_submitted'",
        (group["id"],),
    )[0]["notification_sent_at"] is not None


def test_service_recovery_blocks_expired_rotation_before_intent_and_releases_reserved_intent(coordinator):
    storage, manager = coordinator
    group = _create(coordinator)
    manager.approve(group["id"], approval_id="group-approval", sender_id="owner", command="APPROVE ROTATION")
    now = datetime.now(UTC)
    proposal = {
        "id": "exit-proposal", "proposal_id": "exit-proposal", "symbol": "OLD",
        "side": "sell", "action": "exit", "status": "approved", "qty": 10.0,
        "latest_price": 100.0, "trading_mode": "paper",
        "relationship_type": "rotation_exit", "relationship_group_id": group["id"],
        "rotation_group_id": group["id"], "expires_at": (now + timedelta(minutes=5)).isoformat(),
    }
    storage.execute(
        """INSERT INTO trade_proposals(
             id,run_id,symbol,side,status,notional,created_at,expires_at,relationship_type,
             rotation_group_id,payload)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        ("exit-proposal", "run", "OLD", "sell", "approved", 1000.0, now.isoformat(),
         proposal["expires_at"], "rotation_exit", group["id"],
         __import__("json").dumps(proposal)),
    )
    workflow_store = ApprovalWorkflowStore(storage)
    before_intent = workflow_store.accept_approval(
        approval_id="derived-before", run_id="run", proposal_id="exit-proposal", sender_id="owner",
        raw_message="approve", parsed_action="approve", telegram_update_id=101,
        reply_to_message_id=None, targeting_method="test", acknowledgement_status="received",
        approval_received_at=now.isoformat(),
    )
    storage.execute(
        "UPDATE approvals SET status='consumed',consumed_at=? WHERE id='derived-before'",
        (now.isoformat(),),
    )
    workflow_store.transition(before_intent["id"], ApprovalWorkflowState.VALIDATING)
    before_intent = workflow_store.transition(before_intent["id"], ApprovalWorkflowState.APPROVED_PENDING_INTENT)
    storage.execute(
        "UPDATE rotation_groups SET expires_at=? WHERE id=?",
        ((now - timedelta(seconds=1)).isoformat(), group["id"]),
    )
    service = TradingService.__new__(TradingService)
    service.storage = storage
    service.config = {"effective_config_hash": CONFIG_HASH}
    service.broker = None
    service.run_id = "recovery"
    service._recover_local_workflows()
    assert workflow_store.get(before_intent["id"])["state"] == "terminal"
    assert storage.fetch_all("SELECT COUNT(*) n FROM order_intents")[0]["n"] == 0
    storage.execute("UPDATE approvals SET status='superseded' WHERE id='derived-before'")

    storage.execute(
        "UPDATE rotation_groups SET expires_at=? WHERE id=?",
        ((now + timedelta(minutes=5)).isoformat(), group["id"]),
    )
    second = workflow_store.accept_approval(
        approval_id="derived-after", run_id="run", proposal_id="exit-proposal", sender_id="owner",
        raw_message="approve", parsed_action="approve", telegram_update_id=102,
        reply_to_message_id=None, targeting_method="test", acknowledgement_status="received",
        approval_received_at=now.isoformat(),
    )
    storage.execute(
        "UPDATE approvals SET status='consumed',consumed_at=? WHERE id='derived-after'",
        (now.isoformat(),),
    )
    workflow_store.transition(second["id"], ApprovalWorkflowState.VALIDATING)
    second = workflow_store.transition(second["id"], ApprovalWorkflowState.APPROVED_PENDING_INTENT)
    intent = workflow_store.ensure_intent(second["id"], proposal, run_id="run")
    workflow_store.transition(second["id"], ApprovalWorkflowState.SUBMISSION_PENDING)
    storage.execute(
        "UPDATE rotation_groups SET expires_at=? WHERE id=?",
        ((now - timedelta(seconds=1)).isoformat(), group["id"]),
    )
    service._recover_local_workflows()
    assert workflow_store.get(second["id"])["state"] == "terminal"
    assert DurableExecutionStore(storage).get_intent(intent["id"])["state"] == "expired"
    assert storage.fetch_all(
        "SELECT state FROM risk_reservations WHERE intent_id=?", (intent["id"],)
    )[0]["state"] == "released"


def test_final_execution_boundary_rechecks_rotation_expiry(coordinator):
    storage, manager = coordinator
    group = _create(coordinator)
    manager.approve(group["id"], approval_id="group-approval", sender_id="owner", command="APPROVE ROTATION")
    now = datetime.now(UTC)

    class Broker:
        def get_latest_quote(self, _symbol):
            return type("Quote", (), {
                "bid_price": 100.0, "ask_price": 100.01, "timestamp": datetime.now(UTC),
            })()
        def is_market_open(self): return True
        def get_positions(self): return [{"symbol": "OLD", "qty": 10.0}]
        def get_open_orders(self): return []

    service = TradingService.__new__(TradingService)
    service.storage = storage
    service.broker = Broker()
    service.run_id = "final-boundary"
    service.config = {
        "mode": "paper", "live_enabled": False, "effective_config_hash": CONFIG_HASH,
        "telegram": {
            "approval_price_refresh_required": True,
            "approval_max_price_age_seconds": 120,
            "approval_max_price_move_bps": 25,
            "approval_max_price_move_hard_cap_bps": 75,
        },
        "quotes": {"max_age_seconds": 120, "max_spread_bps": 50, "max_limit_slippage_bps": 25, "price_increment_usd": 0.01},
        "position_sizing": {"enabled": False}, "phase3": {"active": False},
        "phase4": {"active": False}, "winner_expansion": {"enabled": False},
    }
    row = {
        "id": "exit-proposal", "symbol": "OLD", "side": "sell", "notional": 1000.0,
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
        "relationship_type": "rotation_exit", "rotation_group_id": group["id"],
    }
    proposal = {
        **row, "proposal_id": "exit-proposal", "action": "exit", "qty": 10.0,
        "latest_price": 100.0, "relationship_group_id": group["id"],
        "trading_mode": "paper",
    }

    def expire_during_final_validation(_proposal, _price):
        storage.execute(
            "UPDATE rotation_groups SET expires_at=? WHERE id=?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), group["id"]),
        )
        return None

    with (
        patch.object(service, "_final_revalidate_position_management", side_effect=expire_during_final_validation),
        patch.object(service, "_authoritative_runtime_state", return_value={"positions": [], "account": None}),
        patch.object(service, "_get_exposure_snapshot", return_value=None),
        patch.object(service, "_portfolio_context", return_value={"approval_valid": True}),
        patch.object(service, "_get_symbol_cluster", return_value="broad"),
        patch("app.service.Executor.execute") as execute,
    ):
        result, *_ = service._execute_final_revalidation(
            row, proposal, "OLD", "sell", False, "derived-approval"
        )

    assert result.submitted is False
    assert "expired or became terminal" in str(result.reason)
    execute.assert_not_called()


def test_service_rotation_recovery_defers_when_broker_unavailable_then_resumes(coordinator):
    storage, manager = coordinator
    group = _create(coordinator)
    manager.approve(group["id"], approval_id="group-approval", sender_id="owner", command="APPROVE ROTATION")
    now = datetime.now(UTC)
    proposal = {
        "id": "exit-proposal", "proposal_id": "exit-proposal", "symbol": "OLD",
        "side": "sell", "action": "exit", "status": "approved", "qty": 10.0,
        "latest_price": 100.0, "trading_mode": "paper",
        "relationship_type": "rotation_exit", "relationship_group_id": group["id"],
        "rotation_group_id": group["id"], "expires_at": (now + timedelta(minutes=5)).isoformat(),
    }
    storage.execute(
        """INSERT INTO trade_proposals(
             id,run_id,symbol,side,status,notional,created_at,expires_at,relationship_type,
             rotation_group_id,payload)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        ("exit-proposal", "run", "OLD", "sell", "approved", 1000.0, now.isoformat(),
         proposal["expires_at"], "rotation_exit", group["id"],
         __import__("json").dumps(proposal)),
    )
    workflow_store = ApprovalWorkflowStore(storage)
    workflow = workflow_store.accept_approval(
        approval_id="derived", run_id="run", proposal_id="exit-proposal", sender_id="owner",
        raw_message="approve", parsed_action="approve", telegram_update_id=201,
        reply_to_message_id=None, targeting_method="test", acknowledgement_status="received",
        approval_received_at=now.isoformat(),
    )
    storage.execute(
        "UPDATE approvals SET status='consumed',consumed_at=? WHERE id='derived'",
        (now.isoformat(),),
    )
    workflow_store.transition(workflow["id"], ApprovalWorkflowState.VALIDATING)
    workflow = workflow_store.transition(workflow["id"], ApprovalWorkflowState.APPROVED_PENDING_INTENT)
    intent = workflow_store.ensure_intent(workflow["id"], proposal, run_id="run")
    workflow_store.transition(workflow["id"], ApprovalWorkflowState.SUBMISSION_PENDING)
    service = TradingService.__new__(TradingService)
    service.storage = storage
    service.config = {"effective_config_hash": CONFIG_HASH}
    service.broker = None
    service.run_id = "recovery"

    service._recover_local_workflows()
    assert workflow_store.get(workflow["id"])["state"] == "submission_pending"
    assert DurableExecutionStore(storage).get_intent(intent["id"])["state"] == "retryable_pre_submission"

    service.broker = object()
    with (
        patch.object(service, "_portfolio_context", return_value={"approval_valid": True}),
        patch("app.service.Executor.execute", return_value=ExecutionResult(
            True, "submitted", "ta-test", intent_id=intent["id"]
        )),
    ):
        service._recover_local_workflows()
    assert workflow_store.get(workflow["id"])["state"] == "submitted"


def test_invalid_rotation_inputs_fail_closed(coordinator):
    _storage, manager = coordinator
    with pytest.raises(ValueError):
        manager.create_group(
            run_id="run", exit_legs=[], contingent_entries=[],
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
            evaluation_time=datetime.now(UTC),
        )
    with pytest.raises(ValueError):
        manager.create_group(
            run_id="run",
            exit_legs=[{"symbol": "OLD", "side": "buy", "quantity": 1}],
            contingent_entries=[{"candidate_key": "c", "strategy_version": "s", "symbol": "N",
                                 "max_quantity": 1, "max_notional": 1, "max_stop_risk": 1}],
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
            evaluation_time=datetime.now(UTC),
        )
