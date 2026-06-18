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
) -> ApprovalResult:
    if str(sender_id) != str(allowed_user_id):
        return ApprovalResult("reject", False, "unauthorized sender")
    normalized = " ".join(text.lower().strip().split())
    now = now or datetime.now(UTC)
    
    reject_words = r"(?:no|reject|rejected)(?: thanks)?"
    approve_words = r"(?:yes|approve|approved)(?: please)?"
    
    reject_match = re.fullmatch(reject_words + r"(?: (buy|sell) ([a-z.]{1,10}))?(?: (?:proposal )?([a-z0-9-]+))?", normalized)
    approve_match = re.fullmatch(approve_words + r"(?: (buy|sell) ([a-z.]{1,10}))?(?: (?:proposal )?([a-z0-9-]+))?", normalized)
    
    if reject_match:
        side, symbol, proposal_id = reject_match.groups()
        candidates = pending_proposals
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

