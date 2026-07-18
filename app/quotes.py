"""Authoritative quote validation and bounded marketable-limit pricing."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, InvalidOperation
from typing import Any, Mapping


class QuoteValidationError(ValueError):
    """Fail-closed quote rejection with safe, structured market evidence."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        evidence: Mapping[str, Any] | None = None,
    ) -> None:
        self.code = str(code)
        self.evidence = dict(evidence or {})
        super().__init__(message)


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


def _feed_name(raw: Any, broker: Any) -> str:
    value = _value(raw, "feed", None)
    if value is None:
        value = getattr(broker, "equity_realtime_data_feed", None)
    value = getattr(value, "value", value)
    return str(value or "unspecified").strip().lower()


def _quote_evidence(
    *,
    symbol: str,
    feed: str,
    bid: Any = None,
    ask: Any = None,
    timestamp: datetime | None = None,
    age_seconds: float | None = None,
    spread_bps: float | None = None,
    max_age_seconds: float | None = None,
    max_spread_bps: float | None = None,
) -> dict[str, Any]:
    return {
        "symbol": str(symbol).upper(),
        "source": "alpaca_quote",
        "feed": str(feed or "unspecified").lower(),
        "bid": bid,
        "ask": ask,
        "timestamp": timestamp.isoformat() if timestamp is not None else None,
        "age_seconds": age_seconds,
        "spread_bps": spread_bps,
        "max_age_seconds": max_age_seconds,
        "max_spread_bps": max_spread_bps,
    }


def quote_failure_reason(
    symbol: str,
    exc: BaseException,
    config: Mapping[str, Any],
    *,
    stage: str,
) -> str:
    """Return an actionable, non-ambiguous operator explanation."""
    evidence = exc.evidence if isinstance(exc, QuoteValidationError) else {}
    feed = str(evidence.get("feed") or "unspecified").upper()
    quote_cfg = config.get("quotes", {}) or {}
    max_spread = float(evidence.get("max_spread_bps") or quote_cfg.get("max_spread_bps", 50.0))
    code = exc.code if isinstance(exc, QuoteValidationError) else "authoritative_quote_unavailable"
    prefix = "proposal not sent: " if stage == "proposal" else ""
    suffix = (
        " Wait for fresh, spread-valid data before a new proposal is displayed for manual approval."
        if stage == "proposal"
        else " Wait for fresh, spread-valid data; a new proposal and new manual approval are required."
    )
    if code == "spread_exceeds_configured_limit":
        spread = evidence.get("spread_bps")
        spread_text = f"{float(spread):.1f}" if spread is not None else "unknown"
        feed_note = (
            " IEX is a single-exchange feed rather than consolidated NBBO; an entitled real-time SIP feed is required for consolidated quotes."
            if feed == "IEX"
            else ""
        )
        return (
            f"{prefix}Alpaca {feed} quote spread {spread_text} bps exceeded the "
            f"{max_spread:.1f} bps safety limit.{feed_note}{suffix}"
        )
    if code in {"quote_stale", "quote_from_future"}:
        age = evidence.get("age_seconds")
        age_text = f"{float(age):.1f}s" if age is not None else "unknown"
        return f"{prefix}Alpaca {feed} quote was not fresh (age {age_text}).{suffix}"
    if code == "quote_feed_mismatch":
        return f"{prefix}Alpaca quote feed identity did not match current configuration.{suffix}"
    if code == "quote_crossed":
        return f"{prefix}Alpaca {feed} quote was crossed and failed the execution safety policy.{suffix}"
    return f"{prefix}authoritative Alpaca {feed} quote was unavailable or invalid.{suffix}"


def validated_quote(broker: Any, symbol: str, config: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """Fetch and validate a fresh two-sided quote.

    Production and tests use the same authoritative two-sided quote contract.
    A mock broker in a test must inject ``get_latest_quote``; last-trade data is
    never promoted to an executable quote.
    """
    source = "alpaca_quote"
    getter = getattr(broker, "get_latest_quote", None)
    if callable(getter):
        try:
            raw = getter(symbol)
        except BaseException as exc:
            feed = str(getattr(broker, "equity_realtime_data_feed", None) or "unspecified").lower()
            raise QuoteValidationError(
                "authoritative_quote_unavailable",
                "authoritative quote unavailable",
                evidence=_quote_evidence(symbol=symbol, feed=feed),
            ) from exc
    else:
        raise QuoteValidationError(
            "authoritative_quote_unavailable",
            "authoritative quote unavailable",
            evidence=_quote_evidence(symbol=symbol, feed="unspecified"),
        )

    feed = _feed_name(raw, broker)
    bid = _value(raw, "bid_price", _value(raw, "bp"))
    ask = _value(raw, "ask_price", _value(raw, "ap"))
    timestamp = _timestamp(_value(raw, "timestamp", _value(raw, "t")))
    quote_cfg = config.get("quotes", {}) or {}
    max_age = float(quote_cfg.get("max_age_seconds", config.get("risk", {}).get("max_price_age_seconds", 120)))
    max_spread = float(quote_cfg.get("max_spread_bps", 50.0))
    expected_feed = str(((config.get("alpaca", {}) or {}).get("equity_realtime_data_feed") or "")).lower()
    if expected_feed and feed != expected_feed:
        raise QuoteValidationError(
            "quote_feed_mismatch",
            "quote feed identity does not match current configuration",
            evidence=_quote_evidence(
                symbol=symbol, feed=feed, bid=bid, ask=ask, timestamp=timestamp,
                max_age_seconds=max_age, max_spread_bps=max_spread,
            ),
        )
    try:
        bid_value = float(bid)
        ask_value = float(ask)
    except (TypeError, ValueError) as exc:
        raise QuoteValidationError(
            "quote_non_numeric",
            "quote bid and ask must be numeric",
            evidence=_quote_evidence(
                symbol=symbol, feed=feed, bid=bid, ask=ask, timestamp=timestamp,
                max_age_seconds=max_age, max_spread_bps=max_spread,
            ),
        ) from exc
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    if not math.isfinite(bid_value) or not math.isfinite(ask_value) or bid_value <= 0 or ask_value <= 0:
        raise QuoteValidationError(
            "quote_nonpositive_or_nonfinite",
            "quote bid and ask must be finite and positive",
            evidence=_quote_evidence(
                symbol=symbol, feed=feed, bid=bid_value, ask=ask_value, timestamp=timestamp,
                max_age_seconds=max_age, max_spread_bps=max_spread,
            ),
        )
    if bid_value > ask_value:
        raise QuoteValidationError(
            "quote_crossed",
            "quote is crossed",
            evidence=_quote_evidence(
                symbol=symbol, feed=feed, bid=bid_value, ask=ask_value, timestamp=timestamp,
                max_age_seconds=max_age, max_spread_bps=max_spread,
            ),
        )
    if timestamp is None:
        raise QuoteValidationError(
            "quote_timestamp_unavailable",
            "quote timestamp is unavailable",
            evidence=_quote_evidence(
                symbol=symbol, feed=feed, bid=bid_value, ask=ask_value,
                max_age_seconds=max_age, max_spread_bps=max_spread,
            ),
        )
    age = (current - timestamp).total_seconds()
    if age < -5 or age > max_age:
        code = "quote_from_future" if age < -5 else "quote_stale"
        raise QuoteValidationError(
            code,
            f"quote is stale or from the future (age={age:.3f}s)",
            evidence=_quote_evidence(
                symbol=symbol, feed=feed, bid=bid_value, ask=ask_value, timestamp=timestamp,
                age_seconds=age, max_age_seconds=max_age, max_spread_bps=max_spread,
            ),
        )
    midpoint = (bid_value + ask_value) / 2.0
    spread_bps = (ask_value - bid_value) / midpoint * 10000.0
    if not math.isfinite(spread_bps) or spread_bps < 0 or spread_bps > max_spread:
        raise QuoteValidationError(
            "spread_exceeds_configured_limit",
            f"quote spread {spread_bps:.3f} bps is outside the configured {max_spread:.3f} bps bound",
            evidence=_quote_evidence(
                symbol=symbol, feed=feed, bid=bid_value, ask=ask_value, timestamp=timestamp,
                age_seconds=age, spread_bps=spread_bps, max_age_seconds=max_age,
                max_spread_bps=max_spread,
            ),
        )
    return {
        "symbol": symbol.upper(), "bid": bid_value, "ask": ask_value,
        "midpoint": midpoint, "timestamp": timestamp.isoformat(),
        "age_seconds": age, "spread_bps": spread_bps, "source": source,
        "feed": feed,
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
    if source != "alpaca_quote":
        raise ValueError("normal order quote source is not authoritative")
    feed = str(payload.get("quote_feed") or "").lower()
    expected_feed = str(((config.get("alpaca", {}) or {}).get("equity_realtime_data_feed") or "")).lower()
    if expected_feed and feed != expected_feed:
        raise ValueError("persisted quote feed does not match current configuration")
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
    quote["feed"] = feed or "unspecified"
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
