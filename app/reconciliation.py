from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from .utils import iso_now, json_dumps


def _value(obj: Any, name: str, default: Any = None) -> Any:
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


def _status(value: Any) -> str:
    if isinstance(value, Enum):
        value = value.value
    return str(value or "").lower()


@dataclass(frozen=True)
class ReconciliationResult:
    checked: int = 0
    updated: int = 0
    fills_upserted: int = 0
    unknown: int = 0


class BrokerReconciler:
    """Read broker state and reconcile local records; never submits orders."""

    def __init__(self, broker: Any, storage: Any, run_id: str) -> None:
        self.broker = broker
        self.storage = storage
        self.run_id = run_id

    def reconcile(self) -> ReconciliationResult:
        checked = updated = fills_upserted = unknown = 0
        local_orders = self.storage.fetch_all(
            "SELECT * FROM orders WHERE broker_order_id IS NOT NULL OR client_order_id IS NOT NULL ORDER BY created_at"
        )
        for local in local_orders:
            checked += 1
            try:
                if local.get("broker_order_id"):
                    remote = self.broker.get_order(local["broker_order_id"])
                elif local.get("client_order_id"):
                    remote = self.broker.get_order_by_client_order_id(local["client_order_id"])
                else:
                    raise LookupError("order has no broker identifiers")
            except Exception as exc:
                unknown += 1
                self.storage.audit(
                    self.run_id,
                    "order_reconciliation_unknown",
                    {"local_order_id": local["id"], "error_type": type(exc).__name__, "resubmitted": False},
                )
                continue

            remote_status = _status(_value(remote, "status"))
            if not remote_status:
                unknown += 1
                self.storage.audit(
                    self.run_id,
                    "order_reconciliation_unknown",
                    {"local_order_id": local["id"], "reason": "missing broker status", "resubmitted": False},
                )
                continue

            broker_order_id = str(_value(remote, "id", "") or local.get("broker_order_id") or "") or None
            safe_payload = {
                "source": "broker_reconciliation",
                "status": remote_status,
                "filled_qty": _value(remote, "filled_qty"),
                "filled_avg_price": _value(remote, "filled_avg_price"),
            }
            self.storage.execute(
                "UPDATE orders SET broker_order_id=?, status=?, payload=?, updated_at=? WHERE id=?",
                (broker_order_id, remote_status, json_dumps(safe_payload), iso_now(), local["id"]),
            )
            updated += 1

            filled_qty = _value(remote, "filled_qty")
            filled_price = _value(remote, "filled_avg_price")
            filled_at = _value(remote, "filled_at")
            if remote_status in {"filled", "partially_filled"} and filled_qty is not None and float(filled_qty) > 0 and filled_price is not None:
                self.storage.execute(
                    """INSERT INTO fills(run_id,order_id,qty,price,filled_at,payload)
                       VALUES(?,?,?,?,?,?)
                       ON CONFLICT(order_id) DO UPDATE SET
                         run_id=excluded.run_id, qty=excluded.qty, price=excluded.price,
                         filled_at=excluded.filled_at, payload=excluded.payload""",
                    (
                        self.run_id,
                        local["id"],
                        float(filled_qty),
                        float(filled_price),
                        str(filled_at or iso_now()),
                        json_dumps({"source": "broker_reconciliation", "aggregate": True}),
                    ),
                )
                fills_upserted += 1
                self.storage.link_executed_order_records(local["id"])
                self.storage.upsert_actual_trade_outcome_for_order(local["id"])
                if remote_status == "filled":
                    self.storage.execute(
                        "UPDATE proposal_batch_candidates SET candidate_status='filled' WHERE proposal_id=? AND candidate_status='submitted'",
                        (local.get("proposal_id"),),
                    )

        self._snapshot_account_and_positions()
        result = ReconciliationResult(checked, updated, fills_upserted, unknown)
        self.storage.audit(
            self.run_id,
            "broker_reconciliation_complete",
            {
                "checked": checked,
                "updated": updated,
                "fills_upserted": fills_upserted,
                "unknown": unknown,
                "orders_submitted": 0,
            },
        )
        return result

    def _snapshot_account_and_positions(self) -> None:
        try:
            account = self.broker.get_account()
            positions = list(self.broker.get_positions())
        except Exception as exc:
            self.storage.audit(
                self.run_id,
                "account_reconciliation_unknown",
                {"error_type": type(exc).__name__},
            )
            return

        unrealized = sum(float(_value(position, "unrealized_pl", 0) or 0) for position in positions)
        self.storage.execute(
            "INSERT INTO cash_snapshots(run_id,equity,cash,settled_cash,realized_pl,unrealized_pl,created_at) VALUES(?,?,?,?,?,?,?)",
            (
                self.run_id,
                float(_value(account, "equity", 0) or 0),
                float(_value(account, "cash", 0) or 0),
                float(_value(account, "cash", 0) or 0),
                None,
                unrealized,
                iso_now(),
            ),
        )
        for position in positions:
            self.storage.execute(
                "INSERT INTO positions(run_id,symbol,qty,market_value,unrealized_pl,payload,created_at) VALUES(?,?,?,?,?,?,?)",
                (
                    self.run_id,
                    str(_value(position, "symbol", "")),
                    float(_value(position, "qty", 0) or 0),
                    float(_value(position, "market_value", 0) or 0),
                    float(_value(position, "unrealized_pl", 0) or 0),
                    json_dumps({"source": "broker_reconciliation"}),
                    iso_now(),
                ),
            )
