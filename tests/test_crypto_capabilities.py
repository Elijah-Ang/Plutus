from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.broker_alpaca import AlpacaBroker
from app.broker_interface import BrokerSubmissionNotAttempted
from app.configuration import ConfigurationError, validate_config
from app.crypto_capabilities import (
    CRYPTO_OFFICIAL_CONTRACT_VERSION,
    CryptoCapabilityStore,
    OFFICIAL_CONTRACT_FINGERPRINT,
    SUPPORTED_ORDER_TYPES,
    SUPPORTED_REQUEST_BASES,
    SUPPORTED_TIME_IN_FORCE,
    crypto_contract_summary,
)
from app.formula_versions import CRYPTO_CAPABILITY_SCHEMA_VERSION
from app.storage import Storage
from app.utils import json_dumps, load_config


NOW = datetime(2026, 7, 17, 4, 0, tzinfo=UTC)


def _asset(symbol: str, *, tradable: bool = True, status: str = "active", **changes):
    precision = {
        "BTC/USD": ("0.0001", "0.0001", "1"),
        "ETH/USD": ("0.001", "0.001", "0.1"),
    }.get(symbol, ("0.01", "0.01", "0.01"))
    values = {
        "id": f"asset-{symbol}",
        "asset_class": "crypto",
        "exchange": "CRYPTO",
        "symbol": symbol,
        "status": status,
        "tradable": tradable,
        "marginable": False,
        "shortable": False,
        "easy_to_borrow": False,
        "fractionable": True,
        "min_order_size": precision[0],
        "min_trade_increment": precision[1],
        "price_increment": precision[2],
    }
    values.update(changes)
    return SimpleNamespace(**values)


class CapabilityBroker:
    def __init__(self, assets=None, *, verified: bool = True, account_hash: str = "a" * 64):
        self.assets = list(assets if assets is not None else [_asset("BTC/USD"), _asset("ETH/USD")])
        self.verified = verified
        self.account_hash = account_hash
        self.asset_calls = 0

    def paper_account_identity(self):
        return {
            "verified": self.verified,
            "mode": "paper",
            "endpoint_class": "paper",
            "account_status": "active",
            "account_currency": "USD",
            "account_id_hash": self.account_hash,
        }

    def get_crypto_assets(self):
        self.asset_calls += 1
        return list(self.assets)


def _storage(tmp_path) -> Storage:
    storage = Storage(tmp_path / "crypto-capabilities.sqlite3")
    storage.initialize()
    return storage


def _config():
    return deepcopy(load_config())


def test_official_contract_is_separate_versioned_spot_paper_lane():
    summary = crypto_contract_summary()
    contract = summary["contract"]

    assert contract["version"] == CRYPTO_OFFICIAL_CONTRACT_VERSION
    assert summary["contract_fingerprint"] == OFFICIAL_CONTRACT_FINGERPRINT
    assert len(OFFICIAL_CONTRACT_FINGERPRINT) == 64
    assert tuple(contract["order_types"]) == SUPPORTED_ORDER_TYPES
    assert tuple(contract["time_in_force"]) == SUPPORTED_TIME_IN_FORCE
    assert tuple(contract["request_bases"]) == SUPPORTED_REQUEST_BASES
    assert contract["market_profile"] == "continuous_24_7"
    assert contract["paper_support"] is True
    assert contract["spot_only"] is True
    assert contract["asset_exchange"] == "CRYPTO"
    assert contract["long_only"] is True
    assert contract["leverage"] is False
    assert contract["autonomous_execution"] is False
    assert contract["bar_semantics"].startswith("trade_or_quote_midpoint")
    assert contract["fees"]["tier_1_taker_bps"] == 25


def test_current_assets_api_evidence_creates_and_reloads_authoritative_snapshot(tmp_path):
    storage = _storage(tmp_path)
    broker = CapabilityBroker()
    snapshot = CryptoCapabilityStore(storage).capture(_config(), broker, "run-1", now=NOW)

    assert snapshot.authoritative is True
    assert snapshot.paper_account_id_hash == "a" * 64
    assert broker.asset_calls == 1
    assert [asset.symbol for asset in snapshot.assets] == ["BTC/USD", "ETH/USD"]
    assert snapshot.asset("BTC/USD").min_order_size == "0.0001"
    assert snapshot.asset("ETH/USD").price_increment == "0.1"
    assert all(asset.authoritative for asset in snapshot.assets)

    loaded = CryptoCapabilityStore(storage).load_verified(snapshot.id, _config(), now=NOW + timedelta(minutes=1))
    assert loaded == snapshot
    row = storage.fetch_all("SELECT * FROM crypto_capability_snapshots WHERE id=?", (snapshot.id,))[0]
    assert row["official_contract_fingerprint"] == OFFICIAL_CONTRACT_FINGERPRINT
    assert row["schema_version"] == CRYPTO_CAPABILITY_SCHEMA_VERSION
    assert int(row["asset_count"]) == 2


@pytest.mark.parametrize(
    ("asset", "reason"),
    [
        (_asset("BTC/USD", tradable=False), "asset_not_tradable"),
        (_asset("BTC/USD", status="inactive"), "asset_not_active"),
        (_asset("BTC/USD", fractionable=False), "asset_not_fractionable"),
        (_asset("BTC/USD", marginable=True), "asset_unexpectedly_marginable"),
        (_asset("BTC/USD", shortable=True), "asset_unexpectedly_shortable"),
        (_asset("BTC/USD", exchange="OTHER"), "asset_exchange_not_crypto"),
        (_asset("BTC/USD", asset_class="us_equity"), "asset_class_not_crypto"),
        (_asset("BTC/USD", min_order_size="nan"), "min_order_size_must_be_finite_and_positive"),
        (_asset("BTC/USD", min_trade_increment="0"), "min_trade_increment_must_be_finite_and_positive"),
        (_asset("BTC/USD", price_increment=None), "price_increment_is_missing_or_invalid"),
    ],
)
def test_invalid_pair_capability_is_persisted_but_never_authoritative(tmp_path, asset, reason):
    storage = _storage(tmp_path)
    snapshot = CryptoCapabilityStore(storage).capture(
        _config(), CapabilityBroker([asset, _asset("ETH/USD")]), "run-invalid", now=NOW
    )

    assert snapshot.authoritative is False
    assert any(reason in value for value in snapshot.failure_reasons)
    with pytest.raises(RuntimeError, match="not authoritative"):
        CryptoCapabilityStore(storage).load_verified(snapshot.id, _config(), now=NOW + timedelta(seconds=1))
    loaded = CryptoCapabilityStore(storage).load_verified(
        snapshot.id, _config(), now=NOW + timedelta(seconds=1), require_authoritative=False
    )
    assert loaded.asset("BTC/USD").authoritative is False


def test_missing_duplicate_or_unverified_broker_evidence_fails_closed(tmp_path):
    storage = _storage(tmp_path)
    missing = CryptoCapabilityStore(storage).capture(
        _config(), CapabilityBroker([_asset("BTC/USD")]), "missing", now=NOW
    )
    duplicate = CryptoCapabilityStore(storage).capture(
        _config(), CapabilityBroker([_asset("BTC/USD"), _asset("BTC/USD"), _asset("ETH/USD")]), "duplicate", now=NOW
    )
    wrong_account = CryptoCapabilityStore(storage).capture(
        _config(), CapabilityBroker(verified=False, account_hash=""), "account", now=NOW
    )

    assert missing.authoritative is False
    assert any("ETH/USD:asset_missing" in reason for reason in missing.failure_reasons)
    assert duplicate.authoritative is False
    assert "duplicate_asset_identity:BTC/USD" in duplicate.failure_reasons
    assert wrong_account.authoritative is False
    assert "stable_paper_account_identity_missing" in wrong_account.failure_reasons
    assert "paper_account_identity_unverified" in wrong_account.failure_reasons


def test_nonactive_or_non_usd_paper_identity_fails_closed(tmp_path):
    storage = _storage(tmp_path)
    inactive = CapabilityBroker()
    inactive.paper_account_identity = lambda: {
        "verified": True, "mode": "paper", "endpoint_class": "paper",
        "account_status": "limited", "account_currency": "USD", "account_id_hash": "a" * 64,
    }
    wrong_currency = CapabilityBroker()
    wrong_currency.paper_account_identity = lambda: {
        "verified": True, "mode": "paper", "endpoint_class": "paper",
        "account_status": "active", "account_currency": "EUR", "account_id_hash": "a" * 64,
    }

    first = CryptoCapabilityStore(storage).capture(_config(), inactive, "inactive", now=NOW)
    second = CryptoCapabilityStore(storage).capture(_config(), wrong_currency, "currency", now=NOW)

    assert "paper_account_not_active" in first.failure_reasons
    assert "paper_account_currency_not_usd" in second.failure_reasons
    assert first.authoritative is False
    assert second.authoritative is False


def test_input_contract_cannot_be_replaced_and_resigned_locally(tmp_path):
    storage = _storage(tmp_path)
    config = _config()
    snapshot = CryptoCapabilityStore(storage).capture(config, CapabilityBroker(), "run", now=NOW)
    row = dict(storage.fetch_all(
        "SELECT evidence_json FROM crypto_capability_snapshots WHERE id=?", (snapshot.id,)
    )[0])
    evidence = json.loads(row["evidence_json"])
    evidence["input"]["static_contract"]["asset_exchange"] = "ALPACA"
    input_fingerprint = hashlib.sha256(json_dumps(evidence["input"]).encode()).hexdigest()
    evidence["snapshot"]["input_fingerprint"] = input_fingerprint
    snapshot_fingerprint = hashlib.sha256(json_dumps(evidence["snapshot"]).encode()).hexdigest()
    storage.execute(
        """UPDATE crypto_capability_snapshots
           SET evidence_json=?,input_fingerprint=?,snapshot_fingerprint=? WHERE id=?""",
        (json_dumps(evidence), input_fingerprint, snapshot_fingerprint, snapshot.id),
    )

    with pytest.raises(RuntimeError, match="input authority binding mismatch"):
        CryptoCapabilityStore(storage).load_verified(
            snapshot.id, config, now=NOW + timedelta(seconds=1)
        )


def test_configuration_contract_drift_and_wrong_initial_pair_fail_closed(tmp_path):
    storage = _storage(tmp_path)
    config = _config()
    config["crypto"]["capability_contract"]["time_in_force"] = ["day"]
    config["crypto"]["symbols"] = ["BTC/USD", "SOL/USD"]
    snapshot = CryptoCapabilityStore(storage).capture(
        config, CapabilityBroker([_asset("BTC/USD"), _asset("SOL/USD")]), "drift", now=NOW
    )

    assert snapshot.authoritative is False
    assert "configuration_mismatch:time_in_force" in snapshot.failure_reasons
    assert "stage_one_symbols_must_be_exactly_btc_usd_and_eth_usd" in snapshot.failure_reasons


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (("crypto", "broker"), "generic", "alpaca_paper_spot"),
        (("crypto", "market_profile"), "us_equities", "continuous_24_7"),
        (("crypto", "data_feed"), "default", "Alpaca US feed"),
        (("crypto", "allow_margin"), True, "margin must remain disabled"),
        (("crypto", "allow_shorting"), True, "shorting must remain disabled"),
        (("crypto", "symbols"), ["BTC/USD", "SOL/USD"], "exactly BTC/USD and ETH/USD"),
        (("crypto", "capability_contract", "time_in_force"), ["day"], "gtc and ioc"),
        (("crypto", "capability_contract", "maintenance_policy"), "continue", "fail closed"),
    ],
)
def test_safety_critical_crypto_configuration_drift_is_rejected(path, value, message):
    config = _config()
    target = config
    for component in path[:-1]:
        target = target[component]
    target[path[-1]] = value

    with pytest.raises(ConfigurationError, match=message):
        validate_config(config)


def test_snapshot_expiry_config_change_and_fingerprint_corruption_are_rejected(tmp_path):
    storage = _storage(tmp_path)
    config = _config()
    snapshot = CryptoCapabilityStore(storage).capture(config, CapabilityBroker(), "run", now=NOW)
    store = CryptoCapabilityStore(storage)

    with pytest.raises(RuntimeError, match="expired"):
        store.load_verified(snapshot.id, config, now=NOW + timedelta(minutes=60))

    changed = deepcopy(config)
    changed["effective_config_hash"] = "b" * 64
    with pytest.raises(RuntimeError, match="configuration identity changed"):
        store.load_verified(snapshot.id, changed, now=NOW + timedelta(seconds=1))

    storage.execute(
        "UPDATE crypto_capability_snapshots SET snapshot_fingerprint=? WHERE id=?",
        ("0" * 64, snapshot.id),
    )
    with pytest.raises(RuntimeError, match="snapshot fingerprint mismatch"):
        store.load_verified(snapshot.id, config, now=NOW + timedelta(seconds=1))


def test_asset_mutation_with_regenerated_local_fingerprint_still_breaks_group_binding(tmp_path):
    storage = _storage(tmp_path)
    config = _config()
    snapshot = CryptoCapabilityStore(storage).capture(config, CapabilityBroker(), "run", now=NOW)
    row = dict(storage.fetch_all(
        "SELECT * FROM crypto_asset_capabilities WHERE snapshot_id=? AND symbol='BTC/USD'", (snapshot.id,)
    )[0])
    payload = json.loads(row["asset_json"])
    payload["min_order_size"] = "0.5"
    regenerated = hashlib.sha256(json_dumps(payload).encode()).hexdigest()
    storage.execute(
        """UPDATE crypto_asset_capabilities
           SET min_order_size='0.5',asset_json=?,asset_fingerprint=? WHERE id=?""",
        (json_dumps(payload), regenerated, row["id"]),
    )

    with pytest.raises(RuntimeError, match="snapshot/asset binding mismatch"):
        CryptoCapabilityStore(storage).load_verified(
            snapshot.id, config, now=NOW + timedelta(seconds=1)
        )


def test_generic_equity_adapter_rejects_crypto_before_any_broker_call():
    broker = AlpacaBroker(_config(), "paper-key", "paper-secret")
    calls = {"submit": 0}

    def submit_order(*args, **kwargs):
        calls["submit"] += 1
        raise AssertionError("broker submission must not be reached")

    broker.trading.submit_order = submit_order
    for symbol in ("BTC/USD", "ETH-USD", "BTCUSD", "ETHBTC", "BTCUSDT", "SOLUSD"):
        with pytest.raises(BrokerSubmissionNotAttempted, match="data/capability stage"):
            broker.submit_order(symbol, "buy", {"notional": 5.0})
    assert calls["submit"] == 0


def test_alpaca_crypto_read_adapter_uses_explicit_us_feed_and_assets_filter():
    broker = AlpacaBroker(_config(), "paper-key", "paper-secret")
    calls = []

    class CryptoData:
        def get_crypto_bars(self, request, *, feed):
            calls.append(("bars", request.symbol_or_symbols, feed.value))
            return SimpleNamespace(df="bars-frame")

        def get_crypto_latest_quote(self, request, *, feed):
            calls.append(("quote", request.symbol_or_symbols, feed.value))
            return {"BTC/USD": "quote"}

        def get_crypto_latest_trade(self, request, *, feed):
            calls.append(("trade", request.symbol_or_symbols, feed.value))
            return {"BTC/USD": "trade"}

        def get_crypto_latest_orderbook(self, request, *, feed):
            calls.append(("book", request.symbol_or_symbols, feed.value))
            return {"BTC/USD": "book"}

    broker._crypto_data = CryptoData()
    broker.trading.get_all_assets = lambda request: (
        calls.append(("assets", request.asset_class.value, request.status.value)) or [_asset("BTC/USD")]
    )

    assert broker.get_crypto_historical_bars("BTC/USD", "1Hour", 3) == "bars-frame"
    assert broker.get_crypto_latest_quote("BTC/USD") == "quote"
    assert broker.get_crypto_latest_trade("BTC/USD") == "trade"
    assert broker.get_crypto_latest_orderbook("BTC/USD") == "book"
    assert broker.get_crypto_assets()[0].symbol == "BTC/USD"
    assert calls == [
        ("bars", "BTC/USD", "us"),
        ("quote", "BTC/USD", "us"),
        ("trade", "BTC/USD", "us"),
        ("book", "BTC/USD", "us"),
        ("assets", "crypto", "active"),
    ]


def test_crypto_capability_migration_is_idempotent_and_required(tmp_path):
    storage = _storage(tmp_path)
    storage.apply_explicit_migrations()
    first = set(storage.schema_versions())
    storage.apply_explicit_migrations()
    second = set(storage.schema_versions())

    assert first == second
    assert CRYPTO_CAPABILITY_SCHEMA_VERSION in second
    assert next(iter(storage.fetch_all("PRAGMA integrity_check")[0].values())) == "ok"
