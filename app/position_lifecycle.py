from __future__ import annotations

import uuid
from decimal import Decimal, InvalidOperation
from typing import Any

from .fixed_point_accounting import (
    EXACT_DECIMAL_PROVENANCE,
    FIXED_POINT_ACCOUNTING_VERSION,
    decimal_text,
    legacy_float,
    require_exact_decimal,
)
from .utils import iso_now, json_dumps


ZERO = Decimal("0")


def _value(obj: Any, name: str, default: Any = None) -> Any:
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


class PositionLifecycleManager:
    """Bind management state to one continuous non-zero broker holding."""

    def __init__(self, storage: Any) -> None:
        self.storage = storage

    def reconcile(self, positions: list[Any], source: str = "broker_reconciliation") -> dict[str, str]:
        now = iso_now()
        current: dict[str, dict[str, Any]] = {}
        for position in positions:
            symbol = str(_value(position, "symbol", "")).upper()
            quantity = _decimal_or_none(_value(position, "qty", 0)) or ZERO
            if symbol and abs(quantity) > ZERO:
                current[symbol] = {
                    "quantity": quantity,
                    "side": "long" if quantity > ZERO else "short",
                    "broker_position_id": str(_value(position, "asset_id", "") or _value(position, "id", "") or "") or None,
                    "average_entry_price": _decimal_or_none(_value(position, "avg_entry_price")),
                }

        def refresh_opening_quantity(conn: Any, lifecycle: Any) -> None:
            """Grow opening quantity from the original entry intent only.

            Broker position quantity can temporarily represent only a partial
            fill. The lifecycle's opening quantity is therefore sourced from
            the cumulative fill ledger of its first lifecycle-bound ENTRY
            intent, never from later ADD intents or from a reduced position.
            """
            entry = None
            for candidate in conn.execute(
                """SELECT *
                   FROM order_intents
                   WHERE position_lifecycle_id=? AND symbol=? AND lower(side)='buy'
                     AND lower(intended_action)='entry'
                   ORDER BY created_at,id""",
                (lifecycle["id"], lifecycle["symbol"]),
            ).fetchall():
                try:
                    if (require_exact_decimal(dict(candidate), "filled_quantity_decimal", minimum=ZERO) or ZERO) > ZERO:
                        entry = candidate
                        break
                except (TypeError, ValueError) as exc:
                    raise RuntimeError(
                        "position lifecycle entry intent lacks exact fill evidence"
                    ) from exc
            if not entry:
                return
            try:
                cumulative_filled = require_exact_decimal(
                    dict(entry), "filled_quantity_decimal", minimum=ZERO
                ) or ZERO
                prior_opening = require_exact_decimal(
                    dict(lifecycle), "opening_quantity_decimal", minimum=ZERO
                ) or ZERO
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    "position lifecycle opening quantity lacks exact decimal evidence"
                ) from exc
            if int(lifecycle["opening_quantity_frozen"] or 0) == 1:
                return
            entry_terminal = str(entry["state"] or "").lower() in {
                "filled", "cancelled", "rejected", "expired",
            }
            if entry_terminal:
                conn.execute(
                    """UPDATE position_lifecycles
                       SET opening_quantity=?,opening_quantity_decimal=?,opening_quantity_frozen=1,
                           decimal_provenance=?,decimal_accounting_version=?,updated_at=?
                       WHERE id=?""",
                    (
                        legacy_float(max(prior_opening, cumulative_filled)),
                        decimal_text(max(prior_opening, cumulative_filled)),
                        EXACT_DECIMAL_PROVENANCE, FIXED_POINT_ACCOUNTING_VERSION,
                        now, lifecycle["id"],
                    ),
                )
            elif cumulative_filled > prior_opening:
                conn.execute(
                    """UPDATE position_lifecycles
                       SET opening_quantity=?,opening_quantity_decimal=?,decimal_provenance=?,
                           decimal_accounting_version=?,updated_at=? WHERE id=?""",
                    (
                        legacy_float(cumulative_filled), decimal_text(cumulative_filled),
                        EXACT_DECIMAL_PROVENANCE, FIXED_POINT_ACCOUNTING_VERSION,
                        now, lifecycle["id"],
                    ),
                )

        def bind_filled_entry_intents(
            conn: Any,
            *,
            lifecycle_id: str,
            symbol: str,
            boundary: str | None,
        ) -> None:
            """Bind filled entry intents without SQLite REAL comparison.

            ``filled_quantity`` remains a compatibility projection for older
            readers.  Lifecycle identity is an accounting boundary, so the
            positive-fill test must happen after loading the canonical
            decimal projection in Python rather than in SQLite's REAL
            arithmetic/comparison engine.
            """
            candidates = conn.execute(
                """SELECT id,filled_quantity,filled_quantity_decimal,created_at
                   FROM order_intents
                   WHERE symbol=? AND lower(side)='buy'
                     AND lower(intended_action)='entry'
                     AND position_lifecycle_id IS NULL
                     AND (? IS NULL OR created_at>?)
                   ORDER BY created_at,id""",
                (symbol, boundary, boundary),
            ).fetchall()
            for candidate in candidates:
                try:
                    filled = require_exact_decimal(
                        dict(candidate), "filled_quantity_decimal", minimum=ZERO
                    ) or ZERO
                except (TypeError, ValueError, InvalidOperation) as exc:
                    raise RuntimeError(
                        "position lifecycle entry intent lacks exact fill evidence"
                    ) from exc
                if filled <= ZERO:
                    continue
                conn.execute(
                    "UPDATE order_intents SET position_lifecycle_id=?,updated_at=? WHERE id=? AND position_lifecycle_id IS NULL",
                    (lifecycle_id, now, candidate["id"]),
                )

        with self.storage.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            active_rows = {row["symbol"]: row for row in conn.execute("SELECT * FROM position_lifecycles WHERE state='active'").fetchall()}
            for symbol, lifecycle in active_rows.items():
                observed = current.get(symbol)
                flipped = observed is not None and observed["side"] != lifecycle["side"]
                if observed is None or flipped:
                    pm_state = conn.execute("SELECT * FROM position_management_state WHERE symbol=?", (symbol,)).fetchone()
                    archive = json_dumps(dict(pm_state)) if pm_state else None
                    conn.execute(
                        """UPDATE position_lifecycles SET state='closed',current_quantity=0,closed_at=?,updated_at=?,
                           management_state_archive=?,current_quantity_decimal='0',
                           decimal_provenance=?,decimal_accounting_version=? WHERE id=?""",
                        (now, now, archive, EXACT_DECIMAL_PROVENANCE, FIXED_POINT_ACCOUNTING_VERSION, lifecycle["id"]),
                    )
                    conn.execute("DELETE FROM position_management_state WHERE symbol=?", (symbol,))
                    conn.execute(
                        "INSERT INTO audit_events(run_id,event_type,actor,detail,created_at) VALUES(NULL,?,?,?,?)",
                        ("position_lifecycle_closed", "position_lifecycle", json_dumps({"symbol": symbol, "lifecycle_id": lifecycle["id"]}), now),
                    )
            # Refresh after closing flips so one-active-symbol uniqueness remains valid.
            active_symbols = {
                row["symbol"]: row for row in conn.execute("SELECT * FROM position_lifecycles WHERE state='active'").fetchall()
            }
            for symbol, observed in current.items():
                lifecycle = active_symbols.get(symbol)
                if lifecycle:
                    boundary_row = conn.execute(
                        "SELECT MAX(closed_at) boundary FROM position_lifecycles WHERE symbol=? AND state='closed'",
                        (symbol,),
                    ).fetchone()
                    boundary = boundary_row["boundary"] if boundary_row else None
                    bind_filled_entry_intents(
                        conn, lifecycle_id=lifecycle["id"], symbol=symbol, boundary=boundary
                    )
                    conn.execute(
                        """UPDATE position_lifecycles SET broker_position_id=COALESCE(?,broker_position_id),
                           current_quantity=?,current_quantity_decimal=?,average_entry_price=COALESCE(?,average_entry_price),
                           average_entry_price_decimal=COALESCE(?,average_entry_price_decimal),
                           decimal_provenance=?,decimal_accounting_version=?,updated_at=? WHERE id=?""",
                        (
                            observed["broker_position_id"], legacy_float(observed["quantity"]),
                            decimal_text(observed["quantity"]), legacy_float(observed["average_entry_price"]),
                            decimal_text(observed["average_entry_price"]) if observed["average_entry_price"] is not None else None,
                            EXACT_DECIMAL_PROVENANCE, FIXED_POINT_ACCOUNTING_VERSION, now, lifecycle["id"],
                        ),
                    )
                    refresh_opening_quantity(conn, lifecycle)
                    conn.execute(
                        "INSERT INTO audit_events(run_id,event_type,actor,detail,created_at) VALUES(NULL,?,?,?,?)",
                        ("position_lifecycle_observed", "position_lifecycle", json_dumps({"symbol": symbol, "lifecycle_id": lifecycle["id"]}), now),
                    )
                else:
                    lifecycle_id = str(uuid.uuid4())
                    conn.execute(
                        """INSERT INTO position_lifecycles(
                               id,symbol,broker_position_id,side,state,opened_at,opening_quantity,current_quantity,
                               average_entry_price,source,created_at,updated_at,
                               opening_quantity_decimal,current_quantity_decimal,average_entry_price_decimal,
                               decimal_provenance,decimal_accounting_version)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            lifecycle_id, symbol, observed["broker_position_id"], observed["side"], "active", now,
                            legacy_float(observed["quantity"]), legacy_float(observed["quantity"]),
                            legacy_float(observed["average_entry_price"]), source, now, now,
                            decimal_text(observed["quantity"]), decimal_text(observed["quantity"]),
                            decimal_text(observed["average_entry_price"]) if observed["average_entry_price"] is not None else None,
                            EXACT_DECIMAL_PROVENANCE, FIXED_POINT_ACCOUNTING_VERSION,
                        ),
                    )
                    boundary_row = conn.execute(
                        "SELECT MAX(closed_at) boundary FROM position_lifecycles WHERE symbol=? AND state='closed'",
                        (symbol,),
                    ).fetchone()
                    boundary = boundary_row["boundary"] if boundary_row else None
                    bind_filled_entry_intents(
                        conn, lifecycle_id=lifecycle_id, symbol=symbol, boundary=boundary
                    )
                    conn.execute(
                        """UPDATE position_lots SET position_lifecycle_id=?,updated_at=?
                           WHERE symbol=? AND position_lifecycle_id IS NULL AND (? IS NULL OR opened_at>?)""",
                        (lifecycle_id, now, symbol, boundary, boundary),
                    )
                    created = conn.execute(
                        "SELECT * FROM position_lifecycles WHERE id=?",
                        (lifecycle_id,),
                    ).fetchone()
                    if created:
                        refresh_opening_quantity(conn, created)
        return {
            row["symbol"]: row["id"]
            for row in self.storage.fetch_all("SELECT symbol,id FROM position_lifecycles WHERE state='active'")
        }

    def active_id(self, symbol: str) -> str | None:
        rows = self.storage.fetch_all(
            "SELECT id FROM position_lifecycles WHERE symbol=? AND state='active'",
            (symbol.upper(),),
        )
        return str(rows[0]["id"]) if rows else None


def _decimal_or_none(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None
