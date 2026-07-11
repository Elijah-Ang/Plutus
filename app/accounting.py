"""Explicit account/P&L component separation for risk controls and audits."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .formula_versions import ACCOUNTING_VERSION


@dataclass(frozen=True)
class AccountingComponents:
    account_equity_change: float | None
    realized_fifo_pnl: float | None
    unrealized_change: float | None
    external_cash_flow: float | None
    confidence: str
    accounting_version: str = ACCOUNTING_VERSION


def _finite(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def separate_accounting_components(
    *,
    current_equity: Any,
    previous_equity: Any,
    current_realized_fifo_pnl: Any,
    previous_realized_fifo_pnl: Any,
    current_unrealized_pl: Any,
    previous_unrealized_pl: Any,
) -> AccountingComponents:
    """Separate realized, mark-to-market, equity, and external-cash effects.

    External cash flow remains unknown unless every component needed for the
    reconciliation is present. It is never silently treated as zero.
    """

    current_equity_value = _finite(current_equity)
    previous_equity_value = _finite(previous_equity)
    current_realized = _finite(current_realized_fifo_pnl)
    previous_realized = _finite(previous_realized_fifo_pnl)
    current_unrealized = _finite(current_unrealized_pl)
    previous_unrealized = _finite(previous_unrealized_pl)
    equity_change = current_equity_value - previous_equity_value if current_equity_value is not None and previous_equity_value is not None else None
    realized_change = current_realized - previous_realized if current_realized is not None and previous_realized is not None else None
    unrealized_change = current_unrealized - previous_unrealized if current_unrealized is not None and previous_unrealized is not None else None
    external = equity_change - realized_change - unrealized_change if None not in {equity_change, realized_change, unrealized_change} else None
    confidence = "verified" if external is not None else "unavailable"
    return AccountingComponents(equity_change, realized_change, unrealized_change, external, confidence)
