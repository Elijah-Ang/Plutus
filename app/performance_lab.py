"""Fill-bound Performance Lab lifecycle classification.

Proposal workflow evidence remains useful, but it is never actual-trade
evidence.  Only a durable fill may promote an opportunity to ``actual_fill``.
"""

from __future__ import annotations

import math
import sqlite3
from typing import Any

from .formula_versions import PERFORMANCE_LAB_CLASSIFICATION_SCHEMA_VERSION
from .utils import iso_now


PERFORMANCE_EVIDENCE_CTE_SQL = """WITH ranked_performance_execution AS (
    SELECT o.proposal_id,o.id order_id,o.broker_order_id,o.status order_status,
           o.notional submitted_notional,f.id fill_id,f.price fill_price,
           f.qty fill_qty,f.qty*f.price filled_notional,
           ROW_NUMBER() OVER (
             PARTITION BY o.proposal_id
             ORDER BY CASE
                        WHEN f.id IS NOT NULL AND f.qty>0 AND f.price>0 THEN 0
                        WHEN f.id IS NOT NULL THEN 1
                        ELSE 2
                      END,
                      f.filled_at DESC,f.id DESC,o.updated_at DESC,o.id DESC
           ) evidence_rank
      FROM orders o
      LEFT JOIN fills f ON f.order_id=o.id
)"""


def _positive_finite(value: Any) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and number > 0


def classify_performance_outcome(
    *,
    proposal_status: Any,
    order_status: Any,
    authorized_approval: bool,
    fill_id: Any,
    fill_price: Any,
    fill_qty: Any,
) -> str:
    if (
        fill_id is not None
        and _positive_finite(fill_price)
        and _positive_finite(fill_qty)
    ):
        return "actual_fill"
    if fill_id is not None or fill_price is not None or fill_qty is not None:
        return "invalid_fill_evidence"
    proposal = str(proposal_status or "unknown").strip().lower()
    order = str(order_status or "").strip().lower()
    if order in {"unknown", "reconciliation_required", "submitting"}:
        return "ambiguous_submission"
    if order in {"submitted", "partially_filled", "cancel_pending"}:
        return "submitted_unfilled"
    if order in {"cancelled", "canceled", "rejected", "expired"}:
        return "submitted_cancelled_unfilled"
    if order == "filled":
        return "filled_missing_fill_evidence"
    if order in {"created", "reserved", "retryable_pre_submission"}:
        return "intent_unsubmitted"
    if order:
        return "unclassified_unfilled"
    if proposal == "blocked":
        return "approved_blocked" if authorized_approval else "blocked_unfilled"
    if proposal == "rejected":
        return "rejected_unfilled"
    if proposal == "expired":
        return "expired_unfilled"
    if proposal == "superseded":
        return "superseded_unfilled"
    if proposal == "submitted":
        return "submitted_unfilled"
    if proposal == "approved":
        return "approved_unfilled"
    if proposal == "pending":
        return "proposal_unfilled"
    if proposal == "filled":
        return "filled_missing_fill_evidence"
    if proposal == "unknown":
        return "ambiguous_submission"
    return "unclassified_unfilled"


def apply_performance_lab_classification_schema(
    conn: sqlite3.Connection, *, record_migration: bool = True
) -> None:
    """Reclassify legacy proposal rows without inventing execution evidence."""

    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    required = {
        "performance_setups",
        "performance_outcomes",
        "performance_lab_summaries",
        "trade_proposals",
        "orders",
        "fills",
        "approvals",
    }
    if required.issubset(tables):
        rows = conn.execute(
            PERFORMANCE_EVIDENCE_CTE_SQL
            + """ SELECT ps.id setup_id,ps.run_id,p.status proposal_status,
                      EXISTS(SELECT 1 FROM approvals a
                             WHERE a.proposal_id=ps.proposal_id AND a.authorized=1)
                        approval_exists,
                      e.order_status,e.order_id,e.broker_order_id,
                      e.fill_id,e.fill_price,e.fill_qty,e.filled_notional
               FROM performance_setups ps
               JOIN performance_outcomes po ON po.setup_id=ps.id
               JOIN trade_proposals p ON p.id=ps.proposal_id
               LEFT JOIN ranked_performance_execution e
                 ON e.proposal_id=ps.proposal_id AND e.evidence_rank=1
               WHERE ps.proposed=1
               ORDER BY ps.id"""
        ).fetchall()
        affected_runs: set[str] = set()
        for row in rows:
            classification = classify_performance_outcome(
                proposal_status=row[2],
                authorized_approval=bool(row[3]),
                order_status=row[4],
                fill_id=row[7],
                fill_price=row[8],
                fill_qty=row[9],
            )
            valid_fill = classification == "actual_fill"
            conn.execute(
                """UPDATE performance_setups
                   SET order_id=COALESCE(?,order_id),
                       broker_order_id=COALESCE(?,broker_order_id),
                       fill_id=COALESCE(?,fill_id),fill_price=COALESCE(?,fill_price),
                       fill_qty=COALESCE(?,fill_qty),updated_at=?
                   WHERE id=? AND (
                     (? IS NOT NULL AND COALESCE(order_id,'')<>?)
                     OR (? IS NOT NULL AND COALESCE(broker_order_id,'')<>?)
                     OR (? IS NOT NULL AND COALESCE(fill_id,'')<>CAST(? AS TEXT))
                     OR (? IS NOT NULL AND (
                       COALESCE(fill_price,0)<>? OR COALESCE(fill_qty,0)<>?
                     ))
                   )""",
                (
                    row[5], row[6],
                    str(row[7]) if valid_fill else None,
                    row[8] if valid_fill else None,
                    row[9] if valid_fill else None,
                    iso_now(), row[0],
                    row[5], row[5],
                    row[6], row[6],
                    row[7] if valid_fill else None, row[7],
                    row[7] if valid_fill else None, row[8], row[9],
                ),
            )
            conn.execute(
                """UPDATE performance_outcomes
                   SET actual_or_shadow=?,order_id=COALESCE(?,order_id),
                       broker_order_id=COALESCE(?,broker_order_id),
                       fill_id=COALESCE(?,fill_id),entry_price=COALESCE(?,entry_price),
                       entry_qty=COALESCE(?,entry_qty),entry_notional=COALESCE(?,entry_notional),
                       updated_at=?
                   WHERE setup_id=? AND (
                     COALESCE(actual_or_shadow,'')<>?
                     OR (? IS NOT NULL AND COALESCE(order_id,'')<>?)
                     OR (? IS NOT NULL AND COALESCE(broker_order_id,'')<>?)
                     OR (? IS NOT NULL AND COALESCE(fill_id,'')<>CAST(? AS TEXT))
                     OR (? IS NOT NULL AND (
                       COALESCE(entry_price,0)<>? OR COALESCE(entry_qty,0)<>?
                       OR COALESCE(entry_notional,0)<>?
                     ))
                   )""",
                (
                    classification,
                    row[5],
                    row[6],
                    str(row[7]) if valid_fill else None,
                    row[8] if valid_fill else None,
                    row[9] if valid_fill else None,
                    row[10] if valid_fill else None,
                    iso_now(),
                    row[0],
                    classification,
                    row[5], row[5],
                    row[6], row[6],
                    row[7] if valid_fill else None, row[7],
                    row[7] if valid_fill else None,
                    row[8], row[9], row[10],
                ),
            )
            if row[1]:
                affected_runs.add(str(row[1]))
        for run_id in sorted(affected_runs):
            conn.execute(
                """UPDATE performance_lab_summaries
                   SET total_actual_trades=(SELECT COUNT(*) FROM performance_outcomes
                                            WHERE run_id=? AND actual_or_shadow='actual_fill')
                   WHERE run_id=? AND COALESCE(total_actual_trades,-1)<>(
                         SELECT COUNT(*) FROM performance_outcomes
                         WHERE run_id=? AND actual_or_shadow='actual_fill'
                       )""",
                (run_id, run_id, run_id),
            )
    if record_migration:
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version,applied_at,detail) VALUES(?,?,?)",
            (
                PERFORMANCE_LAB_CLASSIFICATION_SCHEMA_VERSION,
                iso_now(),
                "fill-bound Performance Lab outcome classification",
            ),
        )


__all__ = [
    "PERFORMANCE_EVIDENCE_CTE_SQL",
    "apply_performance_lab_classification_schema",
    "classify_performance_outcome",
]
