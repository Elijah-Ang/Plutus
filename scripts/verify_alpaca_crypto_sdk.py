#!/usr/bin/env python3
"""Verify the pinned Alpaca crypto SDK contract without network access."""

from __future__ import annotations

import argparse
import importlib.metadata
import inspect
import json
import re
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
PIN_PATTERN = re.compile(
    r"^alpaca-py==(?P<version>[^\s;]+)"
    r"(?:\s+--hash=sha256:[0-9a-fA-F]{64})+\s*$"
)


def required_alpaca_version(lock_path: Path) -> str:
    """Return the one exact alpaca-py pin from the hash-verified lock."""

    versions = []
    for raw_line in lock_path.read_text(encoding="utf-8").splitlines():
        match = PIN_PATTERN.match(raw_line.strip())
        if match:
            versions.append(match.group("version"))
    if len(versions) != 1:
        raise ValueError(
            "hashed dependency lock must contain exactly one hash-verified alpaca-py pin"
        )
    return versions[0]


def _parameters(value: Any) -> set[str]:
    return set(inspect.signature(value).parameters)


def _require_parameters(label: str, value: Any, required: set[str]) -> list[str]:
    missing = sorted(required - _parameters(value))
    return [f"{label} missing parameters: {', '.join(missing)}"] if missing else []


def _enum_values(value: Any) -> set[str]:
    return {str(member.value) for member in value}


def verify_sdk(lock_path: Path = ROOT / "requirements-hashes.lock") -> dict[str, Any]:
    """Import and validate only SDK types; no client is created and no I/O occurs."""

    required_version = required_alpaca_version(lock_path)
    installed_version = importlib.metadata.version("alpaca-py")
    failures: list[str] = []
    if installed_version != required_version:
        failures.append(
            f"installed alpaca-py {installed_version} does not match locked {required_version}"
        )

    from alpaca.data.enums import CryptoFeed
    from alpaca.data.historical import CryptoHistoricalDataClient
    from alpaca.data.requests import (
        CryptoBarsRequest,
        CryptoLatestOrderbookRequest,
        CryptoLatestQuoteRequest,
        CryptoLatestTradeRequest,
    )
    from alpaca.trading.client import TradingClient
    from alpaca.trading.enums import (
        AssetClass,
        AssetExchange,
        AssetStatus,
        OrderStatus,
        OrderType,
        TimeInForce,
    )
    from alpaca.trading.models import Asset
    from alpaca.trading.requests import (
        GetAssetsRequest,
        LimitOrderRequest,
        MarketOrderRequest,
        StopLimitOrderRequest,
    )

    signature_requirements = (
        ("CryptoHistoricalDataClient", CryptoHistoricalDataClient, {"api_key", "secret_key"}),
        ("TradingClient", TradingClient, {"api_key", "secret_key", "paper"}),
        ("get_crypto_bars", CryptoHistoricalDataClient.get_crypto_bars, {"request_params", "feed"}),
        ("get_crypto_latest_quote", CryptoHistoricalDataClient.get_crypto_latest_quote, {"request_params", "feed"}),
        ("get_crypto_latest_trade", CryptoHistoricalDataClient.get_crypto_latest_trade, {"request_params", "feed"}),
        ("get_crypto_latest_orderbook", CryptoHistoricalDataClient.get_crypto_latest_orderbook, {"request_params", "feed"}),
        ("CryptoBarsRequest", CryptoBarsRequest, {"symbol_or_symbols", "start", "end", "limit", "timeframe"}),
        ("CryptoLatestQuoteRequest", CryptoLatestQuoteRequest, {"symbol_or_symbols"}),
        ("CryptoLatestTradeRequest", CryptoLatestTradeRequest, {"symbol_or_symbols"}),
        ("CryptoLatestOrderbookRequest", CryptoLatestOrderbookRequest, {"symbol_or_symbols"}),
        ("TradingClient.get_all_assets", TradingClient.get_all_assets, {"filter"}),
        ("TradingClient.submit_order", TradingClient.submit_order, {"order_data"}),
        ("TradingClient.cancel_order_by_id", TradingClient.cancel_order_by_id, {"order_id"}),
        ("TradingClient.get_order_by_id", TradingClient.get_order_by_id, {"order_id"}),
        ("TradingClient.get_order_by_client_id", TradingClient.get_order_by_client_id, {"client_id"}),
        ("GetAssetsRequest", GetAssetsRequest, {"status", "asset_class", "exchange"}),
        ("MarketOrderRequest", MarketOrderRequest, {"symbol", "qty", "notional", "side", "time_in_force", "client_order_id"}),
        ("LimitOrderRequest", LimitOrderRequest, {"symbol", "qty", "notional", "side", "time_in_force", "client_order_id", "limit_price"}),
        ("StopLimitOrderRequest", StopLimitOrderRequest, {"symbol", "qty", "notional", "side", "time_in_force", "client_order_id", "stop_price", "limit_price"}),
        ("Asset", Asset, {"id", "asset_class", "exchange", "symbol", "status", "tradable", "marginable", "shortable", "easy_to_borrow", "fractionable", "min_order_size", "min_trade_increment", "price_increment"}),
    )
    for label, value, required in signature_requirements:
        failures.extend(_require_parameters(label, value, required))

    expected_enums = {
        "CryptoFeed": ({"us"}, {str(CryptoFeed.US.value)}),
        "AssetClass.crypto": ({"crypto"}, {str(AssetClass.CRYPTO.value)}),
        "AssetExchange.crypto": ({"CRYPTO"}, {str(AssetExchange.CRYPTO.value)}),
        "AssetStatus.active": ({"active"}, {str(AssetStatus.ACTIVE.value)}),
        "OrderType": ({"market", "limit", "stop_limit"}, _enum_values(OrderType)),
        "TimeInForce": ({"gtc", "ioc"}, _enum_values(TimeInForce)),
    }
    for label, (expected, actual) in expected_enums.items():
        if not expected.issubset(actual):
            failures.append(
                f"{label} missing values: {sorted(expected - actual)}"
            )

    from app.crypto_capabilities import (
        AMBIGUOUS_OR_ACTIVE_ORDER_STATES,
        OFFICIAL_CONTRACT_FINGERPRINT,
        SUPPORTED_ORDER_TYPES,
        SUPPORTED_TIME_IN_FORCE,
        TERMINAL_ORDER_STATES,
    )

    application_order_states = set(AMBIGUOUS_OR_ACTIVE_ORDER_STATES) | set(TERMINAL_ORDER_STATES)
    sdk_order_states = _enum_values(OrderStatus)
    if application_order_states != sdk_order_states:
        failures.append(
            "application order states differ from the pinned SDK: "
            f"expected {sorted(sdk_order_states)}, found {sorted(application_order_states)}"
        )
    if not set(SUPPORTED_ORDER_TYPES).issubset(_enum_values(OrderType)):
        failures.append("application crypto order types differ from the pinned SDK")
    if not set(SUPPORTED_TIME_IN_FORCE).issubset(_enum_values(TimeInForce)):
        failures.append("application crypto time-in-force values differ from the pinned SDK")

    if failures:
        raise RuntimeError("Alpaca crypto SDK contract verification failed: " + "; ".join(failures))
    return {
        "verified": True,
        "network_io": False,
        "package": "alpaca-py",
        "locked_version": required_version,
        "installed_version": installed_version,
        "crypto_feed": str(CryptoFeed.US.value),
        "asset_exchange": str(AssetExchange.CRYPTO.value),
        "official_contract_fingerprint": OFFICIAL_CONTRACT_FINGERPRINT,
        "supported_order_types": sorted(SUPPORTED_ORDER_TYPES),
        "supported_time_in_force": sorted(SUPPORTED_TIME_IN_FORCE),
        "order_statuses": sorted(sdk_order_states),
        "verified_signature_count": len(signature_requirements),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--lock",
        type=Path,
        default=ROOT / "requirements-hashes.lock",
        help="hash-verified dependency lock containing the exact alpaca-py pin",
    )
    args = parser.parse_args()
    print(json.dumps(verify_sdk(args.lock), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
