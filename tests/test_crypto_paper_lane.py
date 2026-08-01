from __future__ import annotations

import hashlib
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from types import SimpleNamespace
from typing import Any

import pytest

from app.crypto_capabilities import CryptoCapabilityStore
from app.crypto_market_data import CryptoMarketDataStore
from app.crypto_paper_lane import CryptoPaperLaneError, CryptoPaperLaneStore, format_crypto_paper_proposal
from app.crypto_risk import CryptoRiskStore
from app.crypto_sizing import CryptoSizingRequest
from app.crypto_strategies import CryptoStrategyStore
from app.lot_ledger import LotLedger
from app.storage import Storage
from app.configuration import effective_config_hash
from app.utils import load_config


NOW = datetime(2026, 7, 18, 4, 0, tzinfo=UTC)
ACCOUNT_ID = "supervised-crypto-paper-account"
ACCOUNT_HASH = hashlib.sha256(ACCOUNT_ID.encode()).hexdigest()


def _asset(symbol: str):
    return SimpleNamespace(
        id=f"asset-{symbol}", asset_class="crypto", exchange="CRYPTO", symbol=symbol,
        status="active", tradable=True, marginable=False, shortable=False,
        easy_to_borrow=False, fractionable=True, min_order_size="0.0001",
        min_trade_increment="0.0001", price_increment="0.1",
    )


class Broker:
    def __init__(self, *, submit_mode: str = "success"):
        self.submit_mode = submit_mode
        self.submit_calls: list[tuple[tuple, dict]] = []
        self.open_orders: list[Any] = []
        self.positions: list[Any] = []
        self.loss_captured_at = NOW
        self.account = SimpleNamespace(
            id=ACCOUNT_ID, status="active", currency="USD", equity="100000", cash="100000",
            non_marginable_buying_power="100000", account_blocked=False, trading_blocked=False,
        )

    def paper_account_identity(self):
        return {
            "verified": True, "mode": "paper", "endpoint_class": "paper",
            "account_status": "active", "account_currency": "USD", "account_id_hash": ACCOUNT_HASH,
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

    def get_crypto_historical_bars(self, symbol, timeframe="1Hour", limit=169):
        rows = []
        price = D("80")
        for index in range(max(169, limit)):
            price += D("0.12")
            rows.append(SimpleNamespace(
                symbol=symbol, timestamp=NOW - timedelta(hours=max(169, limit) - 1 - index), close=str(price),
            ))
        return rows

    def get_positions(self):
        return list(self.positions)

    def get_open_orders(self):
        return list(self.open_orders)

    def get_loss_metrics(self):
        return {
            "daily_loss_dollars": "0", "weekly_loss_dollars": "0", "reference_equity": "100000",
            "daily_loss_confidence": "verified", "weekly_loss_confidence": "verified",
            "provenance": "isolated_current_broker", "metrics_version": "loss_controls_v2",
            "captured_at": self.loss_captured_at.isoformat(),
        }

    def submit_crypto_order(self, *args, **kwargs):
        self.submit_calls.append((args, kwargs))
        if self.submit_mode == "not_attempted":
            from app.broker_interface import BrokerSubmissionNotAttempted

            raise BrokerSubmissionNotAttempted("adapter rejected before network I/O")
        if self.submit_mode == "timeout":
            raise TimeoutError("network timeout after invocation")
        return {"id": "crypto-broker-order-1", "status": "accepted"}


def _bars(count: int = 168):
    rows = []
    price = D("80")
    for index in range(count):
        price += D("0.12")
        rows.append({
            "symbol": "BTC/USD", "timestamp": NOW - timedelta(hours=count - 1 - index),
            "close": str(price), "high": str(price + D("0.10")), "low": str(price - D("0.10")),
        })
    scale = D("100") / D(rows[-1]["close"])
    for row in rows:
        for key in ("close", "high", "low"):
            row[key] = str(D(row[key]) * scale)
    return rows


def _storage(tmp_path):
    storage = Storage(tmp_path / "crypto-paper.sqlite3")
    storage.initialize()
    return storage


def _config():
    config = deepcopy(load_config())
    config["crypto"]["supervised_paper_lane"]["enabled"] = True
    config["crypto"]["supervised_paper_lane"]["execution_enabled"] = True
    return config


def _healthy_providers():
    return {name: (lambda: True) for name in ("power", "internet", "database", "telegram")}


def _disabled_config():
    config = deepcopy(load_config())
    crypto = config["crypto"]
    crypto["mode"] = "research_only"
    crypto["paper_trading_enabled"] = False
    crypto["proposals_enabled"] = False
    crypto["sizing_policy"]["mode"] = "research_only"
    crypto["risk_policy"]["mode"] = "research_only"
    crypto["strategy_policy"]["mode"] = "research_only"
    crypto["strategy_policy"]["lifecycle"] = "RESEARCH_ONLY"
    crypto["supervised_paper_lane"]["enabled"] = False
    crypto["supervised_paper_lane"]["execution_enabled"] = False
    config["effective_config_hash"] = effective_config_hash(config)
    return config


def _evidence(storage, config, broker):
    capability = CryptoCapabilityStore(storage).capture(config, broker, "run", now=NOW)
    storage.execute(
        """INSERT INTO crypto_research_runs(
          id,run_id,status,started_at,symbols,provider,capability_snapshot_id,
          capability_snapshot_fingerprint,capability_authoritative,payload
        ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        ("research", "run", "running", NOW.isoformat(), '["BTC/USD"]', "alpaca", capability.id,
         capability.snapshot_fingerprint, int(capability.authoritative), "{}"),
    )
    market = CryptoMarketDataStore(storage).capture(
        config, broker, capability, "run", "research", "BTC/USD", now=NOW,
    )
    strategy = CryptoStrategyStore(storage).evaluate(
        config, "run", "research", market.id, _bars(), now=NOW,
    )
    request = CryptoSizingRequest(
        source_type="crypto_strategy_decision", source_id=strategy.id,
        source_fingerprint=strategy.decision_fingerprint, symbol="BTC/USD", side="buy", action="entry",
        request_basis="notional", requested_stop_risk_dollars=D("10"), stop_price=strategy.stop_price,
    )
    risk = CryptoRiskStore(storage).evaluate(
        config, broker, "run", capability.id, market.id, request, now=NOW,
    )
    assert risk.risk_eligible
    return strategy, risk


def _ready(tmp_path, *, broker=None, control_providers=None):
    storage = _storage(tmp_path)
    config = _config()
    broker = broker or Broker()
    strategy, risk = _evidence(storage, config, broker)
    providers = _healthy_providers()
    if control_providers:
        providers.update(control_providers)
    lane = CryptoPaperLaneStore(storage, control_providers=providers)
    proposal = lane.create_proposal(config, strategy.id, risk.decision_id, now=NOW)
    lane.bind_telegram_message(proposal.id, "telegram-message-1", config, now=NOW)
    lane.approve_proposal(
        proposal.id, config, sender_id="operator", allowed_sender_id="operator",
        raw_message=f"YES CRYPTO {proposal.id}", reply_to_message_id="telegram-message-1", now=NOW,
    )
    intent = lane.create_intent(proposal.id, config, now=NOW)
    return storage, config, broker, lane, proposal, intent


def test_lane_requires_explicit_enablement_and_is_not_default(tmp_path):
    storage = _storage(tmp_path)
    config = _disabled_config()
    strategy, risk = _evidence(storage, config, Broker())
    with pytest.raises(CryptoPaperLaneError, match="disabled"):
        CryptoPaperLaneStore(storage).create_proposal(config, strategy.id, risk.decision_id, now=NOW)


def test_proposal_display_binds_full_crypto_authority_and_manual_command(tmp_path):
    storage = _storage(tmp_path)
    config = _config()
    broker = Broker()
    strategy, risk = _evidence(storage, config, broker)
    lane = CryptoPaperLaneStore(storage, control_providers=_healthy_providers())
    proposal = lane.create_proposal(config, strategy.id, risk.decision_id, now=NOW)
    assert proposal.display["symbol"] == "BTC/USD"
    assert proposal.display["current_bid"] == "100"
    assert proposal.display["current_ask"] == "100.1"
    assert proposal.display["spread_bps"]
    assert proposal.display["maximum_loss_usd"]
    assert D(proposal.display["expected_reward_usd"]) > 0
    rendered = format_crypto_paper_proposal(proposal)
    assert "expected reward" in rendered
    assert "Expected execution cost" in rendered
    assert "Total portfolio exposure" in rendered
    assert proposal.display["paper_only_warning"].startswith("PAPER ONLY")
    assert proposal.display["approval_command"] == f"YES CRYPTO {proposal.id}"
    assert storage.fetch_all("SELECT COUNT(*) n FROM trade_proposals")[0]["n"] == 0


def test_manual_approval_and_intent_are_idempotent_and_reserve_once(tmp_path):
    storage, config, broker, lane, proposal, intent = _ready(tmp_path)
    with pytest.raises(CryptoPaperLaneError, match="not pending"):
        lane.approve_proposal(proposal.id, config, sender_id="operator", allowed_sender_id="operator", reply_to_message_id="telegram-message-1", now=NOW)
    again = lane.create_intent(proposal.id, config, now=NOW)
    assert again.id == intent.id
    assert storage.fetch_all("SELECT COUNT(*) n FROM crypto_paper_intents")[0]["n"] == 1
    assert storage.fetch_all("SELECT COUNT(*) n FROM crypto_paper_reservations")[0]["n"] == 1


def test_kill_switch_changes_after_snapshot_and_blocks_before_broker_io(tmp_path):
    storage, config, broker, lane, proposal, intent = _ready(tmp_path)
    storage.set_control_state("kill_switch_active", "true", "test", "test", "", None, None, None)
    result = lane.execute_intent(intent.id, config, broker, now=NOW)
    assert result["state"] == "retryable_pre_submission"
    assert result["broker_invocation_occurred"] == 0
    assert broker.submit_calls == []


@pytest.mark.parametrize("control", ["power", "internet", "database"])
def test_required_control_probe_changes_after_snapshot_blocks_before_broker_io(tmp_path, control):
    providers = {control: lambda: False}
    storage, config, broker, lane, proposal, intent = _ready(
        tmp_path, control_providers=providers,
    )
    result = lane.execute_intent(intent.id, config, broker, now=NOW)
    assert result["state"] == "retryable_pre_submission"
    assert result["broker_invocation_occurred"] == 0
    assert control in result["last_error"]
    assert broker.submit_calls == []


def test_required_telegram_health_change_blocks_before_broker_io(tmp_path):
    providers = {"telegram": lambda: False}
    storage, config, broker, lane, proposal, intent = _ready(
        tmp_path, control_providers=providers,
    )
    config["telegram"]["crypto_execution_health_required"] = True
    result = lane.execute_intent(intent.id, config, broker, now=NOW)
    assert result["state"] == "retryable_pre_submission"
    assert result["broker_invocation_occurred"] == 0
    assert "telegram_health_not_verified" in result["last_error"]
    assert broker.submit_calls == []


def test_loss_evidence_becoming_stale_blocks_before_broker_io(tmp_path):
    storage, config, broker, lane, proposal, intent = _ready(tmp_path)
    broker.loss_captured_at = NOW - timedelta(minutes=6)
    result = lane.execute_intent(intent.id, config, broker, now=NOW)
    assert result["state"] == "retryable_pre_submission"
    assert result["broker_invocation_occurred"] == 0
    assert "crypto_loss_evidence_stale" in result["last_error"]
    assert broker.submit_calls == []


def test_open_order_appearing_after_snapshot_blocks_before_broker_io(tmp_path):
    storage, config, broker, lane, proposal, intent = _ready(tmp_path)
    broker.open_orders.append(SimpleNamespace(
        id="unrelated-order", client_order_id="unrelated-client", symbol="BTC/USD",
        side="buy", status="new", qty="0.1", filled_qty="0",
    ))
    result = lane.execute_intent(intent.id, config, broker, now=NOW)
    assert result["state"] == "retryable_pre_submission"
    assert result["broker_invocation_occurred"] == 0
    assert "final_crypto_risk_or_sizing_ineligible" in result["last_error"] or "conflicting_crypto_open_order" in result["last_error"]
    assert broker.submit_calls == []


def test_approval_superseded_or_reservation_revoked_blocks_before_broker_io(tmp_path):
    storage, config, broker, lane, proposal, intent = _ready(tmp_path)
    storage.execute("UPDATE crypto_paper_approvals SET status='expired' WHERE proposal_id=?", (proposal.id,))
    result = lane.execute_intent(intent.id, config, broker, now=NOW)
    assert result["state"] == "retryable_pre_submission"
    assert result["broker_invocation_occurred"] == 0
    assert "approval_not_consumed" in result["last_error"]
    assert broker.submit_calls == []

    storage2, config2, broker2, lane2, proposal2, intent2 = _ready(tmp_path / "revoked")
    storage2.execute("UPDATE crypto_paper_reservations SET state='released' WHERE intent_id=?", (intent2.id,))
    result2 = lane2.execute_intent(intent2.id, config2, broker2, now=NOW)
    assert result2["state"] == "retryable_pre_submission"
    assert result2["broker_invocation_occurred"] == 0
    assert "reservation_not_active" in result2["last_error"]
    assert broker2.submit_calls == []


def test_configuration_identity_change_blocks_before_broker_io(tmp_path):
    storage, config, broker, lane, proposal, intent = _ready(tmp_path)
    changed = deepcopy(config)
    changed["effective_config_hash"] = "f" * 64
    result = lane.execute_intent(intent.id, changed, broker, now=NOW)
    assert result["state"] == "retryable_pre_submission"
    assert result["broker_invocation_occurred"] == 0
    assert "configuration_changed" in result["last_error"] or "identity" in result["last_error"]
    assert broker.submit_calls == []


def test_successful_manual_paper_order_and_fill_release_reservation(tmp_path):
    storage, config, broker, lane, proposal, intent = _ready(tmp_path)
    result = lane.execute_intent(intent.id, config, broker, now=NOW)
    assert result["state"] == "submitted"
    assert result["broker_invocation_occurred"] == 1
    assert len(broker.submit_calls) == 1
    args, kwargs = broker.submit_calls[0]
    assert args[0:2] == ("BTC/USD", "buy")
    assert args[2] == {"notional": intent.requested_notional}
    assert kwargs["client_order_id"] == intent.client_order_id
    fill = lane.record_fill(
        intent.id, "broker-fill-1", intent.requested_quantity, "101", "0.025", config,
        broker_evidence={
            "verified": True, "broker_order_id": "crypto-broker-order-1",
            "client_order_id": intent.client_order_id, "symbol": "BTC/USD", "side": "buy",
            "status": "filled", "cumulative_filled_quantity": intent.requested_quantity,
            "filled_average_price": "101", "fees": "0.025", "paper_account_id_hash": ACCOUNT_HASH,
            "payload": {"id": "crypto-broker-order-1", "status": "filled"},
        },
        occurred_at=NOW,
    )
    assert fill["state"] == "filled"
    reservation = storage.fetch_all("SELECT state,active_notional,active_stop_risk FROM crypto_paper_reservations")[0]
    assert reservation["state"] == "released"
    assert reservation["active_notional"] == "0"
    assert storage.fetch_all("SELECT COUNT(*) n FROM position_lots WHERE symbol='BTC/USD'")[0]["n"] == 1
    metrics = lane.portfolio_metrics(broker, config, now=NOW)
    assert metrics["asset_class"] == "crypto"
    assert D(metrics["crypto_exposure"]) > 0
    assert metrics["crypto_fees"] == "0.025"
    assert metrics["equity_session_metrics_included"] is False
    assert all(value == 0 for value in lane.integrity_report().values())


def test_submit_response_cannot_become_verified_fill_without_reconciliation(tmp_path):
    class FilledResponseBroker(Broker):
        def submit_crypto_order(self, *args, **kwargs):
            self.submit_calls.append((args, kwargs))
            return {"id": "crypto-broker-order-1", "status": "filled"}

    storage, config, broker, lane, proposal, intent = _ready(
        tmp_path, broker=FilledResponseBroker(),
    )
    result = lane.execute_intent(intent.id, config, broker, now=NOW)
    assert result["state"] == "submitted"
    assert storage.fetch_all("SELECT COUNT(*) n FROM crypto_paper_fills")[0]["n"] == 0
    reservation = storage.fetch_all(
        "SELECT state,active_notional FROM crypto_paper_reservations WHERE intent_id=?",
        (intent.id,),
    )[0]
    assert reservation["state"] == "active"
    assert D(reservation["active_notional"]) > 0


def test_duplicate_crypto_fill_is_idempotent_but_conflicting_payload_fails(tmp_path):
    storage, config, broker, lane, proposal, intent = _ready(tmp_path)
    evidence = {
        "verified": True, "broker_order_id": "crypto-broker-order-1",
        "client_order_id": intent.client_order_id, "symbol": "BTC/USD", "side": "buy",
        "status": "filled", "cumulative_filled_quantity": intent.requested_quantity,
        "filled_average_price": "101", "fees": "0.025", "paper_account_id_hash": ACCOUNT_HASH,
        "payload": {"id": "crypto-broker-order-1", "status": "filled"},
    }
    lane.execute_intent(intent.id, config, broker, now=NOW)
    first = lane.record_fill(
        intent.id, "broker-fill-duplicate", intent.requested_quantity, "101", "0.025", config,
        broker_evidence=evidence, occurred_at=NOW,
    )
    duplicate = lane.record_fill(
        intent.id, "broker-fill-duplicate", intent.requested_quantity, "101", "0.025", config,
        broker_evidence=evidence, occurred_at=NOW,
    )
    assert first["status"] == "recorded"
    assert duplicate["status"] == "duplicate"
    with pytest.raises(CryptoPaperLaneError, match="conflicting duplicate"):
        lane.record_fill(
            intent.id, "broker-fill-duplicate", intent.requested_quantity, "102", "0.025", config,
            broker_evidence=evidence, occurred_at=NOW,
        )
    assert storage.fetch_all("SELECT COUNT(*) n FROM crypto_paper_fills")[0]["n"] == 1


def test_partial_crypto_fills_keep_cumulative_performance_and_incremental_fees(tmp_path):
    storage, config, broker, lane, proposal, intent = _ready(tmp_path)
    lane.execute_intent(intent.id, config, broker, now=NOW)
    requested = D(intent.requested_quantity)
    half = requested / D("2")
    remaining = requested - half

    def evidence(cumulative: D, fees: str):
        return {
            "verified": True, "broker_order_id": "crypto-broker-order-1",
            "client_order_id": intent.client_order_id, "symbol": "BTC/USD", "side": "buy",
            "status": "partially_filled" if cumulative < requested else "filled",
            "cumulative_filled_quantity": str(cumulative), "filled_average_price": "101",
            "fees": fees, "paper_account_id_hash": ACCOUNT_HASH,
            "payload": {"id": "crypto-broker-order-1", "filled_qty": str(cumulative)},
        }

    first = lane.record_fill(
        intent.id, "broker-fill-part-1", half, "101", "0.01", config,
        broker_evidence=evidence(half, "0.01"), occurred_at=NOW,
    )
    second = lane.record_fill(
        intent.id, "broker-fill-part-2", remaining, "101", "0.01", config,
        broker_evidence=evidence(requested, "0.02"), occurred_at=NOW,
    )
    assert first["state"] == "partially_filled"
    assert second["state"] == "filled"
    assert storage.fetch_all("SELECT COUNT(*) n FROM crypto_performance_links")[0]["n"] == 2
    outcome = storage.fetch_all(
        "SELECT entry_qty,entry_notional,status FROM performance_outcomes WHERE setup_id IN (SELECT setup_id FROM crypto_performance_links)"
    )[0]
    # Legacy Performance Lab columns are REAL presentation fields; the exact
    # values remain in crypto_performance_links and the crypto ledger.
    assert float(outcome["entry_qty"]) == pytest.approx(float(requested))
    assert float(outcome["entry_notional"]) == pytest.approx(float(requested * D("101")))
    assert outcome["status"] == "actual_fill"
    assert storage.fetch_all("SELECT SUM(fees) fees FROM crypto_paper_fills")[0]["fees"] == pytest.approx(0.02)


def test_supervised_crypto_sell_exit_uses_quantity_and_current_holdings(tmp_path):
    storage = _storage(tmp_path)
    config = _config()
    broker = Broker()
    broker.positions = [SimpleNamespace(
        symbol="BTC/USD", asset_class="crypto", side="long", qty="0.1",
        market_value="10", current_price="100",
    )]
    capability = CryptoCapabilityStore(storage).capture(config, broker, "run-sell", now=NOW)
    storage.execute(
        """INSERT INTO crypto_research_runs(
          id,run_id,status,started_at,symbols,provider,capability_snapshot_id,
          capability_snapshot_fingerprint,capability_authoritative,payload
        ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        ("research-sell", "run-sell", "running", NOW.isoformat(), '["BTC/USD"]', "alpaca", capability.id,
         capability.snapshot_fingerprint, int(capability.authoritative), "{}"),
    )
    market = CryptoMarketDataStore(storage).capture(
        config, broker, capability, "run-sell", "research-sell", "BTC/USD", now=NOW,
    )
    request = CryptoSizingRequest(
        source_type="crypto_position_management", source_id="position-lifecycle-1",
        source_fingerprint="a" * 64, symbol="BTC/USD", side="sell", action="exit",
        request_basis="quantity", requested_exit_quantity=D("0.05"),
    )
    risk = CryptoRiskStore(storage).evaluate(
        config, broker, "run-sell", capability.id, market.id, request, now=NOW,
    )
    assert risk.risk_eligible
    lane = CryptoPaperLaneStore(storage, control_providers=_healthy_providers())
    proposal = lane.create_proposal(config, None, risk.decision_id, now=NOW)
    assert proposal.side == "sell"
    assert proposal.quantity == "0.05"
    assert proposal.display["action"] == "SELL EXIT"
    lane.bind_telegram_message(proposal.id, "telegram-sell-1", config, now=NOW)
    lane.approve_proposal(
        proposal.id, config, sender_id="operator", allowed_sender_id="operator",
        raw_message=f"YES CRYPTO {proposal.id}", reply_to_message_id="telegram-sell-1", now=NOW,
    )
    intent = lane.create_intent(proposal.id, config, now=NOW)
    with storage.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        LotLedger.apply_fill_in_transaction(
            conn,
            intent={
                "id": "seed-crypto-buy", "symbol": "BTC/USD", "side": "buy",
                "requested_quantity": "0.1", "approved_quantity": "0.1",
                "position_lifecycle_id": None, "config_hash": config["effective_config_hash"],
            },
            broker_event_key="seed-crypto-buy-fill", delta_quantity="0.1", fill_price="99",
            occurred_at=NOW.isoformat(), source="crypto_paper_fill", accounting_timezone="UTC",
        )
        conn.execute(
            """INSERT INTO crypto_paper_lots(
               id,symbol,source_fill_event_key,opened_at,original_quantity,remaining_quantity,
               unit_cost,fees_allocated,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?)""",
            ("seed-crypto-lot", "BTC/USD", "seed-crypto-buy-fill", NOW.isoformat(), "0.1", "0.1", "99", "0", NOW.isoformat()),
        )
    result = lane.execute_intent(intent.id, config, broker, now=NOW)
    assert result["state"] == "submitted"
    assert broker.submit_calls[0][0][0:2] == ("BTC/USD", "sell")
    assert broker.submit_calls[0][0][2] == {"qty": "0.05"}
    fill = lane.record_fill(
        intent.id, "broker-sell-fill-1", "0.05", "100", "0.0125", config,
        broker_evidence={
            "verified": True, "broker_order_id": "crypto-broker-order-1",
            "client_order_id": intent.client_order_id, "symbol": "BTC/USD", "side": "sell",
            "status": "filled", "cumulative_filled_quantity": "0.05",
            "filled_average_price": "100", "fees": "0.0125", "paper_account_id_hash": ACCOUNT_HASH,
            "payload": {"id": "crypto-broker-order-1", "status": "filled"},
        },
        occurred_at=NOW,
    )
    assert fill["state"] == "filled"
    assert broker.submit_calls == [broker.submit_calls[0]]
    assert all(value == 0 for value in lane.integrity_report().values())


def test_crypto_sell_holdings_decrease_after_snapshot_blocks_before_broker_io(tmp_path):
    storage = _storage(tmp_path)
    config = _config()
    broker = Broker()
    broker.positions = [SimpleNamespace(
        symbol="BTC/USD", asset_class="crypto", side="long", qty="0.1",
        market_value="10", current_price="100",
    )]
    capability = CryptoCapabilityStore(storage).capture(config, broker, "run-sell-holdings", now=NOW)
    storage.execute(
        """INSERT INTO crypto_research_runs(
          id,run_id,status,started_at,symbols,provider,capability_snapshot_id,
          capability_snapshot_fingerprint,capability_authoritative,payload
        ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        ("research-sell-holdings", "run-sell-holdings", "running", NOW.isoformat(), '["BTC/USD"]', "alpaca",
         capability.id, capability.snapshot_fingerprint, int(capability.authoritative), "{}"),
    )
    market = CryptoMarketDataStore(storage).capture(
        config, broker, capability, "run-sell-holdings", "research-sell-holdings", "BTC/USD", now=NOW,
    )
    risk = CryptoRiskStore(storage).evaluate(
        config, broker, "run-sell-holdings", capability.id, market.id,
        CryptoSizingRequest(
            source_type="crypto_position_management", source_id="position-lifecycle-2",
            source_fingerprint="b" * 64, symbol="BTC/USD", side="sell", action="exit",
            request_basis="quantity", requested_exit_quantity=D("0.05"),
        ), now=NOW,
    )
    lane = CryptoPaperLaneStore(storage, control_providers=_healthy_providers())
    proposal = lane.create_proposal(config, None, risk.decision_id, now=NOW)
    lane.bind_telegram_message(proposal.id, "telegram-sell-2", config, now=NOW)
    lane.approve_proposal(
        proposal.id, config, sender_id="operator", allowed_sender_id="operator",
        raw_message=f"YES CRYPTO {proposal.id}", reply_to_message_id="telegram-sell-2", now=NOW,
    )
    intent = lane.create_intent(proposal.id, config, now=NOW)
    broker.positions = []
    result = lane.execute_intent(intent.id, config, broker, now=NOW)
    assert result["state"] == "retryable_pre_submission"
    assert result["broker_invocation_occurred"] == 0
    assert "sellable_quantity_decreased" in result["last_error"] or "no_sellable_crypto_quantity" in result["last_error"]
    assert broker.submit_calls == []


def test_broker_absent_before_invocation_is_retryable_not_unknown(tmp_path):
    storage, config, broker, lane, proposal, intent = _ready(tmp_path)
    class NoCryptoAdapter(Broker):
        submit_crypto_order = None

    no_adapter = NoCryptoAdapter()
    result = lane.execute_intent(intent.id, config, no_adapter, now=NOW)
    assert result["state"] == "retryable_pre_submission"
    assert result["broker_invocation_occurred"] == 0
    assert storage.fetch_all("SELECT state FROM crypto_paper_intents")[0]["state"] == "retryable_pre_submission"


def test_expired_approval_terminalises_and_releases_without_broker_io(tmp_path):
    storage, config, broker, lane, proposal, intent = _ready(tmp_path)
    expired_at = (NOW - timedelta(seconds=1)).isoformat()
    storage.execute(
        "UPDATE crypto_paper_proposals SET expires_at=? WHERE id=?",
        (expired_at, proposal.id),
    )
    result = lane.execute_intent(intent.id, config, broker, now=NOW)
    assert result["state"] == "expired"
    assert result["broker_invocation_occurred"] == 0
    assert result["broker_call"] is False
    assert broker.submit_calls == []
    states = storage.fetch_all(
        "SELECT p.status proposal_status,a.status approval_status,i.state intent_state,r.state reservation_state "
        "FROM crypto_paper_proposals p "
        "JOIN crypto_paper_approvals a ON a.proposal_id=p.id "
        "JOIN crypto_paper_intents i ON i.proposal_id=p.id "
        "JOIN crypto_paper_reservations r ON r.intent_id=i.id WHERE p.id=?",
        (proposal.id,),
    )
    assert dict(states[0]) == {
        "proposal_status": "expired", "approval_status": "expired",
        "intent_state": "expired", "reservation_state": "released",
    }
    assert all(value == 0 for value in lane.integrity_report().values())


def test_timeout_after_invocation_is_ambiguous_and_never_auto_retried(tmp_path):
    storage, config, broker, lane, proposal, intent = _ready(tmp_path)
    broker.submit_mode = "timeout"
    result = lane.execute_intent(intent.id, config, broker, now=NOW)
    assert result["state"] == "unknown"
    assert result["broker_invocation_occurred"] == 1
    assert len(broker.submit_calls) == 1
    again = lane.execute_intent(intent.id, config, broker, now=NOW)
    assert again["state"] == "unknown"
    assert len(broker.submit_calls) == 1


def test_tampered_display_or_approval_reply_target_fails_closed(tmp_path):
    storage, config, broker, lane, proposal, intent = _ready(tmp_path)
    row = storage.fetch_all("SELECT display_json FROM crypto_paper_proposals WHERE id=?", (proposal.id,))[0]
    display = __import__("json").loads(row["display_json"])
    display["notional_usd"] = "500000"
    storage.execute("UPDATE crypto_paper_proposals SET display_json=? WHERE id=?", (__import__("json").dumps(display), proposal.id))
    with pytest.raises(CryptoPaperLaneError, match="fingerprint"):
        lane.load_proposal(proposal.id, config, now=NOW)


def test_repeated_integrity_checks_have_no_crypto_paper_violations(tmp_path):
    storage, config, broker, lane, proposal, intent = _ready(tmp_path)
    first = lane.integrity_report()
    second = lane.integrity_report()
    assert first == second
    assert set(first) >= {"crypto_paper_intents_without_reservation", "crypto_paper_ambiguous_auto_retry"}
    assert all(value == 0 for value in first.values())


def test_crypto_fifo_events_use_utc_continuous_loss_sessions(tmp_path):
    storage = _storage(tmp_path)
    buy = {"id": "crypto-buy", "symbol": "BTC/USD", "side": "buy", "requested_quantity": "1", "position_lifecycle_id": None}
    sell = {"id": "crypto-sell", "symbol": "BTC/USD", "side": "sell", "requested_quantity": "1", "position_lifecycle_id": None}
    with storage.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        LotLedger.apply_fill_in_transaction(
            conn, intent=buy, broker_event_key="crypto-buy-fill", delta_quantity="1",
            fill_price="100", occurred_at="2026-07-18T23:30:00+00:00", source="crypto_paper_fill",
            accounting_timezone="UTC",
        )
        LotLedger.apply_fill_in_transaction(
            conn, intent=sell, broker_event_key="crypto-sell-fill", delta_quantity="1",
            fill_price="101", occurred_at="2026-07-18T23:30:00+00:00", source="crypto_paper_fill",
            accounting_timezone="UTC",
        )
        event = conn.execute(
            "SELECT trading_day,accounting_timezone FROM realized_pnl_events WHERE broker_event_key='crypto-sell-fill'"
        ).fetchone()
    assert dict(event) == {"trading_day": "2026-07-18", "accounting_timezone": "UTC"}
