from __future__ import annotations

import hashlib
from enum import StrEnum
from typing import Any


class OrderState(StrEnum):
    CREATED = "created"
    RESERVED = "reserved"
    SUBMITTING = "submitting"
    SUBMITTED = "submitted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCEL_PENDING = "cancel_pending"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"
    UNKNOWN = "unknown"
    RECONCILIATION_REQUIRED = "reconciliation_required"


TERMINAL_STATES = {
    OrderState.FILLED,
    OrderState.CANCELLED,
    OrderState.REJECTED,
    OrderState.EXPIRED,
}

BROKER_RELEVANT_STATES = {
    OrderState.SUBMITTING,
    OrderState.SUBMITTED,
    OrderState.PARTIALLY_FILLED,
    OrderState.CANCEL_PENDING,
    OrderState.UNKNOWN,
    OrderState.RECONCILIATION_REQUIRED,
}

ACTIVE_RESERVATION_STATES = {
    OrderState.CREATED,
    OrderState.RESERVED,
    OrderState.SUBMITTING,
    OrderState.SUBMITTED,
    OrderState.PARTIALLY_FILLED,
    OrderState.CANCEL_PENDING,
    OrderState.UNKNOWN,
    OrderState.RECONCILIATION_REQUIRED,
}


ALLOWED_TRANSITIONS: dict[OrderState, set[OrderState]] = {
    OrderState.CREATED: {OrderState.RESERVED, OrderState.REJECTED, OrderState.EXPIRED},
    OrderState.RESERVED: {OrderState.SUBMITTING, OrderState.REJECTED, OrderState.EXPIRED},
    OrderState.SUBMITTING: {
        OrderState.SUBMITTED,
        OrderState.PARTIALLY_FILLED,
        OrderState.FILLED,
        OrderState.REJECTED,
        OrderState.UNKNOWN,
        OrderState.RECONCILIATION_REQUIRED,
    },
    OrderState.SUBMITTED: {
        OrderState.PARTIALLY_FILLED,
        OrderState.FILLED,
        OrderState.CANCEL_PENDING,
        OrderState.CANCELLED,
        OrderState.REJECTED,
        OrderState.EXPIRED,
        OrderState.UNKNOWN,
        OrderState.RECONCILIATION_REQUIRED,
    },
    OrderState.PARTIALLY_FILLED: {
        OrderState.FILLED,
        OrderState.CANCEL_PENDING,
        OrderState.CANCELLED,
        OrderState.EXPIRED,
        OrderState.UNKNOWN,
        OrderState.RECONCILIATION_REQUIRED,
    },
    OrderState.CANCEL_PENDING: {
        OrderState.CANCELLED,
        OrderState.PARTIALLY_FILLED,
        OrderState.FILLED,
        OrderState.UNKNOWN,
        OrderState.RECONCILIATION_REQUIRED,
    },
    OrderState.UNKNOWN: {
        OrderState.RECONCILIATION_REQUIRED,
        OrderState.SUBMITTED,
        OrderState.PARTIALLY_FILLED,
        OrderState.FILLED,
        OrderState.CANCELLED,
        OrderState.REJECTED,
        OrderState.EXPIRED,
    },
    OrderState.RECONCILIATION_REQUIRED: {
        OrderState.UNKNOWN,
        OrderState.SUBMITTED,
        OrderState.PARTIALLY_FILLED,
        OrderState.FILLED,
        OrderState.CANCELLED,
        OrderState.REJECTED,
        OrderState.EXPIRED,
    },
    OrderState.FILLED: set(),
    OrderState.CANCELLED: set(),
    OrderState.REJECTED: set(),
    OrderState.EXPIRED: set(),
}


class InvalidOrderTransition(RuntimeError):
    pass


def validate_transition(current: str | OrderState, target: str | OrderState) -> tuple[OrderState, OrderState]:
    source = OrderState(current)
    destination = OrderState(target)
    if source == destination:
        return source, destination
    if destination not in ALLOWED_TRANSITIONS[source]:
        raise InvalidOrderTransition(f"invalid order transition: {source.value} -> {destination.value}")
    return source, destination


def logical_action_key(proposal: dict[str, Any], source_type: str, sequence: int = 0) -> str:
    source_id = str(
        proposal.get("source_id")
        or proposal.get("proposal_id")
        or proposal.get("id")
        or proposal.get("emergency_action_id")
        or "missing-source"
    )
    lifecycle = str(proposal.get("position_lifecycle_id") or "no-lifecycle")
    parts = (
        source_type,
        source_id,
        lifecycle,
        str(proposal.get("symbol", "")).upper(),
        str(proposal.get("side", "")).lower(),
        str(proposal.get("action") or proposal.get("intended_action") or "entry").lower(),
        str(sequence),
    )
    return "|".join(parts)


def stable_client_order_id(action_key: str) -> str:
    """Return a restart-stable, non-sensitive Alpaca-safe identifier (32 chars)."""
    digest = hashlib.sha256(action_key.encode("utf-8")).hexdigest()
    return f"ta0-{digest[:28]}"


def broker_status_to_state(status: Any, filled_quantity: float = 0.0) -> OrderState | None:
    normalized = str(getattr(status, "value", status) or "").lower().replace("-", "_")
    if normalized in {"new", "accepted", "pending_new", "accepted_for_bidding", "stopped", "suspended"}:
        return OrderState.SUBMITTED
    if normalized in {"filled", "fill"}:
        return OrderState.FILLED
    if normalized in {"partially_filled", "partial_fill"} or filled_quantity > 0:
        return OrderState.PARTIALLY_FILLED
    if normalized in {"pending_cancel", "cancel_pending"}:
        return OrderState.CANCEL_PENDING
    if normalized in {"canceled", "cancelled"}:
        return OrderState.CANCELLED
    if normalized == "rejected":
        return OrderState.REJECTED
    if normalized in {"expired", "done_for_day"}:
        return OrderState.EXPIRED
    return None
