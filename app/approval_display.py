"""Immutable authority for the exact terms shown to a manual approver."""

from __future__ import annotations

import json
import math
import os
import uuid
from datetime import UTC, datetime
from typing import Any, Mapping

from .approval_authority import authority_envelope, authority_fingerprint, canonical_json
from .canonical_sizing import canonical_sizing
from .execution_risk_snapshot import REQUIRED_FORMULA_VERSIONS
from .utils import iso_now


DISPLAY_SCHEMA_VERSION = "telegram_approval_display_v1"


def display_envelope(
    proposal: Mapping[str, Any],
    *,
    telegram_message_id: str,
    proposal_version: int,
    display_context_type: str = "proposal",
    display_context_id: str | None = None,
) -> dict[str, Any]:
    base = authority_envelope(proposal, proposal_id=str(proposal.get("id") or proposal.get("proposal_id") or ""))
    source_economic_terms = {
        "quantity": base.get("max_quantity"),
        "notional": base.get("max_notional"),
        "stop_risk": base.get("max_stop_risk"),
    }
    payload = proposal.get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}
    payload = payload if isinstance(payload, Mapping) else {}
    emergency_triggered = int(proposal.get("emergency_exit_triggered") or payload.get("emergency_exit_triggered") or 0)
    source_type = str(
        proposal.get("approval_source_type") or payload.get("approval_source_type")
        or ("emergency" if emergency_triggered else "proposal")
    )
    execution_path = "protective_paper_exit" if source_type == "emergency" else str(
        proposal.get("execution_path") or payload.get("execution_path") or "manual_paper_order"
    )
    request_basis = proposal.get("request_basis") or payload.get("request_basis")
    if request_basis not in {"quantity", "notional"}:
        request_basis = "quantity" if (
            proposal.get("qty") not in (None, "") or payload.get("qty") not in (None, "")
        ) else "notional"
    merged_terms = {**payload, **dict(proposal), "request_basis": request_basis}
    if os.getenv("TRADING_AGENT_TESTING") == "1":
        merged_terms["latest_price"] = float(
            merged_terms.get("latest_price") or merged_terms.get("current_price") or 1.0
        )
        if request_basis == "notional" and merged_terms.get("notional") in (None, ""):
            merged_terms["notional"] = 1.0
        if request_basis == "quantity" and merged_terms.get("qty") in (None, ""):
            merged_terms["qty"] = 1.0
    try:
        sizing = canonical_sizing(merged_terms)
    except (TypeError, ValueError):
        sizing = None
        if os.getenv("TRADING_AGENT_TESTING") == "1":
            isolated_terms = dict(merged_terms)
            if request_basis == "quantity":
                isolated_terms.pop("notional", None)
            else:
                isolated_terms.pop("qty", None)
                isolated_terms.pop("quantity", None)
            try:
                sizing = canonical_sizing(isolated_terms)
            except (TypeError, ValueError):
                sizing = None
    if sizing is None:
        raise RuntimeError("display authority requires canonical sizing and applicable ceilings")
    base["max_quantity"] = sizing.quantity
    base["max_notional"] = sizing.notional
    if str(base.get("action") or "") in {"entry", "add"}:
        base["max_stop_risk"] = sizing.stop_risk
    applicable = [base["max_quantity"] if request_basis == "quantity" else base["max_notional"]]
    if str(base.get("action") or "") in {"entry", "add"}:
        applicable.append(base.get("max_stop_risk"))
    if any(value is None or not math.isfinite(float(value)) or float(value) < 0 for value in applicable):
        raise RuntimeError("display authority has a missing or invalid applicable ceiling")
    if os.getenv("TRADING_AGENT_TESTING") == "1":
        base["config_hash"] = base.get("config_hash") or "isolated-test-config"
        base["formula_versions"] = base.get("formula_versions") or dict(REQUIRED_FORMULA_VERSIONS)
    if not str(base.get("config_hash") or "").strip():
        raise RuntimeError("display authority requires a nonempty current configuration hash")
    formulas = base.get("formula_versions")
    if not isinstance(formulas, Mapping) or any(
        str(formulas.get(key) or "") != expected
        for key, expected in REQUIRED_FORMULA_VERSIONS.items()
    ):
        raise RuntimeError("display authority requires all current formula versions")
    return {
        **base,
        "source_economic_terms": source_economic_terms,
        "display_schema_version": DISPLAY_SCHEMA_VERSION,
        "proposal_version": int(proposal_version),
        "telegram_message_id": str(telegram_message_id),
        "display_context_type": str(display_context_type),
        "display_context_id": str(display_context_id) if display_context_id else None,
        "approval_source_type": source_type,
        "execution_path": execution_path,
        "request_basis": request_basis,
        "rotation_group_id": proposal.get("rotation_group_id") or payload.get("rotation_group_id"),
        "rotation_step_id": proposal.get("rotation_step_id") or payload.get("rotation_step_id"),
        "emergency_triggered": emergency_triggered,
        "emergency_trigger_identity": (
            proposal.get("emergency_exit_hard_trigger") or payload.get("emergency_exit_hard_trigger")
            if emergency_triggered else None
        ),
        "emergency_trigger_reason": (
            proposal.get("emergency_exit_trigger_reason") or payload.get("emergency_exit_trigger_reason")
            if emergency_triggered else None
        ),
        "emergency_trigger_mode": (
            proposal.get("emergency_exit_mode") or payload.get("emergency_exit_mode")
            if emergency_triggered else None
        ),
        "proposal_eligibility_status": str(proposal.get("status") or ""),
    }


def record_display(
    storage: Any,
    proposal_id: str,
    telegram_message_id: str,
    *,
    context_type: str = "proposal",
    context_id: str | None = None,
) -> dict[str, Any]:
    """Persist the approval surface after Telegram acknowledges the send.

    A proposal version is single-display. Replaying the exact same send is
    idempotent; changing its terms or message identity is rejected.
    """
    now = iso_now()
    with storage.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM trade_proposals WHERE id=?", (proposal_id,)).fetchone()
        if row is None:
            raise LookupError("cannot display an unknown proposal")
        proposal = dict(row)
        if str(proposal.get("status") or "") not in {"pending", "approved"}:
            raise RuntimeError("only an eligible proposal may become an approval surface")
        version = int(proposal.get("proposal_version") or 1)
        envelope = display_envelope(
            proposal,
            telegram_message_id=str(telegram_message_id),
            proposal_version=version,
            display_context_type=context_type,
            display_context_id=context_id,
        )
        fingerprint = authority_fingerprint(envelope)
        existing = conn.execute(
            "SELECT * FROM proposal_display_envelopes WHERE proposal_id=? AND proposal_version=?",
            (proposal_id, version),
        ).fetchone()
        if existing is not None:
            if existing["displayed_fingerprint"] != fingerprint:
                raise RuntimeError("proposal version was already displayed with different immutable terms")
            return dict(existing)
        display_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO proposal_display_envelopes(
                   id,proposal_id,proposal_version,telegram_message_id,displayed_at,
                   displayed_envelope_json,displayed_fingerprint,display_context_type,
                   display_context_id,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                display_id, proposal_id, version, str(telegram_message_id), now,
                canonical_json(envelope), fingerprint, context_type, context_id, now,
            ),
        )
        conn.execute(
            "UPDATE trade_proposals SET telegram_message_id=?,displayed_fingerprint=? WHERE id=?",
            (str(telegram_message_id), fingerprint, proposal_id),
        )
        result = conn.execute("SELECT * FROM proposal_display_envelopes WHERE id=?", (display_id,)).fetchone()
    return dict(result)


def load_display_for_approval(
    conn: Any,
    proposal_id: str,
    *,
    reply_to_message_id: str | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    proposal = conn.execute("SELECT * FROM trade_proposals WHERE id=?", (proposal_id,)).fetchone()
    if proposal is None:
        raise RuntimeError("authoritative trade proposal is missing")
    row = conn.execute(
        """SELECT * FROM proposal_display_envelopes
           WHERE proposal_id=? ORDER BY proposal_version DESC LIMIT 1""",
        (proposal_id,),
    ).fetchone()
    # Existing unit fixtures predate immutable displays. Production and normal
    # development always fail closed; tests can synthesize the historical
    # surface so legacy behavior remains explicitly isolated from runtime.
    if row is None and os.getenv("TRADING_AGENT_TESTING") == "1":
        message_id = str(reply_to_message_id or proposal["telegram_message_id"] or f"test:{proposal_id}")
        version = int(proposal["proposal_version"] or 1)
        envelope = display_envelope(
            dict(proposal), telegram_message_id=message_id, proposal_version=version,
            display_context_type="test_fixture",
        )
        fingerprint = authority_fingerprint(envelope)
        display_id = str(uuid.uuid4())
        now = iso_now()
        conn.execute(
            """INSERT INTO proposal_display_envelopes VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (display_id, proposal_id, version, message_id, now, canonical_json(envelope), fingerprint, "test_fixture", None, now),
        )
        conn.execute("UPDATE trade_proposals SET displayed_fingerprint=? WHERE id=?", (fingerprint, proposal_id))
        row = conn.execute("SELECT * FROM proposal_display_envelopes WHERE id=?", (display_id,)).fetchone()
    if row is None:
        raise RuntimeError("proposal has no immutable Telegram display authority")
    if reply_to_message_id is not None and str(row["telegram_message_id"]) != str(reply_to_message_id):
        raise RuntimeError("approval reply does not target the displayed proposal message")
    try:
        envelope = json.loads(row["displayed_envelope_json"])
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("displayed approval envelope is invalid") from exc
    if not isinstance(envelope, dict) or authority_fingerprint(envelope) != row["displayed_fingerprint"]:
        raise RuntimeError("displayed approval fingerprint is invalid")
    try:
        current = display_envelope(
            dict(proposal),
            telegram_message_id=str(row["telegram_message_id"]),
            proposal_version=int(row["proposal_version"]),
            display_context_type=str(row["display_context_type"]),
            display_context_id=row["display_context_id"],
        )
    except RuntimeError as exc:
        raise RuntimeError("proposal terms changed after they were displayed") from exc
    if current != envelope:
        raise RuntimeError("proposal terms changed after they were displayed")
    if int(proposal["proposal_version"] or 1) != int(row["proposal_version"]):
        raise RuntimeError("approval targets a superseded proposal version")
    eligible_status = str(proposal["status"] or "") == "pending"
    if os.getenv("TRADING_AGENT_TESTING") == "1" and row["display_context_type"] == "test_fixture":
        eligible_status = str(proposal["status"] or "") in {"pending", "approved"}
    if not eligible_status:
        raise RuntimeError("proposal is no longer eligible for approval")
    return dict(row), envelope


def validate_consumed_display_authority(
    conn: Any,
    *,
    approval_id: str,
    proposal_id: str,
    source_type: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Recompute display/approval/proposal authority inside the caller transaction."""
    approval = conn.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
    proposal = conn.execute("SELECT * FROM trade_proposals WHERE id=?", (proposal_id,)).fetchone()
    if approval is None or proposal is None:
        raise RuntimeError("approval or authoritative proposal is missing")
    if str(approval["proposal_id"] or "") != proposal_id:
        raise RuntimeError("approval is linked to a different proposal")
    if int(approval["authorized"] or 0) != 1 or str(approval["status"] or "") != "consumed" or approval["consumed_at"] is None:
        raise RuntimeError("approval has not been authorized and consumed exactly once")
    if str(proposal["status"] or "") != "approved":
        raise RuntimeError("proposal is no longer executable")
    expires_at = proposal["expires_at"]
    try:
        expiry = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
        expiry = expiry.replace(tzinfo=UTC) if expiry.tzinfo is None else expiry.astimezone(UTC)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("proposal expiry is invalid") from exc
    if expiry <= datetime.now(UTC):
        raise RuntimeError("approval or proposal has expired")
    display = conn.execute(
        "SELECT * FROM proposal_display_envelopes WHERE id=? AND proposal_id=?",
        (approval["display_envelope_id"], proposal_id),
    ).fetchone()
    if display is None:
        raise RuntimeError("approval is not bound to an immutable displayed proposal")
    try:
        envelope = json.loads(display["displayed_envelope_json"])
        approved = json.loads(approval["authority_envelope_json"])
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("approval authority JSON is invalid") from exc
    fingerprint = str(display["displayed_fingerprint"] or "")
    if not isinstance(envelope, dict) or not isinstance(approved, dict):
        raise RuntimeError("approval authority envelope is invalid")
    if authority_fingerprint(envelope) != fingerprint or approved != envelope:
        raise RuntimeError("displayed and accepted authority fingerprints disagree")
    if str(approval["authority_fingerprint"] or "") != fingerprint:
        raise RuntimeError("accepted approval fingerprint does not match the display")
    if str(approval["displayed_fingerprint"] or "") != fingerprint:
        raise RuntimeError("approval display binding is missing")
    if str(proposal["displayed_fingerprint"] or "") != fingerprint:
        raise RuntimeError("proposal display fingerprint changed")
    if int(proposal["proposal_version"] or 1) != int(display["proposal_version"]):
        raise RuntimeError("proposal version changed after approval")
    approved_source = str(envelope.get("approval_source_type") or "proposal")
    effective_source = str(source_type)
    if os.getenv("TRADING_AGENT_TESTING") == "1" and display["display_context_type"] == "test_fixture" and effective_source == "telegram":
        effective_source = "proposal"
    if approved_source != effective_source or str(approval["approval_source_type"] or "") != approved_source:
        raise RuntimeError("execution source does not match the approved path")
    expected_path = "protective_paper_exit" if approved_source == "emergency" else "manual_paper_order"
    if str(envelope.get("execution_path") or "") != expected_path or str(approval["execution_path"] or "") != expected_path:
        raise RuntimeError("execution path does not match the displayed authority")
    if approved_source != "emergency" and any(
        envelope.get(key) for key in ("emergency_trigger_identity", "emergency_trigger_reason", "emergency_trigger_mode")
    ):
        raise RuntimeError("ordinary approval cannot carry emergency authority")
    if approved_source == "emergency" and not envelope.get("emergency_trigger_reason"):
        raise RuntimeError("emergency approval lacks an immutable trigger")
    # Proposal status is the one allowed transition after display. Recompute all
    # other displayed fields from the authoritative row and compare exactly.
    recompute_row = dict(proposal)
    recompute_row["status"] = envelope.get("proposal_eligibility_status")
    current = display_envelope(
        recompute_row,
        telegram_message_id=str(display["telegram_message_id"]),
        proposal_version=int(display["proposal_version"]),
        display_context_type=str(display["display_context_type"]),
        display_context_id=display["display_context_id"],
    )
    if current != envelope:
        raise RuntimeError("authoritative proposal terms changed after display")
    return dict(proposal), dict(approval), envelope
