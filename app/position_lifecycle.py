from __future__ import annotations

import uuid
from typing import Any

from .utils import iso_now, json_dumps


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
            quantity = float(_value(position, "qty", 0) or 0)
            if symbol and abs(quantity) > 1e-12:
                current[symbol] = {
                    "quantity": quantity,
                    "side": "long" if quantity > 0 else "short",
                    "broker_position_id": str(_value(position, "asset_id", "") or _value(position, "id", "") or "") or None,
                    "average_entry_price": _float_or_none(_value(position, "avg_entry_price")),
                }

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
                           management_state_archive=? WHERE id=?""",
                        (now, now, archive, lifecycle["id"]),
                    )
                    conn.execute("DELETE FROM position_management_state WHERE symbol=?", (symbol,))
            # Refresh after closing flips so one-active-symbol uniqueness remains valid.
            active_symbols = {
                row["symbol"]: row for row in conn.execute("SELECT * FROM position_lifecycles WHERE state='active'").fetchall()
            }
            for symbol, observed in current.items():
                lifecycle = active_symbols.get(symbol)
                if lifecycle:
                    conn.execute(
                        """UPDATE position_lifecycles SET broker_position_id=COALESCE(?,broker_position_id),
                           current_quantity=?,average_entry_price=COALESCE(?,average_entry_price),updated_at=? WHERE id=?""",
                        (observed["broker_position_id"], observed["quantity"], observed["average_entry_price"], now, lifecycle["id"]),
                    )
                else:
                    lifecycle_id = str(uuid.uuid4())
                    conn.execute(
                        """INSERT INTO position_lifecycles(
                               id,symbol,broker_position_id,side,state,opened_at,opening_quantity,current_quantity,
                               average_entry_price,source,created_at,updated_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (lifecycle_id, symbol, observed["broker_position_id"], observed["side"], "active", now, observed["quantity"], observed["quantity"], observed["average_entry_price"], source, now, now),
                    )
                    boundary_row = conn.execute(
                        "SELECT MAX(closed_at) boundary FROM position_lifecycles WHERE symbol=? AND state='closed'",
                        (symbol,),
                    ).fetchone()
                    boundary = boundary_row["boundary"] if boundary_row else None
                    conn.execute(
                        """UPDATE order_intents SET position_lifecycle_id=?,updated_at=?
                           WHERE symbol=? AND side='buy' AND filled_quantity>0 AND position_lifecycle_id IS NULL
                             AND (? IS NULL OR created_at>?)""",
                        (lifecycle_id, now, symbol, boundary, boundary),
                    )
                    conn.execute(
                        """UPDATE position_lots SET position_lifecycle_id=?,updated_at=?
                           WHERE symbol=? AND position_lifecycle_id IS NULL AND (? IS NULL OR opened_at>?)""",
                        (lifecycle_id, now, symbol, boundary, boundary),
                    )
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


def _float_or_none(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None
