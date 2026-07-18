from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from app.approval_authority import canonical_json
from app.configuration import ConfigurationError, validate_config
from app.crypto_capabilities import CryptoCapabilityStore
from app.crypto_market_data import CryptoMarketDataStore
from app.crypto_risk import CryptoRiskError, CryptoRiskStore
from app.crypto_sizing import CryptoSizingRequest, load_verified_crypto_sizing
from app.execution import DurableExecutionStore
from app.formula_versions import CRYPTO_RISK_SCHEMA_VERSION, CRYPTO_SIZING_SCHEMA_VERSION
from app.storage import Storage
from app.utils import load_config


NOW = datetime(2026, 7, 18, 4, 0, tzinfo=UTC)
ACCOUNT_ID = "paper-account-crypto-risk"
ACCOUNT_HASH = hashlib.sha256(ACCOUNT_ID.encode()).hexdigest()


def _asset(symbol: str):
    return SimpleNamespace(
        id=f"asset-{symbol}", asset_class="crypto", exchange="CRYPTO", symbol=symbol,
        status="active", tradable=True, marginable=False, shortable=False,
        easy_to_borrow=False, fractionable=True,
        min_order_size="0.0001" if symbol == "BTC/USD" else "0.001",
        min_trade_increment="0.0001" if symbol == "BTC/USD" else "0.001",
        price_increment="1" if symbol == "BTC/USD" else "0.1",
    )


class Broker:
    def __init__(
        self,
        *,
        account_id: str = ACCOUNT_ID,
        equity: str = "100000",
        cash: str = "100000",
        buying_power: str = "100000",
        positions=None,
        orders=None,
        daily_loss: str = "0",
        weekly_loss: str = "0",
        volatility_step: D = D("0.0001"),
    ):
        self.account_id = account_id
        self.account = SimpleNamespace(
            id=account_id, status="active", currency="USD", equity=equity, cash=cash,
            non_marginable_buying_power=buying_power, account_blocked=False,
            trading_blocked=False,
        )
        self.positions = list(positions or [])
        self.orders = list(orders or [])
        self.daily_loss = daily_loss
        self.weekly_loss = weekly_loss
        self.volatility_step = volatility_step
        self.submit_calls = 0

    def paper_account_identity(self):
        return {
            "verified": True, "mode": "paper", "endpoint_class": "paper",
            "account_status": "active", "account_currency": "USD",
            "account_id_hash": hashlib.sha256(self.account_id.encode()).hexdigest(),
        }

    def get_account(self):
        return self.account

    def get_crypto_assets(self):
        return [_asset("BTC/USD"), _asset("ETH/USD")]

    def get_crypto_latest_quote(self, symbol):
        return SimpleNamespace(
            bid_price="100", ask_price="100.1", bid_size="20", ask_size="20", timestamp=NOW,
        )

    def get_crypto_latest_trade(self, symbol):
        return SimpleNamespace(price="100.05", size="1", timestamp=NOW)

    def get_crypto_latest_orderbook(self, symbol):
        return SimpleNamespace(
            bids=[SimpleNamespace(price="100", size="20")],
            asks=[SimpleNamespace(price="100.1", size="20")], timestamp=NOW,
        )

    def get_positions(self):
        return list(self.positions)

    def get_open_orders(self):
        return list(self.orders)

    def get_loss_metrics(self):
        return {
            "daily_loss_dollars": self.daily_loss,
            "weekly_loss_dollars": self.weekly_loss,
            "reference_equity": self.account.equity,
            "daily_loss_confidence": "verified",
            "weekly_loss_confidence": "verified",
            "provenance": "fake_current_alpaca_account_and_history",
            "metrics_version": "loss_controls_v2",
        }

    def get_crypto_historical_bars(self, symbol, timeframe="1Hour", limit=169):
        price = D("100")
        rows = []
        for index in range(50):
            price *= D("1") + (self.volatility_step if index % 2 == 0 else -self.volatility_step)
            rows.append(
                SimpleNamespace(
                    symbol=symbol,
                    timestamp=NOW - timedelta(hours=49 - index),
                    close=str(price),
                )
            )
        return rows

    def submit_order(self, *args, **kwargs):
        self.submit_calls += 1
        raise AssertionError("crypto sizing/risk must never call submit_order")


def _position(symbol="BTCUSD", qty="0.02", value="2", price="100"):
    return SimpleNamespace(
        symbol=symbol, asset_class="crypto", qty=qty, market_value=value,
        current_price=price, side="long",
    )


def _order(
    *, symbol="BTCUSD", side="buy", qty="0.01", filled_qty="0",
    notional=None, limit_price="100", client_order_id="crypto-open-1", status="new",
):
    return SimpleNamespace(
        id=f"broker-{client_order_id}", client_order_id=client_order_id,
        symbol=symbol, side=side, qty=qty, filled_qty=filled_qty,
        notional=notional, limit_price=limit_price, status=status,
    )


def _storage(tmp_path):
    storage = Storage(tmp_path / "crypto-sizing-risk.sqlite3")
    storage.initialize()
    return storage


def _config():
    return deepcopy(load_config())


def _request(**changes):
    values = {
        "source_type": "crypto_strategy_research",
        "source_id": "research-candidate-1",
        "source_fingerprint": "b" * 64,
        "symbol": "BTC/USD",
        "side": "buy",
        "action": "entry",
        "request_basis": "notional",
        "requested_stop_risk_dollars": D("1"),
        "stop_price": D("95"),
    }
    values.update(changes)
    return CryptoSizingRequest(**values)


def _evidence(storage, config, broker):
    capability = CryptoCapabilityStore(storage).capture(config, broker, "run", now=NOW)
    market = CryptoMarketDataStore(storage).capture(
        config, broker, capability, "run", "research", "BTC/USD", now=NOW
    )
    assert capability.authoritative and market.execution_eligible
    return capability, market


def _evaluate(tmp_path, broker=None, request=None, config=None):
    storage = _storage(tmp_path)
    config = config or _config()
    broker = broker or Broker()
    capability, market = _evidence(storage, config, broker)
    result = CryptoRiskStore(storage).evaluate(
        config, broker, "run", capability.id, market.id, request or _request(), now=NOW
    )
    return storage, config, broker, capability, market, result


def test_authoritative_decimal_crypto_size_is_bounded_and_never_execution_authority(tmp_path):
    storage, config, broker, capability, market, result = _evaluate(tmp_path)

    assert result.risk_eligible is True
    assert result.execution_authorized is False
    assert result.sizing.eligible is True
    assert result.sizing.authoritative is True
    assert result.sizing.execution_authorized is False
    assert result.sizing.request_basis == "notional"
    assert D(result.sizing.canonical_notional) == D("5")
    assert D(result.sizing.canonical_stop_risk) <= D("1")
    assert D(result.sizing.estimated_fees) > 0
    assert D(result.sizing.estimated_stop_slippage) > 0
    assert result.sizing.capability_snapshot_id == capability.id
    assert result.sizing.market_evidence_id == market.id
    assert broker.submit_calls == 0

    snapshot = CryptoRiskStore(storage).load_verified(result.snapshot_id, config, now=NOW)
    sizing = load_verified_crypto_sizing(storage, result.sizing.id, config)
    decision = CryptoRiskStore(storage).load_verified_decision(result.decision_id, config, now=NOW)
    assert snapshot["id"] == result.snapshot_id
    assert sizing == result.sizing
    assert decision["risk_eligible"] is True
    assert decision["execution_authorized"] is False
    assert decision["sizing_decision_id"] == sizing.id
    integrity = DurableExecutionStore(storage).integrity_report()
    assert integrity["orphaned_crypto_risk_snapshots"] == 0
    assert integrity["incomplete_authoritative_crypto_risk_snapshots"] == 0
    assert integrity["orphaned_crypto_sizing_decisions"] == 0
    assert integrity["invalid_crypto_sizing_authority"] == 0
    assert integrity["orphaned_crypto_risk_decisions"] == 0
    assert integrity["crypto_execution_authority_escape"] == 0


def test_quantity_basis_rounds_down_to_current_asset_increment_and_risk(tmp_path):
    _, _, _, _, _, result = _evaluate(
        tmp_path,
        request=_request(request_basis="quantity", requested_stop_risk_dollars=D("0.137")),
    )

    quantity = D(result.sizing.canonical_quantity)
    assert quantity > 0
    assert quantity % D("0.0001") == 0
    assert D(result.sizing.canonical_notional) <= D("5")
    assert D(result.sizing.canonical_stop_risk) <= D("0.137")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("requested_stop_risk_dollars", 1.0),
        ("stop_price", float("nan")),
        ("requested_stop_risk_dollars", "Infinity"),
        ("stop_price", "-1"),
    ],
)
def test_strategy_sizing_boundary_rejects_float_nonfinite_or_negative_inputs(tmp_path, field, value):
    storage = _storage(tmp_path)
    config = _config()
    broker = Broker()
    capability, market = _evidence(storage, config, broker)

    with pytest.raises((CryptoRiskError, ValueError), match="Decimal|finite|positive|at least"):
        CryptoRiskStore(storage).evaluate(
            config, broker, "run", capability.id, market.id,
            _request(**{field: value}), now=NOW,
        )
    assert broker.submit_calls == 0
    assert storage.fetch_all("SELECT * FROM crypto_risk_snapshots") == []


def test_capacity_below_one_dollar_fails_closed_without_rounding_up(tmp_path):
    broker = Broker(equity="100", cash="0.50", buying_power="0.50")
    _, _, _, _, _, result = _evaluate(tmp_path, broker=broker)

    assert result.risk_eligible is False
    assert result.sizing.eligible is False
    assert "no_authoritative_crypto_notional_capacity" in result.sizing.blockers
    assert broker.submit_calls == 0


def test_wrong_paper_account_fails_snapshot_authority_and_risk(tmp_path):
    broker = Broker(account_id="other-paper-account")
    storage = _storage(tmp_path)
    config = _config()
    capability, market = _evidence(storage, config, broker)
    broker.account_id = ACCOUNT_ID
    broker.account.id = ACCOUNT_ID

    result = CryptoRiskStore(storage).evaluate(
        config, broker, "run", capability.id, market.id, _request(), now=NOW
    )

    assert result.risk_eligible is False
    assert "risk_account_identity_differs_from_capability_snapshot" in result.reasons
    assert broker.submit_calls == 0


def test_broker_open_crypto_order_is_counted_and_blocks_new_buy(tmp_path):
    broker = Broker(orders=[_order(notional="3")])
    storage, _, _, _, _, result = _evaluate(tmp_path, broker=broker)
    snapshot = json.loads(storage.fetch_all(
        "SELECT snapshot_json FROM crypto_risk_snapshots WHERE id=?", (result.snapshot_id,)
    )[0]["snapshot_json"])

    assert result.risk_eligible is False
    assert snapshot["aggregate"]["pending_crypto_buy_notional"] == "3"
    assert snapshot["aggregate"]["open_crypto_order_count"] == 1
    assert D(snapshot["derived_authority"]["hard_notional_ceiling"]) == 0
    assert broker.submit_calls == 0


def test_existing_crypto_position_counts_symbol_cluster_gross_and_full_downside_heat(tmp_path):
    broker = Broker(positions=[_position(value="400", qty="4")])
    storage, _, _, _, _, result = _evaluate(tmp_path, broker=broker)
    snapshot = json.loads(storage.fetch_all(
        "SELECT snapshot_json FROM crypto_risk_snapshots WHERE id=?", (result.snapshot_id,)
    )[0]["snapshot_json"])

    assert snapshot["aggregate"]["crypto_position_gross"] == "400"
    assert snapshot["aggregate"]["symbol_position_gross"] == "400"
    assert snapshot["aggregate"]["crypto_open_stop_risk"] == "400"
    assert result.risk_eligible is False
    assert "no_authoritative_crypto_stop_risk_capacity" in result.sizing.blockers


def test_daily_account_loss_halt_sets_zero_capacity(tmp_path):
    broker = Broker(daily_loss="1000")
    _, _, _, _, _, result = _evaluate(tmp_path, broker=broker)

    assert result.risk_eligible is False
    assert any("daily account loss halt" in reason for reason in result.reasons)
    assert result.sizing.eligible is False
    assert broker.submit_calls == 0


def test_extreme_volatility_halts_new_risk(tmp_path):
    broker = Broker(volatility_step=D("0.03"))
    storage, _, _, _, _, result = _evaluate(tmp_path, broker=broker)
    snapshot = json.loads(storage.fetch_all(
        "SELECT snapshot_json FROM crypto_risk_snapshots WHERE id=?", (result.snapshot_id,)
    )[0]["snapshot_json"])

    assert D(snapshot["volatility_evidence"]["annualized_volatility"]) >= D("1.5")
    assert snapshot["derived_authority"]["volatility_multiplier"] == "0"
    assert result.risk_eligible is False
    assert broker.submit_calls == 0


def test_sell_uses_exact_holdings_and_never_oversells(tmp_path):
    broker = Broker(positions=[_position(qty="0.020000000", value="2")])
    close = _request(
        side="sell", action="exit", request_basis="quantity",
        requested_stop_risk_dollars=None, stop_price=None,
        requested_exit_quantity=D("0.020000000"), close_entire_position=True,
    )
    _, _, _, _, _, result = _evaluate(tmp_path / "close", broker=broker, request=close)

    assert result.risk_eligible is True
    assert D(result.sizing.canonical_quantity) == D("0.02")
    assert D(result.sizing.canonical_stop_risk) == 0
    assert broker.submit_calls == 0

    oversell = _request(
        side="sell", action="exit", request_basis="quantity",
        requested_stop_risk_dollars=None, stop_price=None,
        requested_exit_quantity=D("0.03"), close_entire_position=False,
    )
    _, _, broker2, _, _, blocked = _evaluate(
        tmp_path / "oversell", broker=Broker(positions=[_position(qty="0.02", value="2")]),
        request=oversell,
    )
    assert blocked.risk_eligible is False
    assert "requested_crypto_sell_exceeds_sellable_holding" in blocked.sizing.blockers
    assert broker2.submit_calls == 0


def test_snapshot_column_or_payload_tampering_fails_reload(tmp_path):
    storage, config, _, _, _, result = _evaluate(tmp_path)
    storage.execute(
        "UPDATE crypto_risk_snapshots SET positions_fingerprint=? WHERE id=?",
        ("0" * 64, result.snapshot_id),
    )

    with pytest.raises(CryptoRiskError, match="positions binding mismatch"):
        CryptoRiskStore(storage).load_verified(result.snapshot_id, config, now=NOW)


def test_snapshot_and_sizing_expire_or_config_change_fail_closed(tmp_path):
    storage, config, _, _, _, result = _evaluate(tmp_path)
    with pytest.raises(CryptoRiskError, match="expired"):
        CryptoRiskStore(storage).load_verified(result.snapshot_id, config, now=NOW + timedelta(minutes=2))

    changed = deepcopy(config)
    changed["effective_config_hash"] = "f" * 64
    with pytest.raises((CryptoRiskError, RuntimeError), match="configuration identity changed"):
        CryptoRiskStore(storage).load_verified(result.snapshot_id, changed, now=NOW)
    with pytest.raises(RuntimeError, match="configuration identity changed"):
        load_verified_crypto_sizing(storage, result.sizing.id, changed)


def test_config_policy_tampering_fails_before_any_snapshot_or_order(tmp_path):
    config = _config()
    config["crypto"]["risk_policy"]["maximum_symbol_exposure_pct_equity"] = 2.0
    storage = _storage(tmp_path)
    broker = Broker()
    capability, market = _evidence(storage, _config(), broker)

    with pytest.raises(CryptoRiskError, match="exposure_limits"):
        CryptoRiskStore(storage).evaluate(
            config, broker, "run", capability.id, market.id, _request(), now=NOW
        )
    assert broker.submit_calls == 0
    assert storage.fetch_all("SELECT * FROM crypto_risk_snapshots") == []


def test_buy_limit_price_rounds_up_while_quantity_and_notional_round_down(tmp_path):
    storage, _, _, _, _, result = _evaluate(
        tmp_path,
        request=_request(request_basis="quantity", requested_stop_risk_dollars=D("10")),
    )
    snapshot = json.loads(storage.fetch_all(
        "SELECT snapshot_json FROM crypto_risk_snapshots WHERE id=?", (result.snapshot_id,)
    )[0]["snapshot_json"])

    assert D(result.sizing.limit_price) == D("101")
    assert D(result.sizing.canonical_quantity) % D("0.0001") == 0
    assert D(result.sizing.canonical_notional) <= D(
        snapshot["derived_authority"]["hard_notional_ceiling"]
    )
    assert D(result.sizing.canonical_stop_risk) <= D(
        snapshot["derived_authority"]["hard_stop_risk_ceiling"]
    )


@pytest.mark.parametrize("risk", ["0.0000001", "0.01", "0.099999", "0.10", "0.100001", "1", "100"])
def test_adversarial_stop_risk_boundaries_never_round_up_authority(tmp_path, risk):
    _, _, _, _, _, result = _evaluate(
        tmp_path,
        request=_request(request_basis="quantity", requested_stop_risk_dollars=D(risk)),
    )

    if result.sizing.canonical_stop_risk is not None:
        assert D(result.sizing.canonical_stop_risk) <= D(risk)
        assert D(result.sizing.canonical_notional) <= D("5")
    assert result.sizing.execution_authorized is False


def test_notional_basis_has_two_decimals_and_quantity_basis_has_nine_or_fewer(tmp_path):
    _, _, _, _, _, notional = _evaluate(
        tmp_path / "notional",
        request=_request(requested_stop_risk_dollars=D("0.137")),
    )
    _, _, _, _, _, quantity = _evaluate(
        tmp_path / "quantity",
        request=_request(request_basis="quantity", requested_stop_risk_dollars=D("0.137")),
    )

    assert max(0, -D(notional.sizing.canonical_notional).normalize().as_tuple().exponent) <= 2
    assert max(0, -D(quantity.sizing.canonical_quantity).normalize().as_tuple().exponent) <= 9


def test_pending_sell_quantity_is_subtracted_once_from_exact_sellable_capacity(tmp_path):
    broker = Broker(
        positions=[_position(qty="0.02", value="2")],
        orders=[_order(side="sell", qty="0.005", limit_price="100")],
    )
    request = _request(
        side="sell", action="exit", request_basis="quantity",
        requested_stop_risk_dollars=None, stop_price=None,
        requested_exit_quantity=D("0.015"), close_entire_position=True,
    )
    storage, _, _, _, _, result = _evaluate(tmp_path, broker=broker, request=request)
    snapshot = json.loads(storage.fetch_all(
        "SELECT snapshot_json FROM crypto_risk_snapshots WHERE id=?", (result.snapshot_id,)
    )[0]["snapshot_json"])

    assert snapshot["aggregate"]["position_quantity"] == "0.02"
    assert snapshot["aggregate"]["pending_symbol_sell_quantity"] == "0.005"
    assert result.sizing.eligible is True
    assert D(result.sizing.canonical_quantity) == D("0.015")
    assert broker.submit_calls == 0


def test_matching_broker_order_and_durable_reservation_are_counted_once(tmp_path):
    storage = _storage(tmp_path)
    config = _config()
    broker = Broker(orders=[_order(notional="3", client_order_id="stable-crypto-1")])
    capability, market = _evidence(storage, config, broker)
    created = NOW.isoformat()
    storage.execute(
        """INSERT INTO order_intents(
          id,source_id,source_type,logical_action_key,symbol,side,intended_action,
          request_basis,requested_quantity,reference_price,reserved_notional,
          reserved_stop_risk,client_order_id,trading_mode,state,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "intent-crypto-1", "candidate-1", "crypto_strategy", "logical-crypto-1",
            "BTC/USD", "buy", "entry", "notional", 0.04, 100, 4, 0.2,
            "stable-crypto-1", "paper", "reserved", created, created,
        ),
    )
    storage.execute(
        """INSERT INTO risk_reservations(
          id,intent_id,symbol,cluster_name,initial_notional,active_notional,
          initial_stop_risk,active_stop_risk,state,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "reservation-crypto-1", "intent-crypto-1", "BTC/USD", "crypto_major",
            4, 4, 0.2, 0.2, "active", created, created,
        ),
    )

    result = CryptoRiskStore(storage).evaluate(
        config, broker, "run", capability.id, market.id, _request(), now=NOW
    )
    snapshot = json.loads(storage.fetch_all(
        "SELECT snapshot_json FROM crypto_risk_snapshots WHERE id=?", (result.snapshot_id,)
    )[0]["snapshot_json"])

    assert snapshot["durable_state"]["broker_durable_duplicate_client_order_ids"] == ["stable-crypto-1"]
    assert snapshot["aggregate"]["pending_crypto_buy_notional"] == "4"
    assert snapshot["aggregate"]["pending_crypto_stop_risk"] == "0.2"
    assert result.risk_eligible is False
    assert broker.submit_calls == 0


def test_unrelated_crypto_order_is_not_excluded_from_portfolio_risk(tmp_path):
    broker = Broker(
        orders=[_order(symbol="ETHUSD", notional="4", client_order_id="eth-open")]
    )
    storage, _, _, _, _, result = _evaluate(tmp_path, broker=broker)
    snapshot = json.loads(storage.fetch_all(
        "SELECT snapshot_json FROM crypto_risk_snapshots WHERE id=?", (result.snapshot_id,)
    )[0]["snapshot_json"])

    assert snapshot["aggregate"]["pending_crypto_buy_notional"] == "4"
    assert snapshot["aggregate"]["open_crypto_order_count"] == 1
    assert result.risk_eligible is False


def test_missing_broker_state_persists_zero_authority_without_exception_or_order(tmp_path):
    storage = _storage(tmp_path)
    config = _config()
    broker = Broker()
    capability, market = _evidence(storage, config, broker)
    broker.get_account = lambda: (_ for _ in ()).throw(TimeoutError("account"))
    broker.get_positions = lambda: (_ for _ in ()).throw(TimeoutError("positions"))
    broker.get_open_orders = lambda: (_ for _ in ()).throw(TimeoutError("orders"))
    broker.get_loss_metrics = lambda: (_ for _ in ()).throw(TimeoutError("loss"))
    broker.get_crypto_historical_bars = lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError("bars"))

    result = CryptoRiskStore(storage).evaluate(
        config, broker, "run", capability.id, market.id, _request(), now=NOW
    )
    snapshot = CryptoRiskStore(storage).load_verified(result.snapshot_id, config, now=NOW)

    assert snapshot["authoritative"] is False
    assert snapshot["derived_authority"]["derivation_status"] == "failed_closed"
    assert snapshot["derived_authority"]["hard_notional_ceiling"] == "0"
    assert result.risk_eligible is False
    assert result.sizing.eligible is False
    assert broker.submit_calls == 0


def test_negative_or_wrong_class_position_fails_snapshot_authority(tmp_path):
    negative = SimpleNamespace(
        symbol="BTCUSD", asset_class="crypto", qty="-0.1", market_value="10",
        current_price="100", side="short",
    )
    wrong_class = SimpleNamespace(
        symbol="ETHUSD", asset_class="us_equity", qty="0.1", market_value="10",
        current_price="100", side="long",
    )
    storage, _, broker, _, _, result = _evaluate(
        tmp_path, broker=Broker(positions=[negative, wrong_class])
    )
    snapshot = json.loads(storage.fetch_all(
        "SELECT snapshot_json FROM crypto_risk_snapshots WHERE id=?", (result.snapshot_id,)
    )[0]["snapshot_json"])

    assert snapshot["authoritative"] is False
    assert any("quantity" in reason or "wrong_asset_class" in reason for reason in snapshot["failure_reasons"])
    assert result.risk_eligible is False
    assert broker.submit_calls == 0


def test_risk_decision_tampering_fails_even_if_snapshot_remains_valid(tmp_path):
    storage, config, _, _, _, result = _evaluate(tmp_path)
    storage.execute(
        "UPDATE crypto_risk_decisions SET sizing_fingerprint=? WHERE id=?",
        ("0" * 64, result.decision_id),
    )

    with pytest.raises(CryptoRiskError, match="persisted column mismatch"):
        CryptoRiskStore(storage).load_verified_decision(result.decision_id, config, now=NOW)


def test_crypto_sizing_and_risk_migrations_are_required_and_idempotent(tmp_path):
    storage = _storage(tmp_path)
    storage.apply_explicit_migrations()
    storage.apply_explicit_migrations()
    versions = {
        row["version"] for row in storage.fetch_all("SELECT version FROM schema_migrations")
    }

    assert CRYPTO_SIZING_SCHEMA_VERSION in versions
    assert CRYPTO_RISK_SCHEMA_VERSION in versions
    assert len(storage.fetch_all(
        "SELECT version FROM schema_migrations WHERE version IN (?,?)",
        (CRYPTO_SIZING_SCHEMA_VERSION, CRYPTO_RISK_SCHEMA_VERSION),
    )) == 2


def test_wide_spread_market_evidence_persists_zero_risk_authority(tmp_path):
    storage = _storage(tmp_path)
    config = _config()
    broker = Broker()
    capability = CryptoCapabilityStore(storage).capture(config, broker, "run", now=NOW)
    broker.get_crypto_latest_quote = lambda symbol: SimpleNamespace(
        bid_price="100", ask_price="102", bid_size="20", ask_size="20", timestamp=NOW,
    )
    broker.get_crypto_latest_orderbook = lambda symbol: SimpleNamespace(
        bids=[SimpleNamespace(price="100", size="20")],
        asks=[SimpleNamespace(price="102", size="20")], timestamp=NOW,
    )
    market = CryptoMarketDataStore(storage).capture(
        config, broker, capability, "run", "research", "BTC/USD", now=NOW
    )
    assert market.authoritative is True and market.execution_eligible is False

    result = CryptoRiskStore(storage).evaluate(
        config, broker, "run", capability.id, market.id, _request(), now=NOW
    )
    snapshot = CryptoRiskStore(storage).load_verified(result.snapshot_id, config, now=NOW)
    assert snapshot["authoritative"] is False
    assert "crypto_market_evidence_not_execution_eligible" in snapshot["failure_reasons"]
    assert result.sizing.eligible is False
    assert broker.submit_calls == 0


def test_market_evidence_that_ages_after_capture_cannot_support_risk(tmp_path):
    storage = _storage(tmp_path)
    config = _config()
    broker = Broker()
    capability, market = _evidence(storage, config, broker)
    later = NOW + timedelta(seconds=301)

    result = CryptoRiskStore(storage).evaluate(
        config, broker, "run", capability.id, market.id, _request(), now=later
    )
    snapshot = json.loads(storage.fetch_all(
        "SELECT snapshot_json FROM crypto_risk_snapshots WHERE id=?", (result.snapshot_id,)
    )[0]["snapshot_json"])
    assert "crypto_market_evidence_expired_before_risk_evaluation" in snapshot["failure_reasons"]
    assert result.risk_eligible is False
    assert broker.submit_calls == 0


def test_regenerated_local_sizing_digest_cannot_bless_changed_arithmetic(tmp_path):
    storage, config, _, _, _, result = _evaluate(tmp_path)
    row = storage.fetch_all(
        "SELECT decision_json FROM crypto_sizing_decisions WHERE id=?", (result.sizing.id,)
    )[0]
    payload = json.loads(row["decision_json"])
    payload["canonical_notional"] = "4.99"
    digest = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
    storage.execute(
        """UPDATE crypto_sizing_decisions
           SET canonical_notional=?,decision_json=?,decision_fingerprint=? WHERE id=?""",
        ("4.99", json.dumps(payload, sort_keys=True, separators=(",", ":")), digest, result.sizing.id),
    )

    with pytest.raises(RuntimeError, match="independent Decimal recomputation mismatch"):
        load_verified_crypto_sizing(storage, result.sizing.id, config)


def test_regenerated_local_risk_digest_cannot_bless_changed_capacity(tmp_path):
    storage, config, _, _, _, result = _evaluate(tmp_path)
    row = storage.fetch_all(
        "SELECT snapshot_json FROM crypto_risk_snapshots WHERE id=?", (result.snapshot_id,)
    )[0]
    payload = json.loads(row["snapshot_json"])
    payload["derived_authority"]["hard_notional_ceiling"] = "500"
    digest = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
    storage.execute(
        """UPDATE crypto_risk_snapshots
           SET derived_authority_json=?,snapshot_json=?,snapshot_fingerprint=? WHERE id=?""",
        (
            json.dumps(payload["derived_authority"], sort_keys=True, separators=(",", ":")),
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
            digest, result.snapshot_id,
        ),
    )

    with pytest.raises(CryptoRiskError, match="derived authority mismatch"):
        CryptoRiskStore(storage).load_verified(result.snapshot_id, config, now=NOW)


def test_regenerated_local_risk_digest_cannot_bless_changed_portfolio_aggregate(tmp_path):
    storage, config, _, _, _, result = _evaluate(tmp_path)
    row = storage.fetch_all(
        "SELECT snapshot_json FROM crypto_risk_snapshots WHERE id=?", (result.snapshot_id,)
    )[0]
    payload = json.loads(row["snapshot_json"])
    payload["aggregate"]["crypto_position_gross"] = "0.01"
    digest = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
    storage.execute(
        """UPDATE crypto_risk_snapshots
           SET aggregate_json=?,snapshot_json=?,snapshot_fingerprint=? WHERE id=?""",
        (
            json.dumps(payload["aggregate"], sort_keys=True, separators=(",", ":")),
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
            digest,
            result.snapshot_id,
        ),
    )

    with pytest.raises(CryptoRiskError, match="aggregate independent recomputation mismatch"):
        CryptoRiskStore(storage).load_verified(result.snapshot_id, config, now=NOW)


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (("formula_versions", "crypto_sizing"), "old", "formula_versions.crypto_sizing"),
        (("crypto", "sizing_policy", "maximum_order_notional_usd"), 6.0, "maximum_order_notional_usd"),
        (("crypto", "risk_policy", "maximum_stop_heat_pct_equity"), 0.001, "stop-risk limits"),
        (("crypto", "risk_policy", "loss_session_timezone"), "America/New_York", "must use UTC"),
        (("crypto", "risk_policy", "require_cash_funded"), False, "cash-funded"),
    ],
)
def test_crypto_sizing_risk_configuration_mutations_fail_validation(path, value, message):
    config = _config()
    target = config
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value

    with pytest.raises(ConfigurationError, match=message):
        validate_config(config)


def test_existing_position_cannot_be_mislabeled_as_a_new_entry(tmp_path):
    broker = Broker(positions=[_position(qty="0.02", value="2")])
    _, _, _, _, _, result = _evaluate(tmp_path, broker=broker, request=_request(action="entry"))

    assert result.risk_eligible is False
    assert any("entry requires no existing" in reason for reason in result.reasons)
    assert result.sizing.eligible is False
    assert broker.submit_calls == 0


def test_add_requires_existing_position_and_remains_disabled(tmp_path):
    _, _, broker, _, _, result = _evaluate(tmp_path, request=_request(action="add"))

    assert result.risk_eligible is False
    assert any("ADD requires an existing" in reason for reason in result.reasons)
    assert any("ADDs remain disabled" in reason for reason in result.reasons)
    assert broker.submit_calls == 0


def test_loss_drawdown_and_volatility_halts_do_not_block_valid_risk_reducing_sell(tmp_path):
    storage = _storage(tmp_path)
    storage.execute(
        "INSERT INTO cash_snapshots(run_id,equity,created_at) VALUES(?,?,?)",
        ("historical", 120000, (NOW - timedelta(days=1)).isoformat()),
    )
    config = _config()
    broker = Broker(
        positions=[_position(qty="0.02", value="2")],
        daily_loss="5000", weekly_loss="10000", volatility_step=D("0.03"),
    )
    capability, market = _evidence(storage, config, broker)
    request = _request(
        side="sell", action="exit", request_basis="quantity",
        requested_stop_risk_dollars=None, stop_price=None,
        requested_exit_quantity=D("0.02"), close_entire_position=True,
    )

    result = CryptoRiskStore(storage).evaluate(
        config, broker, "run", capability.id, market.id, request, now=NOW
    )

    assert result.risk_eligible is True
    assert result.sizing.eligible is True
    assert D(result.sizing.canonical_quantity) == D("0.02")
    assert result.execution_authorized is False
    assert broker.submit_calls == 0


def test_same_symbol_open_buy_still_blocks_a_sell_while_pending_sell_is_capacity_only(tmp_path):
    request = _request(
        side="sell", action="exit", request_basis="quantity",
        requested_stop_risk_dollars=None, stop_price=None,
        requested_exit_quantity=D("0.01"), close_entire_position=False,
    )
    buy_broker = Broker(
        positions=[_position(qty="0.02", value="2")],
        orders=[_order(side="buy", qty="0.005", notional="0.5")],
    )
    _, _, _, _, _, blocked = _evaluate(
        tmp_path / "buy", broker=buy_broker, request=request,
    )
    assert blocked.risk_eligible is False
    assert any("order conflicts" in reason for reason in blocked.reasons)

    sell_broker = Broker(
        positions=[_position(qty="0.02", value="2")],
        orders=[_order(side="sell", qty="0.005")],
    )
    _, _, _, _, _, allowed = _evaluate(
        tmp_path / "sell", broker=sell_broker,
        request=_request(
            side="sell", action="exit", request_basis="quantity",
            requested_stop_risk_dollars=None, stop_price=None,
            requested_exit_quantity=D("0.015"), close_entire_position=True,
        ),
    )
    assert allowed.risk_eligible is True
    assert D(allowed.sizing.canonical_quantity) == D("0.015")


def test_volatility_throttle_reduces_notional_without_exceeding_any_cap(tmp_path):
    storage, _, _, _, _, result = _evaluate(
        tmp_path, broker=Broker(volatility_step=D("0.01")),
        request=_request(requested_stop_risk_dollars=D("10")),
    )
    snapshot = json.loads(storage.fetch_all(
        "SELECT snapshot_json FROM crypto_risk_snapshots WHERE id=?", (result.snapshot_id,)
    )[0]["snapshot_json"])

    assert D("0.8") <= D(snapshot["volatility_evidence"]["annualized_volatility"]) < D("1.5")
    assert snapshot["derived_authority"]["volatility_multiplier"] == "0.5"
    assert D(result.sizing.canonical_notional) <= D("2.5")


def test_crypto_loss_control_uses_net_realized_pnl_not_sum_of_losing_legs(tmp_path):
    storage = _storage(tmp_path)
    for identifier, pnl in (("gain", 100), ("loss", -150)):
        storage.execute(
            """INSERT INTO realized_pnl_events(
              id,broker_event_key,symbol,side,quantity,fees,adjustments,realized_pl,
              occurred_at,trading_day,trading_week,accounting_timezone,source,
              provenance,confidence,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                identifier, f"event-{identifier}", "BTC/USD", "sell", 0.1, 0, 0, pnl,
                NOW.isoformat(), NOW.date().isoformat(), "2026-W29", "UTC",
                "paper_fill", "verified_test_fill", "verified", NOW.isoformat(),
            ),
        )
    config = _config()
    broker = Broker()
    capability, market = _evidence(storage, config, broker)
    result = CryptoRiskStore(storage).evaluate(
        config, broker, "run", capability.id, market.id, _request(), now=NOW
    )
    snapshot = json.loads(storage.fetch_all(
        "SELECT snapshot_json FROM crypto_risk_snapshots WHERE id=?", (result.snapshot_id,)
    )[0]["snapshot_json"])

    assert snapshot["loss_evidence"]["daily_crypto_realized_pnl_dollars"] == "-50"
    assert snapshot["loss_evidence"]["daily_crypto_realized_loss_dollars"] == "50"
    assert result.risk_eligible is True
