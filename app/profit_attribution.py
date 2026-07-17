"""Immutable expected-versus-realised profit attribution.

Attribution is downstream of broker fills and the FIFO lot ledger.  It never
creates trading authority.  Complete records reconcile the exact economics
displayed before entry to realised paper P&L while keeping counterfactual and
actual evidence in separate populations.  Historical actual trades without a
compatible economics record remain usable as actual-only evidence, but their
expected-versus-realised attribution is explicitly partial.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from .formula_versions import (
    ACCOUNTING_VERSION,
    EVIDENCE_VERSION,
    PROFIT_ATTRIBUTION_FORMULA_VERSION,
    PROFIT_ATTRIBUTION_SCHEMA_VERSION,
)
from .trade_economics import TradeEconomicsError, TradeEconomicsStore
from .utils import iso_now


ZERO = Decimal("0")
ATTRIBUTION_STATUSES = frozenset({"complete", "partial", "unavailable"})


class ProfitAttributionError(ValueError):
    """Raised when attribution evidence or persisted authority is malformed."""


def _text(value: Any, label: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ProfitAttributionError(f"{label} is required")
    return result


def _utc(value: Any, label: str) -> str:
    try:
        parsed = (
            value
            if isinstance(value, datetime)
            else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        )
    except (TypeError, ValueError) as exc:
        raise ProfitAttributionError(
            f"{label} must be an ISO-8601 timestamp"
        ) from exc
    if parsed.tzinfo is None:
        raise ProfitAttributionError(f"{label} must include a timezone")
    return parsed.astimezone(UTC).isoformat()


def _decimal(
    value: Any,
    label: str,
    *,
    minimum: Decimal | None = None,
    optional: bool = False,
) -> Decimal | None:
    if value is None and optional:
        return None
    if value is None or isinstance(value, bool):
        raise ProfitAttributionError(f"{label} must be finite")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ProfitAttributionError(f"{label} must be finite") from exc
    if not result.is_finite():
        raise ProfitAttributionError(f"{label} must be finite")
    if minimum is not None and result < minimum:
        raise ProfitAttributionError(f"{label} must be at least {minimum}")
    return result


def _decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    if value == ZERO:
        return "0"
    return format(value.normalize(), "f")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str
    )


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AttributionLeg:
    id: str
    lot_id: str
    consumption_id: str
    entry_proposal_id: str | None
    entry_intent_id: str | None
    sell_intent_id: str | None
    trade_economics_id: str | None
    quantity: Any
    actual_entry_price: Any
    actual_exit_price: Any
    allocated_buy_fees: Any
    allocated_sell_fees: Any
    allocated_adjustments: Any = 0
    expected_proposed_quantity: Any | None = None
    expected_entry_price: Any | None = None
    expected_gross_profit: Any | None = None
    expected_execution_cost: Any | None = None
    expected_holding_and_opportunity_cost: Any | None = None
    expected_uncertainty_cost: Any | None = None
    expected_net_profit: Any | None = None
    conservative_expected_net_profit: Any | None = None
    entry_final_ask: Any | None = None
    exit_final_bid: Any | None = None
    approval_delay_seconds: Any | None = None
    authority_status: str = "actual_only"

    def canonical(self) -> dict[str, Any]:
        quantity = _decimal(
            self.quantity, "leg.quantity", minimum=Decimal("0.00000001")
        )
        actual_entry = _decimal(
            self.actual_entry_price, "leg.actual_entry_price", minimum=ZERO
        )
        actual_exit = _decimal(
            self.actual_exit_price, "leg.actual_exit_price", minimum=ZERO
        )
        buy_fees = _decimal(
            self.allocated_buy_fees, "leg.allocated_buy_fees", minimum=ZERO
        )
        sell_fees = _decimal(
            self.allocated_sell_fees, "leg.allocated_sell_fees", minimum=ZERO
        )
        adjustments = _decimal(
            self.allocated_adjustments, "leg.allocated_adjustments"
        )
        optional_nonnegative = (
            "expected_proposed_quantity",
            "expected_entry_price",
            "expected_execution_cost",
            "expected_holding_and_opportunity_cost",
            "expected_uncertainty_cost",
            "entry_final_ask",
            "exit_final_bid",
            "approval_delay_seconds",
        )
        optional: dict[str, str | None] = {}
        for name in optional_nonnegative:
            optional[name] = _decimal_text(
                _decimal(
                    getattr(self, name),
                    f"leg.{name}",
                    minimum=ZERO,
                    optional=True,
                )
            )
        # Expected gross/net values may legitimately be negative.
        for name in (
            "expected_gross_profit",
            "expected_net_profit",
            "conservative_expected_net_profit",
        ):
            optional[name] = _decimal_text(
                _decimal(
                    getattr(self, name),
                    f"leg.{name}",
                    optional=True,
                )
            )
        expected_names = (
            "expected_proposed_quantity",
            "expected_entry_price",
            "expected_gross_profit",
            "expected_execution_cost",
            "expected_holding_and_opportunity_cost",
            "expected_uncertainty_cost",
            "expected_net_profit",
            "conservative_expected_net_profit",
        )
        present = [optional[name] is not None for name in expected_names]
        if any(present) and not all(present):
            raise ProfitAttributionError(
                "leg expected economics must be complete or entirely unavailable"
            )
        if all(present) and Decimal(optional["expected_proposed_quantity"] or "0") <= ZERO:
            raise ProfitAttributionError(
                "leg expected proposed quantity must be positive"
            )
        authority_status = _text(
            self.authority_status, "leg.authority_status"
        )
        if authority_status not in {"verified", "actual_only"}:
            raise ProfitAttributionError(
                "leg authority_status must be verified or actual_only"
            )
        if all(present) != (authority_status == "verified"):
            raise ProfitAttributionError(
                "verified attribution leg requires complete expected economics"
            )
        if authority_status == "verified" and self.trade_economics_id in (
            None,
            "",
        ):
            raise ProfitAttributionError(
                "verified attribution leg requires a trade economics ID"
            )
        return {
            "id": _text(self.id, "leg.id"),
            "lot_id": _text(self.lot_id, "leg.lot_id"),
            "consumption_id": _text(
                self.consumption_id, "leg.consumption_id"
            ),
            "entry_proposal_id": (
                str(self.entry_proposal_id)
                if self.entry_proposal_id not in (None, "")
                else None
            ),
            "entry_intent_id": (
                str(self.entry_intent_id)
                if self.entry_intent_id not in (None, "")
                else None
            ),
            "sell_intent_id": (
                str(self.sell_intent_id)
                if self.sell_intent_id not in (None, "")
                else None
            ),
            "trade_economics_id": (
                str(self.trade_economics_id)
                if self.trade_economics_id not in (None, "")
                else None
            ),
            "quantity": _decimal_text(quantity),
            "actual_entry_price": _decimal_text(actual_entry),
            "actual_exit_price": _decimal_text(actual_exit),
            "allocated_buy_fees": _decimal_text(buy_fees),
            "allocated_sell_fees": _decimal_text(sell_fees),
            "allocated_adjustments": _decimal_text(adjustments),
            **optional,
            "authority_status": authority_status,
        }


@dataclass(frozen=True)
class ProfitAttributionInput:
    position_lifecycle_id: str
    symbol: str
    strategy_version: str | None
    opened_at: str
    closed_at: str
    initial_risk_dollars: Any | None
    legs: Sequence[AttributionLeg] = field(default_factory=tuple)
    unavailable_reason: str | None = None
    evidence_class: str = "actual_paper"
    accounting_version: str = ACCOUNTING_VERSION
    evidence_version: str = EVIDENCE_VERSION

    def canonical(self) -> dict[str, Any]:
        if self.evidence_class != "actual_paper":
            raise ProfitAttributionError(
                "profit attribution accepts actual_paper evidence only"
            )
        if self.accounting_version != ACCOUNTING_VERSION:
            raise ProfitAttributionError(
                "profit attribution accounting version is not current"
            )
        if self.evidence_version != EVIDENCE_VERSION:
            raise ProfitAttributionError(
                "profit attribution evidence version is not current"
            )
        opened = _utc(self.opened_at, "attribution.opened_at")
        closed = _utc(self.closed_at, "attribution.closed_at")
        if datetime.fromisoformat(closed) < datetime.fromisoformat(opened):
            raise ProfitAttributionError(
                "attribution closed_at cannot precede opened_at"
            )
        canonical_legs = tuple(
            sorted(
                (leg.canonical() for leg in self.legs),
                key=lambda item: (
                    item["lot_id"],
                    item["consumption_id"],
                    item["id"],
                ),
            )
        )
        ids = [row["id"] for row in canonical_legs]
        if len(ids) != len(set(ids)):
            raise ProfitAttributionError(
                "attribution leg IDs must be unique"
            )
        consumption_ids = [row["consumption_id"] for row in canonical_legs]
        if len(consumption_ids) != len(set(consumption_ids)):
            raise ProfitAttributionError(
                "attribution consumption IDs must be unique"
            )
        risk = _decimal(
            self.initial_risk_dollars,
            "attribution.initial_risk_dollars",
            minimum=ZERO,
            optional=True,
        )
        strategy = (
            str(self.strategy_version).strip()
            if self.strategy_version not in (None, "")
            else None
        )
        reason = (
            str(self.unavailable_reason).strip()
            if self.unavailable_reason not in (None, "")
            else None
        )
        if not canonical_legs and not reason:
            raise ProfitAttributionError(
                "attribution without legs requires an unavailable reason"
            )
        return {
            "position_lifecycle_id": _text(
                self.position_lifecycle_id,
                "attribution.position_lifecycle_id",
            ),
            "symbol": _text(self.symbol, "attribution.symbol").upper(),
            "strategy_version": strategy,
            "opened_at": opened,
            "closed_at": closed,
            "initial_risk_dollars": _decimal_text(risk),
            "legs": canonical_legs,
            "unavailable_reason": reason,
            "evidence_class": self.evidence_class,
            "accounting_version": self.accounting_version,
            "evidence_version": self.evidence_version,
        }


@dataclass(frozen=True)
class ProfitAttributionRecord:
    id: str
    input: Mapping[str, Any]
    status: str
    confidence: str
    reason: str
    components: Mapping[str, Any]
    input_fingerprint: str
    record_fingerprint: str
    formula_version: str = PROFIT_ATTRIBUTION_FORMULA_VERSION
    schema_version: str = PROFIT_ATTRIBUTION_SCHEMA_VERSION


def calculate_profit_attribution(
    attribution: ProfitAttributionInput,
) -> ProfitAttributionRecord:
    """Reconcile one closed actual-paper lifecycle with Decimal arithmetic."""

    payload = attribution.canonical()
    input_fingerprint = _fingerprint(
        {
            "input": payload,
            "formula_version": PROFIT_ATTRIBUTION_FORMULA_VERSION,
            "schema_version": PROFIT_ATTRIBUTION_SCHEMA_VERSION,
        }
    )
    legs = list(payload["legs"])
    if not legs:
        components = {
            "quantity": None,
            "actual_cost_basis": None,
            "realized_gross_pnl": None,
            "realized_fee_drag": None,
            "realized_net_pnl": None,
            "actual_r_multiple": None,
            "expected_attribution_available": False,
            "reconciliation_residual": None,
        }
        status = "unavailable"
        confidence = "unavailable"
        reason = payload["unavailable_reason"] or "no attributable fill legs"
    else:
        quantity = sum((Decimal(row["quantity"]) for row in legs), ZERO)
        actual_cost_basis = sum(
            (
                Decimal(row["quantity"])
                * Decimal(row["actual_entry_price"])
                for row in legs
            ),
            ZERO,
        )
        actual_proceeds = sum(
            (
                Decimal(row["quantity"])
                * Decimal(row["actual_exit_price"])
                for row in legs
            ),
            ZERO,
        )
        buy_fees = sum(
            (Decimal(row["allocated_buy_fees"]) for row in legs), ZERO
        )
        sell_fees = sum(
            (Decimal(row["allocated_sell_fees"]) for row in legs), ZERO
        )
        adjustments = sum(
            (Decimal(row["allocated_adjustments"]) for row in legs), ZERO
        )
        fee_drag = buy_fees + sell_fees
        realized_gross = actual_proceeds - actual_cost_basis
        realized_net = realized_gross - fee_drag + adjustments
        risk = (
            Decimal(payload["initial_risk_dollars"])
            if payload["initial_risk_dollars"] is not None
            else None
        )
        actual_r = (
            realized_net / risk if risk is not None and risk > ZERO else None
        )
        expected_available = all(
            row["authority_status"] == "verified" for row in legs
        )
        components: dict[str, Any] = {
            "quantity": _decimal_text(quantity),
            "actual_cost_basis": _decimal_text(actual_cost_basis),
            "actual_proceeds": _decimal_text(actual_proceeds),
            "realized_gross_pnl": _decimal_text(realized_gross),
            "realized_buy_fees": _decimal_text(buy_fees),
            "realized_sell_fees": _decimal_text(sell_fees),
            "realized_fee_drag": _decimal_text(fee_drag),
            "realized_adjustments": _decimal_text(adjustments),
            "realized_net_pnl": _decimal_text(realized_net),
            "initial_risk_dollars": _decimal_text(risk),
            "actual_r_multiple": _decimal_text(actual_r),
            "expected_attribution_available": expected_available,
        }
        if not expected_available:
            components.update(
                {
                    "expected_gross_profit": None,
                    "expected_execution_cost": None,
                    "expected_holding_and_opportunity_cost": None,
                    "expected_uncertainty_cost": None,
                    "expected_net_profit": None,
                    "conservative_expected_net_profit": None,
                    "reference_market_pnl": None,
                    "combined_entry_timing_execution_drag": None,
                    "approval_delay_price_drag": None,
                    "entry_fill_slippage_drag": None,
                    "exit_execution_drag": None,
                    "expected_vs_realized_variance": None,
                    "market_outcome_variance": None,
                    "execution_cost_variance": None,
                    "expected_noncash_reserve_release": None,
                    "variance_reconciliation_residual": None,
                    "reconciliation_residual": _decimal_text(
                        realized_net
                        - (realized_gross - fee_drag + adjustments)
                    ),
                }
            )
            status = "partial"
            confidence = "verified_actual_only"
            reason = (
                payload["unavailable_reason"]
                or "realized FIFO P&L verified; compatible expected economics unavailable"
            )
        else:
            expected_gross = ZERO
            expected_execution = ZERO
            expected_holding = ZERO
            expected_uncertainty = ZERO
            expected_net = ZERO
            conservative_net = ZERO
            reference_market_pnl = ZERO
            combined_entry_drag = ZERO
            approval_delay_drag = ZERO
            entry_fill_drag = ZERO
            exit_drag = ZERO
            split_entry_drag_available = True
            exit_reference_available = True
            weighted_delay_numerator = ZERO
            weighted_delay_quantity = ZERO
            approval_delay_complete = True
            for row in legs:
                qty = Decimal(row["quantity"])
                proposed_qty = Decimal(row["expected_proposed_quantity"])
                scale = qty / proposed_qty
                expected_entry = Decimal(row["expected_entry_price"])
                expected_gross += Decimal(row["expected_gross_profit"]) * scale
                expected_execution += (
                    Decimal(row["expected_execution_cost"]) * scale
                )
                expected_holding += (
                    Decimal(row["expected_holding_and_opportunity_cost"])
                    * scale
                )
                expected_uncertainty += (
                    Decimal(row["expected_uncertainty_cost"]) * scale
                )
                expected_net += Decimal(row["expected_net_profit"]) * scale
                conservative_net += (
                    Decimal(row["conservative_expected_net_profit"]) * scale
                )
                actual_entry = Decimal(row["actual_entry_price"])
                actual_exit = Decimal(row["actual_exit_price"])
                combined_entry_drag += qty * (actual_entry - expected_entry)
                entry_quote = (
                    Decimal(row["entry_final_ask"])
                    if row["entry_final_ask"] is not None
                    else None
                )
                exit_quote = (
                    Decimal(row["exit_final_bid"])
                    if row["exit_final_bid"] is not None
                    else None
                )
                if entry_quote is None:
                    split_entry_drag_available = False
                else:
                    approval_delay_drag += qty * (
                        entry_quote - expected_entry
                    )
                    entry_fill_drag += qty * (actual_entry - entry_quote)
                if exit_quote is None:
                    exit_reference_available = False
                    exit_quote = actual_exit
                exit_drag += qty * (exit_quote - actual_exit)
                reference_market_pnl += qty * (
                    exit_quote - expected_entry
                )
                if row["approval_delay_seconds"] is not None:
                    weighted_delay_numerator += (
                        qty * Decimal(row["approval_delay_seconds"])
                    )
                    weighted_delay_quantity += qty
                else:
                    approval_delay_complete = False
            observed_execution_drag = (
                combined_entry_drag + exit_drag + fee_drag
            )
            actual_reconciliation = realized_net - (
                reference_market_pnl
                - combined_entry_drag
                - exit_drag
                - fee_drag
                + adjustments
            )
            variance = realized_net - expected_net
            market_variance = reference_market_pnl - expected_gross
            execution_variance = observed_execution_drag - expected_execution
            reserve_release = expected_holding + expected_uncertainty
            variance_residual = variance - (
                market_variance
                - execution_variance
                + reserve_release
                + adjustments
            )
            weighted_delay = (
                weighted_delay_numerator / weighted_delay_quantity
                if approval_delay_complete and weighted_delay_quantity > ZERO
                else None
            )
            components.update(
                {
                    "expected_gross_profit": _decimal_text(expected_gross),
                    "expected_execution_cost": _decimal_text(
                        expected_execution
                    ),
                    "expected_holding_and_opportunity_cost": _decimal_text(
                        expected_holding
                    ),
                    "expected_uncertainty_cost": _decimal_text(
                        expected_uncertainty
                    ),
                    "expected_net_profit": _decimal_text(expected_net),
                    "conservative_expected_net_profit": _decimal_text(
                        conservative_net
                    ),
                    "reference_market_pnl": _decimal_text(
                        reference_market_pnl
                    ),
                    "combined_entry_timing_execution_drag": _decimal_text(
                        combined_entry_drag
                    ),
                    "approval_delay_price_drag": (
                        _decimal_text(approval_delay_drag)
                        if split_entry_drag_available
                        else None
                    ),
                    "entry_fill_slippage_drag": (
                        _decimal_text(entry_fill_drag)
                        if split_entry_drag_available
                        else None
                    ),
                    "exit_execution_drag": (
                        _decimal_text(exit_drag)
                        if exit_reference_available
                        else None
                    ),
                    "observed_execution_drag": _decimal_text(
                        observed_execution_drag
                    ),
                    "weighted_approval_delay_seconds": _decimal_text(
                        weighted_delay
                    ),
                    "approval_delay_coverage_quantity": _decimal_text(
                        weighted_delay_quantity
                    ),
                    "expected_vs_realized_variance": _decimal_text(variance),
                    "market_outcome_variance": _decimal_text(market_variance),
                    "execution_cost_variance": _decimal_text(
                        execution_variance
                    ),
                    "expected_noncash_reserve_release": _decimal_text(
                        reserve_release
                    ),
                    "variance_reconciliation_residual": _decimal_text(
                        variance_residual
                    ),
                    "reconciliation_residual": _decimal_text(
                        actual_reconciliation
                    ),
                }
            )
            if actual_reconciliation != ZERO or variance_residual != ZERO:
                raise ProfitAttributionError(
                    "profit attribution does not reconcile exactly"
                )
            status = "complete"
            confidence = (
                "verified"
                if split_entry_drag_available and exit_reference_available
                else "verified_combined_execution"
            )
            reason = (
                "expected economics and realised FIFO P&L reconcile exactly"
            )
    body = {
        "input": payload,
        "status": status,
        "confidence": confidence,
        "reason": reason,
        "components": components,
        "input_fingerprint": input_fingerprint,
        "formula_version": PROFIT_ATTRIBUTION_FORMULA_VERSION,
        "schema_version": PROFIT_ATTRIBUTION_SCHEMA_VERSION,
    }
    record_fingerprint = _fingerprint(body)
    return ProfitAttributionRecord(
        id=record_fingerprint[:32],
        input=payload,
        status=status,
        confidence=confidence,
        reason=reason,
        components=components,
        input_fingerprint=input_fingerprint,
        record_fingerprint=record_fingerprint,
    )


def apply_profit_attribution_schema(
    conn: sqlite3.Connection, *, record_migration: bool = True
) -> None:
    """Install immutable lifecycle attribution records."""

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS profit_attribution_records(
          id TEXT PRIMARY KEY,
          position_lifecycle_id TEXT NOT NULL,
          symbol TEXT NOT NULL,
          strategy_version TEXT,
          evidence_class TEXT NOT NULL CHECK(evidence_class='actual_paper'),
          status TEXT NOT NULL CHECK(status IN ('complete','partial','unavailable')),
          confidence TEXT NOT NULL,
          reason TEXT NOT NULL,
          opened_at TEXT NOT NULL,
          closed_at TEXT NOT NULL,
          quantity TEXT,
          initial_risk_dollars TEXT,
          actual_cost_basis TEXT,
          realized_gross_pnl TEXT,
          realized_fee_drag TEXT,
          realized_adjustments TEXT,
          realized_net_pnl TEXT,
          actual_r_multiple TEXT,
          expected_gross_profit TEXT,
          expected_execution_cost TEXT,
          expected_holding_and_opportunity_cost TEXT,
          expected_uncertainty_cost TEXT,
          expected_net_profit TEXT,
          conservative_expected_net_profit TEXT,
          expected_vs_realized_variance TEXT,
          market_outcome_variance TEXT,
          execution_cost_variance TEXT,
          combined_entry_timing_execution_drag TEXT,
          exit_execution_drag TEXT,
          reconciliation_residual TEXT,
          input_json TEXT NOT NULL,
          components_json TEXT NOT NULL,
          input_fingerprint TEXT NOT NULL UNIQUE,
          record_fingerprint TEXT NOT NULL UNIQUE,
          accounting_version TEXT NOT NULL,
          evidence_version TEXT NOT NULL,
          formula_version TEXT NOT NULL,
          schema_version TEXT NOT NULL,
          created_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_profit_attribution_lifecycle
          ON profit_attribution_records(position_lifecycle_id,created_at);
        CREATE INDEX IF NOT EXISTS idx_profit_attribution_strategy
          ON profit_attribution_records(
            strategy_version,status,closed_at);
        """
    )
    if record_migration:
        conn.execute(
            """INSERT OR IGNORE INTO schema_migrations(version,applied_at,detail)
               VALUES(?,?,?)""",
            (
                PROFIT_ATTRIBUTION_SCHEMA_VERSION,
                iso_now(),
                "immutable expected-versus-realised actual-paper lifecycle attribution with exact Decimal reconciliation",
            ),
        )


def _record_columns(record: ProfitAttributionRecord) -> dict[str, Any]:
    payload = record.input
    components = record.components
    return {
        "id": record.id,
        "position_lifecycle_id": payload["position_lifecycle_id"],
        "symbol": payload["symbol"],
        "strategy_version": payload["strategy_version"],
        "evidence_class": payload["evidence_class"],
        "status": record.status,
        "confidence": record.confidence,
        "reason": record.reason,
        "opened_at": payload["opened_at"],
        "closed_at": payload["closed_at"],
        "quantity": components.get("quantity"),
        "initial_risk_dollars": components.get("initial_risk_dollars"),
        "actual_cost_basis": components.get("actual_cost_basis"),
        "realized_gross_pnl": components.get("realized_gross_pnl"),
        "realized_fee_drag": components.get("realized_fee_drag"),
        "realized_adjustments": components.get("realized_adjustments"),
        "realized_net_pnl": components.get("realized_net_pnl"),
        "actual_r_multiple": components.get("actual_r_multiple"),
        "expected_gross_profit": components.get("expected_gross_profit"),
        "expected_execution_cost": components.get(
            "expected_execution_cost"
        ),
        "expected_holding_and_opportunity_cost": components.get(
            "expected_holding_and_opportunity_cost"
        ),
        "expected_uncertainty_cost": components.get(
            "expected_uncertainty_cost"
        ),
        "expected_net_profit": components.get("expected_net_profit"),
        "conservative_expected_net_profit": components.get(
            "conservative_expected_net_profit"
        ),
        "expected_vs_realized_variance": components.get(
            "expected_vs_realized_variance"
        ),
        "market_outcome_variance": components.get(
            "market_outcome_variance"
        ),
        "execution_cost_variance": components.get(
            "execution_cost_variance"
        ),
        "combined_entry_timing_execution_drag": components.get(
            "combined_entry_timing_execution_drag"
        ),
        "exit_execution_drag": components.get("exit_execution_drag"),
        "reconciliation_residual": components.get(
            "reconciliation_residual"
        ),
        "input_json": _canonical_json(dict(payload)),
        "components_json": _canonical_json(dict(components)),
        "input_fingerprint": record.input_fingerprint,
        "record_fingerprint": record.record_fingerprint,
        "accounting_version": payload["accounting_version"],
        "evidence_version": payload["evidence_version"],
        "formula_version": record.formula_version,
        "schema_version": record.schema_version,
    }


class ProfitAttributionStore:
    """Persist and recompute exact lifecycle attribution records."""

    def __init__(self, storage: Any) -> None:
        self.storage = storage

    @staticmethod
    def _optional_id(value: Any) -> str | None:
        return str(value) if value not in (None, "") else None

    @staticmethod
    def _canonical_decimal(
        value: Any,
        label: str,
        *,
        minimum: Decimal | None = None,
        optional: bool = False,
    ) -> str | None:
        return _decimal_text(
            _decimal(
                value,
                label,
                minimum=minimum,
                optional=optional,
            )
        )

    @staticmethod
    def _approval_delay_in_connection(
        conn: sqlite3.Connection,
        proposal_id: str | None,
        estimated_at: str | None,
    ) -> str | None:
        if not proposal_id or not estimated_at:
            return None
        row = conn.execute(
            """SELECT created_at FROM approvals
               WHERE proposal_id=? AND status='consumed'
               ORDER BY created_at DESC,id DESC LIMIT 1""",
            (proposal_id,),
        ).fetchone()
        if row is None or not row["created_at"]:
            return None
        try:
            approved = datetime.fromisoformat(
                str(row["created_at"]).replace("Z", "+00:00")
            )
            estimated = datetime.fromisoformat(
                str(estimated_at).replace("Z", "+00:00")
            )
        except (TypeError, ValueError) as exc:
            raise ProfitAttributionError(
                "durable approval-delay authority is invalid"
            ) from exc
        if approved.tzinfo is None or estimated.tzinfo is None:
            raise ProfitAttributionError(
                "durable approval-delay authority is not timezone-aware"
            )
        delta = approved.astimezone(UTC) - estimated.astimezone(UTC)
        seconds = (
            Decimal(delta.days) * Decimal("86400")
            + Decimal(delta.seconds)
            + Decimal(delta.microseconds) / Decimal("1000000")
        )
        return _decimal_text(max(ZERO, seconds))

    def _verify_durable_authority(
        self,
        conn: sqlite3.Connection,
        record: ProfitAttributionRecord,
    ) -> None:
        """Bind usable attribution to the exact closed FIFO evidence rows."""

        payload = record.input
        lifecycle = conn.execute(
            "SELECT * FROM position_lifecycles WHERE id=?",
            (payload["position_lifecycle_id"],),
        ).fetchone()
        if lifecycle is None:
            raise ProfitAttributionError(
                "durable attribution lifecycle authority is missing"
            )
        lifecycle = dict(lifecycle)
        if (
            str(lifecycle.get("state") or "").lower() != "closed"
            or not lifecycle.get("closed_at")
            or str(lifecycle.get("symbol") or "").upper() != payload["symbol"]
            or _utc(lifecycle.get("opened_at"), "lifecycle.opened_at")
            != payload["opened_at"]
            or _utc(lifecycle.get("closed_at"), "lifecycle.closed_at")
            != payload["closed_at"]
        ):
            raise ProfitAttributionError(
                "durable attribution lifecycle authority is inconsistent"
            )

        lots = [
            dict(row)
            for row in conn.execute(
                """SELECT * FROM position_lots
                   WHERE position_lifecycle_id=? ORDER BY opened_at,id""",
                (payload["position_lifecycle_id"],),
            ).fetchall()
        ]
        versions = {
            str(row["strategy_version"])
            for row in lots
            if row.get("strategy_version")
        }
        durable_strategy = (
            next(iter(versions))
            if len(versions) == 1
            and lots
            and all(row.get("strategy_version") for row in lots)
            else None
        )
        if payload.get("strategy_version") != durable_strategy:
            raise ProfitAttributionError(
                "durable attribution strategy authority is inconsistent"
            )
        complete_risk = bool(lots) and all(
            row.get("initial_risk_dollars") is not None for row in lots
        )
        durable_risk = (
            sum(
                Decimal(str(row["initial_risk_dollars"])) for row in lots
            )
            if complete_risk
            else None
        )
        durable_risk_text = (
            _decimal_text(durable_risk)
            if durable_risk is not None and durable_risk > ZERO
            else None
        )
        if payload.get("initial_risk_dollars") != durable_risk_text:
            raise ProfitAttributionError(
                "durable attribution risk authority is inconsistent"
            )

        # Unavailable records cannot become strategy evidence. Their lifecycle,
        # timestamps, symbol, strategy, and risk still remain durable-bound.
        if record.status == "unavailable":
            return
        if not lots:
            raise ProfitAttributionError(
                "durable attribution lot authority is missing"
            )
        lot_ids = [str(row["id"]) for row in lots]
        placeholders = ",".join("?" for _ in lot_ids)
        consumptions = [
            dict(row)
            for row in conn.execute(
                f"""SELECT * FROM lot_consumptions
                    WHERE position_lifecycle_id=?
                       OR lot_id IN ({placeholders})
                    ORDER BY occurred_at,id""",
                (payload["position_lifecycle_id"], *lot_ids),
            ).fetchall()
        ]
        legs = {
            str(leg["consumption_id"]): leg for leg in payload["legs"]
        }
        if set(legs) != {str(row["id"]) for row in consumptions}:
            raise ProfitAttributionError(
                "durable attribution consumption family is inconsistent"
            )
        lot_map = {str(row["id"]): row for row in lots}
        economics_store = TradeEconomicsStore(self.storage)
        for consumption in consumptions:
            leg = legs[str(consumption["id"])]
            lot = lot_map.get(str(consumption.get("lot_id") or ""))
            if lot is None:
                raise ProfitAttributionError(
                    "durable attribution consumption lot is missing"
                )
            effective_lifecycle = (
                consumption.get("position_lifecycle_id")
                or lot.get("position_lifecycle_id")
            )
            if (
                str(effective_lifecycle or "")
                != payload["position_lifecycle_id"]
                or consumption.get("strategy_version")
                != lot.get("strategy_version")
                or str(leg["lot_id"]) != str(lot["id"])
            ):
                raise ProfitAttributionError(
                    "durable attribution consumption authority is inconsistent"
                )
            quantity = _decimal(
                consumption.get("quantity"),
                "consumption.quantity",
                minimum=Decimal("0.00000001"),
            )
            proceeds = _decimal(
                consumption.get("allocated_proceeds"),
                "consumption.allocated_proceeds",
            )
            cost_basis = _decimal(
                consumption.get("allocated_cost_basis"),
                "consumption.allocated_cost_basis",
            )
            if consumption.get("realized_pnl") is None:
                raise ProfitAttributionError(
                    "durable attribution realized P&L is unavailable"
                )
            expected_actual = {
                "quantity": _decimal_text(quantity),
                "actual_entry_price": _decimal_text(cost_basis / quantity),
                "actual_exit_price": _decimal_text(proceeds / quantity),
                "allocated_buy_fees": self._canonical_decimal(
                    consumption.get("allocated_buy_fees") or 0,
                    "consumption.allocated_buy_fees",
                    minimum=ZERO,
                ),
                "allocated_sell_fees": self._canonical_decimal(
                    consumption.get("allocated_sell_fees") or 0,
                    "consumption.allocated_sell_fees",
                    minimum=ZERO,
                ),
                "allocated_adjustments": self._canonical_decimal(
                    consumption.get("allocated_adjustments") or 0,
                    "consumption.allocated_adjustments",
                ),
                "entry_proposal_id": self._optional_id(
                    lot.get("entry_proposal_id")
                ),
                "entry_intent_id": self._optional_id(
                    lot.get("entry_intent_id")
                ),
                "sell_intent_id": self._optional_id(
                    consumption.get("sell_intent_id")
                ),
            }
            if any(leg.get(name) != value for name, value in expected_actual.items()):
                raise ProfitAttributionError(
                    "durable attribution leg values are inconsistent"
                )

            entry_order = conn.execute(
                "SELECT quote_ask FROM orders WHERE id=?",
                (leg.get("entry_intent_id"),),
            ).fetchone()
            exit_order = conn.execute(
                "SELECT quote_bid FROM orders WHERE id=?",
                (leg.get("sell_intent_id"),),
            ).fetchone()
            expected_entry_ask = self._canonical_decimal(
                entry_order["quote_ask"] if entry_order else None,
                "order.quote_ask",
                minimum=ZERO,
                optional=True,
            )
            expected_exit_bid = self._canonical_decimal(
                exit_order["quote_bid"] if exit_order else None,
                "order.quote_bid",
                minimum=ZERO,
                optional=True,
            )
            economics_id = self._optional_id(leg.get("trade_economics_id"))
            economics_time = None
            if economics_id is not None:
                economics_time_row = conn.execute(
                    """SELECT estimated_at FROM trade_economics_records
                       WHERE id=?""",
                    (economics_id,),
                ).fetchone()
                economics_time = (
                    economics_time_row["estimated_at"]
                    if economics_time_row is not None
                    else None
                )
            expected_delay = self._approval_delay_in_connection(
                conn,
                leg.get("entry_proposal_id"),
                economics_time,
            )
            if (
                leg.get("entry_final_ask") != expected_entry_ask
                or leg.get("exit_final_bid") != expected_exit_bid
                or leg.get("approval_delay_seconds") != expected_delay
            ):
                raise ProfitAttributionError(
                    "durable execution-attribution authority is inconsistent"
                )

            if leg["authority_status"] != "verified":
                continue
            if economics_id is None:
                raise ProfitAttributionError(
                    "verified attribution requires durable trade economics"
                )
            economics_row = conn.execute(
                "SELECT * FROM trade_economics_records WHERE id=?",
                (economics_id,),
            ).fetchone()
            if economics_row is None:
                raise ProfitAttributionError(
                    "durable trade economics authority is missing"
                )
            try:
                economics = economics_store._verified_record_from_row(
                    conn,
                    dict(economics_row),
                    verify_authority=True,
                    require_current_validation_family=False,
                )
            except (TradeEconomicsError, sqlite3.Error) as exc:
                raise ProfitAttributionError(
                    "durable trade economics authority is inconsistent"
                ) from exc
            metrics = economics.metrics
            expected_economics = {
                "expected_proposed_quantity": metrics["proposed_quantity"],
                "expected_entry_price": metrics["entry_estimate"],
                "expected_gross_profit": metrics["expected_gross_profit"],
                "expected_execution_cost": metrics["expected_execution_cost"],
                "expected_holding_and_opportunity_cost": metrics[
                    "expected_holding_and_opportunity_cost"
                ],
                "expected_uncertainty_cost": metrics[
                    "expected_uncertainty_cost"
                ],
                "expected_net_profit": metrics["expected_net_profit"],
                "conservative_expected_net_profit": metrics[
                    "conservative_expected_net_profit"
                ],
            }
            if any(
                leg.get(name) != value
                for name, value in expected_economics.items()
            ):
                raise ProfitAttributionError(
                    "durable expected economics values are inconsistent"
                )

    @staticmethod
    def _verify_row(
        row: Mapping[str, Any], expected: ProfitAttributionRecord
    ) -> None:
        for name, value in _record_columns(expected).items():
            if row.get(name) != value:
                raise ProfitAttributionError(
                    f"persisted profit attribution is inconsistent: {name}"
                )

    def persist(
        self,
        record: ProfitAttributionRecord,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> str:
        if conn is not None:
            if not conn.in_transaction:
                raise ProfitAttributionError(
                    "external attribution persistence requires an active transaction"
                )
            self._persist_in_connection(conn, record)
            return record.id
        with self.storage.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._persist_in_connection(connection, record)
        return record.id

    def _persist_in_connection(
        self, conn: sqlite3.Connection, record: ProfitAttributionRecord
    ) -> None:
        self._verify_durable_authority(conn, record)
        values = {**_record_columns(record), "created_at": iso_now()}
        columns = tuple(values)
        conn.execute(
            f"""INSERT OR IGNORE INTO profit_attribution_records(
                   {",".join(columns)}) VALUES({",".join("?" for _ in columns)})""",
            tuple(values[name] for name in columns),
        )
        row = conn.execute(
            "SELECT * FROM profit_attribution_records WHERE id=?",
            (record.id,),
        ).fetchone()
        if row is None:
            raise ProfitAttributionError(
                "profit attribution persistence failed"
            )
        self._verify_row(dict(row), record)

    def load_verified(self, record_id: str) -> ProfitAttributionRecord:
        with self.storage.connect() as conn:
            conn.execute("BEGIN")
            row = conn.execute(
                "SELECT * FROM profit_attribution_records WHERE id=?",
                (_text(record_id, "record_id"),),
            ).fetchone()
            if row is None:
                raise ProfitAttributionError(
                    "profit attribution record is missing"
                )
            row = dict(row)
            try:
                payload = json.loads(row["input_json"])
                legs = tuple(
                    AttributionLeg(**leg) for leg in payload.pop("legs", [])
                )
                recomputed = calculate_profit_attribution(
                    ProfitAttributionInput(**payload, legs=legs)
                )
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ProfitAttributionError(
                    "persisted profit attribution JSON is invalid"
                ) from exc
            self._verify_row(row, recomputed)
            self._verify_durable_authority(conn, recomputed)
            return recomputed


class ProfitAttributionEngine:
    """Project closed FIFO lifecycles into immutable attribution authority."""

    def __init__(self, storage: Any) -> None:
        self.storage = storage
        self.store = ProfitAttributionStore(storage)

    def _economics_for_proposal(
        self, proposal_id: str | None
    ) -> tuple[str | None, Mapping[str, Any] | None]:
        if not proposal_id:
            return None, None
        rows = self.storage.fetch_all(
            "SELECT trade_economics_id FROM trade_proposals WHERE id=?",
            (proposal_id,),
        )
        if not rows or not rows[0].get("trade_economics_id"):
            return None, None
        economics_id = str(rows[0]["trade_economics_id"])
        try:
            record = TradeEconomicsStore(self.storage).load_verified(
                economics_id,
                # Attribution verifies the immutable authority that existed at
                # estimate time. A later added strategy version must not erase
                # otherwise valid historical expected economics.
                require_current_validation_family=False,
            )
        except (TradeEconomicsError, sqlite3.Error):
            return economics_id, None
        return economics_id, record.metrics

    def _order_quote(self, intent_id: str | None, column: str) -> Any:
        if not intent_id:
            return None
        rows = self.storage.fetch_all(
            f'SELECT "{column}" value FROM orders WHERE id=?',
            (intent_id,),
        )
        return rows[0].get("value") if rows else None

    def _approval_delay(
        self, proposal_id: str | None, estimated_at: str | None
    ) -> Decimal | None:
        if not proposal_id or not estimated_at:
            return None
        rows = self.storage.fetch_all(
            """SELECT created_at FROM approvals
               WHERE proposal_id=? AND status='consumed'
               ORDER BY created_at DESC,id DESC LIMIT 1""",
            (proposal_id,),
        )
        if not rows or not rows[0].get("created_at"):
            return None
        try:
            approved = datetime.fromisoformat(
                str(rows[0]["created_at"]).replace("Z", "+00:00")
            )
            estimated = datetime.fromisoformat(
                str(estimated_at).replace("Z", "+00:00")
            )
            if approved.tzinfo is None or estimated.tzinfo is None:
                return None
            delta = approved.astimezone(UTC) - estimated.astimezone(UTC)
            return max(
                ZERO,
                Decimal(delta.days) * Decimal("86400")
                + Decimal(delta.seconds)
                + Decimal(delta.microseconds) / Decimal("1000000"),
            )
        except (TypeError, ValueError):
            return None

    def refresh_lifecycle(
        self, lifecycle: Mapping[str, Any]
    ) -> ProfitAttributionRecord:
        lifecycle_id = str(lifecycle["id"])
        lots = self.storage.fetch_all(
            """SELECT * FROM position_lots
               WHERE position_lifecycle_id=? ORDER BY opened_at,id""",
            (lifecycle_id,),
        )
        lot_ids = [str(row["id"]) for row in lots]
        consumptions: list[dict[str, Any]] = []
        if lot_ids:
            placeholders = ",".join("?" for _ in lot_ids)
            consumptions = self.storage.fetch_all(
                f"""SELECT * FROM lot_consumptions
                    WHERE position_lifecycle_id=?
                       OR lot_id IN ({placeholders})
                    ORDER BY occurred_at,id""",
                (lifecycle_id, *lot_ids),
            )
        strategy_versions = {
            str(row["strategy_version"])
            for row in lots
            if row.get("strategy_version")
        }
        strategy_version = (
            next(iter(strategy_versions))
            if len(strategy_versions) == 1
            and all(row.get("strategy_version") for row in lots)
            else None
        )
        risk = sum(
            Decimal(str(row["initial_risk_dollars"]))
            for row in lots
            if row.get("initial_risk_dollars") is not None
        )
        complete_risk = bool(lots) and all(
            row.get("initial_risk_dollars") is not None for row in lots
        )
        reason: str | None = None
        legs: list[AttributionLeg] = []
        lot_map = {str(row["id"]): row for row in lots}
        if not lots:
            reason = "no attributed entry lots"
        elif not consumptions:
            reason = "no attributed lot consumptions"
        elif any(
            Decimal(str(row.get("remaining_quantity") or "0")) > ZERO
            for row in lots
        ):
            reason = "closed lifecycle retains unconsumed attributed quantity"
        elif strategy_version is None:
            reason = "mixed or missing strategy attribution"
        else:
            for consumption in consumptions:
                lot = lot_map.get(str(consumption["lot_id"]))
                if lot is None:
                    reason = "consumption references an unavailable lot"
                    continue
                quantity = Decimal(str(consumption.get("quantity") or "0"))
                proceeds = consumption.get("allocated_proceeds")
                cost_basis = consumption.get("allocated_cost_basis")
                if (
                    quantity <= ZERO
                    or proceeds is None
                    or cost_basis is None
                    or consumption.get("realized_pnl") is None
                ):
                    reason = "incomplete FIFO lifecycle accounting"
                    continue
                actual_entry = Decimal(str(cost_basis)) / quantity
                actual_exit = Decimal(str(proceeds)) / quantity
                proposal_id = lot.get("entry_proposal_id")
                economics_id, metrics = self._economics_for_proposal(
                    str(proposal_id) if proposal_id else None
                )
                expected: dict[str, Any] = {}
                authority_status = "actual_only"
                if metrics is not None:
                    expected = {
                        "expected_proposed_quantity": metrics[
                            "proposed_quantity"
                        ],
                        "expected_entry_price": metrics["entry_estimate"],
                        "expected_gross_profit": metrics[
                            "expected_gross_profit"
                        ],
                        "expected_execution_cost": metrics[
                            "expected_execution_cost"
                        ],
                        "expected_holding_and_opportunity_cost": metrics[
                            "expected_holding_and_opportunity_cost"
                        ],
                        "expected_uncertainty_cost": metrics[
                            "expected_uncertainty_cost"
                        ],
                        "expected_net_profit": metrics[
                            "expected_net_profit"
                        ],
                        "conservative_expected_net_profit": metrics[
                            "conservative_expected_net_profit"
                        ],
                    }
                    authority_status = "verified"
                entry_intent_id = lot.get("entry_intent_id")
                sell_intent_id = consumption.get("sell_intent_id")
                estimated_at = None
                if economics_id:
                    economics_rows = self.storage.fetch_all(
                        """SELECT estimated_at FROM trade_economics_records
                           WHERE id=?""",
                        (economics_id,),
                    )
                    estimated_at = (
                        economics_rows[0].get("estimated_at")
                        if economics_rows
                        else None
                    )
                legs.append(
                    AttributionLeg(
                        id=_fingerprint(
                            {
                                "lifecycle": lifecycle_id,
                                "lot": lot["id"],
                                "consumption": consumption["id"],
                            }
                        )[:32],
                        lot_id=str(lot["id"]),
                        consumption_id=str(consumption["id"]),
                        entry_proposal_id=(
                            str(proposal_id) if proposal_id else None
                        ),
                        entry_intent_id=(
                            str(entry_intent_id) if entry_intent_id else None
                        ),
                        sell_intent_id=(
                            str(sell_intent_id) if sell_intent_id else None
                        ),
                        trade_economics_id=economics_id,
                        quantity=quantity,
                        actual_entry_price=actual_entry,
                        actual_exit_price=actual_exit,
                        allocated_buy_fees=consumption.get(
                            "allocated_buy_fees"
                        )
                        or 0,
                        allocated_sell_fees=consumption.get(
                            "allocated_sell_fees"
                        )
                        or 0,
                        allocated_adjustments=consumption.get(
                            "allocated_adjustments"
                        )
                        or 0,
                        entry_final_ask=self._order_quote(
                            str(entry_intent_id) if entry_intent_id else None,
                            "quote_ask",
                        ),
                        exit_final_bid=self._order_quote(
                            str(sell_intent_id) if sell_intent_id else None,
                            "quote_bid",
                        ),
                        approval_delay_seconds=self._approval_delay(
                            str(proposal_id) if proposal_id else None,
                            str(estimated_at) if estimated_at else None,
                        ),
                        authority_status=authority_status,
                        **expected,
                    )
                )
        if reason and reason not in {
            "realized FIFO P&L verified; compatible expected economics unavailable"
        }:
            legs = []
        attribution = ProfitAttributionInput(
            position_lifecycle_id=lifecycle_id,
            symbol=str(lifecycle["symbol"]),
            strategy_version=strategy_version,
            opened_at=str(lifecycle.get("opened_at") or ""),
            closed_at=str(lifecycle.get("closed_at") or ""),
            initial_risk_dollars=risk if complete_risk and risk > ZERO else None,
            legs=tuple(legs),
            unavailable_reason=reason,
        )
        record = calculate_profit_attribution(attribution)
        self.store.persist(record)
        return record

    def refresh_closed(self) -> dict[str, ProfitAttributionRecord]:
        rows = self.storage.fetch_all(
            """SELECT * FROM position_lifecycles
               WHERE state='closed' AND closed_at IS NOT NULL
               ORDER BY closed_at,id"""
        )
        return {
            str(row["id"]): self.refresh_lifecycle(row) for row in rows
        }


__all__ = [
    "AttributionLeg",
    "ProfitAttributionEngine",
    "ProfitAttributionError",
    "ProfitAttributionInput",
    "ProfitAttributionRecord",
    "ProfitAttributionStore",
    "apply_profit_attribution_schema",
    "calculate_profit_attribution",
]
