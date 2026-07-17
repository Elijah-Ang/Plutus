"""Fingerprint fresh Alpaca crypto quote, trade and order-book evidence."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from .approval_authority import canonical_json
from .crypto_capabilities import (
    CRYPTO_DATA_FEED,
    CRYPTO_PROVIDER,
    CryptoCapabilitySnapshot,
    CryptoCapabilityStore,
)
from .formula_versions import (
    CRYPTO_MARKET_DATA_FORMULA_VERSION,
    CRYPTO_MARKET_DATA_SCHEMA_VERSION,
)
from .utils import iso_now, json_dumps


def _hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _value(value: Any, *names: str) -> Any:
    for name in names:
        if isinstance(value, Mapping) and name in value:
            return value[name]
        if not isinstance(value, Mapping) and hasattr(value, name):
            return getattr(value, name)
    return None


def _decimal(value: Any, label: str, failures: list[str], *, positive: bool = True) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        failures.append(f"{label}_missing_or_invalid")
        return None
    if not number.is_finite() or (positive and number <= 0) or (not positive and number < 0):
        failures.append(f"{label}_not_finite_positive" if positive else f"{label}_not_finite_nonnegative")
        return None
    return number


def _text(value: Decimal | None) -> str | None:
    return format(value.normalize(), "f") if value is not None else None


def _utc(value: Any, label: str, failures: list[str]) -> datetime | None:
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        failures.append(f"{label}_timestamp_missing_or_invalid")
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _fresh(
    timestamp: datetime | None,
    now: datetime,
    maximum_age_seconds: Decimal,
    label: str,
    failures: list[str],
) -> str | None:
    if timestamp is None:
        return None
    age = Decimal(str((now - timestamp).total_seconds()))
    if age < Decimal("-1"):
        failures.append(f"{label}_timestamp_in_future")
    elif age > maximum_age_seconds:
        failures.append(f"{label}_stale")
    return _text(age)


def _top(levels: Any) -> Any | None:
    if isinstance(levels, Sequence) and not isinstance(levels, (str, bytes)) and levels:
        return levels[0]
    return None


@dataclass(frozen=True)
class CryptoMarketEvidence:
    id: str
    run_id: str
    research_run_id: str
    capability_snapshot_id: str
    capability_snapshot_fingerprint: str
    symbol: str
    bid_price: str | None
    ask_price: str | None
    bid_size: str | None
    ask_size: str | None
    quote_timestamp: str | None
    trade_price: str | None
    trade_size: str | None
    trade_timestamp: str | None
    orderbook_bid_price: str | None
    orderbook_ask_price: str | None
    orderbook_bid_size: str | None
    orderbook_ask_size: str | None
    orderbook_timestamp: str | None
    spread_bps: str | None
    top_of_book_notional: str | None
    authoritative: bool
    execution_eligible: bool
    failure_reasons: tuple[str, ...]
    warnings: tuple[str, ...]
    captured_at: str
    config_hash: str
    evidence_fingerprint: str


def apply_crypto_market_data_schema(conn: Any, *, record_migration: bool = True) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS crypto_market_data_evidence(
          id TEXT PRIMARY KEY,
          run_id TEXT NOT NULL,
          research_run_id TEXT NOT NULL,
          capability_snapshot_id TEXT NOT NULL,
          capability_snapshot_fingerprint TEXT NOT NULL,
          symbol TEXT NOT NULL,
          provider TEXT NOT NULL CHECK(provider='alpaca'),
          data_feed TEXT NOT NULL CHECK(data_feed='us'),
          bid_price TEXT,ask_price TEXT,bid_size TEXT,ask_size TEXT,
          quote_timestamp TEXT,quote_age_seconds TEXT,
          trade_price TEXT,trade_size TEXT,trade_timestamp TEXT,trade_age_seconds TEXT,
          orderbook_bid_price TEXT,orderbook_ask_price TEXT,
          orderbook_bid_size TEXT,orderbook_ask_size TEXT,
          orderbook_timestamp TEXT,orderbook_age_seconds TEXT,
          spread_bps TEXT,top_of_book_notional TEXT,
          authoritative INTEGER NOT NULL CHECK(authoritative IN (0,1)),
          execution_eligible INTEGER NOT NULL CHECK(execution_eligible IN (0,1)),
          failure_reasons_json TEXT NOT NULL,warnings_json TEXT NOT NULL,
          config_hash TEXT NOT NULL,formula_version TEXT NOT NULL,schema_version TEXT NOT NULL,
          captured_at TEXT NOT NULL,evidence_json TEXT NOT NULL,
          evidence_fingerprint TEXT NOT NULL UNIQUE,
          FOREIGN KEY(capability_snapshot_id) REFERENCES crypto_capability_snapshots(id),
          UNIQUE(research_run_id,symbol)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_crypto_market_evidence_symbol ON crypto_market_data_evidence(symbol,captured_at)"
    )
    existing = {str(row[1]) for row in conn.execute("PRAGMA table_info(crypto_research_snapshots)").fetchall()}
    for column, definition in (
        ("market_evidence_id", "TEXT"),
        ("market_evidence_fingerprint", "TEXT"),
        ("market_evidence_authoritative", "INTEGER"),
        ("market_execution_eligible", "INTEGER"),
    ):
        if column not in existing:
            conn.execute(f"ALTER TABLE crypto_research_snapshots ADD COLUMN {column} {definition}")
    if record_migration:
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version,applied_at,detail) VALUES(?,?,?)",
            (
                CRYPTO_MARKET_DATA_SCHEMA_VERSION,
                iso_now(),
                "fresh Alpaca US crypto quote, trade and order-book evidence",
            ),
        )


def _book_parts(orderbook: Any) -> tuple[Any | None, Any | None, Any]:
    bids = _value(orderbook, "bids", "b")
    asks = _value(orderbook, "asks", "a")
    return _top(bids), _top(asks), _value(orderbook, "timestamp", "t")


def _verify_derived_payload(payload: Mapping[str, Any], config: Mapping[str, Any]) -> None:
    """Recompute data-health classification and economics from persisted evidence."""

    captured_failures: list[str] = []
    captured_warnings: list[str] = []
    bid = _decimal(payload.get("bid_price"), "quote_bid_price", captured_failures)
    ask = _decimal(payload.get("ask_price"), "quote_ask_price", captured_failures)
    bid_size = _decimal(payload.get("bid_size"), "quote_bid_size", captured_failures)
    ask_size = _decimal(payload.get("ask_size"), "quote_ask_size", captured_failures)
    quote_ts = _utc(payload.get("quote_timestamp"), "quote", captured_failures)
    trade_price = _decimal(payload.get("trade_price"), "trade_price", captured_warnings)
    trade_size = _decimal(payload.get("trade_size"), "trade_size", captured_warnings)
    trade_ts = _utc(payload.get("trade_timestamp"), "trade", captured_warnings)
    book_bid = _decimal(payload.get("orderbook_bid_price"), "orderbook_bid_price", captured_failures)
    book_ask = _decimal(payload.get("orderbook_ask_price"), "orderbook_ask_price", captured_failures)
    book_bid_size = _decimal(payload.get("orderbook_bid_size"), "orderbook_bid_size", captured_failures)
    book_ask_size = _decimal(payload.get("orderbook_ask_size"), "orderbook_ask_size", captured_failures)
    book_ts = _utc(payload.get("orderbook_timestamp"), "orderbook", captured_failures)
    captured_at = _utc(payload.get("captured_at"), "captured", captured_failures)
    if captured_at is None:
        raise RuntimeError("crypto market evidence capture timestamp is invalid")

    cfg = config.get("crypto") or {}
    max_age = _decimal(
        cfg.get("max_price_age_seconds"), "maximum_price_age_seconds", captured_failures
    ) or Decimal("0")
    quote_age = _fresh(quote_ts, captured_at, max_age, "quote", captured_failures)
    book_age = _fresh(book_ts, captured_at, max_age, "orderbook", captured_failures)
    trade_age = _fresh(trade_ts, captured_at, max_age, "trade", captured_warnings)
    for key, value in (
        ("quote_age_seconds", quote_age),
        ("trade_age_seconds", trade_age),
        ("orderbook_age_seconds", book_age),
    ):
        if payload.get(key) != value:
            raise RuntimeError(f"crypto market evidence derived age mismatch: {key}")

    spread_bps: Decimal | None = None
    if bid is not None and ask is not None:
        if ask < bid:
            captured_failures.append("quote_market_crossed")
        else:
            midpoint = (bid + ask) / Decimal("2")
            spread_bps = (ask - bid) / midpoint * Decimal("10000") if midpoint > 0 else None
    if book_bid is not None and book_ask is not None and book_ask < book_bid:
        captured_failures.append("orderbook_market_crossed")
    depth: Decimal | None = None
    if None not in {book_bid, book_ask, book_bid_size, book_ask_size}:
        depth = min(book_bid * book_bid_size, book_ask * book_ask_size)  # type: ignore[operator]
    if payload.get("spread_bps") != _text(spread_bps):
        raise RuntimeError("crypto market evidence derived spread mismatch")
    if payload.get("top_of_book_notional") != _text(depth):
        raise RuntimeError("crypto market evidence derived depth mismatch")

    maximum_spread = _decimal(cfg.get("max_spread_bps"), "maximum_spread_bps", captured_failures)
    minimum_depth = _decimal(
        cfg.get("minimum_top_of_book_notional_usd"),
        "minimum_top_of_book_notional_usd",
        captured_failures,
    )
    expected_blockers: list[str] = []
    if spread_bps is not None and maximum_spread is not None and spread_bps > maximum_spread:
        expected_blockers.append("spread_exceeds_configured_limit")
    if depth is not None and minimum_depth is not None and depth < minimum_depth:
        expected_blockers.append("top_of_book_liquidity_below_configured_minimum")
    if depth is None:
        expected_blockers.append("top_of_book_liquidity_unavailable")

    persisted_failures = payload.get("failure_reasons")
    persisted_warnings = payload.get("warnings")
    persisted_blockers = payload.get("execution_blockers")
    if not all(isinstance(value, list) for value in (
        persisted_failures, persisted_warnings, persisted_blockers
    )):
        raise RuntimeError("crypto market evidence classification shape is invalid")
    if not set(captured_failures).issubset(set(persisted_failures)):
        raise RuntimeError("crypto market evidence derived failure classification mismatch")
    if not set(captured_warnings).issubset(set(persisted_warnings)):
        raise RuntimeError("crypto market evidence derived warning classification mismatch")
    if sorted(set(expected_blockers)) != sorted(persisted_blockers):
        raise RuntimeError("crypto market evidence execution blocker classification mismatch")
    expected_authoritative = not persisted_failures
    expected_eligible = expected_authoritative and not persisted_blockers
    if payload.get("authoritative") is not expected_authoritative:
        raise RuntimeError("crypto market evidence authority classification mismatch")
    if payload.get("execution_eligible") is not expected_eligible:
        raise RuntimeError("crypto market evidence eligibility classification mismatch")

    # Silence type-check-only names while documenting that both sides of an
    # authoritative quote were deliberately parsed and validated above.
    _ = bid_size, ask_size, trade_price, trade_size


class CryptoMarketDataStore:
    def __init__(self, storage: Any) -> None:
        self.storage = storage

    def capture(
        self,
        config: Mapping[str, Any],
        broker: Any | None,
        capability: CryptoCapabilitySnapshot,
        run_id: str,
        research_run_id: str,
        symbol: str,
        *,
        now: datetime | None = None,
    ) -> CryptoMarketEvidence:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        symbol = str(symbol or "").strip().upper()
        cfg = config.get("crypto") or {}
        failures: list[str] = []
        warnings: list[str] = []
        asset = capability.asset(symbol)
        if not capability.authoritative or asset is None or not asset.authoritative:
            failures.append("capability_snapshot_not_authoritative_for_symbol")
        if capability.config_hash != str(config.get("effective_config_hash") or ""):
            failures.append("capability_configuration_identity_mismatch")
        config_hash = str(config.get("effective_config_hash") or "").strip()
        if not config_hash:
            failures.append("configuration_hash_missing")
        if str((config.get("formula_versions") or {}).get("crypto_market_data") or "") != CRYPTO_MARKET_DATA_FORMULA_VERSION:
            failures.append("crypto_market_data_formula_identity_mismatch")
        if str(cfg.get("data_source") or "") != CRYPTO_PROVIDER or str(cfg.get("data_feed") or "") != CRYPTO_DATA_FEED:
            failures.append("crypto_data_provider_or_feed_mismatch")

        responses: dict[str, Any] = {"quote": None, "trade": None, "orderbook": None}
        operations = (
            ("quote", "get_crypto_latest_quote"),
            ("trade", "get_crypto_latest_trade"),
            ("orderbook", "get_crypto_latest_orderbook"),
        )
        for label, method_name in operations:
            if broker is None or not hasattr(broker, method_name):
                (warnings if label == "trade" else failures).append(f"{label}_provider_missing")
                continue
            try:
                responses[label] = getattr(broker, method_name)(symbol)
            except Exception as exc:
                (warnings if label == "trade" else failures).append(f"{label}_provider_unavailable:{type(exc).__name__}")

        quote = responses["quote"]
        bid = _decimal(_value(quote, "bid_price", "bp"), "quote_bid_price", failures)
        ask = _decimal(_value(quote, "ask_price", "ap"), "quote_ask_price", failures)
        bid_size = _decimal(_value(quote, "bid_size", "bs"), "quote_bid_size", failures)
        ask_size = _decimal(_value(quote, "ask_size", "as"), "quote_ask_size", failures)
        quote_ts = _utc(_value(quote, "timestamp", "t"), "quote", failures)

        trade = responses["trade"]
        trade_failures: list[str] = []
        trade_price = _decimal(_value(trade, "price", "p"), "trade_price", trade_failures)
        trade_size = _decimal(_value(trade, "size", "s"), "trade_size", trade_failures)
        trade_ts = _utc(_value(trade, "timestamp", "t"), "trade", trade_failures)
        warnings.extend(trade_failures)

        top_bid, top_ask, book_time = _book_parts(responses["orderbook"])
        book_bid = _decimal(_value(top_bid, "price", "p"), "orderbook_bid_price", failures)
        book_ask = _decimal(_value(top_ask, "price", "p"), "orderbook_ask_price", failures)
        book_bid_size = _decimal(_value(top_bid, "size", "s"), "orderbook_bid_size", failures)
        book_ask_size = _decimal(_value(top_ask, "size", "s"), "orderbook_ask_size", failures)
        book_ts = _utc(book_time, "orderbook", failures)

        max_age = _decimal(cfg.get("max_price_age_seconds"), "maximum_price_age_seconds", failures)
        max_age = max_age or Decimal("0")
        quote_age = _fresh(quote_ts, current, max_age, "quote", failures)
        book_age = _fresh(book_ts, current, max_age, "orderbook", failures)
        trade_age = _fresh(trade_ts, current, max_age, "trade", warnings)

        spread_bps: Decimal | None = None
        if bid is not None and ask is not None:
            if ask < bid:
                failures.append("quote_market_crossed")
            else:
                midpoint = (bid + ask) / Decimal("2")
                spread_bps = (ask - bid) / midpoint * Decimal("10000") if midpoint > 0 else None
        if book_bid is not None and book_ask is not None and book_ask < book_bid:
            failures.append("orderbook_market_crossed")

        depth: Decimal | None = None
        if None not in {book_bid, book_ask, book_bid_size, book_ask_size}:
            depth = min(book_bid * book_bid_size, book_ask * book_ask_size)  # type: ignore[operator]

        execution_blockers: list[str] = []
        maximum_spread = _decimal(cfg.get("max_spread_bps"), "maximum_spread_bps", failures)
        minimum_depth = _decimal(
            cfg.get("minimum_top_of_book_notional_usd"), "minimum_top_of_book_notional_usd", failures
        )
        if spread_bps is not None and maximum_spread is not None and spread_bps > maximum_spread:
            execution_blockers.append("spread_exceeds_configured_limit")
        if depth is not None and minimum_depth is not None and depth < minimum_depth:
            execution_blockers.append("top_of_book_liquidity_below_configured_minimum")
        if depth is None:
            execution_blockers.append("top_of_book_liquidity_unavailable")

        failures = sorted(set(failures))
        warnings = sorted(set(warnings))
        execution_blockers = sorted(set(execution_blockers))
        authoritative = not failures
        execution_eligible = authoritative and not execution_blockers
        evidence_id = str(uuid.uuid4())
        payload = {
            "id": evidence_id,
            "run_id": str(run_id),
            "research_run_id": str(research_run_id),
            "capability_snapshot_id": capability.id,
            "capability_snapshot_fingerprint": capability.snapshot_fingerprint,
            "symbol": symbol,
            "provider": CRYPTO_PROVIDER,
            "data_feed": CRYPTO_DATA_FEED,
            "bid_price": _text(bid),
            "ask_price": _text(ask),
            "bid_size": _text(bid_size),
            "ask_size": _text(ask_size),
            "quote_timestamp": quote_ts.isoformat() if quote_ts else None,
            "quote_age_seconds": quote_age,
            "trade_price": _text(trade_price),
            "trade_size": _text(trade_size),
            "trade_timestamp": trade_ts.isoformat() if trade_ts else None,
            "trade_age_seconds": trade_age,
            "orderbook_bid_price": _text(book_bid),
            "orderbook_ask_price": _text(book_ask),
            "orderbook_bid_size": _text(book_bid_size),
            "orderbook_ask_size": _text(book_ask_size),
            "orderbook_timestamp": book_ts.isoformat() if book_ts else None,
            "orderbook_age_seconds": book_age,
            "spread_bps": _text(spread_bps),
            "top_of_book_notional": _text(depth),
            "authoritative": authoritative,
            "execution_eligible": execution_eligible,
            "failure_reasons": failures,
            "execution_blockers": execution_blockers,
            "warnings": warnings,
            "config_hash": config_hash,
            "formula_version": CRYPTO_MARKET_DATA_FORMULA_VERSION,
            "schema_version": CRYPTO_MARKET_DATA_SCHEMA_VERSION,
            "captured_at": current.isoformat(),
        }
        fingerprint = _hash(payload)
        with self.storage.connect() as conn:
            apply_crypto_market_data_schema(conn, record_migration=False)
            conn.execute(
                """
                INSERT INTO crypto_market_data_evidence(
                  id,run_id,research_run_id,capability_snapshot_id,capability_snapshot_fingerprint,
                  symbol,provider,data_feed,bid_price,ask_price,bid_size,ask_size,
                  quote_timestamp,quote_age_seconds,trade_price,trade_size,trade_timestamp,
                  trade_age_seconds,orderbook_bid_price,orderbook_ask_price,orderbook_bid_size,
                  orderbook_ask_size,orderbook_timestamp,orderbook_age_seconds,spread_bps,
                  top_of_book_notional,authoritative,execution_eligible,failure_reasons_json,
                  warnings_json,config_hash,formula_version,schema_version,captured_at,
                  evidence_json,evidence_fingerprint
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    evidence_id, str(run_id), str(research_run_id), capability.id,
                    capability.snapshot_fingerprint, symbol, CRYPTO_PROVIDER, CRYPTO_DATA_FEED,
                    payload["bid_price"], payload["ask_price"], payload["bid_size"], payload["ask_size"],
                    payload["quote_timestamp"], quote_age, payload["trade_price"], payload["trade_size"],
                    payload["trade_timestamp"], trade_age, payload["orderbook_bid_price"],
                    payload["orderbook_ask_price"], payload["orderbook_bid_size"],
                    payload["orderbook_ask_size"], payload["orderbook_timestamp"], book_age,
                    payload["spread_bps"], payload["top_of_book_notional"], int(authoritative),
                    int(execution_eligible), json_dumps(failures + execution_blockers),
                    json_dumps(warnings), payload["config_hash"], CRYPTO_MARKET_DATA_FORMULA_VERSION,
                    CRYPTO_MARKET_DATA_SCHEMA_VERSION, current.isoformat(), json_dumps(payload), fingerprint,
                ),
            )
        return CryptoMarketEvidence(
            id=evidence_id,
            run_id=str(run_id),
            research_run_id=str(research_run_id),
            capability_snapshot_id=capability.id,
            capability_snapshot_fingerprint=capability.snapshot_fingerprint,
            symbol=symbol,
            bid_price=payload["bid_price"], ask_price=payload["ask_price"],
            bid_size=payload["bid_size"], ask_size=payload["ask_size"],
            quote_timestamp=payload["quote_timestamp"], trade_price=payload["trade_price"],
            trade_size=payload["trade_size"], trade_timestamp=payload["trade_timestamp"],
            orderbook_bid_price=payload["orderbook_bid_price"],
            orderbook_ask_price=payload["orderbook_ask_price"],
            orderbook_bid_size=payload["orderbook_bid_size"],
            orderbook_ask_size=payload["orderbook_ask_size"],
            orderbook_timestamp=payload["orderbook_timestamp"], spread_bps=payload["spread_bps"],
            top_of_book_notional=payload["top_of_book_notional"], authoritative=authoritative,
            execution_eligible=execution_eligible,
            failure_reasons=tuple(failures + execution_blockers), warnings=tuple(warnings),
            captured_at=current.isoformat(), config_hash=payload["config_hash"],
            evidence_fingerprint=fingerprint,
        )

    def load_verified(self, evidence_id: str, config: Mapping[str, Any]) -> CryptoMarketEvidence:
        rows = self.storage.fetch_all("SELECT * FROM crypto_market_data_evidence WHERE id=?", (evidence_id,))
        if len(rows) != 1:
            raise RuntimeError("crypto market evidence is missing or duplicated")
        row = dict(rows[0])
        try:
            payload = json.loads(row["evidence_json"])
            failures = json.loads(row["failure_reasons_json"])
            warnings = json.loads(row["warnings_json"])
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("crypto market evidence JSON is invalid") from exc
        if not isinstance(payload, dict) or not isinstance(failures, list) or not isinstance(warnings, list):
            raise RuntimeError("crypto market evidence shape is invalid")
        if _hash(payload) != row["evidence_fingerprint"]:
            raise RuntimeError("crypto market evidence fingerprint mismatch")
        column_map = {
            key: row[key]
            for key in (
                "id", "run_id", "research_run_id", "capability_snapshot_id",
                "capability_snapshot_fingerprint", "symbol", "provider", "data_feed",
                "bid_price", "ask_price", "bid_size", "ask_size", "quote_timestamp",
                "quote_age_seconds", "trade_price", "trade_size", "trade_timestamp",
                "trade_age_seconds", "orderbook_bid_price", "orderbook_ask_price",
                "orderbook_bid_size", "orderbook_ask_size", "orderbook_timestamp",
                "orderbook_age_seconds", "spread_bps", "top_of_book_notional", "config_hash",
                "formula_version", "schema_version", "captured_at",
            )
        }
        column_map.update({
            "authoritative": bool(row["authoritative"]),
            "execution_eligible": bool(row["execution_eligible"]),
        })
        for key, value in column_map.items():
            if payload.get(key) != value:
                raise RuntimeError(f"crypto market evidence persisted column mismatch: {key}")
        combined = sorted(payload.get("failure_reasons", []) + payload.get("execution_blockers", []))
        if sorted(failures) != combined or warnings != payload.get("warnings"):
            raise RuntimeError("crypto market evidence reason binding mismatch")
        if row["config_hash"] != str(config.get("effective_config_hash") or ""):
            raise RuntimeError("crypto market evidence configuration identity changed")
        if str((config.get("formula_versions") or {}).get("crypto_market_data") or "") != CRYPTO_MARKET_DATA_FORMULA_VERSION:
            raise RuntimeError("current crypto market-data formula identity changed")
        if row["formula_version"] != CRYPTO_MARKET_DATA_FORMULA_VERSION or row["schema_version"] != CRYPTO_MARKET_DATA_SCHEMA_VERSION:
            raise RuntimeError("crypto market evidence version is obsolete")
        if (
            row["provider"] != CRYPTO_PROVIDER
            or row["data_feed"] != CRYPTO_DATA_FEED
            or str((config.get("crypto") or {}).get("data_source") or "") != CRYPTO_PROVIDER
            or str((config.get("crypto") or {}).get("data_feed") or "") != CRYPTO_DATA_FEED
        ):
            raise RuntimeError("crypto market evidence provider identity changed")
        capability_rows = self.storage.fetch_all(
            """SELECT snapshot_fingerprint,authoritative,config_hash
               FROM crypto_capability_snapshots WHERE id=?""",
            (row["capability_snapshot_id"],),
        )
        if len(capability_rows) != 1:
            raise RuntimeError("crypto market evidence capability relationship is invalid")
        capability_row = capability_rows[0]
        if (
            capability_row["snapshot_fingerprint"] != row["capability_snapshot_fingerprint"]
            or capability_row["config_hash"] != row["config_hash"]
            or (bool(row["authoritative"]) and not bool(capability_row["authoritative"]))
        ):
            raise RuntimeError("crypto market evidence capability binding mismatch")
        if bool(row["authoritative"]):
            capability = CryptoCapabilityStore(self.storage).load_verified(
                row["capability_snapshot_id"],
                config,
                now=datetime.fromisoformat(str(row["captured_at"]).replace("Z", "+00:00")),
            )
            if capability.snapshot_fingerprint != row["capability_snapshot_fingerprint"]:
                raise RuntimeError("crypto market evidence capability authority changed")
        _verify_derived_payload(payload, config)
        return CryptoMarketEvidence(
            id=row["id"], run_id=row["run_id"], research_run_id=row["research_run_id"],
            capability_snapshot_id=row["capability_snapshot_id"],
            capability_snapshot_fingerprint=row["capability_snapshot_fingerprint"], symbol=row["symbol"],
            bid_price=row["bid_price"], ask_price=row["ask_price"], bid_size=row["bid_size"],
            ask_size=row["ask_size"], quote_timestamp=row["quote_timestamp"], trade_price=row["trade_price"],
            trade_size=row["trade_size"], trade_timestamp=row["trade_timestamp"],
            orderbook_bid_price=row["orderbook_bid_price"], orderbook_ask_price=row["orderbook_ask_price"],
            orderbook_bid_size=row["orderbook_bid_size"], orderbook_ask_size=row["orderbook_ask_size"],
            orderbook_timestamp=row["orderbook_timestamp"], spread_bps=row["spread_bps"],
            top_of_book_notional=row["top_of_book_notional"], authoritative=bool(row["authoritative"]),
            execution_eligible=bool(row["execution_eligible"]), failure_reasons=tuple(failures),
            warnings=tuple(warnings), captured_at=row["captured_at"], config_hash=row["config_hash"],
            evidence_fingerprint=row["evidence_fingerprint"],
        )


__all__ = [
    "CryptoMarketDataStore",
    "CryptoMarketEvidence",
    "apply_crypto_market_data_schema",
]
