from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Callable

from .execution import DurableExecutionStore
from .approval_authority import authority_envelope, authority_fingerprint, canonical_json
from .order_state import OrderState
from .utils import iso_now, json_dumps


class ApprovalWorkflowState(StrEnum):
    RECEIVED = "received"
    AUTHORIZED = "authorized"
    TARGET_RESOLVED = "target_resolved"
    VALIDATING = "validating"
    BLOCKED = "blocked"
    APPROVED_PENDING_INTENT = "approved_pending_intent"
    INTENT_CREATED = "intent_created"
    SUBMISSION_PENDING = "submission_pending"
    SUBMISSION_STARTED = "submission_started"
    SUBMITTED = "submitted"
    UNKNOWN = "unknown"
    TERMINAL = "terminal"
    MANUAL_REVIEW = "manual_review"


TERMINAL_WORKFLOW_STATES = {
    ApprovalWorkflowState.BLOCKED,
    ApprovalWorkflowState.TERMINAL,
    ApprovalWorkflowState.MANUAL_REVIEW,
}

RECOVERABLE_WORKFLOW_STATES = {
    ApprovalWorkflowState.RECEIVED,
    ApprovalWorkflowState.AUTHORIZED,
    ApprovalWorkflowState.TARGET_RESOLVED,
    ApprovalWorkflowState.VALIDATING,
    ApprovalWorkflowState.APPROVED_PENDING_INTENT,
    ApprovalWorkflowState.INTENT_CREATED,
    ApprovalWorkflowState.SUBMISSION_PENDING,
    ApprovalWorkflowState.SUBMISSION_STARTED,
    ApprovalWorkflowState.SUBMITTED,
    ApprovalWorkflowState.UNKNOWN,
}

ALLOWED_WORKFLOW_TRANSITIONS: dict[ApprovalWorkflowState, set[ApprovalWorkflowState]] = {
    ApprovalWorkflowState.RECEIVED: {
        ApprovalWorkflowState.AUTHORIZED,
        ApprovalWorkflowState.BLOCKED,
        ApprovalWorkflowState.TERMINAL,
        ApprovalWorkflowState.MANUAL_REVIEW,
    },
    ApprovalWorkflowState.AUTHORIZED: {
        ApprovalWorkflowState.TARGET_RESOLVED,
        ApprovalWorkflowState.BLOCKED,
        ApprovalWorkflowState.TERMINAL,
        ApprovalWorkflowState.MANUAL_REVIEW,
    },
    ApprovalWorkflowState.TARGET_RESOLVED: {
        ApprovalWorkflowState.VALIDATING,
        ApprovalWorkflowState.BLOCKED,
        ApprovalWorkflowState.TERMINAL,
        ApprovalWorkflowState.MANUAL_REVIEW,
    },
    ApprovalWorkflowState.VALIDATING: {
        ApprovalWorkflowState.APPROVED_PENDING_INTENT,
        ApprovalWorkflowState.BLOCKED,
        ApprovalWorkflowState.TERMINAL,
        ApprovalWorkflowState.MANUAL_REVIEW,
    },
    ApprovalWorkflowState.APPROVED_PENDING_INTENT: {
        ApprovalWorkflowState.INTENT_CREATED,
        ApprovalWorkflowState.BLOCKED,
        ApprovalWorkflowState.TERMINAL,
        ApprovalWorkflowState.MANUAL_REVIEW,
    },
    ApprovalWorkflowState.INTENT_CREATED: {
        ApprovalWorkflowState.SUBMISSION_PENDING,
        ApprovalWorkflowState.SUBMISSION_STARTED,
        ApprovalWorkflowState.TERMINAL,
        ApprovalWorkflowState.UNKNOWN,
        ApprovalWorkflowState.MANUAL_REVIEW,
    },
    ApprovalWorkflowState.SUBMISSION_PENDING: {
        ApprovalWorkflowState.SUBMISSION_STARTED,
        ApprovalWorkflowState.BLOCKED,
        ApprovalWorkflowState.TERMINAL,
        ApprovalWorkflowState.UNKNOWN,
        ApprovalWorkflowState.MANUAL_REVIEW,
    },
    ApprovalWorkflowState.SUBMISSION_STARTED: {
        ApprovalWorkflowState.SUBMITTED,
        ApprovalWorkflowState.UNKNOWN,
        ApprovalWorkflowState.TERMINAL,
        ApprovalWorkflowState.MANUAL_REVIEW,
    },
    ApprovalWorkflowState.SUBMITTED: {
        ApprovalWorkflowState.UNKNOWN,
        ApprovalWorkflowState.TERMINAL,
        ApprovalWorkflowState.MANUAL_REVIEW,
    },
    ApprovalWorkflowState.UNKNOWN: {
        ApprovalWorkflowState.SUBMITTED,
        ApprovalWorkflowState.TERMINAL,
        ApprovalWorkflowState.MANUAL_REVIEW,
    },
    ApprovalWorkflowState.BLOCKED: set(),
    ApprovalWorkflowState.TERMINAL: set(),
    ApprovalWorkflowState.MANUAL_REVIEW: set(),
}


class ApprovalWorkflowConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class RecoverySummary:
    claimed: int = 0
    intent_created: int = 0
    existing_intent_linked: int = 0
    submission_pending: int = 0
    external_ambiguity: int = 0
    blocked: int = 0
    failed_retryable: int = 0


class ApprovalWorkflowStore:
    """SQLite-safe local approval process; this class performs no network I/O."""

    def __init__(self, storage: Any) -> None:
        self.storage = storage

    @staticmethod
    def stable_key(approval_id: str) -> str:
        return f"approval:{approval_id}"

    def get(self, workflow_id: str) -> dict[str, Any]:
        rows = self.storage.fetch_all("SELECT * FROM approval_workflows WHERE id=?", (workflow_id,))
        if not rows:
            raise LookupError(f"approval workflow not found: {workflow_id}")
        return rows[0]

    def get_by_approval(self, approval_id: str) -> dict[str, Any] | None:
        rows = self.storage.fetch_all("SELECT * FROM approval_workflows WHERE approval_id=?", (approval_id,))
        return rows[0] if rows else None

    def create_or_get(
        self,
        *,
        approval_id: str,
        proposal_id: str,
        telegram_update_id: int | None = None,
        initial_state: ApprovalWorkflowState = ApprovalWorkflowState.RECEIVED,
    ) -> dict[str, Any]:
        """Create one stable workflow and reject a second active workflow for a proposal."""
        now = iso_now()
        workflow_id = str(uuid.uuid4())
        key = self.stable_key(approval_id)
        with self.storage.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM approval_workflows WHERE approval_id=? OR logical_workflow_key=?",
                (approval_id, key),
            ).fetchone()
            if existing:
                if str(existing["proposal_id"]) != proposal_id:
                    raise ApprovalWorkflowConflict("stable approval identity targets a different proposal")
                return dict(existing)
            if telegram_update_id is not None:
                same_update = conn.execute(
                    "SELECT * FROM approval_workflows WHERE telegram_update_id=?", (telegram_update_id,)
                ).fetchone()
                if same_update:
                    return dict(same_update)
            active = conn.execute(
                """SELECT id FROM approval_workflows
                   WHERE proposal_id=? AND state NOT IN ('blocked','terminal','manual_review') LIMIT 1""",
                (proposal_id,),
            ).fetchone()
            if active:
                raise ApprovalWorkflowConflict("proposal already has an active executable workflow")
            conn.execute(
                """INSERT INTO approval_workflows(
                       id,approval_id,proposal_id,telegram_update_id,logical_workflow_key,state,
                       created_at,updated_at,version,attempt_count)
                   VALUES(?,?,?,?,?,?,?,?,0,0)""",
                (workflow_id, approval_id, proposal_id, telegram_update_id, key, initial_state.value, now, now),
            )
            row = conn.execute("SELECT * FROM approval_workflows WHERE id=?", (workflow_id,)).fetchone()
        self._audit("approval_workflow_created", workflow_id, {"state": initial_state.value})
        return dict(row)

    def accept_approval(
        self,
        *,
        approval_id: str,
        run_id: str | None,
        proposal_id: str,
        sender_id: str,
        raw_message: str,
        parsed_action: str,
        telegram_update_id: int | None,
        reply_to_message_id: str | None,
        targeting_method: str | None,
        acknowledgement_status: str,
        approval_received_at: str,
    ) -> dict[str, Any]:
        """Atomically persist acceptance and its stable, target-resolved workflow."""
        now = iso_now()
        workflow_id = str(uuid.uuid4())
        key = self.stable_key(approval_id)
        with self.storage.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM approval_workflows WHERE approval_id=? OR logical_workflow_key=?",
                (approval_id, key),
            ).fetchone()
            if existing:
                if str(existing["proposal_id"]) != proposal_id:
                    raise ApprovalWorkflowConflict("stable approval identity targets a different proposal")
                return dict(existing)
            if telegram_update_id is not None:
                same_update = conn.execute(
                    "SELECT * FROM approval_workflows WHERE telegram_update_id=?", (telegram_update_id,)
                ).fetchone()
                if same_update:
                    return dict(same_update)
            active = conn.execute(
                """SELECT id FROM approval_workflows
                   WHERE proposal_id=? AND state NOT IN ('blocked','terminal','manual_review') LIMIT 1""",
                (proposal_id,),
            ).fetchone()
            if active:
                raise ApprovalWorkflowConflict("proposal already has an active executable workflow")
            proposal_row = conn.execute(
                "SELECT * FROM trade_proposals WHERE id=?", (proposal_id,)
            ).fetchone()
            envelope = authority_envelope(
                dict(proposal_row) if proposal_row else {"proposal_id": proposal_id},
                proposal_id=proposal_id,
            )
            conn.execute(
                """INSERT INTO approvals(
                       id,run_id,proposal_id,sender_id,raw_message,parsed_action,authorized,status,created_at,
                       reply_to_message_id,proposal_targeting_method,acknowledgement_status,approval_received_at,
                       authority_envelope_json,authority_fingerprint)
                   VALUES(?,?,?,?,?,?,1,'accepted',?,?,?,?,?,?,?)""",
                (
                    approval_id,
                    run_id,
                    proposal_id,
                    sender_id,
                    raw_message,
                    parsed_action,
                    now,
                    reply_to_message_id,
                    targeting_method,
                    acknowledgement_status,
                    approval_received_at,
                    canonical_json(envelope),
                    authority_fingerprint(envelope),
                ),
            )
            conn.execute(
                """INSERT INTO approval_workflows(
                       id,approval_id,proposal_id,telegram_update_id,logical_workflow_key,state,
                       created_at,updated_at,version,attempt_count)
                   VALUES(?,?,?,?,?, 'target_resolved',?,?,0,0)""",
                (workflow_id, approval_id, proposal_id, telegram_update_id, key, now, now),
            )
            row = conn.execute("SELECT * FROM approval_workflows WHERE id=?", (workflow_id,)).fetchone()
        self._audit("approval_workflow_accepted", workflow_id, {"state": "target_resolved"})
        return dict(row)

    def transition(
        self,
        workflow_id: str,
        target: ApprovalWorkflowState,
        *,
        expected_state: ApprovalWorkflowState | None = None,
        expected_version: int | None = None,
        owner_token: str | None = None,
        intent_id: str | None = None,
        validation_status: str | None = None,
        safe_detail: str | None = None,
    ) -> dict[str, Any]:
        """Compare-and-swap a workflow state and make competing writers explicit."""
        now = iso_now()
        with self.storage.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute("SELECT * FROM approval_workflows WHERE id=?", (workflow_id,)).fetchone()
            if not current:
                raise LookupError(f"approval workflow not found: {workflow_id}")
            source = ApprovalWorkflowState(current["state"])
            if source != target and target not in ALLOWED_WORKFLOW_TRANSITIONS[source]:
                raise ApprovalWorkflowConflict(f"invalid approval workflow transition: {source.value} -> {target.value}")
            if expected_state is not None and source != expected_state:
                raise ApprovalWorkflowConflict("workflow state changed before transition")
            if expected_version is not None and int(current["version"]) != expected_version:
                raise ApprovalWorkflowConflict("workflow version changed before transition")
            if owner_token is not None and current["claim_owner"] != owner_token:
                raise ApprovalWorkflowConflict("workflow recovery ownership was lost")
            terminal_at = now if target in TERMINAL_WORKFLOW_STATES else current["terminal_at"]
            cursor = conn.execute(
                """UPDATE approval_workflows SET state=?,intent_id=COALESCE(?,intent_id),
                       validation_status=COALESCE(?,validation_status),safe_detail=COALESCE(?,safe_detail),
                       terminal_at=?,updated_at=?,version=version+1
                   WHERE id=? AND version=?""",
                (
                    target.value,
                    intent_id,
                    validation_status,
                    safe_detail,
                    terminal_at,
                    now,
                    workflow_id,
                    current["version"],
                ),
            )
            if cursor.rowcount != 1:
                raise ApprovalWorkflowConflict("workflow compare-and-swap lost")
            row = conn.execute("SELECT * FROM approval_workflows WHERE id=?", (workflow_id,)).fetchone()
        self._audit(
            "approval_workflow_transition",
            workflow_id,
            {"from": source.value, "to": target.value, "intent_id": intent_id},
        )
        return dict(row)

    def claim_next(self, owner_token: str, *, lease_seconds: int = 30) -> dict[str, Any] | None:
        now = datetime.now(UTC)
        now_iso = now.isoformat()
        until = (now + timedelta(seconds=max(1, lease_seconds))).isoformat()
        placeholders = ",".join("?" for _ in RECOVERABLE_WORKFLOW_STATES)
        states = tuple(state.value for state in RECOVERABLE_WORKFLOW_STATES)
        with self.storage.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            candidate = conn.execute(
                f"""SELECT id,version FROM approval_workflows
                    WHERE state IN ({placeholders})
                      AND (claim_until IS NULL OR claim_until<=? OR claim_owner=?)
                    ORDER BY updated_at,id LIMIT 1""",
                (*states, now_iso, owner_token),
            ).fetchone()
            if not candidate:
                return None
            changed = conn.execute(
                """UPDATE approval_workflows SET claim_owner=?,claim_until=?,attempt_count=attempt_count+1,
                       updated_at=?,version=version+1
                   WHERE id=? AND version=? AND (claim_until IS NULL OR claim_until<=? OR claim_owner=?)""",
                (owner_token, until, now_iso, candidate["id"], candidate["version"], now_iso, owner_token),
            ).rowcount
            if changed != 1:
                return None
            row = conn.execute("SELECT * FROM approval_workflows WHERE id=?", (candidate["id"],)).fetchone()
        self._audit("approval_workflow_recovery_claimed", row["id"], {"owner": owner_token})
        return dict(row)

    def release_claim(self, workflow_id: str, owner_token: str, *, retryable_error: str | None = None) -> None:
        now = iso_now()
        with self.storage.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            changed = conn.execute(
                """UPDATE approval_workflows SET claim_owner=NULL,claim_until=NULL,
                       last_error_category=COALESCE(?,last_error_category),updated_at=?,version=version+1
                   WHERE id=? AND claim_owner=?""",
                (retryable_error, now, workflow_id, owner_token),
            ).rowcount
            if changed != 1:
                raise ApprovalWorkflowConflict("workflow recovery ownership was lost")
        self._audit(
            "approval_workflow_recovery_released",
            workflow_id,
            {"retryable_error": retryable_error},
        )

    def mark_update_processed(self, workflow_id: str) -> None:
        """Advance the inbox only after an intent, terminal decision, or explicit ambiguity exists."""
        now = iso_now()
        with self.storage.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM approval_workflows WHERE id=?", (workflow_id,)).fetchone()
            if not row:
                raise LookupError(f"approval workflow not found: {workflow_id}")
            represented = bool(row["intent_id"]) or ApprovalWorkflowState(row["state"]) in {
                ApprovalWorkflowState.BLOCKED,
                ApprovalWorkflowState.UNKNOWN,
                ApprovalWorkflowState.TERMINAL,
                ApprovalWorkflowState.MANUAL_REVIEW,
            }
            if not represented:
                raise ApprovalWorkflowConflict("business operation is not durably represented")
            if row["telegram_update_id"] is not None:
                conn.execute(
                    """UPDATE telegram_updates SET processing_state='processed',processed_at=?,approval_id=?
                       WHERE update_id=?""",
                    (now, row["approval_id"], row["telegram_update_id"]),
                )
            conn.execute(
                "UPDATE approval_workflows SET update_processed_at=?,updated_at=?,version=version+1 WHERE id=?",
                (now, now, workflow_id),
            )
        self._audit("approval_workflow_update_processed", workflow_id, {})

    def ensure_intent(
        self,
        workflow_id: str,
        proposal: dict[str, Any],
        *,
        run_id: str | None,
        source_type: str = "telegram",
    ) -> dict[str, Any]:
        workflow = self.get(workflow_id)
        if workflow.get("intent_id"):
            return DurableExecutionStore(self.storage).get_intent(str(workflow["intent_id"]))
        existing = self.storage.fetch_all("SELECT * FROM order_intents WHERE approval_id=?", (workflow["approval_id"],))
        if existing:
            self.transition(
                workflow_id,
                ApprovalWorkflowState.INTENT_CREATED,
                intent_id=existing[0]["id"],
                safe_detail="linked existing durable intent",
            )
            return existing[0]
        state = ApprovalWorkflowState(workflow["state"])
        if state != ApprovalWorkflowState.APPROVED_PENDING_INTENT:
            raise ApprovalWorkflowConflict("workflow is not eligible to create an intent")
        # create_or_get_intent commits intent + reservation + workflow link together.
        approval_rows = self.storage.fetch_all(
            "SELECT authority_fingerprint FROM approvals WHERE id=? AND proposal_id=?",
            (workflow["approval_id"], workflow["proposal_id"]),
        )
        durable_proposal = {
            **proposal,
            "proposal_id": workflow["proposal_id"],
            "source_id": workflow["proposal_id"],
        }
        if approval_rows and approval_rows[0].get("authority_fingerprint"):
            durable_proposal["approval_authority_fingerprint"] = approval_rows[0]["authority_fingerprint"]
        intent = DurableExecutionStore(self.storage).create_or_get_intent(
            durable_proposal,
            run_id=run_id,
            source_type=source_type,
            approval_id=workflow["approval_id"],
        )
        refreshed = self.get(workflow_id)
        if refreshed["state"] != ApprovalWorkflowState.INTENT_CREATED.value:
            self.transition(
                workflow_id,
                ApprovalWorkflowState.INTENT_CREATED,
                intent_id=intent["id"],
                safe_detail="intent and reservation committed",
            )
        return intent

    def recover(
        self,
        *,
        owner_token: str,
        proposal_loader: Callable[[str], dict[str, Any] | None],
        run_id: str | None,
        validator: Callable[[dict[str, Any], dict[str, Any] | None], tuple[str, dict[str, Any] | None, str | None]] | None = None,
        action_validator: Callable[[dict[str, Any], dict[str, Any] | None], tuple[str, dict[str, Any] | None, str | None]] | None = None,
        submitter: Callable[[dict[str, Any], dict[str, Any]], str] | None = None,
        lookup_reconciler: Callable[[dict[str, Any], dict[str, Any] | None], str] | None = None,
        max_items: int = 100,
    ) -> RecoverySummary:
        """Resume locally deterministic work; UNKNOWN is surfaced, never resubmitted."""
        counts = {field: 0 for field in RecoverySummary.__dataclass_fields__}
        seen: set[str] = set()
        for _ in range(max_items):
            workflow = self.claim_next(owner_token)
            if not workflow or workflow["id"] in seen:
                if workflow:
                    self.release_claim(workflow["id"], owner_token)
                break
            seen.add(workflow["id"])
            counts["claimed"] += 1
            workflow_id = workflow["id"]
            try:
                proposal_for_recovery: dict[str, Any] | None = None
                state = ApprovalWorkflowState(workflow["state"])
                intent_rows = self.storage.fetch_all(
                    "SELECT * FROM order_intents WHERE approval_id=?", (workflow["approval_id"],)
                )
                if intent_rows and not workflow.get("intent_id"):
                    self.transition(
                        workflow_id,
                        ApprovalWorkflowState.INTENT_CREATED,
                        owner_token=owner_token,
                        intent_id=intent_rows[0]["id"],
                        safe_detail="recovery linked existing intent",
                    )
                    counts["existing_intent_linked"] += 1
                    state = ApprovalWorkflowState.INTENT_CREATED
                if state in {
                    ApprovalWorkflowState.RECEIVED,
                    ApprovalWorkflowState.AUTHORIZED,
                    ApprovalWorkflowState.TARGET_RESOLVED,
                    ApprovalWorkflowState.VALIDATING,
                } and validator is not None:
                    proposal = proposal_loader(workflow["proposal_id"])
                    proposal_for_recovery = proposal
                    # The validation callback runs with no open DB transaction. It
                    # may refresh local inputs, but must return a durable policy
                    # decision rather than mutate this workflow itself.
                    decision, validated_proposal, reason = validator(workflow, proposal)
                    if decision == "approved":
                        current = state
                        for target in (
                            ApprovalWorkflowState.AUTHORIZED,
                            ApprovalWorkflowState.TARGET_RESOLVED,
                            ApprovalWorkflowState.VALIDATING,
                            ApprovalWorkflowState.APPROVED_PENDING_INTENT,
                        ):
                            if current == target:
                                continue
                            if target not in ALLOWED_WORKFLOW_TRANSITIONS[current]:
                                continue
                            self.transition(
                                workflow_id,
                                target,
                                owner_token=owner_token,
                                validation_status="passed" if target == ApprovalWorkflowState.APPROVED_PENDING_INTENT else None,
                                safe_detail=reason,
                            )
                            current = target
                        state = current
                        workflow = self.get(workflow_id)
                        if validated_proposal is not None:
                            proposal = validated_proposal
                            proposal_for_recovery = validated_proposal
                    elif decision == "blocked":
                        self.transition(
                            workflow_id,
                            ApprovalWorkflowState.TERMINAL,
                            owner_token=owner_token,
                            validation_status="blocked",
                            safe_detail=reason or "recovery validation blocked",
                        )
                        counts["blocked"] += 1
                        state = ApprovalWorkflowState.TERMINAL
                    elif decision == "manual_review":
                        self.transition(
                            workflow_id,
                            ApprovalWorkflowState.MANUAL_REVIEW,
                            owner_token=owner_token,
                            validation_status="ambiguous",
                            safe_detail=reason or "validation ambiguity requires operator review",
                        )
                        counts["external_ambiguity"] += 1
                        state = ApprovalWorkflowState.MANUAL_REVIEW
                    elif decision != "retry":
                        raise ValueError("validator returned an unsupported recovery decision")
                if state == ApprovalWorkflowState.APPROVED_PENDING_INTENT:
                    proposal = proposal_for_recovery or proposal_loader(workflow["proposal_id"])
                    action_allowed = True
                    action_reason: str | None = None
                    if proposal is not None and action_validator is not None:
                        action_decision, validated_proposal, action_reason = action_validator(
                            self.get(workflow_id), proposal
                        )
                        if validated_proposal is not None:
                            proposal = validated_proposal
                        if action_decision == "blocked":
                            self.transition(
                                workflow_id,
                                ApprovalWorkflowState.TERMINAL,
                                owner_token=owner_token,
                                validation_status="blocked",
                                safe_detail=action_reason or "recovery action authority is no longer current",
                            )
                            counts["blocked"] += 1
                            action_allowed = False
                        elif action_decision == "retry":
                            self._audit(
                                "approval_workflow_recovery_deferred",
                                workflow_id,
                                {"state": state.value, "reason": action_reason or "upstream coordinator owns recovery"},
                            )
                            action_allowed = False
                        elif action_decision != "approved":
                            raise ValueError("action validator returned an unsupported recovery decision")
                    if proposal is None:
                        self.transition(
                            workflow_id,
                            ApprovalWorkflowState.MANUAL_REVIEW,
                            owner_token=owner_token,
                            safe_detail="proposal record unavailable during recovery",
                        )
                        counts["external_ambiguity"] += 1
                    elif not action_allowed:
                        pass
                    elif str(proposal.get("status")) in {"expired", "rejected", "superseded"}:
                        self.transition(
                            workflow_id,
                            ApprovalWorkflowState.TERMINAL,
                            owner_token=owner_token,
                            validation_status="expired_or_ineligible",
                            safe_detail="recovery did not revive an ineligible proposal",
                        )
                        counts["blocked"] += 1
                    else:
                        self.ensure_intent(workflow_id, proposal, run_id=run_id)
                        counts["intent_created"] += 1
                        self.transition(
                            workflow_id,
                            ApprovalWorkflowState.SUBMISSION_PENDING,
                            owner_token=owner_token,
                            safe_detail="local recovery completed; broker submission remains separately bounded",
                        )
                        counts["submission_pending"] += 1
                elif state == ApprovalWorkflowState.INTENT_CREATED:
                    intent = intent_rows[0] if intent_rows else None
                    intent_state = str(intent.get("state")) if intent else "missing"
                    if intent_state in {"created", "reserved"}:
                        self.transition(
                            workflow_id,
                            ApprovalWorkflowState.SUBMISSION_PENDING,
                            owner_token=owner_token,
                            safe_detail="existing reserved intent ready for bounded submission",
                        )
                        counts["submission_pending"] += 1
                    elif intent_state in {"submitting", "unknown", "reconciliation_required"}:
                        self.transition(
                            workflow_id,
                            ApprovalWorkflowState.UNKNOWN,
                            owner_token=owner_token,
                            safe_detail="intent state proves external submission ambiguity; lookup only",
                        )
                        counts["external_ambiguity"] += 1
                    elif intent_state in {"submitted", "partially_filled", "cancel_pending"}:
                        self.transition(
                            workflow_id,
                            ApprovalWorkflowState.SUBMISSION_STARTED,
                            owner_token=owner_token,
                            safe_detail="workflow submission reconstructed from broker-relevant intent state",
                        )
                        self.transition(
                            workflow_id,
                            ApprovalWorkflowState.SUBMITTED,
                            owner_token=owner_token,
                            safe_detail="workflow reconstructed from broker-relevant intent state",
                        )
                    elif intent_state in {"filled", "cancelled", "rejected", "expired"}:
                        self.transition(
                            workflow_id,
                            ApprovalWorkflowState.SUBMISSION_STARTED,
                            owner_token=owner_token,
                            safe_detail="workflow submission reconstructed from terminal intent state",
                        )
                        self.transition(
                            workflow_id,
                            ApprovalWorkflowState.TERMINAL,
                            owner_token=owner_token,
                            safe_detail="workflow reconstructed from terminal intent state",
                        )
                    else:
                        self.transition(
                            workflow_id,
                            ApprovalWorkflowState.MANUAL_REVIEW,
                            owner_token=owner_token,
                            safe_detail="intent state cannot be determined safely",
                        )
                        counts["external_ambiguity"] += 1
                elif state == ApprovalWorkflowState.SUBMISSION_PENDING and submitter is not None:
                    intent = intent_rows[0] if intent_rows else (
                        DurableExecutionStore(self.storage).get_intent(str(workflow["intent_id"]))
                        if workflow.get("intent_id") else None
                    )
                    if intent is None:
                        self.transition(
                            workflow_id,
                            ApprovalWorkflowState.MANUAL_REVIEW,
                            owner_token=owner_token,
                            safe_detail="submission-pending workflow has no durable intent",
                        )
                        counts["external_ambiguity"] += 1
                    elif str(intent.get("state")) not in {"created", "reserved"}:
                        self.transition(
                            workflow_id,
                            ApprovalWorkflowState.UNKNOWN,
                            owner_token=owner_token,
                            safe_detail="submission callback suppressed because intent is already broker-relevant",
                        )
                        counts["external_ambiguity"] += 1
                    else:
                        proposal = proposal_loader(workflow["proposal_id"])
                        action_allowed = True
                        action_reason: str | None = None
                        if action_validator is not None:
                            action_decision, _validated_proposal, action_reason = action_validator(
                                self.get(workflow_id), proposal
                            )
                            if action_decision == "blocked":
                                DurableExecutionStore(self.storage).transition(
                                    str(intent["id"]),
                                    OrderState.EXPIRED,
                                    event_type="recovery_action_authority_expired",
                                    safe_summary=action_reason or "dependent authority expired",
                                    expected_state=OrderState(str(intent["state"])),
                                )
                                self.transition(
                                    workflow_id,
                                    ApprovalWorkflowState.TERMINAL,
                                    owner_token=owner_token,
                                    validation_status="blocked",
                                    safe_detail=action_reason or "recovery submission authority is no longer current",
                                )
                                counts["blocked"] += 1
                                action_allowed = False
                            elif action_decision == "retry":
                                self._audit(
                                    "approval_workflow_recovery_deferred",
                                    workflow_id,
                                    {"state": state.value, "reason": action_reason or "upstream coordinator owns submission"},
                                )
                                action_allowed = False
                            elif action_decision != "approved":
                                raise ValueError("action validator returned an unsupported recovery decision")
                        if action_allowed:
                            self.transition(
                                workflow_id,
                                ApprovalWorkflowState.SUBMISSION_STARTED,
                                owner_token=owner_token,
                                safe_detail="bounded submission callback starting",
                            )
                            # Deliberately outside every SQLite transaction.
                            try:
                                outcome = submitter(self.get(workflow_id), intent)
                            except Exception as exc:
                                outcome = "unknown"
                                reason = type(exc).__name__
                            target = {
                                "submitted": ApprovalWorkflowState.SUBMITTED,
                                "terminal": ApprovalWorkflowState.TERMINAL,
                                "unknown": ApprovalWorkflowState.UNKNOWN,
                            }.get(str(outcome).lower())
                            if target is None:
                                raise ValueError("submitter returned an unsupported recovery outcome")
                            self.transition(
                                workflow_id,
                                target,
                                owner_token=owner_token,
                                safe_detail=("bounded submission result persisted" if target != ApprovalWorkflowState.UNKNOWN else f"ambiguous submission outcome: {locals().get('reason', 'unknown')}")
                            )
                            if target == ApprovalWorkflowState.UNKNOWN:
                                counts["external_ambiguity"] += 1
                elif state == ApprovalWorkflowState.UNKNOWN and lookup_reconciler is not None:
                    intent = intent_rows[0] if intent_rows else None
                    # Lookup-only callback: this path never calls submitter and runs
                    # after the UNKNOWN state and recovery claim are committed.
                    outcome = str(lookup_reconciler(workflow, intent)).lower()
                    target = {
                        "submitted": ApprovalWorkflowState.SUBMITTED,
                        "terminal": ApprovalWorkflowState.TERMINAL,
                        "unknown": ApprovalWorkflowState.UNKNOWN,
                    }.get(outcome)
                    if target is None:
                        raise ValueError("lookup reconciler returned an unsupported recovery outcome")
                    if target != ApprovalWorkflowState.UNKNOWN:
                        self.transition(
                            workflow_id,
                            target,
                            owner_token=owner_token,
                            safe_detail="lookup-only reconciliation persisted",
                        )
                    else:
                        counts["external_ambiguity"] += 1
                elif state in {
                    ApprovalWorkflowState.SUBMISSION_STARTED,
                    ApprovalWorkflowState.SUBMITTED,
                } and lookup_reconciler is not None:
                    intent = intent_rows[0] if intent_rows else None
                    # Broker-relevant workflows are lookup-only. In particular,
                    # a filled/cancelled/rejected/expired intent must terminalise
                    # its local workflow after restart instead of being claimed
                    # forever. This path never calls the submission callback.
                    outcome = str(lookup_reconciler(workflow, intent)).lower()
                    if outcome == "terminal":
                        self.transition(
                            workflow_id,
                            ApprovalWorkflowState.TERMINAL,
                            owner_token=owner_token,
                            safe_detail="lookup-only reconciliation terminalised broker-relevant workflow",
                        )
                    elif outcome == "unknown":
                        self.transition(
                            workflow_id,
                            ApprovalWorkflowState.UNKNOWN,
                            owner_token=owner_token,
                            safe_detail="lookup-only reconciliation found unresolved broker state",
                        )
                        counts["external_ambiguity"] += 1
                    elif outcome == "submitted":
                        if state == ApprovalWorkflowState.SUBMISSION_STARTED:
                            self.transition(
                                workflow_id,
                                ApprovalWorkflowState.SUBMITTED,
                                owner_token=owner_token,
                                safe_detail="lookup-only reconciliation confirmed submitted intent",
                            )
                    else:
                        raise ValueError("lookup reconciler returned an unsupported recovery outcome")
                elif state in {
                    ApprovalWorkflowState.SUBMISSION_STARTED,
                    ApprovalWorkflowState.SUBMITTED,
                    ApprovalWorkflowState.UNKNOWN,
                }:
                    # Submission started without a conclusive durable result is external
                    # ambiguity: reconciliation may look up the stable client ID, but this
                    # local worker must never issue a second submit.
                    counts["external_ambiguity"] += 1
                    self._audit(
                        "approval_workflow_recovery_reconciliation_required",
                        workflow_id,
                        {"state": state.value, "automatic_resubmission": False},
                    )
                else:
                    self._audit(
                        "approval_workflow_recovery_deferred",
                        workflow_id,
                        {"state": state.value, "reason": "requires deterministic upstream handler"},
                    )
            except (sqlite3.OperationalError, ApprovalWorkflowConflict, ValueError) as exc:
                counts["failed_retryable"] += 1
                self.release_claim(workflow_id, owner_token, retryable_error=type(exc).__name__)
                continue
            self.release_claim(workflow_id, owner_token)
        return RecoverySummary(**counts)

    def _audit(self, event_type: str, workflow_id: str, detail: dict[str, Any]) -> None:
        rows = self.storage.fetch_all(
            "SELECT approval_id,proposal_id FROM approval_workflows WHERE id=?", (workflow_id,)
        )
        envelope = {"workflow_id": workflow_id, **detail}
        if rows:
            envelope.update(approval_id=rows[0]["approval_id"], proposal_id=rows[0]["proposal_id"])
        self.storage.execute(
            "INSERT INTO audit_events(run_id,event_type,actor,detail,created_at) VALUES(NULL,?,?,?,?)",
            (event_type, "approval_recovery", json_dumps(envelope), iso_now()),
        )
