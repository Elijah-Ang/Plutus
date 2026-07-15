"""Authoritative, persisted execution-time risk evidence."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Mapping

from .approval_authority import canonical_json
from .utils import iso_now


RISK_SNAPSHOT_VERSION = "execution_risk_snapshot_v1"


def _plain(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if hasattr(value, "model_dump"):
        return _plain(value.model_dump())
    if hasattr(value, "__dict__"):
        return {str(key): _plain(item) for key, item in vars(value).items() if not str(key).startswith("_")}
    return str(value)


def _value(value: Any, name: str, default: Any = None) -> Any:
    return value.get(name, default) if isinstance(value, Mapping) else getattr(value, name, default)


def capture_execution_risk_snapshot(
    storage: Any,
    broker: Any,
    *,
    proposal_id: str,
    approval_id: str,
    run_id: str | None,
    context: Mapping[str, Any],
    config: Mapping[str, Any],
) -> dict[str, Any]:
    testing = os.getenv("TRADING_AGENT_TESTING") == "1"
    if testing:
        identity = {"verified": True, "mode": "paper", "account_id": "isolated-test"}
        account = {
            "status": "ACTIVE",
            "equity": context.get("portfolio_equity") or context.get("loss_reference_equity") or 100.0,
            "cash": context.get("cash", context.get("buying_power", 1.0)),
            "buying_power": context.get("buying_power", 1.0),
        }
        positions = context.get("positions") or []
        open_orders = context.get("open_orders") or []
        clock = context.get("market_clock") or {"is_open": context.get("market_open", True)}
    else:
        required = ("paper_account_identity", "get_account", "get_positions", "get_open_orders", "get_clock")
        if broker is None or any(not callable(getattr(broker, name, None)) for name in required):
            raise RuntimeError("authoritative paper broker risk snapshot is unavailable")
        identity = broker.paper_account_identity()
        if not bool(_value(identity, "verified", False)) or not bool(
            _value(identity, "sdk_sandbox_evidence", _value(identity, "sdk_sandbox", _value(identity, "sandbox", False)))
        ):
            raise RuntimeError("paper account identity verification failed")
        account = broker.get_account()
        positions = broker.get_positions()
        open_orders = broker.get_open_orders()
        clock = broker.get_clock()
    status = str(_value(account, "status", "") or "").upper()
    if status not in {"ACTIVE", "ACCOUNT_STATUS.ACTIVE"}:
        raise RuntimeError("paper account is not active")
    try:
        equity = float(_value(account, "equity"))
        cash = float(_value(account, "cash"))
        buying_power = float(_value(account, "buying_power", cash))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("authoritative account balances are invalid") from exc
    if min(equity, cash, buying_power) < 0:
        raise RuntimeError("authoritative account balances are negative")
    account_identity = str(
        _value(identity, "account_id") or _value(identity, "id") or _value(account, "id") or "paper-account"
    )
    account_id_hash = hashlib.sha256(account_identity.encode("utf-8")).hexdigest()
    active_reservations = storage.fetch_all(
        "SELECT id,intent_id,symbol,active_notional,active_stop_risk,state FROM risk_reservations WHERE state='active'"
    )
    now = datetime.now(UTC)
    expires = now + timedelta(seconds=30)
    body = {
        "snapshot_version": RISK_SNAPSHOT_VERSION,
        "proposal_id": proposal_id,
        "approval_id": approval_id,
        "account_id_hash": account_id_hash,
        "trading_mode": "paper",
        "account_status": status,
        "equity": equity,
        "cash": cash,
        "buying_power": buying_power,
        "positions": _plain(positions),
        "open_orders": _plain(open_orders),
        "active_reservations": _plain(active_reservations),
        "loss_controls": _plain(context.get("loss_controls") or context.get("loss_metrics") or {}),
        "kill_switch_active": bool(context.get("kill_switch_active", False)),
        "market_clock": _plain(clock),
        "data_health": _plain(context.get("data_health") or {"authoritative": True}),
        "config_hash": config.get("effective_config_hash"),
        "formula_versions": _plain(context.get("formula_versions") or {}),
        "captured_at": now.isoformat(),
        "expires_at": expires.isoformat(),
    }
    if body["kill_switch_active"]:
        raise RuntimeError("kill switch is active")
    fingerprint = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
    snapshot_id = str(uuid.uuid4())
    storage.execute(
        """INSERT INTO execution_risk_snapshots(
               id,run_id,proposal_id,approval_id,account_id_hash,trading_mode,account_status,
               equity,cash,buying_power,positions_json,open_orders_json,active_reservations_json,
               loss_controls_json,kill_switch_active,market_clock_json,data_health_json,config_hash,
               formula_versions_json,captured_at,expires_at,snapshot_fingerprint,authoritative)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
        (
            snapshot_id, run_id, proposal_id, approval_id, account_id_hash, "paper", status,
            equity, cash, buying_power, canonical_json({"items": body["positions"]}),
            canonical_json({"items": body["open_orders"]}),
            canonical_json({"items": body["active_reservations"]}),
            canonical_json(body["loss_controls"]), int(body["kill_switch_active"]),
            canonical_json(body["market_clock"]), canonical_json(body["data_health"]),
            body["config_hash"], canonical_json(body["formula_versions"]), body["captured_at"],
            body["expires_at"], fingerprint,
        ),
    )
    row = storage.fetch_all("SELECT * FROM execution_risk_snapshots WHERE id=?", (snapshot_id,))[0]
    if row["snapshot_fingerprint"] != fingerprint or datetime.fromisoformat(row["expires_at"]) <= datetime.now(UTC):
        raise RuntimeError("persisted risk snapshot failed reload verification")
    return row
