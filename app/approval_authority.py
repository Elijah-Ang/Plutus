"""Canonical immutable terms bound to a durable manual approval."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping


AUTHORITY_FIELDS = (
    "proposal_id",
    "symbol",
    "side",
    "action",
    "position_lifecycle_id",
    "strategy_version",
    "relationship_type",
    "relationship_group_id",
    "expires_at",
    "max_quantity",
    "max_notional",
    "max_stop_risk",
    "config_hash",
    "formula_versions",
)

_FORMULA_KEYS = (
    "formula_version",
    "formula_versions",
    "risk_formula_version",
    "evidence_version",
    "schema_version",
    "configuration_schema_version",
    "strategy_policy_version",
    "registry_formula_version",
    "allocation_formula_version",
    "rotation_formula_version",
    "stop_policy_version",
    "sizing_policy_version",
    "accounting_version",
)


def _payload(row: Mapping[str, Any]) -> dict[str, Any]:
    value = row.get("payload")
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def _first(row: Mapping[str, Any], payload: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value is None or value == "":
            value = payload.get(key)
        if value is not None and value != "":
            return value
    return None


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and abs(result) != float("inf") else None


def _formula_versions(row: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    explicit = _first(row, payload, "formula_versions", "formula_versions_json")
    if isinstance(explicit, str):
        try:
            explicit = json.loads(explicit)
        except (TypeError, ValueError, json.JSONDecodeError):
            explicit = None
    if isinstance(explicit, Mapping):
        return {str(key): explicit[key] for key in sorted(explicit)}
    values: dict[str, Any] = {}
    for key in _FORMULA_KEYS:
        value = _first(row, payload, key)
        if value is not None and value != "":
            values[key] = value
    return values


def authority_envelope(row: Mapping[str, Any], *, proposal_id: str | None = None) -> dict[str, Any]:
    """Build a canonical envelope from authoritative DB columns plus payload."""
    payload = _payload(row)
    side = str(_first(row, payload, "side") or "").lower() or None
    action = _first(row, payload, "action", "intended_action", "candidate_action")
    if action is None and side:
        action = "exit" if side == "sell" else "entry"
    relationship_group_id = _first(
        row, payload, "relationship_group_id", "rotation_group_id", "relationship_group"
    )
    return {
        "proposal_id": str(proposal_id or _first(row, payload, "proposal_id", "id") or ""),
        "symbol": str(_first(row, payload, "symbol") or "").upper() or None,
        "side": side,
        "action": str(action).lower() if action is not None else None,
        "position_lifecycle_id": _first(row, payload, "position_lifecycle_id"),
        "strategy_version": _first(row, payload, "strategy_version"),
        "relationship_type": _first(row, payload, "relationship_type"),
        "relationship_group_id": relationship_group_id,
        "expires_at": _first(row, payload, "expires_at"),
        "max_quantity": _number(_first(
            row, payload, "max_quantity", "approved_quantity_ceiling", "approved_quantity", "qty", "quantity"
        )),
        "max_notional": _number(_first(
            row, payload, "max_notional", "approved_notional_ceiling", "approved_notional", "notional"
        )),
        "max_stop_risk": _number(_first(
            row, payload, "max_stop_risk", "approved_stop_risk_ceiling", "stop_risk_dollars", "initial_risk_dollars"
        )),
        "config_hash": _first(row, payload, "config_hash", "effective_config_hash"),
        "formula_versions": _formula_versions(row, payload),
    }


def canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def authority_fingerprint(envelope: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(envelope).encode("utf-8")).hexdigest()

