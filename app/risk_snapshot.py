from __future__ import annotations

import uuid
from dataclasses import dataclass, asdict
from typing import Any

from .execution import DurableExecutionStore
from .utils import iso_now, json_dumps


def _value(obj: Any, name: str, default: Any = None) -> Any:
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


def _float(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


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
        symbol_exposure: dict[str, float] = {}
        cluster_exposure: dict[str, float] = {}
        exposures_known = True
        for position in positions:
            symbol = str(_value(position, "symbol", "")).upper()
            quantity = _float(_value(position, "qty"))
            market_value = _float(_value(position, "market_value"))
            if market_value is None:
                price = _float(_value(position, "current_price")) or _float(_value(position, "avg_entry_price"))
                market_value = quantity * price if quantity is not None and price is not None else None
            if not symbol or market_value is None:
                exposures_known = False
                continue
            gross += abs(market_value)
            net += market_value
            symbol_exposure[symbol] = symbol_exposure.get(symbol, 0.0) + market_value
            cluster = self.cluster_resolver(symbol)
            if cluster:
                cluster_exposure[cluster] = cluster_exposure.get(cluster, 0.0) + market_value
        if not exposures_known:
            unavailable.extend(["filled_gross_exposure", "filled_net_exposure"])

        reservation = DurableExecutionStore(self.storage).active_reservations()
        reserved = float(reservation["active_reserved_notional"])
        reserved_stop = float(reservation["active_reserved_stop_risk"])
        unknown = float(
            self.storage.fetch_all(
                """SELECT COALESCE(SUM(r.active_notional),0) value FROM risk_reservations r
                   JOIN order_intents i ON i.id=r.intent_id WHERE r.state='active' AND i.state='unknown'"""
            )[0]["value"]
        )
        held_stop = self._held_stop_risk(positions)
        if held_stop is None:
            unavailable.append("held_open_stop_risk")

        # The repository does not yet have a complete lot-cost ledger covering all
        # historical manual/broker activity. Report realized metrics unavailable
        # instead of fabricating zero; legacy absolute equity-loss controls remain active.
        unavailable.extend(["daily_realized_pl", "daily_realized_loss_pct", "weekly_realized_pl", "weekly_realized_loss_pct"])
        return CanonicalRiskSnapshot(
            calculated_at=iso_now(), source_at=source_at, source_status="degraded" if unavailable else "healthy",
            portfolio_equity=equity, filled_gross_exposure=gross if exposures_known else None,
            filled_net_exposure=net if exposures_known else None, active_reserved_exposure=reserved,
            projected_gross_exposure=(gross + reserved) if exposures_known else None,
            held_open_stop_risk=held_stop, active_reserved_stop_risk=reserved_stop,
            projected_total_open_risk=(held_stop + reserved_stop) if held_stop is not None else None,
            daily_realized_pl=None, daily_realized_loss_pct=None, weekly_realized_pl=None, weekly_realized_loss_pct=None,
            unresolved_unknown_exposure=unknown, buying_power=buying_power, cash=cash,
            symbol_exposure=symbol_exposure, cluster_exposure=cluster_exposure, unavailable=tuple(sorted(set(unavailable))),
        )

    def persist(self, run_id: str, snapshot: CanonicalRiskSnapshot) -> str:
        identifier = str(uuid.uuid4())
        self.storage.execute(
            """INSERT INTO risk_snapshots_v2(
                   id,run_id,calculated_at,source_at,source_status,portfolio_equity,filled_gross_exposure,
                   filled_net_exposure,active_reserved_exposure,projected_gross_exposure,held_open_stop_risk,
                   active_reserved_stop_risk,projected_total_open_risk,daily_realized_pl,daily_realized_loss_pct,
                   weekly_realized_pl,weekly_realized_loss_pct,unresolved_unknown_exposure,buying_power,cash,
                   symbol_exposure_json,cluster_exposure_json,raw_inputs)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                identifier, run_id, snapshot.calculated_at, snapshot.source_at, snapshot.source_status,
                snapshot.portfolio_equity, snapshot.filled_gross_exposure, snapshot.filled_net_exposure,
                snapshot.active_reserved_exposure, snapshot.projected_gross_exposure, snapshot.held_open_stop_risk,
                snapshot.active_reserved_stop_risk, snapshot.projected_total_open_risk, snapshot.daily_realized_pl,
                snapshot.daily_realized_loss_pct, snapshot.weekly_realized_pl, snapshot.weekly_realized_loss_pct,
                snapshot.unresolved_unknown_exposure, snapshot.buying_power, snapshot.cash,
                json_dumps(snapshot.symbol_exposure), json_dumps(snapshot.cluster_exposure),
                json_dumps({"unavailable": snapshot.unavailable, "calculation": "filled_plus_active_reservations"}),
            ),
        )
        return identifier

    def _held_stop_risk(self, positions: list[Any]) -> float | None:
        total = 0.0
        for position in positions:
            symbol = str(_value(position, "symbol", "")).upper()
            qty = _float(_value(position, "qty"))
            if not symbol or qty is None or qty <= 0:
                continue
            rows = self.storage.fetch_all(
                "SELECT initial_stop_price,avg_entry_price FROM position_management_state WHERE symbol=?",
                (symbol,),
            )
            entry = _float(_value(position, "avg_entry_price"))
            stop = _float(rows[0].get("initial_stop_price")) if rows else None
            if entry is None or stop is None:
                return None
            total += qty * max(entry - stop, 0.0)
        return total
