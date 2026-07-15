#!/usr/bin/env python3
"""Clear one proven-stale local blocker on a non-production database copy.

This command never calls a broker and never creates a proposal, approval,
intent, order, or fill. Dry-run is the default; --apply requires the exact
source identity printed by the investigation.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.runtime_guard import is_production_path
from app.storage import Storage
from app.utils import iso_now, json_dumps


def inspect(storage: Storage, symbol: str, expected_source_id: str) -> dict:
    blockers = storage.fetch_all(
        "SELECT * FROM exit_blocker_states WHERE symbol=? AND active=1", (symbol.upper(),)
    )
    if len(blockers) > 1:
        raise RuntimeError("multiple active blockers violate the durable invariant")
    if not blockers:
        return {"eligible": True, "already_clear": True, "reason": "no active blocker"}
    blocker = blockers[0]
    if str(blocker["source_id"]) != expected_source_id:
        raise RuntimeError("active blocker source identity does not match --expected-source-id")
    proposal_id = blocker.get("proposal_id") or (
        blocker["source_id"] if "proposal" in str(blocker["source_type"]) else None
    )
    if not proposal_id:
        return {"eligible": False, "already_clear": False, "reason": "blocker is not proposal-backed"}
    proposals = storage.fetch_all("SELECT id,status,expires_at FROM trade_proposals WHERE id=?", (proposal_id,))
    if len(proposals) != 1 or str(proposals[0]["status"]) not in {"blocked", "expired", "rejected", "superseded", "stale_resolved"}:
        return {"eligible": False, "already_clear": False, "reason": "proposal is not proven terminal"}
    ambiguous = storage.fetch_all(
        """SELECT id,state FROM order_intents WHERE proposal_id=? AND state IN (
             'created','reserved','submitting','submitted','partially_filled','cancel_pending','unknown','reconciliation_required')""",
        (proposal_id,),
    )
    orders = storage.fetch_all(
        "SELECT id,status FROM orders WHERE proposal_id=? AND status NOT IN ('filled','cancelled','canceled','expired','rejected')",
        (proposal_id,),
    )
    if ambiguous or orders:
        return {"eligible": False, "already_clear": False, "reason": "broker-relevant state is unresolved"}
    return {
        "eligible": True,
        "already_clear": False,
        "reason": "terminal proposal has no broker-relevant intent or order",
        "blocker_id": blocker["id"],
        "proposal_id": proposal_id,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--expected-source-id", required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True)
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    path = args.db.expanduser().resolve()
    if is_production_path(path):
        raise SystemExit("refusing to repair a production-paper database; use a verified copy")
    storage = Storage(path)
    result = inspect(storage, args.symbol, args.expected_source_id)
    if args.apply and result.get("eligible") and not result.get("already_clear"):
        now = iso_now()
        with storage.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """UPDATE exit_blocker_states SET active=0,state='cleared',cleared_at=?,terminal_at=?,
                       updated_at=?,trigger_reason='operator repair: proven terminal local blocker'
                   WHERE id=? AND active=1 AND source_id=?""",
                (now, now, now, result["blocker_id"], args.expected_source_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("repair precondition changed before commit")
            conn.execute(
                "INSERT INTO audit_events(run_id,event_type,actor,detail,created_at) VALUES(NULL,?,?,?,?)",
                ("exit_blocker_repair_applied", "operator_command", json_dumps(result), now),
            )
        result["applied"] = True
    else:
        result["applied"] = False
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("eligible") else 2


if __name__ == "__main__":
    raise SystemExit(main())
