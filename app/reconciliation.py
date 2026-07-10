from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .execution import DurableExecutionStore
from .order_state import BROKER_RELEVANT_STATES, OrderState, broker_status_to_state
from .position_lifecycle import PositionLifecycleManager
from .utils import iso_now, json_dumps


def _value(obj: Any, name: str, default: Any = None) -> Any:
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


def _status(value: Any) -> str:
    if isinstance(value, Enum):
        value = value.value
    return str(value or "").lower()


def _is_not_found(exc: BaseException) -> bool:
    original = getattr(exc, "original", None)
    status_code = (
        getattr(exc, "status_code", None)
        or getattr(exc, "code", None)
        or getattr(original, "status_code", None)
        or getattr(original, "code", None)
    )
    return status_code == 404 or "not found" in str(exc).lower()


@dataclass(frozen=True)
class ReconciliationResult:
    checked: int = 0
    updated: int = 0
    fills_upserted: int = 0
    unknown: int = 0
    found: int = 0
    not_yet_found: int = 0
    confirmed_absent: int = 0
    local_only_terminal: int = 0
    divergence: int = 0
    manual_review_required: int = 0


class BrokerReconciler:
    """Reconcile broker-relevant local state only; this class never submits orders."""

    CONFIRMED_ABSENCE_OBSERVATIONS = 3

    def __init__(self, broker: Any, storage: Any, run_id: str, telegram: Any | None = None) -> None:
        self.broker = broker
        self.storage = storage
        self.run_id = run_id
        self.telegram = telegram
        self.intent_store = DurableExecutionStore(storage)

    def reconcile(self) -> ReconciliationResult:
        counts = {
            "checked": 0,
            "updated": 0,
            "fills_upserted": 0,
            "unknown": 0,
            "found": 0,
            "not_yet_found": 0,
            "confirmed_absent": 0,
            "local_only_terminal": 0,
            "divergence": 0,
            "manual_review_required": 0,
        }
        state_values = tuple(state.value for state in BROKER_RELEVANT_STATES)
        placeholders = ",".join("?" for _ in state_values)
        intents = self.storage.fetch_all(
            f"SELECT * FROM order_intents WHERE state IN ({placeholders}) ORDER BY created_at",
            state_values,
        )
        for intent in intents:
            counts["checked"] += 1
            outcome = self._reconcile_intent(intent)
            for key, value in outcome.items():
                counts[key] += value

        # Backward-compatible migration window: reconcile old broker-relevant order
        # rows, but never query local blocked/rejected/expired records.
        legacy = self.storage.fetch_all(
            """SELECT o.* FROM orders o LEFT JOIN order_intents i ON i.id=o.id
               WHERE i.id IS NULL
                 AND (o.broker_order_id IS NOT NULL OR o.client_order_id IS NOT NULL)
                 AND lower(COALESCE(o.status,'')) IN ('pending_new','new','accepted','submitted','partially_filled','unknown','reconciliation_required','pending_cancel')
               ORDER BY o.created_at"""
        )
        for local in legacy:
            counts["checked"] += 1
            outcome = self._reconcile_legacy_order(local)
            for key, value in outcome.items():
                counts[key] += value

        self._retry_pending_fill_notifications()
        self._snapshot_account_and_positions()
        result = ReconciliationResult(**counts)
        self.storage.audit(
            self.run_id,
            "broker_reconciliation_complete",
            {**counts, "orders_submitted": 0},
        )
        self._heartbeat("healthy" if counts["unknown"] == 0 else "degraded", counts)
        return result

    def _retry_pending_fill_notifications(self) -> None:
        if self.telegram is None:
            return
        rows = self.storage.fetch_all(
            """SELECT o.*,f.qty AS retry_fill_qty,f.price AS retry_fill_price
               FROM fills f JOIN orders o ON o.id=f.order_id
               WHERE f.fill_notification_status='pending' AND f.fill_notified_at IS NULL"""
        )
        for row in rows:
            self._maybe_notify_fill(
                row,
                "filled" if row.get("status") == "filled" else "partially_filled",
                float(row.get("retry_fill_qty") or 0),
                float(row.get("retry_fill_price") or 0),
            )

    def _lookup(self, row: dict[str, Any]) -> tuple[Any, str, str]:
        if row.get("broker_order_id"):
            return self.broker.get_order(row["broker_order_id"]), "broker_order_id", "present"
        if row.get("client_order_id"):
            return self.broker.get_order_by_client_order_id(row["client_order_id"]), "client_order_id", "present"
        raise LookupError("record has no broker identifier")

    def _record_attempt(
        self,
        intent_id: str,
        lookup_type: str,
        outcome: str,
        broker_status: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.storage.execute(
            """INSERT INTO reconciliation_attempts(
                   id,intent_id,lookup_type,lookup_value_redacted,outcome,broker_status,safe_detail,created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (str(uuid.uuid4()), intent_id, lookup_type, "[REDACTED_IDENTIFIER]", outcome, broker_status, json_dumps(detail or {}), iso_now()),
        )

    def _reconcile_intent(self, intent: dict[str, Any]) -> dict[str, int]:
        outcome: dict[str, int] = {}
        lookup_type = "broker_order_id" if intent.get("broker_order_id") else "client_order_id"
        try:
            remote, lookup_type, _ = self._lookup(intent)
        except Exception as exc:
            if _is_not_found(exc):
                not_found_count = int(intent.get("not_found_count") or 0) + 1
                self.storage.execute(
                    "UPDATE order_intents SET not_found_count=?,last_reconciliation_at=?,updated_at=? WHERE id=?",
                    (not_found_count, iso_now(), iso_now(), intent["id"]),
                )
                if not_found_count >= self.CONFIRMED_ABSENCE_OBSERVATIONS:
                    self.intent_store.transition(
                        intent["id"],
                        OrderState.REJECTED,
                        event_type="broker_non_submission_confirmed",
                        safe_summary="broker order confirmed absent after bounded reconciliation",
                    )
                    self._record_attempt(intent["id"], lookup_type, "confirmed_absent")
                    outcome["confirmed_absent"] = 1
                    outcome["updated"] = 1
                else:
                    if intent["state"] != OrderState.UNKNOWN.value:
                        self.intent_store.transition(intent["id"], OrderState.RECONCILIATION_REQUIRED, event_type="broker_order_not_yet_found")
                    self._record_attempt(intent["id"], lookup_type, "not_yet_found", detail={"observation": not_found_count})
                    outcome["not_yet_found"] = 1
                    outcome["unknown"] = 1
                return outcome
            self.storage.execute(
                "UPDATE order_intents SET last_reconciliation_at=?,updated_at=? WHERE id=?",
                (iso_now(), iso_now(), intent["id"]),
            )
            self._record_attempt(intent["id"], lookup_type, "lookup_failed", detail={"error_type": type(exc).__name__})
            self.storage.audit(self.run_id, "order_reconciliation_unknown", {"intent_id": intent["id"], "error_type": type(exc).__name__, "resubmitted": False})
            outcome["unknown"] = 1
            outcome["manual_review_required"] = 1
            return outcome

        remote_status = _status(_value(remote, "status"))
        filled_qty = float(_value(remote, "filled_qty", 0) or 0)
        target = broker_status_to_state(remote_status, filled_qty)
        broker_order_id = str(_value(remote, "id", "") or intent.get("broker_order_id") or "") or None
        self.storage.execute(
            "UPDATE order_intents SET broker_order_id=COALESCE(?,broker_order_id),not_found_count=0,last_reconciliation_at=?,updated_at=? WHERE id=?",
            (broker_order_id, iso_now(), iso_now(), intent["id"]),
        )
        self._record_attempt(intent["id"], lookup_type, "found", remote_status)
        outcome["found"] = 1
        if target is None:
            outcome["divergence"] = 1
            outcome["manual_review_required"] = 1
            return outcome

        fill_price = _value(remote, "filled_avg_price")
        if filled_qty > 0 and fill_price is not None:
            event_key = str(_value(remote, "execution_id", "") or f"{broker_order_id or intent['client_order_id']}:{filled_qty:.12g}:{float(fill_price):.12g}")
            before = float(intent.get("filled_quantity") or 0)
            updated = self.intent_store.record_fill(
                intent["id"],
                cumulative_quantity=filled_qty,
                fill_price=float(fill_price),
                broker_event_key=event_key,
                broker_order_id=broker_order_id,
                occurred_at=str(_value(remote, "filled_at", iso_now())),
                price_is_cumulative_average=True,
            )
            if float(updated.get("filled_quantity") or 0) > before:
                outcome["fills_upserted"] = 1
            self.storage.link_executed_order_records(intent["id"])
            self.storage.upsert_actual_trade_outcome_for_order(intent["id"])
            self._maybe_notify_fill(intent, updated["state"], float(updated["filled_quantity"]), float(updated.get("average_fill_price") or fill_price))
            target = OrderState(updated["state"])
        elif OrderState(intent["state"]) != target:
            # Broker REST snapshots and stream events may arrive out of order. A
            # later observation of "submitted" must never regress a local partial
            # fill, terminal state, or cancellation-in-progress. Persist the
            # observation for audit and leave the monotonic local state intact.
            current = OrderState(intent["state"])
            if current in {OrderState.PARTIALLY_FILLED, OrderState.CANCEL_PENDING} and target == OrderState.SUBMITTED:
                self._record_attempt(
                    intent["id"],
                    lookup_type,
                    "stale_status_ignored",
                    remote_status,
                    {"retained_state": current.value},
                )
            else:
                self.intent_store.transition(intent["id"], target, event_type="broker_status_reconciled", broker_order_id=broker_order_id)

        self._update_proposal_projection(intent, target)
        outcome["updated"] = 1
        return outcome

    def _reconcile_legacy_order(self, local: dict[str, Any]) -> dict[str, int]:
        outcome: dict[str, int] = {}
        try:
            remote, _, _ = self._lookup(local)
        except Exception as exc:
            self.storage.audit(self.run_id, "order_reconciliation_unknown", {"local_order_id": local["id"], "error_type": type(exc).__name__, "resubmitted": False})
            outcome["unknown"] = 1
            return outcome
        remote_status = _status(_value(remote, "status"))
        if not remote_status:
            outcome["unknown"] = 1
            return outcome
        broker_order_id = str(_value(remote, "id", "") or local.get("broker_order_id") or "") or None
        self.storage.execute(
            "UPDATE orders SET broker_order_id=?,status=?,payload=?,updated_at=? WHERE id=?",
            (broker_order_id, remote_status, json_dumps({"source": "broker_reconciliation", "status": remote_status}), iso_now(), local["id"]),
        )
        filled_qty = _value(remote, "filled_qty")
        filled_price = _value(remote, "filled_avg_price")
        if remote_status in {"filled", "partially_filled"} and filled_qty is not None and float(filled_qty) > 0 and filled_price is not None:
            existing = self.storage.fetch_all("SELECT * FROM fills WHERE order_id=?", (local["id"],))
            prior_qty = float(existing[0].get("qty") or 0) if existing else 0
            new_qty = max(prior_qty, float(filled_qty))
            self.storage.execute(
                """INSERT INTO fills(run_id,order_id,qty,price,filled_at,payload,fill_notification_status)
                   VALUES(?,?,?,?,?,?,?) ON CONFLICT(order_id) DO UPDATE SET
                   run_id=excluded.run_id,qty=MAX(fills.qty,excluded.qty),price=CASE WHEN excluded.qty>=fills.qty THEN excluded.price ELSE fills.price END,
                   filled_at=CASE WHEN excluded.qty>=fills.qty THEN excluded.filled_at ELSE fills.filled_at END,payload=excluded.payload""",
                (self.run_id, local["id"], new_qty, float(filled_price), str(_value(remote, "filled_at", iso_now())), json_dumps({"source": "legacy_broker_reconciliation", "aggregate": True}), "pending"),
            )
            if new_qty > prior_qty:
                outcome["fills_upserted"] = 1
            self.storage.link_executed_order_records(local["id"])
            self.storage.upsert_actual_trade_outcome_for_order(local["id"])
            self._maybe_notify_fill(local, remote_status, new_qty, float(filled_price))
            self._update_proposal_projection(local, OrderState.FILLED if remote_status == "filled" else OrderState.PARTIALLY_FILLED)
        outcome["found"] = 1
        outcome["updated"] = 1
        return outcome

    def _update_proposal_projection(self, local: dict[str, Any], state: OrderState) -> None:
        proposal_id = local.get("proposal_id")
        if not proposal_id:
            return
        if state == OrderState.FILLED:
            self.storage.execute("UPDATE proposal_batch_candidates SET candidate_status='filled' WHERE proposal_id=? AND candidate_status='submitted'", (proposal_id,))
            self.storage.execute("UPDATE trade_proposals SET status='filled' WHERE id=? AND status IN ('approved','submitted','unknown')", (proposal_id,))
        elif state == OrderState.PARTIALLY_FILLED:
            self.storage.execute("UPDATE trade_proposals SET status='submitted' WHERE id=? AND status IN ('approved','unknown')", (proposal_id,))

    def _maybe_notify_fill(self, local_order: dict[str, Any], remote_status: str, filled_qty: float, filled_price: float) -> None:
        fill_rows = self.storage.fetch_all("SELECT id,fill_notified_at,fill_notification_status FROM fills WHERE order_id=?", (local_order["id"],))
        if not fill_rows or self.telegram is None:
            return
        fill_row = fill_rows[0]
        if fill_row.get("fill_notified_at") or str(fill_row.get("fill_notification_status") or "") not in {"", "pending", "None"}:
            return
        action = "Buy" if str(local_order.get("side", "")).lower() == "buy" else "Sell"
        prefix = "filled" if remote_status == "filled" else "partially filled"
        message = f"✅ Paper order {prefix}: {action} {local_order.get('symbol')}\nQty: {filled_qty}\nAvg fill: ${filled_price:.3f}\nMode: paper only."
        try:
            self.telegram.send_message(message)
        except Exception as exc:
            self.storage.execute("UPDATE fills SET fill_notification_status='pending',fill_notification_error=? WHERE id=?", (type(exc).__name__, fill_row["id"]))
            return
        self.storage.execute("UPDATE fills SET fill_notified_at=?,fill_notification_status='sent',fill_notification_error=NULL WHERE id=?", (iso_now(), fill_row["id"]))

    def _snapshot_account_and_positions(self) -> None:
        try:
            account = self.broker.get_account()
            positions = list(self.broker.get_positions())
        except Exception as exc:
            self.storage.audit(self.run_id, "account_reconciliation_unknown", {"error_type": type(exc).__name__})
            return
        PositionLifecycleManager(self.storage).reconcile(positions)
        unrealized_values = [_value(position, "unrealized_pl") for position in positions]
        unrealized = sum(float(value) for value in unrealized_values if value is not None) if all(value is not None for value in unrealized_values) else None
        self.storage.execute(
            "INSERT INTO cash_snapshots(run_id,equity,cash,settled_cash,realized_pl,unrealized_pl,created_at) VALUES(?,?,?,?,?,?,?)",
            (self.run_id, _float_or_none(_value(account, "equity")), _float_or_none(_value(account, "cash")), _float_or_none(_value(account, "cash")), None, unrealized, iso_now()),
        )
        for position in positions:
            self.storage.execute(
                "INSERT INTO positions(run_id,symbol,qty,market_value,unrealized_pl,payload,created_at) VALUES(?,?,?,?,?,?,?)",
                (self.run_id, str(_value(position, "symbol", "")), _float_or_none(_value(position, "qty")), _float_or_none(_value(position, "market_value")), _float_or_none(_value(position, "unrealized_pl")), json_dumps({"source": "broker_reconciliation"}), iso_now()),
            )

    def _heartbeat(self, state: str, detail: dict[str, Any]) -> None:
        now = iso_now()
        self.storage.execute(
            """INSERT INTO health_heartbeats(component,state,attempted_at,completed_at,successful_at,detail,updated_at)
               VALUES('reconciliation',?,?,?,?,?,?) ON CONFLICT(component) DO UPDATE SET
               state=excluded.state,attempted_at=excluded.attempted_at,completed_at=excluded.completed_at,
               successful_at=excluded.successful_at,detail=excluded.detail,updated_at=excluded.updated_at""",
            (state, now, now, now if state == "healthy" else None, json_dumps(detail), now),
        )


def _float_or_none(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None
