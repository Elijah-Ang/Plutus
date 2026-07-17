from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import hashlib
import json
from types import SimpleNamespace

import pytest

from app.crypto_capabilities import CryptoCapabilityStore
from app.crypto_market_data import CryptoMarketDataStore
from app.execution import DurableExecutionStore
from app.formula_versions import CRYPTO_MARKET_DATA_SCHEMA_VERSION
from app.storage import Storage
from app.utils import load_config
from app.utils import json_dumps


NOW = datetime(2026, 7, 17, 5, 30, tzinfo=UTC)


def _asset(symbol: str, *, tradable: bool = True):
    return SimpleNamespace(
        id=f"asset-{symbol}", asset_class="crypto", exchange="CRYPTO", symbol=symbol,
        status="active", tradable=tradable, marginable=False, shortable=False,
        easy_to_borrow=False, fractionable=True,
        min_order_size="0.0001" if symbol == "BTC/USD" else "0.001",
        min_trade_increment="0.0001" if symbol == "BTC/USD" else "0.001",
        price_increment="1" if symbol == "BTC/USD" else "0.1",
    )


class MarketBroker:
    def __init__(
        self,
        *,
        bid="100",
        ask="100.1",
        bid_size="20",
        ask_size="20",
        quote_time=NOW,
        book_time=NOW,
        trade_time=NOW,
        book_bid="100",
        book_ask="100.1",
        book_bid_size="20",
        book_ask_size="20",
        trade_available=True,
        asset_tradable=True,
    ):
        self.quote = SimpleNamespace(
            bid_price=bid, ask_price=ask, bid_size=bid_size, ask_size=ask_size, timestamp=quote_time
        )
        self.book = SimpleNamespace(
            bids=[SimpleNamespace(price=book_bid, size=book_bid_size)],
            asks=[SimpleNamespace(price=book_ask, size=book_ask_size)],
            timestamp=book_time,
        )
        self.trade = (
            SimpleNamespace(price="100.05", size="0.5", timestamp=trade_time)
            if trade_available else None
        )
        self.asset_tradable = asset_tradable

    def paper_account_identity(self):
        return {
            "verified": True, "mode": "paper", "endpoint_class": "paper",
            "account_status": "active", "account_currency": "USD", "account_id_hash": "a" * 64,
        }

    def get_crypto_assets(self):
        return [_asset("BTC/USD", tradable=self.asset_tradable), _asset("ETH/USD")]

    def get_crypto_latest_quote(self, symbol):
        return self.quote

    def get_crypto_latest_trade(self, symbol):
        return self.trade

    def get_crypto_latest_orderbook(self, symbol):
        return self.book


def _storage(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    storage = Storage(tmp_path / "market-evidence.sqlite3")
    storage.initialize()
    return storage


def _config():
    return deepcopy(load_config())


def _capture(tmp_path, broker: MarketBroker, config=None):
    storage = _storage(tmp_path)
    config = config or _config()
    capability = CryptoCapabilityStore(storage).capture(config, broker, "run", now=NOW)
    evidence = CryptoMarketDataStore(storage).capture(
        config, broker, capability, "run", "research-run", "BTC/USD", now=NOW
    )
    return storage, config, capability, evidence


def test_fresh_quote_trade_and_orderbook_create_authoritative_eligible_evidence(tmp_path):
    storage, config, capability, evidence = _capture(tmp_path, MarketBroker())

    assert capability.authoritative is True
    assert evidence.authoritative is True
    assert evidence.execution_eligible is True
    assert evidence.failure_reasons == ()
    assert evidence.warnings == ()
    assert evidence.bid_price == "100"
    assert evidence.ask_price == "100.1"
    assert Decimal(evidence.spread_bps) == pytest.approx(Decimal("9.995002498750624687656171914"))
    assert evidence.top_of_book_notional == "2000"
    assert len(evidence.evidence_fingerprint) == 64

    loaded = CryptoMarketDataStore(storage).load_verified(evidence.id, config)
    assert loaded == evidence
    row = storage.fetch_all("SELECT * FROM crypto_market_data_evidence WHERE id=?", (evidence.id,))[0]
    assert row["capability_snapshot_id"] == capability.id
    assert row["capability_snapshot_fingerprint"] == capability.snapshot_fingerprint
    assert all(value == 0 for value in DurableExecutionStore(storage).integrity_report().values())


def test_missing_latest_trade_is_warning_not_false_liquidity_authority(tmp_path):
    _, _, _, evidence = _capture(tmp_path, MarketBroker(trade_available=False))

    assert evidence.authoritative is True
    assert evidence.execution_eligible is True
    assert any("trade_" in warning for warning in evidence.warnings)
    assert evidence.trade_price is None


@pytest.mark.parametrize(
    ("broker", "reason"),
    [
        (MarketBroker(quote_time=NOW - timedelta(minutes=6)), "quote_stale"),
        (MarketBroker(book_time=NOW - timedelta(minutes=6)), "orderbook_stale"),
        (MarketBroker(quote_time=NOW + timedelta(seconds=2)), "quote_timestamp_in_future"),
        (MarketBroker(bid="101", ask="100"), "quote_market_crossed"),
        (MarketBroker(book_bid="101", book_ask="100"), "orderbook_market_crossed"),
        (MarketBroker(bid_size=None), "quote_bid_size_missing_or_invalid"),
        (MarketBroker(book_ask_size="nan"), "orderbook_ask_size_not_finite_positive"),
    ],
)
def test_stale_crossed_or_malformed_required_evidence_fails_authority(tmp_path, broker, reason):
    _, _, _, evidence = _capture(tmp_path, broker)

    assert evidence.authoritative is False
    assert evidence.execution_eligible is False
    assert reason in evidence.failure_reasons


def test_wide_spread_and_shallow_book_remain_evidence_but_block_execution(tmp_path):
    _, _, _, wide = _capture(tmp_path / "wide", MarketBroker(ask="102"))
    _, _, _, shallow = _capture(
        tmp_path / "shallow", MarketBroker(book_bid_size="1", book_ask_size="1")
    )

    assert wide.authoritative is True
    assert wide.execution_eligible is False
    assert "spread_exceeds_configured_limit" in wide.failure_reasons
    assert shallow.authoritative is True
    assert shallow.execution_eligible is False
    assert "top_of_book_liquidity_below_configured_minimum" in shallow.failure_reasons


def test_unverified_pair_capability_prevents_market_evidence_authority(tmp_path):
    _, _, capability, evidence = _capture(tmp_path, MarketBroker(asset_tradable=False))

    assert capability.authoritative is False
    assert evidence.authoritative is False
    assert "capability_snapshot_not_authoritative_for_symbol" in evidence.failure_reasons


def test_market_evidence_fingerprint_column_and_configuration_tampering_fail(tmp_path):
    storage, config, _, evidence = _capture(tmp_path, MarketBroker())
    store = CryptoMarketDataStore(storage)

    storage.execute(
        "UPDATE crypto_market_data_evidence SET evidence_fingerprint=? WHERE id=?",
        ("0" * 64, evidence.id),
    )
    with pytest.raises(RuntimeError, match="fingerprint mismatch"):
        store.load_verified(evidence.id, config)

    storage, config, _, evidence = _capture(tmp_path / "column", MarketBroker())
    storage.execute("UPDATE crypto_market_data_evidence SET bid_price='1' WHERE id=?", (evidence.id,))
    with pytest.raises(RuntimeError, match="persisted column mismatch: bid_price"):
        CryptoMarketDataStore(storage).load_verified(evidence.id, config)

    storage, config, _, evidence = _capture(tmp_path / "config", MarketBroker())
    changed = deepcopy(config)
    changed["effective_config_hash"] = "b" * 64
    with pytest.raises(RuntimeError, match="configuration identity changed"):
        CryptoMarketDataStore(storage).load_verified(evidence.id, changed)


def test_market_evidence_capability_relationship_is_verified(tmp_path):
    storage, config, capability, evidence = _capture(tmp_path, MarketBroker())
    storage.execute(
        "UPDATE crypto_capability_snapshots SET snapshot_fingerprint=? WHERE id=?",
        ("f" * 64, capability.id),
    )

    with pytest.raises(RuntimeError, match="capability binding mismatch"):
        CryptoMarketDataStore(storage).load_verified(evidence.id, config)
    assert DurableExecutionStore(storage).integrity_report()["orphaned_crypto_market_evidence"] == 1


def test_execution_eligibility_cannot_be_changed_and_resigned_locally(tmp_path):
    storage, config, _, evidence = _capture(tmp_path, MarketBroker(ask="102"))
    row = dict(storage.fetch_all(
        "SELECT evidence_json FROM crypto_market_data_evidence WHERE id=?", (evidence.id,)
    )[0])
    payload = json.loads(row["evidence_json"])
    payload["execution_blockers"] = []
    payload["execution_eligible"] = True
    fingerprint = hashlib.sha256(json_dumps(payload).encode()).hexdigest()
    storage.execute(
        """UPDATE crypto_market_data_evidence
           SET execution_eligible=1,failure_reasons_json='[]',evidence_json=?,evidence_fingerprint=?
           WHERE id=?""",
        (json_dumps(payload), fingerprint, evidence.id),
    )

    with pytest.raises(RuntimeError, match="execution blocker classification mismatch"):
        CryptoMarketDataStore(storage).load_verified(evidence.id, config)


def test_market_evidence_migration_is_idempotent_and_required(tmp_path):
    storage = _storage(tmp_path)
    storage.apply_explicit_migrations()
    first = set(storage.schema_versions())
    storage.apply_explicit_migrations()

    assert first == set(storage.schema_versions())
    assert CRYPTO_MARKET_DATA_SCHEMA_VERSION in first
