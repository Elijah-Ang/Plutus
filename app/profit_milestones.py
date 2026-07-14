"""Fill-authoritative take-profit milestone accounting.

Proposal and order states are deliberately projections only.  A milestone can
advance only from a unique durable broker fill event linked through its order
intent to one active position lifecycle and one displayed take-profit level.
"""

from __future__ import annotations

import json
import math
import uuid
from typing import Any, Mapping

from .formula_versions import PLUTUS_AUDIT_SCHEMA_VERSION
from .utils import iso_now, json_dumps


TAKE_PROFIT_PROGRESS_FORMULA_VERSION = "take_profit_fill_progress_v1"


def apply_profit_milestone_schema(conn: Any, *, record_migration: bool = True) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS take_profit_milestones(
             id TEXT PRIMARY KEY,position_lifecycle_id TEXT NOT NULL,symbol TEXT NOT NULL,
             take_profit_level INTEGER NOT NULL CHECK(take_profit_level IN (1,2,3)),
             target_quantity REAL NOT NULL CHECK(target_quantity>0),
             cumulative_filled_quantity REAL NOT NULL DEFAULT 0 CHECK(cumulative_filled_quantity>=0),
             completed_fraction REAL NOT NULL DEFAULT 0 CHECK(completed_fraction>=0 AND completed_fraction<=1),
             status TEXT NOT NULL CHECK(status IN ('pending_fill','partially_filled','filled')),
             formula_version TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,completed_at TEXT,
             UNIQUE(position_lifecycle_id,take_profit_level))"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS take_profit_milestone_actions(
             id TEXT PRIMARY KEY,milestone_id TEXT NOT NULL,proposal_id TEXT NOT NULL,
             order_intent_id TEXT NOT NULL UNIQUE,requested_quantity REAL NOT NULL CHECK(requested_quantity>0),
             cumulative_filled_quantity REAL NOT NULL DEFAULT 0 CHECK(cumulative_filled_quantity>=0),
             completed_fraction REAL NOT NULL DEFAULT 0 CHECK(completed_fraction>=0 AND completed_fraction<=1),
             status TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
             UNIQUE(milestone_id,proposal_id,order_intent_id))"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS take_profit_milestone_fill_links(
             id TEXT PRIMARY KEY,milestone_id TEXT NOT NULL,action_id TEXT NOT NULL,
             broker_fill_event_id TEXT NOT NULL UNIQUE,broker_event_key TEXT NOT NULL UNIQUE,
             delta_quantity REAL NOT NULL CHECK(delta_quantity>=0),
             cumulative_intent_quantity REAL NOT NULL CHECK(cumulative_intent_quantity>=0),
             fill_price REAL NOT NULL CHECK(fill_price>=0),occurred_at TEXT NOT NULL,
             applied_at TEXT NOT NULL,payload TEXT NOT NULL)"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_take_profit_progress_lifecycle "
        "ON take_profit_milestones(position_lifecycle_id,take_profit_level,status)"
    )
    if record_migration:
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version,applied_at,detail) VALUES(?,?,?)",
            (
                PLUTUS_AUDIT_SCHEMA_VERSION,
                iso_now(),
                "fill-based take-profit progress, lifecycle provenance, directional approvals, and explicit risk units",
            ),
        )


def _take_profit_level(proposal: Mapping[str, Any]) -> int | None:
    if str(proposal.get("position_management_decision_type") or "") != "TAKE_PROFIT_PARTIAL":
        return None
    decision = proposal.get("position_management_decision")
    if isinstance(decision, str):
        try:
            decision = json.loads(decision)
        except (TypeError, ValueError, json.JSONDecodeError):
            decision = {}
    try:
        level = int((decision or {}).get("take_profit_level") or proposal.get("take_profit_level") or 0)
    except (AttributeError, TypeError, ValueError):
        return None
    return level if level in {1, 2, 3} else None


def bind_take_profit_intent_in_transaction(
    conn: Any,
    *,
    intent: Mapping[str, Any],
    proposal: Mapping[str, Any],
    now: str | None = None,
) -> dict[str, Any] | None:
    level = _take_profit_level(proposal)
    if level is None:
        return None
    if str(intent.get("side") or "").lower() != "sell":
        raise RuntimeError("take-profit milestone must be linked to a SELL intent")
    lifecycle_id = str(intent.get("position_lifecycle_id") or proposal.get("position_lifecycle_id") or "")
    proposal_id = str(intent.get("proposal_id") or proposal.get("id") or proposal.get("proposal_id") or "")
    symbol = str(intent.get("symbol") or proposal.get("symbol") or "").upper()
    requested = float(intent.get("requested_quantity") or 0.0)
    if not lifecycle_id or not proposal_id or not symbol or not math.isfinite(requested) or requested <= 0:
        raise RuntimeError("take-profit intent requires lifecycle, proposal, symbol, and positive quantity")
    active = conn.execute(
        "SELECT id FROM position_lifecycles WHERE id=? AND symbol=? AND state='active'",
        (lifecycle_id, symbol),
    ).fetchone()
    if active is None:
        raise RuntimeError("take-profit intent is not linked to the active position lifecycle")
    timestamp = now or iso_now()
    row = conn.execute(
        "SELECT * FROM take_profit_milestones WHERE position_lifecycle_id=? AND take_profit_level=?",
        (lifecycle_id, level),
    ).fetchone()
    if row is None:
        milestone_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO take_profit_milestones(
                 id,position_lifecycle_id,symbol,take_profit_level,target_quantity,
                 cumulative_filled_quantity,completed_fraction,status,formula_version,created_at,updated_at)
               VALUES(?,?,?,?,?,0,0,'pending_fill',?,?,?)""",
            (
                milestone_id,
                lifecycle_id,
                symbol,
                level,
                requested,
                TAKE_PROFIT_PROGRESS_FORMULA_VERSION,
                timestamp,
                timestamp,
            ),
        )
    else:
        milestone_id = str(row["id"])
        if str(row["status"]) == "filled":
            raise RuntimeError("take-profit milestone is already fully filled")
        remaining = max(
            0.0,
            float(row["target_quantity"]) - float(row["cumulative_filled_quantity"] or 0.0),
        )
        if requested > remaining + 1e-9:
            raise RuntimeError("take-profit intent exceeds the unfilled milestone quantity")
    action = conn.execute(
        "SELECT * FROM take_profit_milestone_actions WHERE order_intent_id=?",
        (intent["id"],),
    ).fetchone()
    if action is None:
        action_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO take_profit_milestone_actions(
                 id,milestone_id,proposal_id,order_intent_id,requested_quantity,
                 cumulative_filled_quantity,completed_fraction,status,created_at,updated_at)
               VALUES(?,?,?,?,?,0,0,'submitted_zero_fill',?,?)""",
            (action_id, milestone_id, proposal_id, intent["id"], requested, timestamp, timestamp),
        )
    else:
        action_id = str(action["id"])
        if str(action["milestone_id"]) != milestone_id or str(action["proposal_id"]) != proposal_id:
            raise RuntimeError("take-profit intent linkage changed")
    return {"milestone_id": milestone_id, "action_id": action_id, "level": level}


def apply_take_profit_terminal_state_in_transaction(
    conn: Any,
    *,
    order_intent_id: str,
    terminal_state: str,
    now: str,
) -> None:
    """Persist a terminal order outcome without advancing milestone quantity."""
    action = conn.execute(
        "SELECT id,cumulative_filled_quantity FROM take_profit_milestone_actions WHERE order_intent_id=?",
        (order_intent_id,),
    ).fetchone()
    if action is None:
        return
    filled = float(action["cumulative_filled_quantity"] or 0.0)
    action_status = (
        f"partially_filled_{terminal_state}" if filled > 0
        else f"{terminal_state}_zero_fill"
    )
    conn.execute(
        "UPDATE take_profit_milestone_actions SET status=?,updated_at=? WHERE id=?",
        (action_status, now, action["id"]),
    )


def apply_take_profit_fill_in_transaction(
    conn: Any,
    *,
    intent: Mapping[str, Any],
    fill_event_id: str,
    broker_event_key: str,
    cumulative_quantity: float,
    delta_quantity: float,
    fill_price: float,
    occurred_at: str,
    now: str,
) -> None:
    if str(intent.get("side") or "").lower() != "sell" or delta_quantity <= 0:
        return
    action = conn.execute(
        """SELECT a.*,m.position_lifecycle_id,m.symbol,m.take_profit_level,m.target_quantity,
                  m.cumulative_filled_quantity AS milestone_filled,m.status AS milestone_status
           FROM take_profit_milestone_actions a
           JOIN take_profit_milestones m ON m.id=a.milestone_id
           WHERE a.order_intent_id=?""",
        (intent["id"],),
    ).fetchone()
    if action is None:
        return
    inserted = conn.execute(
        """INSERT OR IGNORE INTO take_profit_milestone_fill_links(
             id,milestone_id,action_id,broker_fill_event_id,broker_event_key,delta_quantity,
             cumulative_intent_quantity,fill_price,occurred_at,applied_at,payload)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            str(uuid.uuid4()), action["milestone_id"], action["id"], fill_event_id,
            broker_event_key, delta_quantity, cumulative_quantity, fill_price,
            occurred_at, now, json_dumps({"fill_authoritative": True}),
        ),
    )
    if inserted.rowcount != 1:
        return
    action_filled = min(float(action["requested_quantity"]), float(action["cumulative_filled_quantity"] or 0) + delta_quantity)
    action_fraction = min(1.0, action_filled / float(action["requested_quantity"]))
    conn.execute(
        """UPDATE take_profit_milestone_actions SET cumulative_filled_quantity=?,completed_fraction=?,
             status=?,updated_at=? WHERE id=?""",
        (
            action_filled,
            action_fraction,
            "filled" if action_fraction >= 1.0 - 1e-12 else "partially_filled",
            now,
            action["id"],
        ),
    )
    milestone_filled = min(float(action["target_quantity"]), float(action["milestone_filled"] or 0) + delta_quantity)
    milestone_fraction = min(1.0, milestone_filled / float(action["target_quantity"]))
    milestone_status = "filled" if milestone_fraction >= 1.0 - 1e-12 else "partially_filled"
    conn.execute(
        """UPDATE take_profit_milestones SET cumulative_filled_quantity=?,completed_fraction=?,
             status=?,updated_at=?,completed_at=CASE WHEN ?='filled' THEN COALESCE(completed_at,?) ELSE completed_at END
           WHERE id=?""",
        (milestone_filled, milestone_fraction, milestone_status, now, milestone_status, now, action["milestone_id"]),
    )
    level = int(action["take_profit_level"])
    conn.execute(
        f"""UPDATE position_management_state SET
              take_profit_level_{level}_hit=CASE WHEN ?='filled' THEN 1 ELSE take_profit_level_{level}_hit END,
              updated_at=?
            WHERE symbol=? AND position_lifecycle_id=?""",
        (milestone_status, now, action["symbol"], action["position_lifecycle_id"]),
    )


def remaining_take_profit_quantity(
    storage: Any,
    *,
    position_lifecycle_id: str,
    take_profit_level: int,
) -> float | None:
    rows = storage.fetch_all(
        """SELECT target_quantity,cumulative_filled_quantity FROM take_profit_milestones
           WHERE position_lifecycle_id=? AND take_profit_level=?""",
        (position_lifecycle_id, take_profit_level),
    )
    if not rows:
        return None
    return max(0.0, float(rows[0]["target_quantity"]) - float(rows[0]["cumulative_filled_quantity"] or 0.0))


__all__ = [
    "TAKE_PROFIT_PROGRESS_FORMULA_VERSION",
    "apply_profit_milestone_schema",
    "apply_take_profit_fill_in_transaction",
    "apply_take_profit_terminal_state_in_transaction",
    "bind_take_profit_intent_in_transaction",
    "remaining_take_profit_quantity",
]
