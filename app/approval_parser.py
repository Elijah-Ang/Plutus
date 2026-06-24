from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

APPROVE = {"yes", "approve", "approved", "yes please"}
REJECT = {"no", "reject", "rejected", "no thanks"}


@dataclass(frozen=True)
class ApprovalResult:
    action: str
    accepted: bool
    reason: str
    proposal_id: str | None = None


def _time(value: str | datetime) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def parse_approval(
    text: str,
    sender_id: str | int,
    allowed_user_id: str | int,
    pending_proposals: list[dict[str, Any]],
    now: datetime | None = None,
    reply_to_message_id: str | int | None = None,
) -> ApprovalResult:
    if str(sender_id) != str(allowed_user_id):
        return ApprovalResult("reject", False, "unauthorized sender")
    normalized = " ".join(text.lower().strip().split())
    now = now or datetime.now(UTC)
    
    reject_words = r"(?:no|reject|rejected)(?: thanks)?"
    approve_words = r"(?:yes|approve|approved)(?: please)?"
    
    is_plain_reject = bool(re.fullmatch(reject_words, normalized))
    is_plain_approve = bool(re.fullmatch(approve_words, normalized))
    
    reject_match = re.fullmatch(reject_words + r"(?: (buy|sell) ([a-z.]{1,10}))?(?: (?:proposal )?([a-z0-9.-]+))?", normalized)
    approve_match = re.fullmatch(approve_words + r"(?: (buy|sell) ([a-z.]{1,10}))?(?: (?:proposal )?([a-z0-9.-]+))?", normalized)
    
    # 1. Handle reply_to_message_id targeting
    if reply_to_message_id is not None:
        if not is_plain_approve and not is_plain_reject:
            if not reject_match and not approve_match:
                return ApprovalResult("unclear", False, "message is not an unambiguous approval or rejection")
        
        action = "approve" if (is_plain_approve or approve_match) else "reject"
        
        # Match against telegram_message_id
        candidates = [p for p in pending_proposals if p.get("telegram_message_id") and str(p["telegram_message_id"]) == str(reply_to_message_id)]
        if not candidates:
            return ApprovalResult(action, False, "reply-to target proposal not found or already handled")
            
        proposal = candidates[0]
        if action == "approve":
            if _time(proposal["expires_at"]) <= now:
                return ApprovalResult("approve", False, "proposal expired", str(proposal["id"]))
            return ApprovalResult("approve", True, "unambiguous authorized approval", str(proposal["id"]))
        else:
            return ApprovalResult("reject", True, "explicit rejection", str(proposal["id"]))

    # 2. Handle plain yes/no (without reply-to)
    if is_plain_approve or is_plain_reject:
        action = "approve" if is_plain_approve else "reject"
        if len(pending_proposals) > 1:
            return ApprovalResult(action, False, "ambiguous plain action with multiple pending proposals")
        elif len(pending_proposals) == 1:
            proposal = pending_proposals[0]
            if action == "approve":
                if _time(proposal["expires_at"]) <= now:
                    return ApprovalResult("approve", False, "proposal expired", str(proposal["id"]))
                return ApprovalResult("approve", True, "unambiguous authorized approval", str(proposal["id"]))
            else:
                return ApprovalResult("reject", True, "explicit rejection", str(proposal["id"]))
        else:
            return ApprovalResult(action, False, "exactly one matching pending proposal is required")
            
    # 3. Handle standard matching with explicit symbol/id/side
    if reject_match:
        side, symbol, proposal_id = reject_match.groups()
        candidates = pending_proposals
        if proposal_id and not side and not symbol:
            symbol_matches = [p for p in candidates if str(p.get("symbol", "")).lower() == proposal_id.lower()]
            if len(symbol_matches) == 1:
                return ApprovalResult("reject", True, "explicit rejection", str(symbol_matches[0]["id"]))
        if proposal_id:
            candidates = [p for p in candidates if str(p["id"]).lower().startswith(proposal_id.lower())]
        if side:
            candidates = [p for p in candidates if str(p.get("side", "")).lower() == side.lower()]
        if symbol:
            candidates = [p for p in candidates if str(p.get("symbol", "")).lower() == symbol.lower()]
        if len(candidates) != 1:
            return ApprovalResult("reject", False, "identify proposal when pending count is not one")
        return ApprovalResult("reject", True, "explicit rejection", str(candidates[0]["id"]))
        
    if approve_match:
        side, symbol, proposal_id = approve_match.groups()
        candidates = pending_proposals
        if proposal_id and not side and not symbol:
            symbol_matches = [p for p in candidates if str(p.get("symbol", "")).lower() == proposal_id.lower()]
            if len(symbol_matches) == 1:
                proposal = symbol_matches[0]
                if _time(proposal["expires_at"]) <= now:
                    return ApprovalResult("approve", False, "proposal expired", str(proposal["id"]))
                return ApprovalResult("approve", True, "unambiguous authorized approval", str(proposal["id"]))
        if proposal_id:
            candidates = [p for p in candidates if str(p["id"]).lower().startswith(proposal_id.lower())]
        if side:
            candidates = [p for p in candidates if str(p.get("side", "")).lower() == side.lower()]
        if symbol:
            candidates = [p for p in candidates if str(p.get("symbol", "")).lower() == symbol.lower()]
        if len(candidates) != 1:
            return ApprovalResult("approve", False, "exactly one matching pending proposal is required")
        proposal = candidates[0]
        if _time(proposal["expires_at"]) <= now:
            return ApprovalResult("approve", False, "proposal expired", str(proposal["id"]))
        return ApprovalResult("approve", True, "unambiguous authorized approval", str(proposal["id"]))
        
    return ApprovalResult("unclear", False, "message is not an unambiguous approval or rejection")


def parse_reply(*args: Any, **kwargs: Any) -> ApprovalResult:
    return parse_approval(*args, **kwargs)
