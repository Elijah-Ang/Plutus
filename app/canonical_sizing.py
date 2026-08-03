"""One canonical quantity/notional/risk calculation for approval and execution.

The public execution boundary intentionally returns :class:`Decimal` values.
SQLite ``REAL`` columns and broker request projections are compatibility
surfaces only; the sizing identity itself must never be evaluated in binary
floating point.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import math
from typing import Any, Mapping


ZERO = Decimal("0")


@dataclass(frozen=True)
class CanonicalSizing:
    request_basis: str
    quantity: Decimal
    notional: Decimal
    stop_risk: Decimal
    reference_price: Decimal
    stop_price: Decimal | None


def _decimal(value: Any, label: str, *, positive: bool = False) -> Decimal:
    if value is None or isinstance(value, bool):
        raise ValueError(f"{label} must be numeric")
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be numeric") from exc
    if not number.is_finite() or (positive and number <= ZERO) or (not positive and number < ZERO):
        raise ValueError(f"{label} must be finite and {'positive' if positive else 'nonnegative'}")
    return number


def _positive(value: Any, label: str) -> Decimal:
    return _decimal(value, label, positive=True)


def _text(value: Decimal) -> str:
    return "0" if value == ZERO else format(value.normalize(), "f")


def _matches_legacy_float(expected: Decimal, supplied: Decimal, raw: Any) -> bool:
    """Accept only a representational ULP error from an old float caller.

    Production authority is persisted and replayed as Decimal text.  A few
    historical internal callers still pass a float notional or stop-risk
    projection computed from other floats, so the projection can differ from
    the exact identity by one binary ULP.  Permit that boundary artifact only
    for an actual ``float`` input; Decimal and string inputs remain exact.
    """

    if expected == supplied or not isinstance(raw, float) or not math.isfinite(raw):
        return expected == supplied
    magnitude = max(abs(raw), 1.0)
    ulp = Decimal(str(math.ulp(magnitude)))
    return abs(expected - supplied) <= ulp * Decimal("2")


def canonical_sizing(terms: Mapping[str, Any]) -> CanonicalSizing:
    prices = [terms.get("latest_price"), terms.get("reference_price"), terms.get("limit_price")]
    references = [_positive(value, "reference price") for value in prices if value not in (None, "")]
    if not references:
        raise ValueError("a positive conservative reference price is required")
    reference = max(references)
    raw_qty = terms.get("qty", terms.get("quantity"))
    raw_notional = terms.get("notional")
    explicit_basis = str(terms.get("request_basis") or "").lower()
    if explicit_basis and explicit_basis not in {"quantity", "notional"}:
        raise ValueError("request_basis must be quantity or notional")
    if raw_qty in (None, "") and raw_notional in (None, ""):
        raise ValueError("quantity or notional is required")
    if explicit_basis == "quantity" or (not explicit_basis and raw_qty not in (None, "")):
        quantity = _positive(raw_qty, "quantity")
        notional = quantity * reference
        basis = "quantity"
        if raw_notional not in (None, ""):
            supplied = _positive(raw_notional, "notional")
            if not _matches_legacy_float(notional, supplied, raw_notional):
                raise ValueError("quantity and notional are mathematically inconsistent")
    else:
        notional = _positive(raw_notional, "notional")
        quantity = notional / reference
        basis = "notional"
        if raw_qty not in (None, ""):
            supplied = _positive(raw_qty, "quantity")
            if not _matches_legacy_float(quantity, supplied, raw_qty):
                raise ValueError("quantity and notional are mathematically inconsistent")
    raw_stop = terms.get("stop_price", terms.get("intended_stop_price"))
    stop = _positive(raw_stop, "stop price") if raw_stop not in (None, "") else None
    stop_risk = quantity * max(reference - (stop or reference), ZERO)
    supplied_risk = terms.get("stop_risk_dollars")
    if supplied_risk not in (None, ""):
        supplied = _decimal(supplied_risk, "stop risk")
        if not _matches_legacy_float(stop_risk, supplied, supplied_risk):
            raise ValueError("stop risk does not match canonical quantity, price, and stop")
    return CanonicalSizing(basis, quantity, notional, stop_risk, reference, stop)


def enforce_ceilings(sizing: CanonicalSizing, terms: Mapping[str, Any], *, required: bool = False) -> None:
    checks = (
        ("quantity", sizing.quantity, terms.get("approved_quantity_ceiling")),
        ("notional", sizing.notional, terms.get("approved_notional_ceiling")),
        ("stop risk", sizing.stop_risk, terms.get("approved_stop_risk_ceiling")),
    )
    for label, actual, raw in checks:
        if raw in (None, ""):
            if required:
                raise ValueError(f"approved {label} ceiling is required")
            continue
        ceiling = _decimal(raw, f"approved {label} ceiling")
        if actual > ceiling and not _matches_legacy_float(actual, ceiling, raw):
            raise RuntimeError(f"canonical {label} exceeds approved ceiling")


__all__ = ["CanonicalSizing", "canonical_sizing", "enforce_ceilings"]
