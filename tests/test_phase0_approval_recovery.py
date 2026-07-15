from __future__ import annotations

import threading
import uuid
import sqlite3
import json
from datetime import UTC, datetime, timedelta

import pytest

from app.approval_workflow import (
    ApprovalWorkflowConflict,
    ApprovalWorkflowState,
    ApprovalWorkflowStore,
)
from app.execution import DurableExecutionStore, Executor
from app.risk_engine import RiskDecision
from app.storage import Storage


def _storage(tmp_path) -> Storage:
    storage = Storage(tmp_path / "approval-recovery.db")
    storage.initialize()
    return storage


def _proposal(proposal_id: str = "proposal-1", *, status: str = "approved") -> dict:
    now = datetime.now(UTC)
    return {
        "id": proposal_id,
        "proposal_id": proposal_id,
        "source_id": proposal_id,
        "status": status,
        "symbol": "QQQ",
        "side": "buy",
        "action": "entry",
        "notional": 10.0,
        "latest_price": 100.0,
        "stop_price": 95.0,
        "trading_mode": "paper",
        "order_type": "limit", "quote_source": "alpaca_quote", "quote_bid": 99.9,
        "quote_ask": 100.1, "quote_midpoint": 100.0, "quote_timestamp": now.isoformat(),
        "quote_spread_bps": 20.0, "limit_price": 100.36,
        "expires_at": "2099-01-01T00:00:00+00:00",
    }


def _workflow(
    storage: Storage,
    *,
    proposal_id: str = "proposal-1",
    approval_id: str = "approval-1",
    update_id: int = 101,
    state: ApprovalWorkflowState = ApprovalWorkflowState.APPROVED_PENDING_INTENT,
):
    proposal = _proposal(proposal_id)
    now = datetime.now(UTC)
    storage.execute(
        """INSERT INTO trade_proposals(
             id,symbol,side,notional,status,created_at,expires_at,strategy_version,payload)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (proposal_id, proposal["symbol"], proposal["side"], proposal["notional"], "pending",
         now.isoformat(), proposal["expires_at"], "rule_based_v2", json.dumps(proposal)),
    )
    if state in {
        ApprovalWorkflowState.RECEIVED,
        ApprovalWorkflowState.AUTHORIZED,
        ApprovalWorkflowState.UNKNOWN,
    }:
        workflow = ApprovalWorkflowStore(storage).create_or_get(
            approval_id=approval_id,
            proposal_id=proposal_id,
            telegram_update_id=update_id,
            initial_state=state,
        )
        from app.approval_authority import authority_envelope, authority_fingerprint, canonical_json
        envelope = authority_envelope(proposal, proposal_id=proposal_id)
        storage.execute(
            """INSERT INTO approvals(
                 id,run_id,proposal_id,sender_id,raw_message,parsed_action,authorized,status,created_at,
                 consumed_at,authority_envelope_json,authority_fingerprint)
               VALUES(?,?,?,?,?,?,1,'consumed',?,?,?,?)""",
            (approval_id, "run", proposal_id, "owner", "approve", "approve", now.isoformat(), now.isoformat(),
             canonical_json(envelope), authority_fingerprint(envelope)),
        )
        return workflow
    store = ApprovalWorkflowStore(storage)
    accepted = store.accept_approval(
        approval_id=approval_id, run_id="run", proposal_id=proposal_id,
        sender_id="owner", raw_message="approve", parsed_action="approve",
        telegram_update_id=update_id, reply_to_message_id=None, targeting_method="test",
        acknowledgement_status="received", approval_received_at=now.isoformat(),
    )
    assert storage.consume_approval(proposal_id, approval_id)
    if state == ApprovalWorkflowState.APPROVED_PENDING_INTENT:
        store.transition(accepted["id"], ApprovalWorkflowState.VALIDATING,
                         expected_state=ApprovalWorkflowState.TARGET_RESOLVED)
        return store.transition(accepted["id"], state, expected_state=ApprovalWorkflowState.VALIDATING)
    if state == ApprovalWorkflowState.VALIDATING:
        return store.transition(accepted["id"], state, expected_state=ApprovalWorkflowState.TARGET_RESOLVED)
    return accepted


def test_duplicate_telegram_update_has_one_stable_workflow(tmp_path):
    storage = _storage(tmp_path)
    store = ApprovalWorkflowStore(storage)
    first = store.create_or_get(
        approval_id="approval-a", proposal_id="proposal-a", telegram_update_id=7
    )
    duplicate = store.create_or_get(
        approval_id="approval-a", proposal_id="proposal-a", telegram_update_id=7
    )

    assert duplicate["id"] == first["id"]
    assert duplicate["logical_workflow_key"] == "approval:approval-a"
    assert storage.fetch_all("SELECT COUNT(*) n FROM approval_workflows")[0]["n"] == 1


def test_approval_acceptance_and_workflow_are_one_transaction(tmp_path):
    storage = _storage(tmp_path)
    store = ApprovalWorkflowStore(storage)
    workflow = store.accept_approval(
        approval_id="approval-atomic",
        run_id="run-1",
        proposal_id="proposal-atomic",
        sender_id="synthetic-user",
        raw_message="yes",
        parsed_action="approve",
        telegram_update_id=77,
        reply_to_message_id="10",
        targeting_method="reply",
        acknowledgement_status="received",
        approval_received_at=datetime.now(UTC).isoformat(),
    )

    approval = storage.fetch_all("SELECT * FROM approvals WHERE id='approval-atomic'")
    assert len(approval) == 1
    assert approval[0]["status"] == "accepted"
    assert workflow["approval_id"] == approval[0]["id"]
    assert workflow["state"] == "target_resolved"

    # Force the precise boundary after approval insertion but before workflow
    # insertion. SQLite must roll back both writes.
    storage.execute(
        """CREATE TRIGGER fail_atomic_workflow BEFORE INSERT ON approval_workflows
           WHEN NEW.approval_id='approval-fault'
           BEGIN SELECT RAISE(ABORT, 'injected workflow persistence failure'); END"""
    )
    with pytest.raises(sqlite3.IntegrityError):
        store.accept_approval(
            approval_id="approval-fault",
            run_id="run-2",
            proposal_id="different-proposal",
            sender_id="synthetic-user",
            raw_message="yes",
            parsed_action="approve",
            telegram_update_id=78,
            reply_to_message_id=None,
            targeting_method="explicit",
            acknowledgement_status="received",
            approval_received_at=datetime.now(UTC).isoformat(),
        )
    assert storage.fetch_all("SELECT COUNT(*) n FROM approvals WHERE id='approval-fault'")[0]["n"] == 0
    assert storage.fetch_all("SELECT COUNT(*) n FROM approval_workflows WHERE approval_id='approval-fault'")[0]["n"] == 0


def test_second_active_workflow_for_same_proposal_is_rejected(tmp_path):
    storage = _storage(tmp_path)
    store = ApprovalWorkflowStore(storage)
    store.create_or_get(approval_id="approval-a", proposal_id="proposal-a", telegram_update_id=1)

    with pytest.raises(ApprovalWorkflowConflict, match="active executable workflow"):
        store.create_or_get(approval_id="approval-b", proposal_id="proposal-a", telegram_update_id=2)


def test_crash_before_intent_is_recovered_exactly_once(tmp_path):
    storage = _storage(tmp_path)
    proposal = _proposal()
    workflow = _workflow(storage)

    # Simulated dead process: only the committed approval workflow exists. A fresh
    # store performs the startup sweep; no object or connection is reused.
    fresh_store = ApprovalWorkflowStore(Storage(storage.path))
    summary = fresh_store.recover(
        owner_token="recovery-a",
        proposal_loader=lambda proposal_id: proposal if proposal_id == proposal["id"] else None,
        run_id="run-1",
    )
    repeated = fresh_store.recover(
        owner_token="recovery-b",
        proposal_loader=lambda _proposal_id: proposal,
        run_id="run-2",
    )

    intents = storage.fetch_all("SELECT * FROM order_intents WHERE approval_id='approval-1'")
    reservations = storage.fetch_all("SELECT * FROM risk_reservations")
    recovered = fresh_store.get(workflow["id"])
    assert summary.intent_created == 1
    assert summary.submission_pending == 1
    assert repeated.intent_created == 0
    assert len(intents) == len(reservations) == 1
    assert intents[0]["client_order_id"].startswith("ta0-")
    assert reservations[0]["state"] == "active"
    assert recovered["state"] == ApprovalWorkflowState.SUBMISSION_PENDING.value
    assert recovered["intent_id"] == intents[0]["id"]
    assert storage.fetch_all(
        "SELECT COUNT(*) n FROM audit_events WHERE event_type='approval_workflow_recovery_claimed'"
    )[0]["n"] >= 1


def test_crash_after_intent_commit_links_existing_intent(tmp_path):
    storage = _storage(tmp_path)
    proposal = _proposal()
    workflow = _workflow(storage)
    intent = DurableExecutionStore(storage).create_or_get_intent(
        proposal,
        run_id="run-before-crash",
        source_type="telegram",
        approval_id="approval-1",
    )
    # Model the exact lost local completion marker while preserving the durable
    # intent/reservation commit.
    storage.execute(
        "UPDATE approval_workflows SET state='approved_pending_intent',intent_id=NULL WHERE id=?",
        (workflow["id"],),
    )

    summary = ApprovalWorkflowStore(Storage(storage.path)).recover(
        owner_token="fresh-process",
        proposal_loader=lambda _proposal_id: proposal,
        run_id="run-after-crash",
    )

    linked = ApprovalWorkflowStore(storage).get(workflow["id"])
    assert summary.existing_intent_linked == 1
    assert summary.intent_created == 0
    assert linked["intent_id"] == intent["id"]
    assert linked["state"] == ApprovalWorkflowState.SUBMISSION_PENDING.value
    assert storage.fetch_all("SELECT COUNT(*) n FROM order_intents")[0]["n"] == 1
    assert storage.fetch_all("SELECT COUNT(*) n FROM risk_reservations")[0]["n"] == 1


def test_recovery_never_revives_expired_proposal(tmp_path):
    storage = _storage(tmp_path)
    workflow = _workflow(storage)
    expired = _proposal(status="expired")

    summary = ApprovalWorkflowStore(storage).recover(
        owner_token="worker",
        proposal_loader=lambda _proposal_id: expired,
        run_id="run-1",
    )

    assert summary.blocked == 1
    assert ApprovalWorkflowStore(storage).get(workflow["id"])["state"] == "terminal"
    assert storage.fetch_all("SELECT COUNT(*) n FROM order_intents")[0]["n"] == 0


def test_recovery_action_authority_blocks_before_intent_creation(tmp_path):
    storage = _storage(tmp_path)
    workflow = _workflow(storage)
    proposal = _proposal()

    summary = ApprovalWorkflowStore(storage).recover(
        owner_token="authority-worker",
        proposal_loader=lambda _proposal_id: proposal,
        action_validator=lambda _workflow, loaded: (
            "blocked", loaded, "dependent group expired"
        ),
        run_id="run",
    )

    assert summary.blocked == 1
    assert ApprovalWorkflowStore(storage).get(workflow["id"])["state"] == "terminal"
    assert storage.fetch_all("SELECT COUNT(*) n FROM order_intents")[0]["n"] == 0


def test_recovery_action_authority_rechecked_before_submission_and_releases_intent(tmp_path):
    storage = _storage(tmp_path)
    proposal = _proposal()
    workflow = _workflow(storage)
    store = ApprovalWorkflowStore(storage)
    intent = store.ensure_intent(workflow["id"], proposal, run_id="run")
    store.transition(workflow["id"], ApprovalWorkflowState.SUBMISSION_PENDING)
    submitted = []

    summary = store.recover(
        owner_token="submission-authority-worker",
        proposal_loader=lambda _proposal_id: proposal,
        action_validator=lambda _workflow, loaded: (
            "blocked", loaded, "dependent group expired"
        ),
        submitter=lambda *_args: submitted.append(True) or "submitted",
        run_id="run",
    )

    assert summary.blocked == 1
    assert submitted == []
    assert store.get(workflow["id"])["state"] == "terminal"
    assert DurableExecutionStore(storage).get_intent(intent["id"])["state"] == "expired"
    assert storage.fetch_all(
        "SELECT state FROM risk_reservations WHERE intent_id=?", (intent["id"],)
    )[0]["state"] == "released"


def test_update_cannot_be_processed_before_business_state_is_durable(tmp_path):
    storage = _storage(tmp_path)
    storage.ingest_telegram_update(
        88,
        message_id=9,
        message_timestamp=1,
        safe_message_type="approval",
        normalized_action="approve",
        target_hint="proposal-1",
        sender_authorized=True,
    )
    workflow = _workflow(
        storage,
        update_id=88,
        state=ApprovalWorkflowState.AUTHORIZED,
    )
    store = ApprovalWorkflowStore(storage)

    with pytest.raises(ApprovalWorkflowConflict, match="not durably represented"):
        store.mark_update_processed(workflow["id"])

    store.transition(workflow["id"], ApprovalWorkflowState.BLOCKED, safe_detail="validation blocked")
    store.mark_update_processed(workflow["id"])
    update = storage.fetch_all("SELECT * FROM telegram_updates WHERE update_id=88")[0]
    assert update["processing_state"] == "processed"
    assert update["approval_id"] == "approval-1"


def test_two_recovery_workers_have_one_lease_owner(tmp_path):
    storage = _storage(tmp_path)
    workflow = _workflow(storage)
    barrier = threading.Barrier(2)
    results: list[dict | None] = []
    errors: list[BaseException] = []

    def claim() -> None:
        try:
            barrier.wait()
            results.append(
                ApprovalWorkflowStore(Storage(storage.path)).claim_next(
                    f"worker-{uuid.uuid4()}", lease_seconds=60
                )
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [threading.Thread(target=claim), threading.Thread(target=claim)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    assert winners[0]["id"] == workflow["id"]
    assert winners[0]["claim_owner"]


def test_compare_and_swap_prevents_two_state_winners(tmp_path):
    storage = _storage(tmp_path)
    workflow = _workflow(storage, state=ApprovalWorkflowState.RECEIVED)
    store = ApprovalWorkflowStore(storage)
    original_version = workflow["version"]

    store.transition(
        workflow["id"],
        ApprovalWorkflowState.AUTHORIZED,
        expected_state=ApprovalWorkflowState.RECEIVED,
        expected_version=original_version,
    )
    with pytest.raises(ApprovalWorkflowConflict, match="state changed|version changed"):
        store.transition(
            workflow["id"],
            ApprovalWorkflowState.BLOCKED,
            expected_state=ApprovalWorkflowState.RECEIVED,
            expected_version=original_version,
        )


def test_unknown_recovery_requires_reconciliation_and_never_creates_intent(tmp_path):
    storage = _storage(tmp_path)
    workflow = _workflow(storage, state=ApprovalWorkflowState.UNKNOWN)

    summary = ApprovalWorkflowStore(storage).recover(
        owner_token="worker",
        proposal_loader=lambda _proposal_id: _proposal(),
        run_id="run-1",
    )

    assert summary.external_ambiguity == 1
    assert ApprovalWorkflowStore(storage).get(workflow["id"])["state"] == "unknown"
    assert storage.fetch_all("SELECT COUNT(*) n FROM order_intents")[0]["n"] == 0
    audits = storage.fetch_all(
        "SELECT detail FROM audit_events WHERE event_type='approval_workflow_recovery_reconciliation_required'"
    )
    assert len(audits) == 1
    assert '"automatic_resubmission":false' in audits[0]["detail"]


def test_received_workflow_runs_deterministic_validation_then_creates_intent(tmp_path):
    storage = _storage(tmp_path)
    proposal = _proposal()
    workflow = _workflow(storage, state=ApprovalWorkflowState.RECEIVED)
    calls = []

    def validate(workflow_row, loaded):
        calls.append((workflow_row["state"], loaded["id"]))
        # A separate write succeeds, proving no workflow transaction is held
        # across the callback.
        storage.audit("run-1", "validator_callback", {"offline": True})
        return "approved", loaded, "deterministic final validation passed"

    summary = ApprovalWorkflowStore(storage).recover(
        owner_token="validator-worker",
        proposal_loader=lambda _proposal_id: proposal,
        validator=validate,
        run_id="run-1",
    )

    recovered = ApprovalWorkflowStore(storage).get(workflow["id"])
    assert calls == [("received", "proposal-1")]
    assert summary.intent_created == 1
    assert recovered["state"] == "submission_pending"
    assert recovered["validation_status"] == "passed"
    assert storage.fetch_all("SELECT COUNT(*) n FROM order_intents")[0]["n"] == 1


def test_submission_callback_runs_after_started_commit_and_persists_result(tmp_path):
    storage = _storage(tmp_path)
    proposal = _proposal()
    workflow = _workflow(storage)
    store = ApprovalWorkflowStore(storage)
    intent = store.ensure_intent(workflow["id"], proposal, run_id="run-1")
    store.transition(workflow["id"], ApprovalWorkflowState.SUBMISSION_PENDING)
    observed = []

    def submit(workflow_row, intent_row):
        observed.append((workflow_row["state"], intent_row["client_order_id"]))
        storage.audit("run-1", "synthetic_submit_callback", {"paper_only": True})
        return "submitted"

    store.recover(
        owner_token="submit-worker",
        proposal_loader=lambda _proposal_id: proposal,
        submitter=submit,
        run_id="run-1",
    )

    assert observed == [("submission_started", intent["client_order_id"])]
    assert store.get(workflow["id"])["state"] == "submitted"


def test_unknown_uses_lookup_only_and_cannot_call_submitter(tmp_path):
    storage = _storage(tmp_path)
    proposal = _proposal()
    workflow = _workflow(storage)
    store = ApprovalWorkflowStore(storage)
    intent = store.ensure_intent(workflow["id"], proposal, run_id="run-1")
    store.transition(workflow["id"], ApprovalWorkflowState.UNKNOWN)
    lookups = []

    def lookup(workflow_row, intent_row):
        lookups.append((workflow_row["state"], intent_row["client_order_id"]))
        return "submitted"

    def forbidden_submit(*_args):  # pragma: no cover - must never execute
        raise AssertionError("UNKNOWN recovery attempted a duplicate submission")

    store.recover(
        owner_token="lookup-worker",
        proposal_loader=lambda _proposal_id: proposal,
        submitter=forbidden_submit,
        lookup_reconciler=lookup,
        run_id="run-1",
    )

    assert lookups == [("unknown", intent["client_order_id"])]
    assert store.get(workflow["id"])["state"] == "submitted"


def test_submitted_terminal_intent_is_lookup_only_and_terminalised_after_restart(tmp_path):
    storage = _storage(tmp_path)
    proposal = _proposal()
    workflow = _workflow(storage)
    store = ApprovalWorkflowStore(storage)
    intent = store.ensure_intent(workflow["id"], proposal, run_id="run-1")
    store.transition(workflow["id"], ApprovalWorkflowState.SUBMISSION_PENDING)
    store.transition(workflow["id"], ApprovalWorkflowState.SUBMISSION_STARTED)
    store.transition(workflow["id"], ApprovalWorkflowState.SUBMITTED)
    storage.execute("UPDATE order_intents SET state='filled',terminal_at=updated_at WHERE id=?", (intent["id"],))
    lookups = []

    def lookup(workflow_row, intent_row):
        lookups.append((workflow_row["state"], intent_row["state"]))
        return "terminal"

    def forbidden_submit(*_args):  # pragma: no cover - must never execute
        raise AssertionError("SUBMITTED recovery attempted a duplicate submission")

    store.recover(
        owner_token="terminal-lookup-worker",
        proposal_loader=lambda _proposal_id: proposal,
        submitter=forbidden_submit,
        lookup_reconciler=lookup,
        run_id="run-1",
    )

    assert lookups == [("submitted", "filled")]
    assert store.get(workflow["id"])["state"] == "terminal"


def test_blocked_workflow_cannot_be_reopened_by_intent_creation(tmp_path):
    storage = _storage(tmp_path)
    workflow = _workflow(storage, state=ApprovalWorkflowState.APPROVED_PENDING_INTENT)
    ApprovalWorkflowStore(storage).transition(workflow["id"], ApprovalWorkflowState.BLOCKED)
    with pytest.raises(RuntimeError, match="not eligible"):
        DurableExecutionStore(storage).create_or_get_intent(
            _proposal(), run_id="run", source_type="telegram", approval_id="approval-1"
        )
    assert storage.fetch_all("SELECT COUNT(*) n FROM order_intents")[0]["n"] == 0


def test_expired_approved_workflow_never_creates_intent(tmp_path):
    storage = _storage(tmp_path)
    _workflow(storage, state=ApprovalWorkflowState.APPROVED_PENDING_INTENT)
    expired = {**_proposal(), "status": "approved", "expires_at": "2020-01-01T00:00:00+00:00"}
    with pytest.raises(ValueError, match="expired approval"):
        DurableExecutionStore(storage).create_or_get_intent(
            expired, run_id="run", source_type="telegram", approval_id="approval-1"
        )


def test_submission_started_is_durable_before_broker_invocation(tmp_path):
    storage = _storage(tmp_path)
    workflow = _workflow(storage, state=ApprovalWorkflowState.VALIDATING)
    candidate = _proposal()
    candidate["expires_at"] = storage.fetch_all(
        "SELECT expires_at FROM trade_proposals WHERE id=?", (candidate["id"],)
    )[0]["expires_at"]

    class Risk:
        config = {}
        def evaluate(self, proposal, context, final=False): return RiskDecision(True, ())

    class Broker:
        def submit_order(self, *args):
            assert ApprovalWorkflowStore(storage).get(workflow["id"])["state"] == "submission_started"
            return {"id": "paper-1", "status": "submitted"}

    result = Executor(Broker(), Risk(), storage, "run").execute(
        candidate, {"approval_valid": True}, approval_id="approval-1"
    )
    assert result.submitted
    assert ApprovalWorkflowStore(storage).get(workflow["id"])["state"] == "submitted"
