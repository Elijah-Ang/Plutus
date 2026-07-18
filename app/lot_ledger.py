from __future__ import annotations

import sqlite3
import json
import uuid
from dataclasses import dataclass
from decimal import Decimal
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .formula_versions import ACCOUNTING_VERSION, EVIDENCE_VERSION
from .fixed_point_accounting import (
    EXACT_DECIMAL_PROVENANCE,
    ZERO,
    decimal_text,
    decimal_value,
    legacy_float,
    row_decimal,
)
from .formula_versions import FIXED_POINT_ACCOUNTING_VERSION
from .utils import iso_now


ACCOUNTING_TIMEZONE = "America/New_York"
VERIFIED_CONFIDENCE = {"verified"}
KNOWN_CONFIDENCE = {"verified", "reconstructed"}
ALL_CONFIDENCE = {"verified", "reconstructed", "partially_reconstructed", "unavailable"}


def _datetime(value: str | datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return result.replace(tzinfo=UTC) if result.tzinfo is None else result.astimezone(UTC)


def _period_keys(value: str | datetime | None) -> tuple[str, str]:
    local = _datetime(value).astimezone(ZoneInfo(ACCOUNTING_TIMEZONE))
    monday = local.date() - timedelta(days=local.weekday())
    return local.date().isoformat(), monday.isoformat()


@dataclass(frozen=True)
class RealizedPnlSummary:
    as_of: str
    accounting_timezone: str
    daily_realized_pl: Decimal | None
    weekly_realized_pl: Decimal | None
    daily_confidence: str
    weekly_confidence: str
    daily_boundary: str
    weekly_boundary: str
    provenance: str


class LotLedger:
    """Prospective long-only FIFO lot ledger.

    Trading days and weeks use America/New_York civil time; weeks begin Monday.
    Realized P&L excludes unrealized movement. Fees and explicit adjustments are
    included when supplied. Unknown historical basis stays unknown.
    """

    def __init__(self, storage: Any) -> None:
        self.storage = storage

    def set_coverage(self, *, effective_from: str, confidence: str, provenance: str) -> None:
        if confidence not in ALL_CONFIDENCE:
            raise ValueError(f"invalid P&L confidence: {confidence}")
        now = iso_now()
        self.storage.execute(
            """INSERT INTO pnl_ledger_status(scope,effective_from,confidence,provenance,updated_at)
               VALUES('prospective',?,?,?,?) ON CONFLICT(scope) DO UPDATE SET
               effective_from=excluded.effective_from,confidence=excluded.confidence,
               provenance=excluded.provenance,updated_at=excluded.updated_at""",
            (effective_from, confidence, provenance, now),
        )

    @staticmethod
    def apply_fill_in_transaction(
        conn: sqlite3.Connection,
        *,
        intent: Any,
        broker_event_key: str,
        delta_quantity: Any,
        fill_price: Any,
        occurred_at: str,
        fees: Any = 0,
        adjustments: Any = 0,
        source: str = "broker_fill",
    ) -> None:
        """Apply the deduplicated delta while the caller's fill transaction is open."""
        quantity = decimal_value(delta_quantity, "delta_quantity", minimum=ZERO)
        assert quantity is not None
        if quantity == ZERO:
            return
        price = decimal_value(fill_price, "fill_price", minimum=ZERO)
        fee_amount = decimal_value(fees, "fees", minimum=ZERO)
        adjustment_amount = decimal_value(adjustments, "adjustments")
        assert price is not None and fee_amount is not None and adjustment_amount is not None
        if price == ZERO:
            raise ValueError("fill_price must be positive")
        symbol = str(intent["symbol"]).upper()
        side = str(intent["side"]).lower()
        now = iso_now()
        day, week = _period_keys(occurred_at)
        status = conn.execute("SELECT * FROM pnl_ledger_status WHERE scope='prospective'").fetchone()
        base_confidence = str(status["confidence"]) if status else "unavailable"
        provenance = str(status["provenance"]) if status else "migration coverage not established"

        def value(obj: Any, key: str, default: Any = None) -> Any:
            try:
                return obj[key]
            except (KeyError, IndexError, TypeError):
                return getattr(obj, key, default)

        proposal = None
        proposal_id = value(intent, "proposal_id")
        if proposal_id:
            proposal = conn.execute("SELECT * FROM trade_proposals WHERE id=?", (proposal_id,)).fetchone()
        proposal_payload: dict[str, Any] = {}
        if proposal is not None and proposal["payload"]:
            try:
                decoded = json.loads(proposal["payload"])
                proposal_payload = decoded if isinstance(decoded, dict) else {}
            except (TypeError, ValueError):
                proposal_payload = {}

        def metadata(key: str, *, proposal_key: str | None = None) -> Any:
            explicit = value(intent, key)
            if explicit is not None:
                return explicit
            if proposal is not None and key in proposal.keys():
                explicit = proposal[key]
                if explicit is not None:
                    return explicit
            return proposal_payload.get(proposal_key or key)

        strategy_version = metadata("strategy_version")
        entry_regime = metadata("entry_regime", proposal_key="volatility_regime")
        entry_score = metadata("entry_score", proposal_key="score")
        initial_risk_dollars = metadata("initial_risk_dollars")
        config_hash = metadata("config_hash")
        evidence_version = metadata("evidence_version") or EVIDENCE_VERSION
        formula_version = metadata("formula_version") or ACCOUNTING_VERSION

        if side == "buy":
            requested_quantity = decimal_value(
                value(intent, "approved_quantity")
                or value(intent, "requested_quantity")
                or quantity,
                "requested_quantity",
                minimum=ZERO,
            )
            assert requested_quantity is not None
            if requested_quantity == ZERO:
                raise ValueError("requested_quantity must be positive")
            original_risk = (
                decimal_value(initial_risk_dollars, "initial_risk_dollars", minimum=ZERO)
                if initial_risk_dollars is not None
                else None
            )
            allocated_risk = None
            if original_risk is not None:
                prior_rows = conn.execute(
                    "SELECT * FROM position_lots WHERE entry_intent_id=?",
                    (value(intent, "id"),),
                ).fetchall()
                prior_risk = sum(
                    (
                        row_decimal(
                            dict(row),
                            "initial_risk_dollars_decimal",
                            "initial_risk_dollars",
                            allow_none=True,
                        )
                        or ZERO
                        for row in prior_rows
                    ),
                    ZERO,
                )
                remaining_risk = max(ZERO, original_risk - prior_risk)
                allocated_risk = min(
                    remaining_risk,
                    original_risk * quantity / max(requested_quantity, quantity),
                )
            conn.execute(
                """INSERT OR IGNORE INTO position_lots(
                       id,symbol,position_lifecycle_id,source_fill_event_key,opened_at,original_quantity,
                       remaining_quantity,unit_cost,fees_allocated,source,provenance,confidence,created_at,updated_at,
                       strategy_version,entry_proposal_id,entry_intent_id,entry_regime,entry_score,initial_risk_dollars,
                       config_hash,evidence_version,formula_version,original_quantity_decimal,
                       remaining_quantity_decimal,unit_cost_decimal,fees_allocated_decimal,
                       initial_risk_dollars_decimal,decimal_provenance,decimal_accounting_version)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    str(uuid.uuid4()), symbol, intent["position_lifecycle_id"], broker_event_key,
                    occurred_at, legacy_float(quantity), legacy_float(quantity), legacy_float(price),
                    legacy_float(fee_amount), source, provenance,
                    base_confidence, now, now, strategy_version, proposal_id, value(intent, "id"), entry_regime,
                    entry_score, legacy_float(allocated_risk), config_hash, evidence_version, formula_version,
                    decimal_text(quantity), decimal_text(quantity), decimal_text(price),
                    decimal_text(fee_amount),
                    decimal_text(allocated_risk) if allocated_risk is not None else None,
                    EXACT_DECIMAL_PROVENANCE, FIXED_POINT_ACCOUNTING_VERSION,
                ),
            )
        elif side == "sell":
            remaining = quantity
            basis = ZERO
            known_qty = ZERO
            confidences: set[str] = set()
            consumption_rows: list[tuple[Any, ...]] = []
            allocated_sell_fees_total = ZERO
            allocated_adjustments_total = ZERO
            lots = conn.execute(
                """SELECT * FROM position_lots WHERE symbol=?
                   AND (COALESCE(remaining_quantity_decimal,'0')<>'0' OR remaining_quantity>0)
                   ORDER BY opened_at,id""",
                (symbol,),
            ).fetchall()
            for lot in lots:
                if remaining == ZERO:
                    break
                lot_row = dict(lot)
                lot_remaining = row_decimal(
                    lot_row, "remaining_quantity_decimal", "remaining_quantity"
                )
                lot_original = row_decimal(
                    lot_row, "original_quantity_decimal", "original_quantity"
                )
                lot_unit_cost = row_decimal(lot_row, "unit_cost_decimal", "unit_cost")
                lot_fees = row_decimal(
                    lot_row, "fees_allocated_decimal", "fees_allocated"
                )
                assert (
                    lot_remaining is not None
                    and lot_original is not None
                    and lot_unit_cost is not None
                    and lot_fees is not None
                )
                if lot_remaining <= ZERO or lot_original <= ZERO:
                    raise ValueError("persisted FIFO lot geometry is invalid")
                consumed = min(remaining, lot_remaining)
                new_remaining = lot_remaining - consumed
                known_qty += consumed
                remaining -= consumed
                confidences.add(str(lot["confidence"]))
                conn.execute(
                    """UPDATE position_lots
                       SET remaining_quantity=?,remaining_quantity_decimal=?,closed_at=?,updated_at=?
                       WHERE id=?""",
                    (
                        legacy_float(new_remaining),
                        decimal_text(new_remaining),
                        occurred_at if new_remaining == ZERO else None,
                        now,
                        lot["id"],
                    ),
                )
                allocated_proceeds = consumed * price
                allocated_cost_basis = consumed * lot_unit_cost
                prior_buy_fees = ZERO
                for prior_consumption in conn.execute(
                    "SELECT allocated_buy_fees_decimal,allocated_buy_fees FROM lot_consumptions WHERE lot_id=?",
                    (lot["id"],),
                ).fetchall():
                    prior_buy_fees += row_decimal(
                        dict(prior_consumption),
                        "allocated_buy_fees_decimal",
                        "allocated_buy_fees",
                    ) or ZERO
                remaining_buy_fees = max(ZERO, lot_fees - prior_buy_fees)
                # Give the final consumption the exact residual so fractional
                # fee allocation reconciles without repeating-decimal drift.
                allocated_buy_fees = (
                    remaining_buy_fees
                    if new_remaining == ZERO
                    else min(
                        remaining_buy_fees,
                        lot_fees * (consumed / lot_original),
                    )
                )
                allocated_sell_fees = (
                    fee_amount - allocated_sell_fees_total
                    if remaining == ZERO
                    else fee_amount * (consumed / quantity)
                )
                allocated_adjustments = (
                    adjustment_amount - allocated_adjustments_total
                    if remaining == ZERO
                    else adjustment_amount * (consumed / quantity)
                )
                allocated_sell_fees_total += allocated_sell_fees
                allocated_adjustments_total += allocated_adjustments
                basis += allocated_cost_basis + allocated_buy_fees
                lot_confidence = str(lot["confidence"] or "unavailable")
                consumption_confidence = lot_confidence if lot_confidence in KNOWN_CONFIDENCE and base_confidence in KNOWN_CONFIDENCE else "partially_reconstructed"
                consumption_rows.append(
                    (
                        str(uuid.uuid4()), broker_event_key, value(intent, "id"),
                        lot["position_lifecycle_id"] or value(intent, "position_lifecycle_id"), lot["id"],
                        lot["strategy_version"], legacy_float(consumed), legacy_float(allocated_proceeds),
                        legacy_float(allocated_cost_basis), legacy_float(allocated_buy_fees),
                        legacy_float(allocated_sell_fees), legacy_float(allocated_adjustments),
                        legacy_float(
                            allocated_proceeds - allocated_cost_basis - allocated_buy_fees
                            - allocated_sell_fees + allocated_adjustments
                        ) if consumption_confidence in KNOWN_CONFIDENCE else None,
                        occurred_at, consumption_confidence, ACCOUNTING_VERSION,
                        decimal_text(consumed), decimal_text(allocated_proceeds),
                        decimal_text(allocated_cost_basis), decimal_text(allocated_buy_fees),
                        decimal_text(allocated_sell_fees), decimal_text(allocated_adjustments),
                        decimal_text(
                            allocated_proceeds - allocated_cost_basis - allocated_buy_fees
                            - allocated_sell_fees + allocated_adjustments
                        ) if consumption_confidence in KNOWN_CONFIDENCE else None,
                        EXACT_DECIMAL_PROVENANCE, FIXED_POINT_ACCOUNTING_VERSION,
                    )
                )
            fully_based = remaining == ZERO
            confidence = base_confidence
            if not fully_based:
                confidence = "partially_reconstructed" if known_qty > 0 else "unavailable"
            elif not confidences or any(item not in KNOWN_CONFIDENCE for item in confidences):
                confidence = "partially_reconstructed"
            elif "reconstructed" in confidences or base_confidence == "reconstructed":
                confidence = "reconstructed"
            elif base_confidence != "verified":
                confidence = base_confidence
            proceeds = quantity * price
            realized = (
                proceeds - basis - fee_amount + adjustment_amount
                if fully_based and confidence in KNOWN_CONFIDENCE
                else None
            )
            remaining_position = sum(
                (
                    row_decimal(dict(row), "remaining_quantity_decimal", "remaining_quantity")
                    or ZERO
                    for row in conn.execute(
                        "SELECT * FROM position_lots WHERE symbol=?", (symbol,)
                    ).fetchall()
                ),
                ZERO,
            )
            conn.execute(
                """INSERT OR IGNORE INTO realized_pnl_events(
                       id,broker_event_key,intent_id,symbol,side,quantity,gross_proceeds,cost_basis,fees,
                       adjustments,realized_pl,remaining_position_quantity,occurred_at,trading_day,trading_week,
                       accounting_timezone,source,provenance,confidence,created_at,quantity_decimal,
                       gross_proceeds_decimal,cost_basis_decimal,fees_decimal,adjustments_decimal,
                       realized_pl_decimal,remaining_position_quantity_decimal,decimal_provenance,
                       decimal_accounting_version)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    str(uuid.uuid4()), broker_event_key, intent["id"], symbol, side,
                    legacy_float(quantity), legacy_float(proceeds),
                    legacy_float(basis) if known_qty > ZERO else None,
                    legacy_float(fee_amount), legacy_float(adjustment_amount), legacy_float(realized),
                    legacy_float(remaining_position), occurred_at, day, week, ACCOUNTING_TIMEZONE,
                    source, provenance, confidence, now, decimal_text(quantity),
                    decimal_text(proceeds), decimal_text(basis) if known_qty > ZERO else None,
                    decimal_text(fee_amount), decimal_text(adjustment_amount),
                    decimal_text(realized) if realized is not None else None,
                    decimal_text(remaining_position), EXACT_DECIMAL_PROVENANCE,
                    FIXED_POINT_ACCOUNTING_VERSION,
                ),
            )
            conn.executemany(
                """INSERT OR IGNORE INTO lot_consumptions(
                     id,broker_event_key,sell_intent_id,position_lifecycle_id,lot_id,strategy_version,
                     quantity,allocated_proceeds,allocated_cost_basis,allocated_buy_fees,allocated_sell_fees,
                     allocated_adjustments,realized_pnl,occurred_at,confidence,accounting_version,
                     quantity_decimal,allocated_proceeds_decimal,allocated_cost_basis_decimal,
                     allocated_buy_fees_decimal,allocated_sell_fees_decimal,allocated_adjustments_decimal,
                     realized_pnl_decimal,decimal_provenance,decimal_accounting_version)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                consumption_rows,
            )
        else:
            raise ValueError(f"unsupported fill side for FIFO ledger: {side}")
        conn.execute(
            "UPDATE pnl_ledger_status SET last_event_at=?,updated_at=? WHERE scope='prospective'",
            (occurred_at, now),
        )

    def record_manual_adjustment(
        self, *, symbol: str, quantity: Any, unit_cost: Any | None, occurred_at: str,
        provenance: str, confidence: str = "reconstructed",
    ) -> str:
        """Record a broker/manual opening-basis adjustment without fabricating certainty."""
        if confidence not in ALL_CONFIDENCE:
            raise ValueError(f"invalid P&L confidence: {confidence}")
        canonical_quantity = decimal_value(quantity, "quantity", minimum=ZERO)
        assert canonical_quantity is not None
        if canonical_quantity == ZERO:
            raise ValueError("manual adjustment quantity must be positive")
        if not str(symbol).strip():
            raise ValueError("manual adjustment symbol is required")
        if not str(provenance).strip():
            raise ValueError("manual adjustment provenance is required")
        identifier = str(uuid.uuid4())
        price = (
            decimal_value(unit_cost, "unit_cost", minimum=ZERO)
            if unit_cost is not None
            else ZERO
        )
        assert price is not None
        if unit_cost is not None and price == ZERO:
            raise ValueError("manual adjustment unit_cost must be positive when supplied")
        actual_confidence = confidence if unit_cost is not None else "unavailable"
        now = iso_now()
        self.storage.execute(
            """INSERT INTO position_lots(id,symbol,source_fill_event_key,opened_at,original_quantity,
                   remaining_quantity,unit_cost,fees_allocated,source,provenance,confidence,created_at,updated_at,
                   original_quantity_decimal,remaining_quantity_decimal,unit_cost_decimal,
                   fees_allocated_decimal,decimal_provenance,decimal_accounting_version,formula_version)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                identifier, str(symbol).upper(), f"manual:{identifier}", occurred_at,
                legacy_float(canonical_quantity), legacy_float(canonical_quantity), legacy_float(price),
                0.0, "manual_adjustment", provenance, actual_confidence, now, now,
                decimal_text(canonical_quantity), decimal_text(canonical_quantity), decimal_text(price),
                "0", EXACT_DECIMAL_PROVENANCE, FIXED_POINT_ACCOUNTING_VERSION, ACCOUNTING_VERSION,
            ),
        )
        return identifier

    def summary(self, *, as_of: str | datetime | None = None) -> RealizedPnlSummary:
        moment = _datetime(as_of)
        day, week = _period_keys(moment)
        status_rows = self.storage.fetch_all("SELECT * FROM pnl_ledger_status WHERE scope='prospective'")
        status = status_rows[0] if status_rows else None
        provenance = str(status["provenance"]) if status else "migration coverage not established"
        daily, daily_confidence = self._period_summary("trading_day", day, status, moment)
        weekly, weekly_confidence = self._period_summary("trading_week", week, status, moment)
        return RealizedPnlSummary(
            as_of=moment.isoformat(), accounting_timezone=ACCOUNTING_TIMEZONE,
            daily_realized_pl=daily, weekly_realized_pl=weekly,
            daily_confidence=daily_confidence, weekly_confidence=weekly_confidence,
            daily_boundary=day, weekly_boundary=week, provenance=provenance,
        )

    def cumulative_realized_pl(self, *, as_of: str | datetime | None = None) -> tuple[Decimal | None, str]:
        """Return cumulative FIFO realized P&L with explicit coverage confidence."""
        moment = _datetime(as_of)
        status_rows = self.storage.fetch_all("SELECT * FROM pnl_ledger_status WHERE scope='prospective'")
        status = status_rows[0] if status_rows else None
        if not status or not status.get("effective_from"):
            return None, "unavailable"
        if str(status.get("confidence")) not in KNOWN_CONFIDENCE:
            return None, str(status.get("confidence") or "unavailable")
        rows = self.storage.fetch_all(
            "SELECT realized_pl,realized_pl_decimal,confidence FROM realized_pnl_events WHERE occurred_at<=?",
            (moment.isoformat(),),
        )
        if any(
            row.get("realized_pl_decimal") is None and row.get("realized_pl") is None
            for row in rows
        ) or any(str(row.get("confidence")) not in KNOWN_CONFIDENCE for row in rows):
            return None, "partially_reconstructed"
        confidence = "reconstructed" if str(status.get("confidence")) == "reconstructed" or any(str(row.get("confidence")) == "reconstructed" for row in rows) else "verified"
        return sum(
            (
                row_decimal(row, "realized_pl_decimal", "realized_pl")
                or ZERO
                for row in rows
            ),
            ZERO,
        ), confidence

    def _period_summary(self, column: str, key: str, status: Any, moment: datetime) -> tuple[Decimal | None, str]:
        if not status or not status.get("effective_from"):
            return None, "unavailable"
        local = moment.astimezone(ZoneInfo(ACCOUNTING_TIMEZONE))
        boundary_date = local.date() if column == "trading_day" else local.date() - timedelta(days=local.weekday())
        boundary = datetime.combine(boundary_date, datetime.min.time(), ZoneInfo(ACCOUNTING_TIMEZONE)).astimezone(UTC)
        coverage = _datetime(status["effective_from"])
        status_confidence = str(status["confidence"])
        rows = self.storage.fetch_all(
            f"SELECT realized_pl,realized_pl_decimal,confidence FROM realized_pnl_events WHERE {column}=?",
            (key,),
        )
        row_confidences = {str(row["confidence"]) for row in rows}
        if coverage > boundary:
            # A verified prospective mechanism does not make a partially covered
            # day/week verified. Pre-boundary activity is deliberately unknown.
            return None, "unavailable"
        if status_confidence not in KNOWN_CONFIDENCE:
            confidence = status_confidence if status_confidence in ALL_CONFIDENCE else "unavailable"
            return None, confidence
        if any(
            row.get("realized_pl_decimal") is None and row.get("realized_pl") is None
            for row in rows
        ) or any(c not in KNOWN_CONFIDENCE for c in row_confidences):
            return None, "partially_reconstructed"
        confidence = "reconstructed" if status_confidence == "reconstructed" or "reconstructed" in row_confidences else "verified"
        return sum(
            (
                row_decimal(row, "realized_pl_decimal", "realized_pl")
                or ZERO
                for row in rows
            ),
            ZERO,
        ), confidence
