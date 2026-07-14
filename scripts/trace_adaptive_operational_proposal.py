#!/usr/bin/env python3
"""Read-only trace of the first natural operational-paper BUY or ADD."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any


TRADING_TABLES = ("trade_proposals", "approvals", "risk_reservations", "order_intents", "orders", "fills")


def _json(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def _one(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
    row = conn.execute(sql, params).fetchone()
    return dict(row) if row else None


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {name: int(conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]) for name in TRADING_TABLES}


def build_trace(database: str | Path, *, after: str | None = None) -> dict[str, Any]:
    path = Path(database).expanduser().resolve()
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        before = _counts(conn)
        params: tuple[Any, ...] = (after,) if after else ()
        time_clause = "AND p.created_at>=?" if after else ""
        sizing = _one(
            conn,
            f"""SELECT s.*,p.symbol,p.side,p.status proposal_status,p.created_at proposal_created_at,
                       p.expires_at,p.telegram_message_id,p.payload proposal_payload
                FROM adaptive_sizing_operational_decisions s
                JOIN trade_proposals p ON p.id=s.proposal_id
                WHERE s.stage='proposal' AND lower(p.side)='buy'
                  AND COALESCE(json_extract(p.payload,'$.action'),'entry') IN ('entry','add')
                  AND s.operating_mode='operational_paper' AND s.operational_enforced=1
                  {time_clause}
                ORDER BY p.created_at,s.created_at LIMIT 1""",
            params,
        )
        if sizing is None:
            after_counts = _counts(conn)
            return {
                "status": "pending_first_natural_operational_buy_or_add",
                "after": after,
                "trading_state_counts_before": before,
                "trading_state_counts_after": after_counts,
                "trading_state_mutations": sum(abs(after_counts[k] - before[k]) for k in before),
            }

        proposal_id = str(sizing["proposal_id"])
        payload = _json(sizing.pop("proposal_payload"))
        conviction = _one(
            conn,
            """SELECT * FROM adaptive_conviction_operational_decisions
               WHERE proposal_id=? AND decision_stage='proposal' ORDER BY created_at LIMIT 1""",
            (proposal_id,),
        )
        final_sizing = _one(
            conn,
            """SELECT * FROM adaptive_sizing_operational_decisions
               WHERE proposal_id=? AND stage='final_revalidation' ORDER BY created_at DESC LIMIT 1""",
            (proposal_id,),
        )
        final_conviction = _one(
            conn,
            """SELECT * FROM adaptive_conviction_operational_decisions
               WHERE proposal_id=? AND decision_stage='final_revalidation' ORDER BY created_at DESC LIMIT 1""",
            (proposal_id,),
        )
        approval = _one(conn, "SELECT * FROM approvals WHERE proposal_id=? ORDER BY created_at DESC LIMIT 1", (proposal_id,))
        intent = _one(conn, "SELECT * FROM order_intents WHERE proposal_id=? ORDER BY created_at DESC LIMIT 1", (proposal_id,))
        reservation = _one(conn, "SELECT * FROM risk_reservations WHERE intent_id=? LIMIT 1", (intent["id"],)) if intent else None
        order = _one(conn, "SELECT * FROM orders WHERE proposal_id=? ORDER BY created_at DESC LIMIT 1", (proposal_id,))
        fill = _one(conn, "SELECT * FROM fills WHERE order_id=? ORDER BY filled_at DESC LIMIT 1", (order["id"],)) if order else None

        displayed_quantity = float(payload.get("approved_quantity_ceiling") or payload.get("qty") or sizing["adaptive_quantity"] or 0.0)
        displayed_notional = float(payload.get("approved_notional_ceiling") or sizing["displayed_adaptive_ceiling"] or 0.0)
        displayed_stop_risk = float(payload.get("approved_stop_risk_ceiling") or sizing["adaptive_constrained_stop_risk_dollars"] or 0.0)
        submitted_quantity = float((intent or {}).get("requested_quantity") or (order or {}).get("qty") or 0.0)
        submitted_notional = float((intent or {}).get("requested_notional") or (order or {}).get("notional") or 0.0)
        stop_distance = float(payload.get("stop_distance_dollars") or 0.0)
        submitted_stop_risk = submitted_quantity * stop_distance if submitted_quantity and stop_distance else 0.0
        after_counts = _counts(conn)
        return {
            "status": "complete" if intent else "awaiting_manual_approval_or_execution",
            "proposal": {
                "id": proposal_id, "symbol": sizing["symbol"], "side": sizing["side"],
                "action": payload.get("action", "entry"), "status": sizing["proposal_status"],
                "created_at": sizing["proposal_created_at"], "expires_at": sizing["expires_at"],
                "telegram_message_id": sizing["telegram_message_id"],
                "strategy_state": payload.get("strategy_state"), "policy_id": sizing.get("policy_id"),
            },
            "candidate_inputs": _json((conviction or {}).get("raw_inputs_json")),
            "proposal_conviction": conviction,
            "proposal_sizing": sizing,
            "displayed_telegram_ceilings": {
                "quantity": displayed_quantity, "notional": displayed_notional,
                "stop_risk_dollars": displayed_stop_risk,
                "deployment_mode": payload.get("deployment_mode"),
                "opportunity_class": payload.get("opportunity_class"),
                "binding_cap": sizing.get("binding_adaptive_cap"),
            },
            "approval": approval,
            "approval_time_conviction": final_conviction,
            "approval_time_sizing": final_sizing,
            "reservation": reservation,
            "intent": intent,
            "order": order,
            "fill": fill,
            "hard_ceiling_checks": {
                "submitted_quantity": submitted_quantity,
                "submitted_notional": submitted_notional,
                "submitted_stop_risk_dollars": submitted_stop_risk,
                "submitted_quantity_lte_displayed": submitted_quantity <= displayed_quantity + 1e-9,
                "submitted_notional_lte_displayed": submitted_notional <= displayed_notional + 1e-9,
                "submitted_stop_risk_lte_displayed": submitted_stop_risk <= displayed_stop_risk + 1e-9,
            },
            "trading_state_counts_before": before,
            "trading_state_counts_after": after_counts,
            "trading_state_mutations": sum(abs(after_counts[k] - before[k]) for k in before),
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database")
    parser.add_argument("--after", help="only consider proposals created at or after this ISO timestamp")
    args = parser.parse_args()
    try:
        result = build_trace(args.database, after=args.after)
    except (OSError, sqlite3.Error, ValueError):
        print("Operational proposal trace unavailable: the read-only evidence set is incomplete or incompatible.")
        return 2
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
