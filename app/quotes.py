"""Authoritative quote validation and bounded marketable-limit pricing."""

from __future__ import annotations

import math
import os
from datetime import UTC, datetime
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, InvalidOperation
from typing import Any


def _value(value: Any, name: str, default: Any = None) -> Any:
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def _timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    except (TypeError, ValueError, OverflowError):
        return None


def validated_quote(broker: Any, symbol: str, config: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """Fetch and validate a fresh two-sided quote.

    Production never falls back to last trade: a normal order without an
    authoritative quote is rejected. Test-only fallback keeps unrelated unit
    fakes deterministic while the quote-specific tests exercise the real gate.
    """
    source = "alpaca_quote"
    getter = getattr(broker, "get_latest_quote", None)
    if callable(getter):
        try:
            raw = getter(symbol)
        except BaseException as exc:
            raise ValueError("authoritative quote unavailable") from exc
    else:
        if os.getenv("TRADING_AGENT_TESTING") != "1":
            raise ValueError("authoritative quote unavailable")
        try:
            trade = broker.get_latest_price(symbol)
            price = float(_value(trade, "price", 0) or 0)
            timestamp = _timestamp(_value(trade, "timestamp"))
        except BaseException as exc:
            raise ValueError("authoritative quote unavailable") from exc
        raw = {"bid_price": price, "ask_price": price, "timestamp": timestamp}
        source = "test_trade_fallback"

    bid = _value(raw, "bid_price", _value(raw, "bp"))
    ask = _value(raw, "ask_price", _value(raw, "ap"))
    timestamp = _timestamp(_value(raw, "timestamp", _value(raw, "t")))
    try:
        bid_value = float(bid)
        ask_value = float(ask)
    except (TypeError, ValueError) as exc:
        raise ValueError("quote bid and ask must be numeric") from exc
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    if not math.isfinite(bid_value) or not math.isfinite(ask_value) or bid_value <= 0 or ask_value <= 0:
        raise ValueError("quote bid and ask must be finite and positive")
    if bid_value > ask_value:
        raise ValueError("quote is crossed")
    if timestamp is None:
        raise ValueError("quote timestamp is unavailable")
    age = (current - timestamp).total_seconds()
    quote_cfg = config.get("quotes", {}) or {}
    max_age = float(quote_cfg.get("max_age_seconds", config.get("risk", {}).get("max_price_age_seconds", 120)))
    if age < -5 or age > max_age:
        raise ValueError(f"quote is stale or from the future (age={age:.3f}s)")
    midpoint = (bid_value + ask_value) / 2.0
    spread_bps = (ask_value - bid_value) / midpoint * 10000.0
    max_spread = float(quote_cfg.get("max_spread_bps", 50.0))
    if not math.isfinite(spread_bps) or spread_bps < 0 or spread_bps > max_spread:
        raise ValueError("quote spread is outside the configured bound")
    return {
        "symbol": symbol.upper(), "bid": bid_value, "ask": ask_value,
        "midpoint": midpoint, "timestamp": timestamp.isoformat(),
        "age_seconds": age, "spread_bps": spread_bps, "source": source,
    }


def bounded_marketable_limit(quote: dict[str, Any], side: str, config: dict[str, Any]) -> float:
    side = str(side).lower()
    if side not in {"buy", "sell"}:
        raise ValueError("order side must be buy or sell")
    slippage_bps = float((config.get("quotes", {}) or {}).get("max_limit_slippage_bps", 25.0))
    if not math.isfinite(slippage_bps) or slippage_bps < 0:
        raise ValueError("limit slippage bound is invalid")
    raw = float(quote["ask"] if side == "buy" else quote["bid"])
    raw *= 1.0 + slippage_bps / 10000.0 if side == "buy" else 1.0 - slippage_bps / 10000.0
    increment = float((config.get("quotes", {}) or {}).get("price_increment_usd", 0.01))
    if not math.isfinite(increment) or increment <= 0:
        raise ValueError("price increment is invalid")
    try:
        value = Decimal(str(raw)) / Decimal(str(increment))
        rounding = ROUND_CEILING if side == "buy" else ROUND_FLOOR
        rounded = (value.to_integral_value(rounding=rounding) * Decimal(str(increment)))
        result = float(rounded)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("limit price rounding failed") from exc
    if not math.isfinite(result) or result <= 0:
        raise ValueError("rounded limit price is unsafe")
    if side == "buy" and result < float(quote["ask"]):
        raise ValueError("rounded buy limit is not marketable")
    if side == "sell" and result > float(quote["bid"]):
        raise ValueError("rounded sell limit is not marketable")
    return result


def validate_quote_payload(payload: dict[str, Any], side: str, config: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """Validate the quote envelope persisted on a normal order candidate."""
    source = str(payload.get("quote_source") or "")
    if source != "alpaca_quote" and not (os.getenv("TRADING_AGENT_TESTING") == "1" and source == "test_trade_fallback"):
        raise ValueError("normal order quote source is not authoritative")
    quote = {
        "bid": payload.get("quote_bid"), "ask": payload.get("quote_ask"),
        "midpoint": payload.get("quote_midpoint"), "timestamp": payload.get("quote_timestamp"),
        "spread_bps": payload.get("quote_spread_bps"),
    }
    try:
        bid = float(quote["bid"]); ask = float(quote["ask"]); midpoint = float(quote["midpoint"]); spread = float(quote["spread_bps"])
    except (TypeError, ValueError) as exc:
        raise ValueError("persisted quote is malformed") from exc
    timestamp = _timestamp(quote["timestamp"])
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    max_age = float((config.get("quotes", {}) or {}).get("max_age_seconds", config.get("risk", {}).get("max_price_age_seconds", 120)))
    age = (current - timestamp).total_seconds() if timestamp else float("inf")
    expected_midpoint = (bid + ask) / 2.0
    if not all(math.isfinite(value) and value > 0 for value in (bid, ask, midpoint)) or bid > ask or abs(midpoint - expected_midpoint) > 1e-9:
        raise ValueError("persisted quote is crossed or malformed")
    if not math.isfinite(spread) or spread < 0 or spread > float((config.get("quotes", {}) or {}).get("max_spread_bps", 50.0)):
        raise ValueError("persisted quote spread is outside the configured bound")
    if age < -5 or age > max_age:
        raise ValueError("persisted quote is stale")
    quote["bid"] = bid; quote["ask"] = ask; quote["spread_bps"] = spread
    quote["midpoint"] = expected_midpoint
    quote["timestamp"] = timestamp.isoformat()
    expected_limit = bounded_marketable_limit(quote, side, config)
    limit_price = float(payload.get("limit_price") or 0)
    if abs(limit_price - expected_limit) > max(float((config.get("quotes", {}) or {}).get("price_increment_usd", 0.01)), 1e-9):
        raise ValueError("persisted limit price is outside the bounded quote policy")
    return quote


def implementation_shortfall_bps(quote: dict[str, Any], side: str, fill_price: float) -> float | None:
    try:
        fill = float(fill_price)
        mid = float(quote["midpoint"])
    except (TypeError, ValueError, KeyError):
        return None
    if not math.isfinite(fill) or not math.isfinite(mid) or fill <= 0 or mid <= 0:
        return None
    return ((fill - mid) / mid * 10000.0) if str(side).lower() == "buy" else ((mid - fill) / mid * 10000.0)
