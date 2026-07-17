"""Authoritative Alpaca spot-crypto capability evidence.

The static API contract in this module is versioned from primary Alpaca
documentation.  Pair-specific tradability and precision are deliberately not
hard-coded: they are re-read from the authenticated paper Trading API and
persisted in an immutable, fingerprinted snapshot.

This module is read-only with respect to Alpaca.  It cannot build or submit an
order.  Crypto execution remains disabled until later, separately reviewed
stages bind sizing, risk, display, approval, intent and reconciliation to this
evidence.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from .approval_authority import canonical_json
from .formula_versions import (
    CRYPTO_CAPABILITY_FORMULA_VERSION,
    CRYPTO_CAPABILITY_SCHEMA_VERSION,
)
from .utils import iso_now, json_dumps


CRYPTO_OFFICIAL_CONTRACT_VERSION = "alpaca_spot_crypto_contract_2026_07_17"
CRYPTO_DATA_FEED = "us"
CRYPTO_MARKET_PROFILE = "continuous_24_7"
CRYPTO_PROVIDER = "alpaca"
CRYPTO_BROKER = "alpaca_paper_spot"
SUPPORTED_ORDER_TYPES = ("limit", "market", "stop_limit")
SUPPORTED_TIME_IN_FORCE = ("gtc", "ioc")
SUPPORTED_REQUEST_BASES = ("notional", "quantity")
TERMINAL_ORDER_STATES = ("canceled", "expired", "filled", "rejected")
AMBIGUOUS_OR_ACTIVE_ORDER_STATES = (
    "accepted",
    "accepted_for_bidding",
    "calculated",
    "done_for_day",
    "held",
    "new",
    "partially_filled",
    "pending_cancel",
    "pending_new",
    "pending_replace",
    "pending_review",
    "replaced",
    "stopped",
    "suspended",
)
STABLECOIN_BASES = frozenset({"DAI", "PAXG", "USDC", "USDT"})

OFFICIAL_SOURCES = (
    "https://docs.alpaca.markets/us/docs/crypto-trading",
    "https://docs.alpaca.markets/us/docs/crypto-orders",
    "https://docs.alpaca.markets/us/docs/crypto-fees",
    "https://docs.alpaca.markets/us/docs/paper-trading",
    "https://docs.alpaca.markets/us/docs/historical-crypto-data-1",
    "https://docs.alpaca.markets/us/docs/real-time-crypto-pricing-data",
    "https://alpaca.markets/sdks/python/api_reference/data/crypto/historical.html",
    "https://alpaca.markets/sdks/python/api_reference/trading/models.html",
)


def _hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _enum_value(value: Any) -> str:
    raw = getattr(value, "value", value)
    return str(raw or "").strip()


def _bool(value: Any) -> bool:
    return value is True


def _decimal_text(value: Any, label: str) -> str:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{label} is missing or invalid") from exc
    if not number.is_finite() or number <= 0:
        raise ValueError(f"{label} must be finite and positive")
    return format(number.normalize(), "f")


def _utc(value: Any) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _value(value: Any, key: str, default: Any = None) -> Any:
    return value.get(key, default) if isinstance(value, Mapping) else getattr(value, key, default)


def _normalize_symbol(value: Any) -> str:
    return str(value or "").strip().upper()


def _official_contract() -> dict[str, Any]:
    """Return the exact static protocol contract used by this build."""

    return {
        "version": CRYPTO_OFFICIAL_CONTRACT_VERSION,
        "provider": CRYPTO_PROVIDER,
        "broker": CRYPTO_BROKER,
        "paper_support": True,
        "spot_only": True,
        "asset_exchange": "CRYPTO",
        "long_only": True,
        "leverage": False,
        "borrowing": False,
        "derivatives": False,
        "manual_approval_required": True,
        "autonomous_execution": False,
        "market_profile": CRYPTO_MARKET_PROFILE,
        "trading_hours": "24_hours_7_days",
        "data_feed": CRYPTO_DATA_FEED,
        "data_types": ["bars", "orderbooks", "quotes", "trades"],
        "bar_semantics": "trade_or_quote_midpoint; zero volume may still have price",
        "order_types": list(SUPPORTED_ORDER_TYPES),
        "time_in_force": list(SUPPORTED_TIME_IN_FORCE),
        "request_bases": list(SUPPORTED_REQUEST_BASES),
        "qty_and_notional_mutually_exclusive": True,
        "pair_precision_source": "authenticated_assets_api_current_response",
        "client_order_id_supported": True,
        "client_order_id_max_length": 128,
        "cancellation_supported": True,
        "partial_fills_possible": True,
        "paper_fill_model_warning": "simulation ignores queue position, market impact and latency slippage",
        "terminal_order_states": list(TERMINAL_ORDER_STATES),
        "active_or_ambiguous_order_states": list(AMBIGUOUS_OR_ACTIVE_ORDER_STATES),
        "fees": {
            "tier_1_maker_bps": 15,
            "tier_1_taker_bps": 25,
            "charged_to_received_asset": True,
            "activity_types": ["CFEE", "FEE"],
            "posting_lag": "end_of_day_possible",
        },
        "maintenance": {
            "published_execution_calendar_available": False,
            "policy": "fail_closed_on_asset_provider_or_reconciliation_uncertainty",
        },
        "official_sources": list(OFFICIAL_SOURCES),
    }


OFFICIAL_CONTRACT_FINGERPRINT = _hash(_official_contract())


@dataclass(frozen=True)
class CryptoAssetCapability:
    symbol: str
    asset_id: str
    asset_class: str
    exchange: str
    status: str
    tradable: bool
    fractionable: bool
    marginable: bool
    shortable: bool
    easy_to_borrow: bool
    min_order_size: str | None
    min_trade_increment: str | None
    price_increment: str | None
    base_asset: str
    quote_currency: str
    authoritative: bool
    failure_reasons: tuple[str, ...]
    asset_fingerprint: str

    def payload(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "asset_id": self.asset_id,
            "asset_class": self.asset_class,
            "exchange": self.exchange,
            "status": self.status,
            "tradable": self.tradable,
            "fractionable": self.fractionable,
            "marginable": self.marginable,
            "shortable": self.shortable,
            "easy_to_borrow": self.easy_to_borrow,
            "min_order_size": self.min_order_size,
            "min_trade_increment": self.min_trade_increment,
            "price_increment": self.price_increment,
            "base_asset": self.base_asset,
            "quote_currency": self.quote_currency,
            "authoritative": self.authoritative,
            "failure_reasons": list(self.failure_reasons),
        }


@dataclass(frozen=True)
class CryptoCapabilitySnapshot:
    id: str
    run_id: str
    paper_account_id_hash: str
    assets: tuple[CryptoAssetCapability, ...]
    config_hash: str
    captured_at: str
    expires_at: str
    authoritative: bool
    failure_reasons: tuple[str, ...]
    input_fingerprint: str
    snapshot_fingerprint: str

    def asset(self, symbol: str) -> CryptoAssetCapability | None:
        normalized = _normalize_symbol(symbol)
        return next((asset for asset in self.assets if asset.symbol == normalized), None)


def apply_crypto_capability_schema(conn: Any, *, record_migration: bool = True) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS crypto_capability_snapshots(
          id TEXT PRIMARY KEY,
          run_id TEXT NOT NULL,
          provider TEXT NOT NULL CHECK(provider='alpaca'),
          broker TEXT NOT NULL CHECK(broker='alpaca_paper_spot'),
          trading_mode TEXT NOT NULL CHECK(trading_mode='paper'),
          paper_account_id_hash TEXT NOT NULL,
          market_profile TEXT NOT NULL CHECK(market_profile='continuous_24_7'),
          data_feed TEXT NOT NULL CHECK(data_feed='us'),
          asset_count INTEGER NOT NULL CHECK(asset_count>=0),
          official_contract_version TEXT NOT NULL,
          official_contract_fingerprint TEXT NOT NULL,
          config_hash TEXT NOT NULL,
          formula_version TEXT NOT NULL,
          schema_version TEXT NOT NULL,
          captured_at TEXT NOT NULL,
          expires_at TEXT NOT NULL,
          authoritative INTEGER NOT NULL CHECK(authoritative IN (0,1)),
          failure_reasons_json TEXT NOT NULL,
          evidence_json TEXT NOT NULL,
          input_fingerprint TEXT NOT NULL,
          snapshot_fingerprint TEXT NOT NULL UNIQUE
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS crypto_asset_capabilities(
          id TEXT PRIMARY KEY,
          snapshot_id TEXT NOT NULL,
          symbol TEXT NOT NULL,
          asset_id TEXT NOT NULL,
          asset_class TEXT NOT NULL,
          exchange TEXT NOT NULL,
          status TEXT NOT NULL,
          tradable INTEGER NOT NULL CHECK(tradable IN (0,1)),
          fractionable INTEGER NOT NULL CHECK(fractionable IN (0,1)),
          marginable INTEGER NOT NULL CHECK(marginable IN (0,1)),
          shortable INTEGER NOT NULL CHECK(shortable IN (0,1)),
          easy_to_borrow INTEGER NOT NULL CHECK(easy_to_borrow IN (0,1)),
          min_order_size TEXT,
          min_trade_increment TEXT,
          price_increment TEXT,
          base_asset TEXT NOT NULL,
          quote_currency TEXT NOT NULL,
          authoritative INTEGER NOT NULL CHECK(authoritative IN (0,1)),
          failure_reasons_json TEXT NOT NULL,
          asset_json TEXT NOT NULL,
          asset_fingerprint TEXT NOT NULL,
          UNIQUE(snapshot_id,symbol),
          FOREIGN KEY(snapshot_id) REFERENCES crypto_capability_snapshots(id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_crypto_capability_captured ON crypto_capability_snapshots(captured_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_crypto_asset_capability_symbol ON crypto_asset_capabilities(symbol,snapshot_id)"
    )
    for table in ("crypto_research_runs", "crypto_research_snapshots"):
        existing = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for column, definition in (
            ("capability_snapshot_id", "TEXT"),
            ("capability_snapshot_fingerprint", "TEXT"),
            ("capability_authoritative", "INTEGER"),
        ):
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    if record_migration:
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version,applied_at,detail) VALUES(?,?,?)",
            (
                CRYPTO_CAPABILITY_SCHEMA_VERSION,
                iso_now(),
                "immutable Alpaca paper spot-crypto contract and current Assets API precision evidence",
            ),
        )


def _configured_contract(config: Mapping[str, Any]) -> dict[str, Any]:
    cfg = config.get("crypto") or {}
    contract = cfg.get("capability_contract") or {}
    return {
        "provider": str(cfg.get("data_source") or ""),
        "broker": str(cfg.get("broker") or ""),
        "market_profile": str(cfg.get("market_profile") or ""),
        "data_feed": str(cfg.get("data_feed") or ""),
        "quote_currency": str(cfg.get("quote_currency") or "").upper(),
        "order_types": sorted(map(str, contract.get("order_types") or [])),
        "time_in_force": sorted(map(str, contract.get("time_in_force") or [])),
        "request_bases": sorted(map(str, contract.get("request_bases") or [])),
        "default_time_in_force": str(contract.get("default_time_in_force") or ""),
        "require_asset_api_verification": contract.get("require_asset_api_verification") is True,
        "require_paper_account_identity": contract.get("require_paper_account_identity") is True,
        "maintenance_policy": str(contract.get("maintenance_policy") or ""),
        "weekend_policy": str(contract.get("weekend_policy") or ""),
        "stablecoin_policy": str(contract.get("stablecoin_policy") or ""),
        "formula_version": str((config.get("formula_versions") or {}).get("crypto_capability") or ""),
    }


def _config_failures(config: Mapping[str, Any]) -> list[str]:
    actual = _configured_contract(config)
    expected = {
        "provider": CRYPTO_PROVIDER,
        "broker": CRYPTO_BROKER,
        "market_profile": CRYPTO_MARKET_PROFILE,
        "data_feed": CRYPTO_DATA_FEED,
        "quote_currency": "USD",
        "order_types": sorted(SUPPORTED_ORDER_TYPES),
        "time_in_force": sorted(SUPPORTED_TIME_IN_FORCE),
        "request_bases": sorted(SUPPORTED_REQUEST_BASES),
        "default_time_in_force": "gtc",
        "require_asset_api_verification": True,
        "require_paper_account_identity": True,
        "maintenance_policy": "fail_closed",
        "weekend_policy": "continuous_same_controls",
        "stablecoin_policy": "reject_stablecoin_base_and_non_usd_quote",
        "formula_version": CRYPTO_CAPABILITY_FORMULA_VERSION,
    }
    return [f"configuration_mismatch:{key}" for key in sorted(expected) if actual.get(key) != expected[key]]


def _asset_capability(symbol: str, raw: Any | None) -> CryptoAssetCapability:
    failures: list[str] = []
    expected = _normalize_symbol(symbol)
    raw_symbol = _normalize_symbol(_value(raw, "symbol")) if raw is not None else ""
    asset_id = str(_value(raw, "id") or "").strip() if raw is not None else ""
    asset_class = _enum_value(_value(raw, "asset_class", _value(raw, "class"))).lower() if raw is not None else ""
    exchange = _enum_value(_value(raw, "exchange")).upper() if raw is not None else ""
    status = _enum_value(_value(raw, "status")).lower() if raw is not None else ""
    tradable = _bool(_value(raw, "tradable")) if raw is not None else False
    fractionable = _bool(_value(raw, "fractionable")) if raw is not None else False
    marginable = _bool(_value(raw, "marginable")) if raw is not None else False
    shortable = _bool(_value(raw, "shortable")) if raw is not None else False
    easy_to_borrow = _bool(_value(raw, "easy_to_borrow")) if raw is not None else False
    parts = expected.split("/", 1) if expected.count("/") == 1 else (expected, "")
    base_asset, quote_currency = parts[0], parts[1]

    if raw is None:
        failures.append("asset_missing_from_current_alpaca_crypto_assets")
    if raw_symbol != expected:
        failures.append("asset_symbol_mismatch")
    if not asset_id:
        failures.append("asset_identity_missing")
    if asset_class != "crypto":
        failures.append("asset_class_not_crypto")
    if exchange != "CRYPTO":
        failures.append("asset_exchange_not_crypto")
    if status != "active":
        failures.append("asset_not_active")
    if not tradable:
        failures.append("asset_not_tradable")
    if not fractionable:
        failures.append("asset_not_fractionable")
    if marginable:
        failures.append("asset_unexpectedly_marginable")
    if shortable:
        failures.append("asset_unexpectedly_shortable")
    if easy_to_borrow:
        failures.append("asset_unexpectedly_borrowable")
    if quote_currency != "USD":
        failures.append("quote_currency_not_usd")
    if base_asset in STABLECOIN_BASES:
        failures.append("stablecoin_base_rejected")

    decimals: dict[str, str | None] = {}
    for key in ("min_order_size", "min_trade_increment", "price_increment"):
        try:
            decimals[key] = _decimal_text(_value(raw, key), key) if raw is not None else None
        except ValueError as exc:
            decimals[key] = None
            failures.append(str(exc).replace(" ", "_"))

    normalized = {
        "symbol": expected,
        "asset_id": asset_id,
        "asset_class": asset_class,
        "exchange": exchange,
        "status": status,
        "tradable": tradable,
        "fractionable": fractionable,
        "marginable": marginable,
        "shortable": shortable,
        "easy_to_borrow": easy_to_borrow,
        **decimals,
        "base_asset": base_asset,
        "quote_currency": quote_currency,
        "authoritative": not failures,
        "failure_reasons": sorted(set(failures)),
    }
    fingerprint = _hash(normalized)
    return CryptoAssetCapability(
        symbol=expected,
        asset_id=asset_id,
        asset_class=asset_class,
        exchange=exchange,
        status=status,
        tradable=tradable,
        fractionable=fractionable,
        marginable=marginable,
        shortable=shortable,
        easy_to_borrow=easy_to_borrow,
        min_order_size=decimals["min_order_size"],
        min_trade_increment=decimals["min_trade_increment"],
        price_increment=decimals["price_increment"],
        base_asset=base_asset,
        quote_currency=quote_currency,
        authoritative=not failures,
        failure_reasons=tuple(sorted(set(failures))),
        asset_fingerprint=fingerprint,
    )


def _identity_hash(identity: Mapping[str, Any]) -> str:
    value = str(identity.get("account_id_hash") or "").strip().lower()
    if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
        return ""
    return value


class CryptoCapabilityStore:
    def __init__(self, storage: Any) -> None:
        self.storage = storage

    def capture(
        self,
        config: Mapping[str, Any],
        broker: Any | None,
        run_id: str,
        *,
        now: datetime | None = None,
    ) -> CryptoCapabilitySnapshot:
        now = (now or datetime.now(UTC)).astimezone(UTC)
        cfg = config.get("crypto") or {}
        raw_symbols = tuple(_normalize_symbol(value) for value in (cfg.get("symbols") or []) if value)
        symbols = tuple(dict.fromkeys(raw_symbols))
        failures = _config_failures(config)
        try:
            ttl_minutes = int((cfg.get("capability_contract") or {}).get("snapshot_ttl_minutes"))
        except (TypeError, ValueError):
            ttl_minutes = 0
            failures.append("capability_snapshot_ttl_invalid")
        if not 1 <= ttl_minutes <= 1440:
            failures.append("capability_snapshot_ttl_out_of_range")
        expires = now + timedelta(minutes=max(0, ttl_minutes))
        config_hash = str(config.get("effective_config_hash") or "").strip()
        if not config_hash:
            failures.append("configuration_hash_missing")
        if not symbols:
            failures.append("configured_crypto_symbols_missing")
        if len(raw_symbols) != len(symbols):
            failures.append("duplicate_configured_crypto_symbol")
        if raw_symbols != ("BTC/USD", "ETH/USD"):
            failures.append("stage_one_symbols_must_be_exactly_btc_usd_and_eth_usd")

        identity: Mapping[str, Any] = {}
        if broker is None or not hasattr(broker, "paper_account_identity"):
            failures.append("paper_account_identity_provider_missing")
        else:
            try:
                raw_identity = broker.paper_account_identity()
                identity = raw_identity if isinstance(raw_identity, Mapping) else {}
            except Exception as exc:
                failures.append(f"paper_account_identity_unavailable:{type(exc).__name__}")
        account_hash = _identity_hash(identity)
        if identity.get("verified") is not True:
            failures.append("paper_account_identity_unverified")
        if identity.get("mode") != "paper" or identity.get("endpoint_class") != "paper":
            failures.append("paper_endpoint_identity_mismatch")
        if str(identity.get("account_status") or "").lower() != "active":
            failures.append("paper_account_not_active")
        if str(identity.get("account_currency") or "").upper() != "USD":
            failures.append("paper_account_currency_not_usd")
        if not account_hash:
            failures.append("stable_paper_account_identity_missing")

        raw_assets: Sequence[Any] = ()
        if broker is None or not hasattr(broker, "get_crypto_assets"):
            failures.append("crypto_assets_provider_missing")
        else:
            try:
                fetched = broker.get_crypto_assets()
                raw_assets = tuple(fetched or ())
            except Exception as exc:
                failures.append(f"crypto_assets_unavailable:{type(exc).__name__}")
        by_symbol: dict[str, Any] = {}
        duplicates: set[str] = set()
        for raw in raw_assets:
            symbol = _normalize_symbol(_value(raw, "symbol"))
            if symbol in by_symbol:
                duplicates.add(symbol)
            else:
                by_symbol[symbol] = raw
        if duplicates:
            failures.extend(f"duplicate_asset_identity:{symbol}" for symbol in sorted(duplicates))

        assets = tuple(_asset_capability(symbol, by_symbol.get(symbol)) for symbol in symbols)
        for asset in assets:
            failures.extend(f"{asset.symbol}:{reason}" for reason in asset.failure_reasons)
        failures = sorted(set(failures))

        static_contract = _official_contract()
        configured_contract = _configured_contract(config)
        input_payload = {
            "static_contract": static_contract,
            "static_contract_fingerprint": OFFICIAL_CONTRACT_FINGERPRINT,
            "configured_contract": configured_contract,
            "config_hash": config_hash,
            "paper_identity": {
                "verified": identity.get("verified") is True,
                "mode": str(identity.get("mode") or ""),
                "endpoint_class": str(identity.get("endpoint_class") or ""),
                "account_status": str(identity.get("account_status") or ""),
                "account_currency": str(identity.get("account_currency") or ""),
                "account_id_hash": account_hash,
            },
            "assets": [asset.payload() | {"asset_fingerprint": asset.asset_fingerprint} for asset in assets],
        }
        input_fingerprint = _hash(input_payload)
        snapshot_id = str(uuid.uuid4())
        authoritative = not failures and bool(assets) and all(asset.authoritative for asset in assets)
        snapshot_payload = {
            "id": snapshot_id,
            "run_id": str(run_id),
            "provider": CRYPTO_PROVIDER,
            "broker": CRYPTO_BROKER,
            "trading_mode": "paper",
            "paper_account_id_hash": account_hash,
            "market_profile": CRYPTO_MARKET_PROFILE,
            "data_feed": CRYPTO_DATA_FEED,
            "asset_count": len(assets),
            "official_contract_version": CRYPTO_OFFICIAL_CONTRACT_VERSION,
            "official_contract_fingerprint": OFFICIAL_CONTRACT_FINGERPRINT,
            "config_hash": config_hash,
            "formula_version": CRYPTO_CAPABILITY_FORMULA_VERSION,
            "schema_version": CRYPTO_CAPABILITY_SCHEMA_VERSION,
            "captured_at": now.isoformat(),
            "expires_at": expires.isoformat(),
            "authoritative": authoritative,
            "failure_reasons": failures,
            "input_fingerprint": input_fingerprint,
            "assets": input_payload["assets"],
        }
        snapshot_fingerprint = _hash(snapshot_payload)
        evidence = {"input": input_payload, "snapshot": snapshot_payload}

        with self.storage.connect() as conn:
            apply_crypto_capability_schema(conn, record_migration=False)
            conn.execute(
                """
                INSERT INTO crypto_capability_snapshots(
                  id,run_id,provider,broker,trading_mode,paper_account_id_hash,
                  market_profile,data_feed,asset_count,official_contract_version,
                  official_contract_fingerprint,config_hash,formula_version,
                  schema_version,captured_at,expires_at,authoritative,
                  failure_reasons_json,evidence_json,input_fingerprint,snapshot_fingerprint
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    snapshot_id,
                    str(run_id),
                    CRYPTO_PROVIDER,
                    CRYPTO_BROKER,
                    "paper",
                    account_hash,
                    CRYPTO_MARKET_PROFILE,
                    CRYPTO_DATA_FEED,
                    len(assets),
                    CRYPTO_OFFICIAL_CONTRACT_VERSION,
                    OFFICIAL_CONTRACT_FINGERPRINT,
                    config_hash,
                    CRYPTO_CAPABILITY_FORMULA_VERSION,
                    CRYPTO_CAPABILITY_SCHEMA_VERSION,
                    now.isoformat(),
                    expires.isoformat(),
                    int(authoritative),
                    json_dumps(failures),
                    json_dumps(evidence),
                    input_fingerprint,
                    snapshot_fingerprint,
                ),
            )
            for asset in assets:
                payload = asset.payload()
                conn.execute(
                    """
                    INSERT INTO crypto_asset_capabilities(
                      id,snapshot_id,symbol,asset_id,asset_class,exchange,status,
                      tradable,fractionable,marginable,shortable,easy_to_borrow,
                      min_order_size,min_trade_increment,price_increment,base_asset,
                      quote_currency,authoritative,failure_reasons_json,asset_json,
                      asset_fingerprint
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        str(uuid.uuid4()),
                        snapshot_id,
                        asset.symbol,
                        asset.asset_id,
                        asset.asset_class,
                        asset.exchange,
                        asset.status,
                        int(asset.tradable),
                        int(asset.fractionable),
                        int(asset.marginable),
                        int(asset.shortable),
                        int(asset.easy_to_borrow),
                        asset.min_order_size,
                        asset.min_trade_increment,
                        asset.price_increment,
                        asset.base_asset,
                        asset.quote_currency,
                        int(asset.authoritative),
                        json_dumps(asset.failure_reasons),
                        json_dumps(payload),
                        asset.asset_fingerprint,
                    ),
                )
        return CryptoCapabilitySnapshot(
            id=snapshot_id,
            run_id=str(run_id),
            paper_account_id_hash=account_hash,
            assets=assets,
            config_hash=config_hash,
            captured_at=now.isoformat(),
            expires_at=expires.isoformat(),
            authoritative=authoritative,
            failure_reasons=tuple(failures),
            input_fingerprint=input_fingerprint,
            snapshot_fingerprint=snapshot_fingerprint,
        )

    def load_verified(
        self,
        snapshot_id: str,
        config: Mapping[str, Any],
        *,
        now: datetime | None = None,
        require_authoritative: bool = True,
    ) -> CryptoCapabilitySnapshot:
        rows = self.storage.fetch_all("SELECT * FROM crypto_capability_snapshots WHERE id=?", (snapshot_id,))
        if len(rows) != 1:
            raise RuntimeError("crypto capability snapshot is missing or duplicated")
        row = dict(rows[0])
        asset_rows = [dict(value) for value in self.storage.fetch_all(
            "SELECT * FROM crypto_asset_capabilities WHERE snapshot_id=? ORDER BY symbol", (snapshot_id,)
        )]
        try:
            evidence = json.loads(row["evidence_json"])
            failure_reasons = json.loads(row["failure_reasons_json"])
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("crypto capability snapshot JSON is invalid") from exc
        if not isinstance(evidence, dict) or not isinstance(failure_reasons, list):
            raise RuntimeError("crypto capability snapshot evidence shape is invalid")
        snapshot_payload = evidence.get("snapshot")
        input_payload = evidence.get("input")
        if not isinstance(snapshot_payload, dict) or not isinstance(input_payload, dict):
            raise RuntimeError("crypto capability snapshot evidence is incomplete")
        if _hash(input_payload) != row["input_fingerprint"]:
            raise RuntimeError("crypto capability input fingerprint mismatch")
        if _hash(snapshot_payload) != row["snapshot_fingerprint"]:
            raise RuntimeError("crypto capability snapshot fingerprint mismatch")
        expected_columns = {
            "id": row["id"],
            "run_id": row["run_id"],
            "provider": row["provider"],
            "broker": row["broker"],
            "trading_mode": row["trading_mode"],
            "paper_account_id_hash": row["paper_account_id_hash"],
            "market_profile": row["market_profile"],
            "data_feed": row["data_feed"],
            "asset_count": int(row["asset_count"]),
            "official_contract_version": row["official_contract_version"],
            "official_contract_fingerprint": row["official_contract_fingerprint"],
            "config_hash": row["config_hash"],
            "formula_version": row["formula_version"],
            "schema_version": row["schema_version"],
            "captured_at": row["captured_at"],
            "expires_at": row["expires_at"],
            "authoritative": bool(row["authoritative"]),
            "failure_reasons": failure_reasons,
            "input_fingerprint": row["input_fingerprint"],
        }
        for key, value in expected_columns.items():
            if snapshot_payload.get(key) != value:
                raise RuntimeError(f"crypto capability persisted column mismatch: {key}")
        if row["official_contract_fingerprint"] != OFFICIAL_CONTRACT_FINGERPRINT:
            raise RuntimeError("crypto capability official contract is obsolete")
        if row["formula_version"] != CRYPTO_CAPABILITY_FORMULA_VERSION or row["schema_version"] != CRYPTO_CAPABILITY_SCHEMA_VERSION:
            raise RuntimeError("crypto capability version is obsolete")
        if row["config_hash"] != str(config.get("effective_config_hash") or ""):
            raise RuntimeError("crypto capability configuration identity changed")
        if _config_failures(config):
            raise RuntimeError("current crypto capability configuration is invalid")
        if int(row["asset_count"]) != len(asset_rows):
            raise RuntimeError("crypto capability asset count mismatch")

        assets: list[CryptoAssetCapability] = []
        for asset_row in asset_rows:
            try:
                payload = json.loads(asset_row["asset_json"])
                reasons = json.loads(asset_row["failure_reasons_json"])
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise RuntimeError("crypto asset capability JSON is invalid") from exc
            if not isinstance(payload, dict) or not isinstance(reasons, list):
                raise RuntimeError("crypto asset capability evidence shape is invalid")
            if _hash(payload) != asset_row["asset_fingerprint"]:
                raise RuntimeError("crypto asset capability fingerprint mismatch")
            persisted = {
                "symbol": asset_row["symbol"],
                "asset_id": asset_row["asset_id"],
                "asset_class": asset_row["asset_class"],
                "exchange": asset_row["exchange"],
                "status": asset_row["status"],
                "tradable": bool(asset_row["tradable"]),
                "fractionable": bool(asset_row["fractionable"]),
                "marginable": bool(asset_row["marginable"]),
                "shortable": bool(asset_row["shortable"]),
                "easy_to_borrow": bool(asset_row["easy_to_borrow"]),
                "min_order_size": asset_row["min_order_size"],
                "min_trade_increment": asset_row["min_trade_increment"],
                "price_increment": asset_row["price_increment"],
                "base_asset": asset_row["base_asset"],
                "quote_currency": asset_row["quote_currency"],
                "authoritative": bool(asset_row["authoritative"]),
                "failure_reasons": reasons,
            }
            if payload != persisted:
                raise RuntimeError(f"crypto asset persisted column mismatch: {asset_row['symbol']}")
            assets.append(CryptoAssetCapability(
                **{key: value for key, value in persisted.items() if key != "failure_reasons"},
                failure_reasons=tuple(reasons),
                asset_fingerprint=asset_row["asset_fingerprint"],
            ))
        expected_assets = snapshot_payload.get("assets")
        actual_assets = [asset.payload() | {"asset_fingerprint": asset.asset_fingerprint} for asset in assets]
        if expected_assets != actual_assets:
            raise RuntimeError("crypto capability snapshot/asset binding mismatch")
        paper_identity = input_payload.get("paper_identity")
        if (
            input_payload.get("static_contract") != _official_contract()
            or input_payload.get("static_contract_fingerprint") != OFFICIAL_CONTRACT_FINGERPRINT
            or input_payload.get("configured_contract") != _configured_contract(config)
            or input_payload.get("config_hash") != row["config_hash"]
            or input_payload.get("assets") != actual_assets
            or not isinstance(paper_identity, dict)
            or paper_identity.get("account_id_hash") != row["paper_account_id_hash"]
        ):
            raise RuntimeError("crypto capability input authority binding mismatch")
        authoritative_identity = (
            paper_identity.get("verified") is True
            and paper_identity.get("mode") == "paper"
            and paper_identity.get("endpoint_class") == "paper"
            and str(paper_identity.get("account_status") or "").lower() == "active"
            and str(paper_identity.get("account_currency") or "").upper() == "USD"
            and bool(row["paper_account_id_hash"])
        )
        expected_authoritative = bool(
            not failure_reasons
            and assets
            and all(asset.authoritative for asset in assets)
            and authoritative_identity
        )
        if bool(row["authoritative"]) != expected_authoritative:
            raise RuntimeError("crypto capability authority classification mismatch")
        current = (now or datetime.now(UTC)).astimezone(UTC)
        if _utc(row["captured_at"]) > current + timedelta(seconds=1):
            raise RuntimeError("crypto capability snapshot capture time is in the future")
        if current >= _utc(row["expires_at"]):
            raise RuntimeError("crypto capability snapshot expired")
        if require_authoritative and not bool(row["authoritative"]):
            raise RuntimeError("crypto capability snapshot is not authoritative")
        if require_authoritative and (failure_reasons or not all(asset.authoritative for asset in assets)):
            raise RuntimeError("crypto capability snapshot contains blocking evidence")
        return CryptoCapabilitySnapshot(
            id=row["id"],
            run_id=row["run_id"],
            paper_account_id_hash=row["paper_account_id_hash"],
            assets=tuple(assets),
            config_hash=row["config_hash"],
            captured_at=row["captured_at"],
            expires_at=row["expires_at"],
            authoritative=bool(row["authoritative"]),
            failure_reasons=tuple(failure_reasons),
            input_fingerprint=row["input_fingerprint"],
            snapshot_fingerprint=row["snapshot_fingerprint"],
        )


def crypto_contract_summary() -> dict[str, Any]:
    """Public, side-effect-free contract evidence for reports and tests."""

    return {
        "contract": _official_contract(),
        "contract_fingerprint": OFFICIAL_CONTRACT_FINGERPRINT,
        "formula_version": CRYPTO_CAPABILITY_FORMULA_VERSION,
        "schema_version": CRYPTO_CAPABILITY_SCHEMA_VERSION,
    }


__all__ = [
    "AMBIGUOUS_OR_ACTIVE_ORDER_STATES",
    "CRYPTO_BROKER",
    "CRYPTO_DATA_FEED",
    "CRYPTO_MARKET_PROFILE",
    "CRYPTO_OFFICIAL_CONTRACT_VERSION",
    "CryptoAssetCapability",
    "CryptoCapabilitySnapshot",
    "CryptoCapabilityStore",
    "OFFICIAL_CONTRACT_FINGERPRINT",
    "SUPPORTED_ORDER_TYPES",
    "SUPPORTED_REQUEST_BASES",
    "SUPPORTED_TIME_IN_FORCE",
    "TERMINAL_ORDER_STATES",
    "apply_crypto_capability_schema",
    "crypto_contract_summary",
]
