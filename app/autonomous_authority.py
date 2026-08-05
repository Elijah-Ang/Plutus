"""Deterministic paper authority for system-generated proposals.

The system authority is intentionally represented in the same immutable
display, approval, and workflow tables as a human approval.  The synthetic
identity is deterministic and cannot be supplied by a caller to bypass the
stored proposal terms.
"""

from __future__ import annotations

import json
from typing import Any

from .approval_display import record_display
from .approval_workflow import ApprovalWorkflowStore
from .utils import iso_now, json_dumps


AUTONOMOUS_SENDER_ID = "plutus:autonomous"
AUTONOMOUS_RAW_MESSAGE = "SYSTEM AUTONOMOUS PAPER AUTHORITY"


def _payload(row: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(row.get("payload") or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        value = {}
    return dict(value) if isinstance(value, dict) else {}


def authorize_proposal(storage: Any, proposal_id: str, *, run_id: str | None = None) -> dict[str, Any]:
    """Create or reuse deterministic authority for one paper proposal.

    This function does not decide direction, size, or eligibility.  Those
    values must already be present in the persisted proposal and are captured
    by ``record_display`` before the authority is consumed.
    """

    rows = storage.fetch_all("SELECT * FROM trade_proposals WHERE id=?", (proposal_id,))
    if not rows:
        raise LookupError("autonomous authority requires an existing proposal")
    row = dict(rows[0])
    payload = _payload(row)
    emergency = int(row.get("emergency_exit_triggered") or payload.get("emergency_exit_triggered") or 0) == 1
    source_type = "emergency" if emergency else "autonomous_system"
    execution_path = "protective_paper_exit" if emergency else "autonomous_paper_order"

    # The payload is part of the exact immutable display envelope.  Persist the
    # system source before creating the display so a restart cannot reinterpret
    # an autonomous proposal as a manual proposal.
    changed = False
    for key, value in (
        ("approval_source_type", source_type),
        ("execution_path", execution_path),
        ("autonomous_entry_requested", not emergency and str(row.get("side") or "").lower() == "buy"),
        ("autonomous_exit_requested", str(row.get("side") or "").lower() == "sell"),
    ):
        if payload.get(key) != value:
            payload[key] = value
            changed = True
    if changed:
        storage.execute("UPDATE trade_proposals SET payload=? WHERE id=? AND status IN ('pending','approved')", (json_dumps(payload), proposal_id))
        row["payload"] = json_dumps(payload)

    approval_id = f"autonomous:{proposal_id}"
    display_id = approval_id
    existing = storage.fetch_all("SELECT * FROM approvals WHERE id=?", (approval_id,))
    if existing:
        workflow = ApprovalWorkflowStore(storage).get_by_approval(approval_id)
        if workflow is None:
            raise RuntimeError("autonomous approval exists without its durable workflow")
        return {"approval_id": approval_id, "workflow": workflow, "source_type": source_type}

    record_display(
        storage,
        proposal_id,
        display_id,
        context_type="autonomous_paper",
        context_id=proposal_id,
    )
    workflow = ApprovalWorkflowStore(storage).accept_approval(
        approval_id=approval_id,
        run_id=run_id,
        proposal_id=proposal_id,
        sender_id=AUTONOMOUS_SENDER_ID,
        raw_message=AUTONOMOUS_RAW_MESSAGE,
        parsed_action="approve",
        telegram_update_id=None,
        reply_to_message_id=display_id,
        targeting_method="deterministic_autonomous_authority",
        acknowledgement_status="not_applicable",
        approval_received_at=iso_now(),
    )
    if not storage.consume_approval(proposal_id, approval_id):
        # A restart can race the first worker after the workflow is accepted.
        # Re-read the durable state and only fail if the proposal was not
        # actually consumed by either worker.
        consumed = storage.fetch_all(
            "SELECT 1 FROM approvals WHERE id=? AND status='consumed' AND consumed_at IS NOT NULL",
            (approval_id,),
        )
        if not consumed:
            raise RuntimeError("autonomous paper authority could not be consumed")
    return {"approval_id": approval_id, "workflow": workflow, "source_type": source_type}


__all__ = ["authorize_proposal", "AUTONOMOUS_SENDER_ID", "AUTONOMOUS_RAW_MESSAGE"]
