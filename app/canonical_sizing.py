"""One canonical quantity/notional/risk calculation for approval and execution."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class CanonicalSizing:
    request_basis: str
    quantity: float
    notional: float
    stop_risk: float
    reference_price: float
    stop_price: float | None


def _positive(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be numeric") from exc
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{label} must be finite and positive")
    return number


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
            tolerance = max(1e-6, notional * 1e-6)
            if abs(supplied - notional) > tolerance:
                raise ValueError("quantity and notional are mathematically inconsistent")
    else:
        notional = _positive(raw_notional, "notional")
        quantity = notional / reference
        basis = "notional"
        if raw_qty not in (None, ""):
            supplied = _positive(raw_qty, "quantity")
            tolerance = max(1e-9, quantity * 1e-6)
            if abs(supplied - quantity) > tolerance:
                raise ValueError("quantity and notional are mathematically inconsistent")
    raw_stop = terms.get("stop_price", terms.get("intended_stop_price"))
    stop = _positive(raw_stop, "stop price") if raw_stop not in (None, "") else None
    stop_risk = quantity * max(reference - float(stop or reference), 0.0)
    supplied_risk = terms.get("stop_risk_dollars")
    if supplied_risk not in (None, ""):
        try:
            supplied = float(supplied_risk)
        except (TypeError, ValueError) as exc:
            raise ValueError("stop risk must be numeric") from exc
        if not math.isfinite(supplied) or supplied < 0 or abs(supplied - stop_risk) > max(1e-6, stop_risk * 1e-6):
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
        try:
            ceiling = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"approved {label} ceiling must be numeric") from exc
        if not math.isfinite(ceiling) or ceiling < 0:
            raise ValueError(f"approved {label} ceiling must be finite and nonnegative")
        if actual > ceiling + 1e-9:
            raise RuntimeError(f"canonical {label} exceeds approved ceiling")
