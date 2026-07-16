"""Durable exit-priority state machine and explicit safe remediation."""

from __future__ import annotations

import uuid
from typing import Any, Mapping

from .utils import iso_now, json_dumps


TERMINAL_BLOCKER_STATES = {"cleared", "terminal_attempt_failed", "position_closed", "superseded"}


def _classification(source_type: str, status: str) -> tuple[str, str, str]:
    if source_type == "broker_open_order":
        return "broker_order_open", "wait for broker reconciliation", "reconcile only; never resubmit"
    if source_type in {"sell_order_intent", "active_sell_reservation"}:
        if status in {"unknown", "reconciliation_required", "submitting"}:
            return "reconciliation_required", "manual broker lookup may be required", "lookup by stable client order ID; never retry"
        return "exit_intent_in_progress", "no new approval is needed", "reconcile the existing intent"
    if source_type in {"active_sell_proposal", "active_sell_batch_candidate"}:
        return "awaiting_manual_approval", "approve or reject the displayed exit", "expire safely if no response"
    if source_type in {"current_position_management_decision", "current_cycle_exit_signal"}:
        return "fresh_decision_awaiting_proposal", "wait for a fresh displayed proposal", "generate a new proposal only from fresh valid data"
    return "active_exit_priority", "review current exit evidence", "revalidate from current state"


class ExitBlockerStore:
    def __init__(self, storage: Any, run_id: str | None = None) -> None:
        self.storage = storage
        self.run_id = run_id

    def observe(self, blocker: Mapping[str, Any]) -> dict[str, Any]:
        symbol = str(blocker.get("symbol") or "").upper()
        source_type = str(blocker.get("source_type") or blocker.get("source") or "")
        source_id = str(blocker.get("source_id") or "")
        if not symbol or not source_type or not source_id:
            raise ValueError("active exit blocker requires symbol and stable provenance")
        status = str(blocker.get("status") or "active").lower()
        state, user_action, automatic = _classification(source_type, status)
        now = iso_now()
        proposal_id = blocker.get("proposal_id") or (source_id if "proposal" in source_type else None)
        approval_id = workflow_id = intent_id = broker_order_id = None
        if proposal_id:
            rows = self.storage.fetch_all(
                """SELECT a.id approval_id,w.id workflow_id,w.intent_id
                   FROM approvals a LEFT JOIN approval_workflows w ON w.approval_id=a.id
                   WHERE a.proposal_id=? ORDER BY a.created_at DESC LIMIT 1""",
                (proposal_id,),
            )
            if rows:
                approval_id, workflow_id, intent_id = rows[0].get("approval_id"), rows[0].get("workflow_id"), rows[0].get("intent_id")
        intent_id = blocker.get("order_intent_id") or intent_id
        if intent_id:
            rows = self.storage.fetch_all("SELECT broker_order_id,proposal_id,approval_id FROM order_intents WHERE id=?", (intent_id,))
            if rows:
                broker_order_id = rows[0].get("broker_order_id")
                proposal_id = proposal_id or rows[0].get("proposal_id")
                approval_id = approval_id or rows[0].get("approval_id")
        if source_type == "broker_open_order":
            broker_order_id = source_id
        lifecycle_id = blocker.get("position_lifecycle_id")
        with self.storage.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute("SELECT * FROM exit_blocker_states WHERE symbol=? AND active=1", (symbol,)).fetchone()
            if current and current["source_type"] == source_type and current["source_id"] == source_id:
                conn.execute(
                    """UPDATE exit_blocker_states SET state=?,updated_at=?,
                           trigger_reason=?,user_action_required=?,automatic_recovery=?,detail_json=?,
                           proposal_id=COALESCE(?,proposal_id),approval_id=COALESCE(?,approval_id),
                           workflow_id=COALESCE(?,workflow_id),intent_id=COALESCE(?,intent_id),
                           broker_order_id=COALESCE(?,broker_order_id) WHERE id=?""",
                    (state, now, str(blocker.get("reason") or "active exit priority"), user_action, automatic,
                     json_dumps(dict(blocker)), proposal_id, approval_id, workflow_id, intent_id, broker_order_id, current["id"]),
                )
                blocker_id = current["id"]
            else:
                generation = int(current["generation"] or 0) + 1 if current else int(
                    (conn.execute("SELECT COALESCE(MAX(generation),0) n FROM exit_blocker_states WHERE symbol=?", (symbol,)).fetchone()["n"] or 0) + 1
                )
                if current:
                    conn.execute(
                        "UPDATE exit_blocker_states SET active=0,state='superseded',cleared_at=?,terminal_at=?,updated_at=? WHERE id=?",
                        (now, now, now, current["id"]),
                    )
                blocker_id = str(uuid.uuid4())
                conn.execute(
                    """INSERT INTO exit_blocker_states(
                           id,symbol,position_lifecycle_id,generation,state,source_type,source_id,run_id,
                           proposal_id,approval_id,workflow_id,intent_id,broker_order_id,trigger_reason,active,
                           user_action_required,automatic_recovery,recovery_classification,created_at,updated_at,detail_json)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?,?,?)""",
                    (blocker_id, symbol, lifecycle_id, generation, state, source_type, source_id,
                     self.run_id, proposal_id, approval_id, workflow_id, intent_id, broker_order_id,
                     str(blocker.get("reason") or "active exit priority"), user_action, automatic,
                     "safe_automatic" if state == "fresh_decision_awaiting_proposal" else "state_dependent",
                     now, now, json_dumps(dict(blocker))),
                )
            row = conn.execute("SELECT * FROM exit_blocker_states WHERE id=?", (blocker_id,)).fetchone()
        result = dict(blocker)
        result.update({
            "blocker_state_id": row["id"], "blocker_state": row["state"],
            "blocker_generation": row["generation"], "user_action_required": row["user_action_required"],
            "automatic_recovery": row["automatic_recovery"], "proposal_id": row["proposal_id"],
            "approval_id": row["approval_id"], "workflow_id": row["workflow_id"],
            "order_intent_id": row["intent_id"], "broker_order_id": row["broker_order_id"],
        })
        return result

    def clear_absent(self, *, observed_symbols: set[str] | None = None, reason: str = "no current authoritative exit blocker") -> int:
        now = iso_now()
        rows = self.storage.fetch_all("SELECT id,symbol FROM exit_blocker_states WHERE active=1")
        cleared = 0
        for row in rows:
            if observed_symbols is not None and str(row["symbol"]).upper() not in observed_symbols:
                continue
            self.storage.execute(
                """UPDATE exit_blocker_states SET active=0,state='cleared',cleared_at=?,terminal_at=?,
                       updated_at=?,trigger_reason=? WHERE id=? AND active=1""",
                (now, now, now, reason, row["id"]),
            )
            cleared += 1
        return cleared
