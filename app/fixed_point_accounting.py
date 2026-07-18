"""Canonical fixed-point evidence for fills, FIFO lots, and realized P&L.

SQLite ``REAL`` columns remain as compatibility projections for existing report
and risk code.  The authoritative accounting values are normalized decimal
strings so broker quantities, prices, fees, basis, and realized P&L never rely
on binary floating-point arithmetic.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .formula_versions import (
    FIXED_POINT_ACCOUNTING_SCHEMA_VERSION,
    FIXED_POINT_ACCOUNTING_VERSION,
)
from .utils import iso_now


ZERO = Decimal("0")
EXACT_DECIMAL_PROVENANCE = "exact_source_decimal"
RECONSTRUCTED_REAL_PROVENANCE = "reconstructed_from_sqlite_real"


TABLE_DECIMAL_COLUMNS: dict[str, dict[str, str]] = {
    "cash_snapshots": {
        "equity_decimal": "TEXT",
        "cash_decimal": "TEXT",
        "settled_cash_decimal": "TEXT",
        "realized_fifo_pnl_decimal": "TEXT",
        "unrealized_pl_decimal": "TEXT",
        "account_equity_change_decimal": "TEXT",
        "unrealized_change_decimal": "TEXT",
        "external_cash_flow_decimal": "TEXT",
        "decimal_provenance": "TEXT",
        "decimal_accounting_version": "TEXT",
    },
    "order_intents": {
        "filled_quantity_decimal": "TEXT",
        "average_fill_price_decimal": "TEXT",
        "decimal_provenance": "TEXT",
        "decimal_accounting_version": "TEXT",
    },
    "broker_fill_events": {
        "cumulative_filled_quantity_decimal": "TEXT",
        "delta_quantity_decimal": "TEXT",
        "fill_price_decimal": "TEXT",
        "fees_decimal": "TEXT",
        "adjustments_decimal": "TEXT",
        "decimal_provenance": "TEXT",
        "decimal_accounting_version": "TEXT",
    },
    "fills": {
        "qty_decimal": "TEXT",
        "price_decimal": "TEXT",
        "decimal_provenance": "TEXT",
        "decimal_accounting_version": "TEXT",
    },
    "position_lots": {
        "original_quantity_decimal": "TEXT",
        "remaining_quantity_decimal": "TEXT",
        "unit_cost_decimal": "TEXT",
        "fees_allocated_decimal": "TEXT",
        "initial_risk_dollars_decimal": "TEXT",
        "decimal_provenance": "TEXT",
        "decimal_accounting_version": "TEXT",
    },
    "realized_pnl_events": {
        "quantity_decimal": "TEXT",
        "gross_proceeds_decimal": "TEXT",
        "cost_basis_decimal": "TEXT",
        "fees_decimal": "TEXT",
        "adjustments_decimal": "TEXT",
        "realized_pl_decimal": "TEXT",
        "remaining_position_quantity_decimal": "TEXT",
        "decimal_provenance": "TEXT",
        "decimal_accounting_version": "TEXT",
    },
    "lot_consumptions": {
        "quantity_decimal": "TEXT",
        "allocated_proceeds_decimal": "TEXT",
        "allocated_cost_basis_decimal": "TEXT",
        "allocated_buy_fees_decimal": "TEXT",
        "allocated_sell_fees_decimal": "TEXT",
        "allocated_adjustments_decimal": "TEXT",
        "realized_pnl_decimal": "TEXT",
        "decimal_provenance": "TEXT",
        "decimal_accounting_version": "TEXT",
    },
}


BACKFILL_FIELDS: dict[str, dict[str, str]] = {
    "cash_snapshots": {
        "equity_decimal": "equity",
        "cash_decimal": "cash",
        "settled_cash_decimal": "settled_cash",
        "realized_fifo_pnl_decimal": "realized_fifo_pnl",
        "unrealized_pl_decimal": "unrealized_pl",
        "account_equity_change_decimal": "account_equity_change",
        "unrealized_change_decimal": "unrealized_change",
        "external_cash_flow_decimal": "external_cash_flow",
    },
    "order_intents": {
        "filled_quantity_decimal": "filled_quantity",
        "average_fill_price_decimal": "average_fill_price",
    },
    "broker_fill_events": {
        "cumulative_filled_quantity_decimal": "cumulative_filled_quantity",
        "delta_quantity_decimal": "delta_quantity",
        "fill_price_decimal": "fill_price",
        # Legacy broker-fill rows did not persist these inputs.  Their values
        # cannot be reconstructed safely from aggregate realized-P&L rows.
        "fees_decimal": "__unavailable__",
        "adjustments_decimal": "__unavailable__",
    },
    "fills": {"qty_decimal": "qty", "price_decimal": "price"},
    "position_lots": {
        "original_quantity_decimal": "original_quantity",
        "remaining_quantity_decimal": "remaining_quantity",
        "unit_cost_decimal": "unit_cost",
        "fees_allocated_decimal": "fees_allocated",
        "initial_risk_dollars_decimal": "initial_risk_dollars",
    },
    "realized_pnl_events": {
        "quantity_decimal": "quantity",
        "gross_proceeds_decimal": "gross_proceeds",
        "cost_basis_decimal": "cost_basis",
        "fees_decimal": "fees",
        "adjustments_decimal": "adjustments",
        "realized_pl_decimal": "realized_pl",
        "remaining_position_quantity_decimal": "remaining_position_quantity",
    },
    "lot_consumptions": {
        "quantity_decimal": "quantity",
        "allocated_proceeds_decimal": "allocated_proceeds",
        "allocated_cost_basis_decimal": "allocated_cost_basis",
        "allocated_buy_fees_decimal": "allocated_buy_fees",
        "allocated_sell_fees_decimal": "allocated_sell_fees",
        "allocated_adjustments_decimal": "allocated_adjustments",
        "realized_pnl_decimal": "realized_pnl",
    },
}


NONNEGATIVE_FIELDS: dict[str, frozenset[str]] = {
    "cash_snapshots": frozenset(
        {"equity_decimal", "cash_decimal", "settled_cash_decimal"}
    ),
    "order_intents": frozenset({"filled_quantity_decimal", "average_fill_price_decimal"}),
    "broker_fill_events": frozenset(
        {
            "cumulative_filled_quantity_decimal",
            "delta_quantity_decimal",
            "fill_price_decimal",
            "fees_decimal",
        }
    ),
    "fills": frozenset({"qty_decimal", "price_decimal"}),
    "position_lots": frozenset(
        {
            "original_quantity_decimal",
            "remaining_quantity_decimal",
            "unit_cost_decimal",
            "fees_allocated_decimal",
            "initial_risk_dollars_decimal",
        }
    ),
    "realized_pnl_events": frozenset(
        {
            "quantity_decimal",
            "gross_proceeds_decimal",
            "cost_basis_decimal",
            "fees_decimal",
            "remaining_position_quantity_decimal",
        }
    ),
    "lot_consumptions": frozenset(
        {
            "quantity_decimal",
            "allocated_proceeds_decimal",
            "allocated_cost_basis_decimal",
            "allocated_buy_fees_decimal",
            "allocated_sell_fees_decimal",
        }
    ),
}


def decimal_value(
    value: Any,
    field: str,
    *,
    minimum: Decimal | None = None,
    allow_none: bool = False,
) -> Decimal | None:
    """Parse finite decimal evidence without first passing through ``float``."""

    if value is None:
        if allow_none:
            return None
        raise ValueError(f"{field} is required")
    if isinstance(value, bool) or (isinstance(value, str) and not value.strip()):
        raise ValueError(f"{field} must be a finite decimal")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a finite decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{field} must be a finite decimal")
    if minimum is not None and result < minimum:
        raise ValueError(f"{field} must be at least {decimal_text(minimum)}")
    return ZERO if result == ZERO else result


def decimal_text(value: Decimal | Any) -> str:
    result = decimal_value(value, "decimal")
    assert result is not None
    if result == ZERO:
        return "0"
    rendered = format(result, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def row_decimal(
    row: Mapping[str, Any],
    canonical_field: str,
    legacy_field: str,
    *,
    allow_none: bool = False,
) -> Decimal | None:
    """Load authoritative text, falling back only for a pre-migration row."""

    canonical = row.get(canonical_field)
    if canonical not in (None, ""):
        return decimal_value(canonical, canonical_field, allow_none=allow_none)
    return decimal_value(row.get(legacy_field), legacy_field, allow_none=allow_none)


def legacy_float(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def apply_fixed_point_accounting_schema(
    conn: sqlite3.Connection, *, record_migration: bool = True
) -> None:
    """Add and backfill canonical decimal evidence without rewriting REAL data."""

    for table, columns in TABLE_DECIMAL_COLUMNS.items():
        if not _table_exists(conn, table):
            continue
        present = {str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")')}
        for name, definition in columns.items():
            if name not in present:
                conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {definition}')

        rows = conn.execute(
            f'SELECT rowid AS __migration_rowid__,* FROM "{table}"'
        ).fetchall()
        for raw_row in rows:
            row = dict(raw_row)
            updates: dict[str, Any] = {}
            for canonical, legacy in BACKFILL_FIELDS[table].items():
                if row.get(canonical) not in (None, ""):
                    continue
                source = (
                    ZERO
                    if legacy == "__zero__"
                    else None
                    if legacy == "__unavailable__"
                    else row.get(legacy)
                )
                if source is None:
                    continue
                try:
                    updates[canonical] = decimal_text(
                        decimal_value(source, f"{table}.{legacy}")
                    )
                except ValueError:
                    # Preserve invalid legacy evidence for integrity reporting;
                    # never invent an authoritative canonical replacement.
                    continue
            if updates or row.get("decimal_provenance") in (None, ""):
                updates.setdefault("decimal_provenance", RECONSTRUCTED_REAL_PROVENANCE)
                updates.setdefault(
                    "decimal_accounting_version", FIXED_POINT_ACCOUNTING_VERSION
                )
            if updates:
                assignments = ",".join(f'"{name}"=?' for name in updates)
                conn.execute(
                    f'UPDATE "{table}" SET {assignments} WHERE rowid=?',
                    (*updates.values(), row["__migration_rowid__"]),
                )

    if record_migration:
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version,applied_at,detail) VALUES(?,?,?)",
            (
                FIXED_POINT_ACCOUNTING_SCHEMA_VERSION,
                iso_now(),
                "additive canonical Decimal fill, FIFO lot, fee, basis, and realized-P&L evidence",
            ),
        )


def _projection_matches(canonical: Decimal, legacy: Any) -> bool:
    if legacy is None:
        return False
    try:
        legacy_decimal = decimal_value(legacy, "legacy projection")
    except ValueError:
        return False
    assert legacy_decimal is not None
    return float(canonical) == float(legacy_decimal)


def fixed_point_integrity_report(storage: Any) -> dict[str, int]:
    """Return canonical-evidence and exact-arithmetic integrity counters."""

    result = {
        "fixed_point_missing_canonical_evidence": 0,
        "fixed_point_invalid_canonical_evidence": 0,
        "fixed_point_legacy_projection_mismatch": 0,
        "fixed_point_fifo_geometry_mismatch": 0,
        "fixed_point_realized_formula_mismatch": 0,
        "fixed_point_consumption_reconciliation_mismatch": 0,
        "fixed_point_wrong_version_or_provenance": 0,
    }
    all_rows: dict[str, list[dict[str, Any]]] = {}
    for table, mapping in BACKFILL_FIELDS.items():
        try:
            rows = storage.fetch_all(f'SELECT * FROM "{table}"')
        except sqlite3.Error:
            continue
        all_rows[table] = rows
        for row in rows:
            if (
                row.get("decimal_provenance")
                not in {EXACT_DECIMAL_PROVENANCE, RECONSTRUCTED_REAL_PROVENANCE}
                or row.get("decimal_accounting_version")
                != FIXED_POINT_ACCOUNTING_VERSION
            ):
                result["fixed_point_wrong_version_or_provenance"] += 1
            parsed: dict[str, Decimal] = {}
            for canonical, legacy in mapping.items():
                legacy_value = (
                    ZERO
                    if legacy == "__zero__"
                    else None
                    if legacy == "__unavailable__"
                    else row.get(legacy)
                )
                required = legacy_value is not None
                raw = row.get(canonical)
                if required and raw in (None, ""):
                    result["fixed_point_missing_canonical_evidence"] += 1
                    continue
                if raw in (None, ""):
                    continue
                try:
                    value = decimal_value(raw, f"{table}.{canonical}")
                    assert value is not None
                    if decimal_text(value) != str(raw):
                        raise ValueError("decimal text is not canonical")
                    if canonical in NONNEGATIVE_FIELDS.get(table, frozenset()) and value < ZERO:
                        raise ValueError("negative value")
                except ValueError:
                    result["fixed_point_invalid_canonical_evidence"] += 1
                    continue
                parsed[canonical] = value
                if legacy not in {"__zero__", "__unavailable__"} and not _projection_matches(value, legacy_value):
                    result["fixed_point_legacy_projection_mismatch"] += 1

            if row.get("decimal_provenance") == EXACT_DECIMAL_PROVENANCE:
                exact_required = {
                    "broker_fill_events": {
                        "cumulative_filled_quantity_decimal",
                        "delta_quantity_decimal",
                        "fill_price_decimal",
                        "fees_decimal",
                        "adjustments_decimal",
                    },
                    "fills": {"qty_decimal", "price_decimal"},
                    "position_lots": {
                        "original_quantity_decimal",
                        "remaining_quantity_decimal",
                        "unit_cost_decimal",
                        "fees_allocated_decimal",
                    },
                    "realized_pnl_events": {
                        "quantity_decimal",
                        "gross_proceeds_decimal",
                        "fees_decimal",
                        "adjustments_decimal",
                        "remaining_position_quantity_decimal",
                    },
                    "lot_consumptions": {
                        "quantity_decimal",
                        "allocated_proceeds_decimal",
                        "allocated_cost_basis_decimal",
                        "allocated_buy_fees_decimal",
                        "allocated_sell_fees_decimal",
                        "allocated_adjustments_decimal",
                    },
                    "order_intents": {"filled_quantity_decimal"},
                }.get(table, set())
                if table == "order_intents":
                    filled = parsed.get("filled_quantity_decimal", ZERO)
                    if filled > ZERO:
                        exact_required.add("average_fill_price_decimal")
                if table in {"realized_pnl_events", "lot_consumptions"} and str(
                    row.get("confidence") or ""
                ) in {"verified", "reconstructed"}:
                    exact_required.add(
                        "realized_pl_decimal"
                        if table == "realized_pnl_events"
                        else "realized_pnl_decimal"
                    )
                    if table == "realized_pnl_events":
                        exact_required.add("cost_basis_decimal")
                result["fixed_point_missing_canonical_evidence"] += sum(
                    row.get(field) in (None, "") for field in exact_required
                )

            if table == "position_lots":
                original = parsed.get("original_quantity_decimal")
                remaining = parsed.get("remaining_quantity_decimal")
                if original is not None and (
                    original <= ZERO
                    or remaining is None
                    or remaining < ZERO
                    or remaining > original
                ):
                    result["fixed_point_fifo_geometry_mismatch"] += 1
            elif table == "realized_pnl_events":
                proceeds = parsed.get("gross_proceeds_decimal")
                basis = parsed.get("cost_basis_decimal")
                fees = parsed.get("fees_decimal")
                adjustments = parsed.get("adjustments_decimal")
                realized = parsed.get("realized_pl_decimal")
                if all(value is not None for value in (proceeds, basis, fees, adjustments, realized)):
                    assert proceeds is not None and basis is not None
                    assert fees is not None and adjustments is not None and realized is not None
                    if realized != proceeds - basis - fees + adjustments:
                        result["fixed_point_realized_formula_mismatch"] += 1
            elif table == "lot_consumptions":
                proceeds = parsed.get("allocated_proceeds_decimal")
                basis = parsed.get("allocated_cost_basis_decimal")
                buy_fees = parsed.get("allocated_buy_fees_decimal")
                sell_fees = parsed.get("allocated_sell_fees_decimal")
                adjustments = parsed.get("allocated_adjustments_decimal")
                realized = parsed.get("realized_pnl_decimal")
                if all(
                    value is not None
                    for value in (proceeds, basis, buy_fees, sell_fees, adjustments, realized)
                ):
                    assert proceeds is not None and basis is not None
                    assert buy_fees is not None and sell_fees is not None
                    assert adjustments is not None and realized is not None
                    if realized != proceeds - basis - buy_fees - sell_fees + adjustments:
                        result["fixed_point_realized_formula_mismatch"] += 1

    consumptions = all_rows.get("lot_consumptions", [])
    by_event: dict[str, list[dict[str, Any]]] = {}
    for row in consumptions:
        by_event.setdefault(str(row.get("broker_event_key") or ""), []).append(row)
    for event in all_rows.get("realized_pnl_events", []):
        if event.get("realized_pl_decimal") in (None, ""):
            continue
        rows = by_event.get(str(event.get("broker_event_key") or ""), [])
        try:
            expected = {
                "quantity": decimal_value(event["quantity_decimal"], "event.quantity"),
                "proceeds": decimal_value(event["gross_proceeds_decimal"], "event.proceeds"),
                "basis": decimal_value(event["cost_basis_decimal"], "event.basis"),
                "fees": decimal_value(event["fees_decimal"], "event.fees"),
                "adjustments": decimal_value(event["adjustments_decimal"], "event.adjustments"),
                "realized": decimal_value(event["realized_pl_decimal"], "event.realized"),
            }
            actual = {
                "quantity": sum(
                    (decimal_value(row["quantity_decimal"], "consumption.quantity") for row in rows),
                    ZERO,
                ),
                "proceeds": sum(
                    (decimal_value(row["allocated_proceeds_decimal"], "consumption.proceeds") for row in rows),
                    ZERO,
                ),
                "basis": sum(
                    (
                        decimal_value(row["allocated_cost_basis_decimal"], "consumption.basis")
                        + decimal_value(row["allocated_buy_fees_decimal"], "consumption.buy_fees")
                        for row in rows
                    ),
                    ZERO,
                ),
                "fees": sum(
                    (decimal_value(row["allocated_sell_fees_decimal"], "consumption.sell_fees") for row in rows),
                    ZERO,
                ),
                "adjustments": sum(
                    (decimal_value(row["allocated_adjustments_decimal"], "consumption.adjustments") for row in rows),
                    ZERO,
                ),
                "realized": sum(
                    (decimal_value(row["realized_pnl_decimal"], "consumption.realized") for row in rows),
                    ZERO,
                ),
            }
            if expected != actual:
                result["fixed_point_consumption_reconciliation_mismatch"] += 1
        except (KeyError, TypeError, ValueError):
            result["fixed_point_consumption_reconciliation_mismatch"] += 1
    return result
