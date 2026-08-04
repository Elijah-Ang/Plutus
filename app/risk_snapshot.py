from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from .execution import DurableExecutionStore
from .fixed_point_accounting import (
    EXACT_DECIMAL_PROVENANCE,
    FIXED_POINT_ACCOUNTING_VERSION,
    ZERO,
    decimal_text,
    sum_exact_decimal_rows,
)
from .lot_ledger import ACCOUNTING_TIMEZONE, LotLedger
from .formula_versions import ACCOUNTING_VERSION
from .utils import iso_now, json_dumps


def _value(obj: Any, name: str, default: Any = None) -> Any:
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


def _float(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _decimal(value: Any, label: str) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


@dataclass(frozen=True)
class CanonicalRiskSnapshot:
    calculated_at: str
    source_at: str | None
    source_status: str
    portfolio_equity: float | None
    filled_gross_exposure: float | None
    filled_net_exposure: float | None
    active_reserved_exposure: float
    projected_gross_exposure: float | None
    held_open_stop_risk: float | None
    active_reserved_stop_risk: float
    projected_total_open_risk: float | None
    daily_realized_pl: float | None
    daily_realized_loss_pct: float | None
    weekly_realized_pl: float | None
    weekly_realized_loss_pct: float | None
    unresolved_unknown_exposure: float
    buying_power: float | None
    cash: float | None
    symbol_exposure: dict[str, float]
    cluster_exposure: dict[str, float]
    unavailable: tuple[str, ...]
    daily_realized_pl_status: str = "unavailable"
    weekly_realized_pl_status: str = "unavailable"
    realized_pl_timezone: str = ACCOUNTING_TIMEZONE
    exact_values: dict[str, str] = field(default_factory=dict)


class RiskSnapshotBuilder:
    def __init__(self, storage: Any, cluster_resolver: Any | None = None) -> None:
        self.storage = storage
        self.cluster_resolver = cluster_resolver or (lambda symbol: None)

    def build(self, positions: list[Any], account: Any, *, source_at: str | None = None) -> CanonicalRiskSnapshot:
        unavailable: list[str] = []
        equity = _float(_value(account, "equity")) if account is not None else None
        cash = _float(_value(account, "cash")) if account is not None else None
        buying_power = _float(_value(account, "buying_power")) if account is not None else None
        if equity is None or equity <= 0:
            unavailable.append("portfolio_equity")
            equity = None
        if cash is None:
            unavailable.append("cash")
        if buying_power is None:
            unavailable.append("buying_power")

        gross = 0.0
        net = 0.0
        gross_decimal = ZERO
        net_decimal = ZERO
        symbol_exposure: dict[str, float] = {}
        cluster_exposure: dict[str, float] = {}
        symbol_values_decimal: dict[str, Decimal] = {}
        cluster_values_decimal: dict[str, Decimal] = {}
        exposures_known = True
        for position in positions:
            symbol = str(_value(position, "symbol", "")).upper()
            quantity = _float(_value(position, "qty"))
            market_value = _float(_value(position, "market_value"))
            market_value_decimal = _decimal(_value(position, "market_value"), "position market value")
            if market_value is None:
                price = _float(_value(position, "current_price")) or _float(_value(position, "avg_entry_price"))
                market_value = quantity * price if quantity is not None and price is not None else None
                quantity_decimal = _decimal(_value(position, "qty"), "position quantity")
                price_decimal = _decimal(_value(position, "current_price"), "position current price") or _decimal(
                    _value(position, "avg_entry_price"), "position average entry price"
                )
                if quantity_decimal is not None and price_decimal is not None:
                    market_value_decimal = quantity_decimal * price_decimal
            if not symbol or market_value is None:
                exposures_known = False
                continue
            if market_value_decimal is None:
                market_value_decimal = _decimal(market_value, "position market value")
            if market_value_decimal is None:
                exposures_known = False
                continue
            gross += abs(market_value)
            net += market_value
            gross_decimal += abs(market_value_decimal)
            net_decimal += market_value_decimal
            symbol_exposure[symbol] = symbol_exposure.get(symbol, 0.0) + market_value
            symbol_values_decimal[symbol] = symbol_values_decimal.get(symbol, ZERO) + market_value_decimal
            cluster = self.cluster_resolver(symbol)
            if cluster:
                cluster_exposure[cluster] = cluster_exposure.get(cluster, 0.0) + market_value
                cluster_values_decimal[cluster] = cluster_values_decimal.get(cluster, ZERO) + market_value_decimal
        if not exposures_known:
            unavailable.extend(["filled_gross_exposure", "filled_net_exposure"])

        reservation = DurableExecutionStore(self.storage).active_reservations()
        reserved_decimal = _decimal(
            reservation["active_reserved_notional_decimal"],
            "active reserved notional",
        )
        reserved_stop_decimal = _decimal(
            reservation["active_reserved_stop_risk_decimal"],
            "active reserved stop risk",
        )
        if reserved_decimal is None or reserved_stop_decimal is None:
            raise RuntimeError("active reservation aggregate lacks exact decimal evidence")
        reserved = float(reserved_decimal)
        reserved_stop = float(reserved_stop_decimal)
        unknown_rows = self.storage.fetch_all(
            """SELECT r.active_notional_decimal
               FROM risk_reservations r
               JOIN order_intents i ON i.id=r.intent_id
               WHERE r.state='active' AND i.state='unknown'"""
        )
        unknown_decimal = sum_exact_decimal_rows(
            unknown_rows,
            "active_notional_decimal",
            label="unknown active reservation notional",
            minimum=ZERO,
        )
        unknown = float(unknown_decimal)
        held_stop_decimal = self._held_stop_risk(positions)
        held_stop = None if held_stop_decimal is None else float(held_stop_decimal)
        if held_stop_decimal is None:
            unavailable.append("held_open_stop_risk")

        realized = LotLedger(self.storage).summary()
        # The FIFO ledger is authoritative Decimal evidence.  Risk snapshots
        # retain REAL compatibility projections for existing percentage gates.
        daily_pl_decimal = realized.daily_realized_pl
        weekly_pl_decimal = realized.weekly_realized_pl
        daily_pl = float(daily_pl_decimal) if daily_pl_decimal is not None else None
        weekly_pl = float(weekly_pl_decimal) if weekly_pl_decimal is not None else None
        equity_decimal = _decimal(_value(account, "equity"), "portfolio equity")
        daily_loss_pct_decimal = (
            max(ZERO, -daily_pl_decimal) / equity_decimal * Decimal("100")
            if daily_pl_decimal is not None and equity_decimal is not None and equity_decimal > ZERO
            else None
        )
        weekly_loss_pct_decimal = (
            max(ZERO, -weekly_pl_decimal) / equity_decimal * Decimal("100")
            if weekly_pl_decimal is not None and equity_decimal is not None and equity_decimal > ZERO
            else None
        )
        daily_loss_pct = float(daily_loss_pct_decimal) if daily_loss_pct_decimal is not None else None
        weekly_loss_pct = float(weekly_loss_pct_decimal) if weekly_loss_pct_decimal is not None else None
        if daily_pl is None:
            unavailable.extend(["daily_realized_pl", "daily_realized_loss_pct"])
        if weekly_pl is None:
            unavailable.extend(["weekly_realized_pl", "weekly_realized_loss_pct"])
        return CanonicalRiskSnapshot(
            calculated_at=iso_now(), source_at=source_at, source_status="degraded" if unavailable else "healthy",
            portfolio_equity=equity, filled_gross_exposure=gross if exposures_known else None,
            filled_net_exposure=net if exposures_known else None, active_reserved_exposure=reserved,
            projected_gross_exposure=(gross + reserved) if exposures_known else None,
            held_open_stop_risk=held_stop, active_reserved_stop_risk=reserved_stop,
            projected_total_open_risk=(held_stop + reserved_stop) if held_stop is not None else None,
            daily_realized_pl=daily_pl, daily_realized_loss_pct=daily_loss_pct,
            weekly_realized_pl=weekly_pl, weekly_realized_loss_pct=weekly_loss_pct,
            unresolved_unknown_exposure=unknown, buying_power=buying_power, cash=cash,
            symbol_exposure=symbol_exposure, cluster_exposure=cluster_exposure, unavailable=tuple(sorted(set(unavailable))),
            daily_realized_pl_status=realized.daily_confidence,
            weekly_realized_pl_status=realized.weekly_confidence,
            exact_values={
                "portfolio_equity_decimal": None if equity_decimal is None or equity is None else decimal_text(equity_decimal),
                "filled_gross_exposure_decimal": decimal_text(gross_decimal) if exposures_known else None,
                "filled_net_exposure_decimal": decimal_text(net_decimal) if exposures_known else None,
                "active_reserved_exposure_decimal": decimal_text(reserved_decimal),
                "projected_gross_exposure_decimal": decimal_text(gross_decimal + reserved_decimal) if exposures_known else None,
                "held_open_stop_risk_decimal": None if held_stop_decimal is None else decimal_text(held_stop_decimal),
                "active_reserved_stop_risk_decimal": decimal_text(reserved_stop_decimal),
                "projected_total_open_risk_decimal": None if held_stop_decimal is None else decimal_text(held_stop_decimal + reserved_stop_decimal),
                "daily_realized_pl_decimal": None if daily_pl_decimal is None else decimal_text(daily_pl_decimal),
                "daily_realized_loss_pct_decimal": None if daily_loss_pct_decimal is None else decimal_text(daily_loss_pct_decimal),
                "weekly_realized_pl_decimal": None if weekly_pl_decimal is None else decimal_text(weekly_pl_decimal),
                "weekly_realized_loss_pct_decimal": None if weekly_loss_pct_decimal is None else decimal_text(weekly_loss_pct_decimal),
                "unresolved_unknown_exposure_decimal": decimal_text(unknown_decimal),
                "buying_power_decimal": None if buying_power is None else decimal_text(_decimal(_value(account, "buying_power"), "buying power")),
                "cash_decimal": None if cash is None else decimal_text(_decimal(_value(account, "cash"), "cash")),
                "symbol_exposure_decimal": (
                    {
                        symbol: decimal_text(value / equity_decimal * Decimal("100"))
                        for symbol, value in symbol_values_decimal.items()
                    }
                    if equity_decimal is not None and equity_decimal > ZERO
                    else None
                ),
                "cluster_exposure_decimal": (
                    {
                        cluster: decimal_text(value / equity_decimal * Decimal("100"))
                        for cluster, value in cluster_values_decimal.items()
                    }
                    if equity_decimal is not None and equity_decimal > ZERO
                    else None
                ),
            },
        )

    def persist(self, run_id: str, snapshot: CanonicalRiskSnapshot) -> str:
        identifier = str(uuid.uuid4())
        self.storage.execute(
            """INSERT INTO risk_snapshots_v2(
                   id,run_id,calculated_at,source_at,source_status,portfolio_equity,filled_gross_exposure,
                   filled_net_exposure,active_reserved_exposure,projected_gross_exposure,held_open_stop_risk,
                   active_reserved_stop_risk,projected_total_open_risk,daily_realized_pl,daily_realized_loss_pct,
                   weekly_realized_pl,weekly_realized_loss_pct,unresolved_unknown_exposure,buying_power,cash,
                   symbol_exposure_json,cluster_exposure_json,raw_inputs,
                   portfolio_equity_decimal,filled_gross_exposure_decimal,filled_net_exposure_decimal,
                   active_reserved_exposure_decimal,projected_gross_exposure_decimal,held_open_stop_risk_decimal,
                   active_reserved_stop_risk_decimal,projected_total_open_risk_decimal,daily_realized_pl_decimal,
                   daily_realized_loss_pct_decimal,weekly_realized_pl_decimal,weekly_realized_loss_pct_decimal,
                   unresolved_unknown_exposure_decimal,buying_power_decimal,cash_decimal,
                   decimal_provenance,decimal_accounting_version)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                identifier, run_id, snapshot.calculated_at, snapshot.source_at, snapshot.source_status,
                snapshot.portfolio_equity, snapshot.filled_gross_exposure, snapshot.filled_net_exposure,
                snapshot.active_reserved_exposure, snapshot.projected_gross_exposure, snapshot.held_open_stop_risk,
                snapshot.active_reserved_stop_risk, snapshot.projected_total_open_risk, snapshot.daily_realized_pl,
                snapshot.daily_realized_loss_pct, snapshot.weekly_realized_pl, snapshot.weekly_realized_loss_pct,
                snapshot.unresolved_unknown_exposure, snapshot.buying_power, snapshot.cash,
                json_dumps(snapshot.symbol_exposure), json_dumps(snapshot.cluster_exposure),
                json_dumps({
                    "unavailable": snapshot.unavailable,
                    "calculation": "filled_plus_active_reservations",
                    "daily_realized_pl_status": snapshot.daily_realized_pl_status,
                    "weekly_realized_pl_status": snapshot.weekly_realized_pl_status,
                    "realized_pl_timezone": snapshot.realized_pl_timezone,
                    "day_boundary": "00:00 America/New_York",
                    "week_boundary": "Monday 00:00 America/New_York",
                    "realized_vs_unrealized": "realized fills only; unrealized excluded",
                    "accounting_version": ACCOUNTING_VERSION,
                    "external_cash_flow": "not used as realized loss; unknown remains unknown",
                }),
                snapshot.exact_values.get("portfolio_equity_decimal"),
                snapshot.exact_values.get("filled_gross_exposure_decimal"),
                snapshot.exact_values.get("filled_net_exposure_decimal"),
                snapshot.exact_values.get("active_reserved_exposure_decimal"),
                snapshot.exact_values.get("projected_gross_exposure_decimal"),
                snapshot.exact_values.get("held_open_stop_risk_decimal"),
                snapshot.exact_values.get("active_reserved_stop_risk_decimal"),
                snapshot.exact_values.get("projected_total_open_risk_decimal"),
                snapshot.exact_values.get("daily_realized_pl_decimal"),
                snapshot.exact_values.get("daily_realized_loss_pct_decimal"),
                snapshot.exact_values.get("weekly_realized_pl_decimal"),
                snapshot.exact_values.get("weekly_realized_loss_pct_decimal"),
                snapshot.exact_values.get("unresolved_unknown_exposure_decimal"),
                snapshot.exact_values.get("buying_power_decimal"),
                snapshot.exact_values.get("cash_decimal"),
                EXACT_DECIMAL_PROVENANCE,
                FIXED_POINT_ACCOUNTING_VERSION,
            ),
        )
        return identifier

    def _held_stop_risk(self, positions: list[Any]) -> Decimal | None:
        total = ZERO
        for position in positions:
            symbol = str(_value(position, "symbol", "")).upper()
            qty_decimal = _decimal(_value(position, "qty"), "position quantity")
            if not symbol or qty_decimal is None:
                continue
            if qty_decimal < ZERO:
                # A short broker position is outside the supported safety
                # envelope; do not understate its risk as zero.
                return None
            if qty_decimal == ZERO:
                continue
            rows = self.storage.fetch_all(
                """SELECT initial_stop_price,trailing_stop_price,authoritative_protective_stop,
                          avg_entry_price FROM position_management_state WHERE symbol=?""",
                (symbol,),
            )
            current_price = _decimal(_value(position, "current_price"), "position current price")
            if current_price is None and qty_decimal > ZERO:
                market_value = _decimal(_value(position, "market_value"), "position market value")
                current_price = market_value / qty_decimal if market_value is not None else None
            stops = []
            if rows:
                stops = [
                    value for value in (
                        _decimal(rows[0].get("initial_stop_price"), "initial stop"),
                        _decimal(rows[0].get("trailing_stop_price"), "trailing stop"),
                        _decimal(rows[0].get("authoritative_protective_stop"), "authoritative protective stop"),
                    )
                    if value is not None and value > ZERO
                ]
            if current_price is None or current_price <= ZERO or not stops:
                return None
            # For a long position, current open stop risk is the loss from the
            # authoritative current mark to the tightest durable protective
            # stop. Entry-price risk becomes stale after the stop advances.
            total += qty_decimal * max(current_price - max(stops), ZERO)
        return total
