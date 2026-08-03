from __future__ import annotations

import hashlib
import sqlite3
import time
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
from app.fixed_point_accounting import fixed_point_integrity_report
from app.lot_ledger import LotLedger
from app.storage import Storage
from app.strategy_performance import StrategyPerformanceEngine
from app.configuration import effective_config_hash
from app.service import TradingService
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

    def crypto_submission_available(self):
        return True


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


def _evidence(storage, config, broker, *, request_basis="notional"):
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
        request_basis=request_basis, requested_stop_risk_dollars=D("10"), stop_price=strategy.stop_price,
    )
    risk = CryptoRiskStore(storage).evaluate(
        config, broker, "run", capability.id, market.id, request, now=NOW,
    )
    assert risk.risk_eligible
    return strategy, risk


def _ready(tmp_path, *, broker=None, control_providers=None, config=None, request_basis="notional"):
    storage = _storage(tmp_path)
    config = config or _config()
    broker = broker or Broker()
    strategy, risk = _evidence(storage, config, broker, request_basis=request_basis)
    providers = _healthy_providers()
    if control_providers:
        providers.update(control_providers)
    lane = CryptoPaperLaneStore(storage, control_providers=providers)
    proposal = lane.create_proposal(config, strategy.id, risk.decision_id, now=NOW)
    lane.bind_telegram_message(proposal.id, "telegram-message-1", config, chat_id="123", now=NOW)
    lane.approve_proposal(
        proposal.id, config, sender_id="operator", allowed_sender_id="operator",
        raw_message=f"YES CRYPTO {proposal.id}", reply_to_message_id="telegram-message-1", chat_id="123", now=NOW,
    )
    intent = lane.create_intent(proposal.id, config, now=NOW)
    return storage, config, broker, lane, proposal, intent


def _attested_reconciliation_event(lane, intent_id, broker_evidence, *, occurred_at):
    """Create the same durable order-verification authority used by reconciliation.

    Direct fill tests still exercise the public fill API, but they must first
    persist the broker evidence event that the production reconciler supplies.
    """

    with lane.storage.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        intent = conn.execute("SELECT * FROM crypto_paper_intents WHERE id=?", (intent_id,)).fetchone()
        assert intent is not None
        evidence_id, evidence_fingerprint = lane._record_order_evidence_locked(
            conn, intent=intent, evidence=broker_evidence, captured_at=occurred_at,
        )
        return lane._record_reconciliation_event_locked(
            conn,
            intent=intent,
            event_type="order_verified",
            evidence={
                **broker_evidence,
                "evidence_id": evidence_id,
                "evidence_fingerprint": evidence_fingerprint,
            },
            now=occurred_at,
        )


def _record_fill(lane, intent_id, broker_event_key, quantity, price, fees, config, *, broker_evidence, occurred_at):
    event_id = _attested_reconciliation_event(
        lane, intent_id, broker_evidence, occurred_at=occurred_at,
    )
    return lane.record_fill(
        intent_id, broker_event_key, quantity, price, fees, config,
        broker_evidence=broker_evidence,
        reconciliation_event_id=event_id,
        occurred_at=occurred_at,
    )


class _TelegramListenerFake:
    """Small real-listener-shaped Telegram double for route integration tests."""

    is_mock = False
    allowed_user_id = "operator"
    chat_id = "123"

    def __init__(self, updates):
        self._updates = list(updates)
        self.sent: list[tuple[str, str | None]] = []

    def is_authorized(self, sender_id):
        return str(sender_id) == self.allowed_user_id

    def is_available(self, force=False):
        return True

    def get_updates(self, timeout=0, offset=None):
        updates, self._updates = self._updates, []
        return updates

    def send_message(self, text, chat_id=None):
        self.sent.append((str(text), None if chat_id is None else str(chat_id)))
        return SimpleNamespace(message_id=f"response-{len(self.sent)}")


def _listener_pending_crypto_proposal(tmp_path, monkeypatch):
    # The production route uses wall-clock ``now`` for recovery, approval, and
    # final controls.  Rebind the fixture's deterministic broker timestamps to
    # the current test instant so this exercises the real freshness/expiry path.
    current = datetime.now(UTC).replace(microsecond=0)
    monkeypatch.setitem(globals(), "NOW", current)
    original_bars = globals()["_bars"]

    def aligned_bars(count=168):
        original_now = globals()["NOW"]
        monkeypatch.setitem(
            globals(),
            "NOW",
            current.replace(minute=0, second=0, microsecond=0),
        )
        try:
            return original_bars(count)
        finally:
            monkeypatch.setitem(globals(), "NOW", original_now)

    monkeypatch.setitem(globals(), "_bars", aligned_bars)
    storage = _storage(tmp_path)
    config = _config()
    broker = Broker()
    strategy, risk = _evidence(storage, config, broker)
    lane = CryptoPaperLaneStore(storage, control_providers=_healthy_providers())
    proposal = lane.create_proposal(config, strategy.id, risk.decision_id, now=current)
    rendered = format_crypto_paper_proposal(proposal)
    lane.bind_telegram_message(
        proposal.id,
        "telegram-message-1",
        config,
        chat_id="123",
        rendered_text=rendered,
        now=current,
    )
    return storage, config, broker, proposal, rendered


def _crypto_listener_update(text, *, update_id=1, reply_to="telegram-message-1"):
    message = {
        "message_id": "telegram-command-1",
        "date": time.time(),
        "from": {"id": "operator"},
        "chat": {"id": "123"},
        "text": text,
    }
    if reply_to is not None:
        message["reply_to_message"] = {"message_id": reply_to}
    return {"update_id": update_id, "message": message}


def test_real_telegram_listener_routes_exact_crypto_yes_to_paper_lane(tmp_path, monkeypatch):
    storage, config, broker, proposal, _rendered = _listener_pending_crypto_proposal(
        tmp_path, monkeypatch
    )
    telegram = _TelegramListenerFake(
        [_crypto_listener_update(f"YES CRYPTO {proposal.id}")]
    )
    service = TradingService(config, storage, broker, "listener-integration-run")
    service.telegram = telegram
    service.listener_started_at = time.time() - 5
    service._crypto_paper_control_providers = lambda: _healthy_providers()

    service.process_telegram()

    approval = storage.fetch_all(
        "SELECT status,reply_to_message_id,telegram_chat_id FROM crypto_paper_approvals WHERE proposal_id=?",
        (proposal.id,),
    )[0]
    intent = storage.fetch_all(
        "SELECT state,broker_invocation_occurred FROM crypto_paper_intents WHERE proposal_id=?",
        (proposal.id,),
    )[0]
    assert approval["status"] == "consumed"
    assert approval["reply_to_message_id"] == "telegram-message-1"
    assert approval["telegram_chat_id"] == "123"
    assert intent["state"] == "submitted"
    assert intent["broker_invocation_occurred"] == 1
    assert len(broker.submit_calls) == 1
    assert storage.fetch_all("SELECT COUNT(*) n FROM crypto_paper_fills")[0]["n"] == 0
    assert any("Crypto paper order submitted" in message for message, _chat in telegram.sent)


def test_real_telegram_listener_routes_standalone_exact_crypto_yes_to_paper_lane(tmp_path, monkeypatch):
    storage, config, broker, proposal, _rendered = _listener_pending_crypto_proposal(
        tmp_path, monkeypatch
    )
    telegram = _TelegramListenerFake(
        [_crypto_listener_update(f"YES CRYPTO {proposal.id}", reply_to=None)]
    )
    service = TradingService(config, storage, broker, "listener-standalone-yes-run")
    service.telegram = telegram
    service.listener_started_at = time.time() - 5
    service._crypto_paper_control_providers = lambda: _healthy_providers()

    service.process_telegram()

    approval = storage.fetch_all(
        "SELECT status,reply_to_message_id,telegram_chat_id FROM crypto_paper_approvals WHERE proposal_id=?",
        (proposal.id,),
    )[0]
    intent = storage.fetch_all(
        "SELECT state,broker_invocation_occurred FROM crypto_paper_intents WHERE proposal_id=?",
        (proposal.id,),
    )[0]
    assert approval["status"] == "consumed"
    assert approval["reply_to_message_id"] == "telegram-message-1"
    assert approval["telegram_chat_id"] == "123"
    assert intent["state"] == "submitted"
    assert len(broker.submit_calls) == 1


def test_real_telegram_listener_routes_plain_yes_reply_to_crypto_proposal(tmp_path, monkeypatch):
    storage, config, broker, proposal, _rendered = _listener_pending_crypto_proposal(
        tmp_path, monkeypatch
    )
    telegram = _TelegramListenerFake([_crypto_listener_update("YES")])
    service = TradingService(config, storage, broker, "listener-plain-yes-run")
    service.telegram = telegram
    service.listener_started_at = time.time() - 5
    service._crypto_paper_control_providers = lambda: _healthy_providers()

    service.process_telegram()

    approval = storage.fetch_all(
        "SELECT raw_message,reply_to_message_id FROM crypto_paper_approvals WHERE proposal_id=?",
        (proposal.id,),
    )[0]
    intent = storage.fetch_all(
        "SELECT state FROM crypto_paper_intents WHERE proposal_id=?", (proposal.id,)
    )[0]
    assert approval["raw_message"] == "YES"
    assert approval["reply_to_message_id"] == "telegram-message-1"
    assert intent["state"] == "submitted"
    assert len(broker.submit_calls) == 1
    assert any("Crypto paper order submitted" in message for message, _chat in telegram.sent)


def test_real_telegram_listener_routes_exact_crypto_no_without_execution(tmp_path, monkeypatch):
    storage, config, broker, proposal, _rendered = _listener_pending_crypto_proposal(
        tmp_path, monkeypatch
    )
    telegram = _TelegramListenerFake(
        [_crypto_listener_update(f"NO CRYPTO {proposal.id}")]
    )
    service = TradingService(config, storage, broker, "listener-integration-run")
    service.telegram = telegram
    service.listener_started_at = time.time() - 5
    service._crypto_paper_control_providers = lambda: _healthy_providers()

    service.process_telegram()

    proposal_row = storage.fetch_all(
        "SELECT status FROM crypto_paper_proposals WHERE id=?", (proposal.id,)
    )[0]
    assert proposal_row["status"] == "rejected", telegram.sent
    assert storage.fetch_all("SELECT COUNT(*) n FROM crypto_paper_intents")[0]["n"] == 0
    assert storage.fetch_all("SELECT COUNT(*) n FROM crypto_paper_reservations")[0]["n"] == 0
    assert broker.submit_calls == []
    assert any("rejected" in message.lower() for message, _chat in telegram.sent)


def test_real_telegram_listener_routes_standalone_exact_crypto_no_without_execution(tmp_path, monkeypatch):
    storage, config, broker, proposal, _rendered = _listener_pending_crypto_proposal(
        tmp_path, monkeypatch
    )
    telegram = _TelegramListenerFake(
        [_crypto_listener_update(f"NO CRYPTO {proposal.id}", reply_to=None)]
    )
    service = TradingService(config, storage, broker, "listener-standalone-no-run")
    service.telegram = telegram
    service.listener_started_at = time.time() - 5
    service._crypto_paper_control_providers = lambda: _healthy_providers()

    service.process_telegram()

    assert storage.fetch_all(
        "SELECT status FROM crypto_paper_proposals WHERE id=?", (proposal.id,)
    )[0]["status"] == "rejected"
    assert storage.fetch_all("SELECT COUNT(*) n FROM crypto_paper_intents")[0]["n"] == 0
    assert broker.submit_calls == []


def test_real_telegram_listener_routes_plain_no_reply_without_execution(tmp_path, monkeypatch):
    storage, config, broker, proposal, _rendered = _listener_pending_crypto_proposal(
        tmp_path, monkeypatch
    )
    telegram = _TelegramListenerFake([_crypto_listener_update("NO")])
    service = TradingService(config, storage, broker, "listener-plain-no-run")
    service.telegram = telegram
    service.listener_started_at = time.time() - 5
    service._crypto_paper_control_providers = lambda: _healthy_providers()

    service.process_telegram()

    assert storage.fetch_all(
        "SELECT status FROM crypto_paper_proposals WHERE id=?", (proposal.id,)
    )[0]["status"] == "rejected"
    assert storage.fetch_all("SELECT COUNT(*) n FROM crypto_paper_approvals")[0]["n"] == 0
    assert broker.submit_calls == []
    assert any("rejected" in message.lower() for message, _chat in telegram.sent)


def test_crypto_telegram_outbox_is_durable_once_and_quarantines_ambiguous_send(tmp_path):
    storage = _storage(tmp_path)
    config = _config()
    broker = Broker()
    strategy, risk = _evidence(storage, config, broker)
    lane = CryptoPaperLaneStore(storage, control_providers=_healthy_providers())
    proposal = lane.create_proposal(config, strategy.id, risk.decision_id, now=NOW)
    rendered = format_crypto_paper_proposal(proposal)
    queued = lane.queue_telegram_display(
        proposal.id, config, chat_id="123", rendered_text=rendered, now=NOW,
    )
    assert queued["status"] == "queued"
    with pytest.raises(CryptoPaperLaneError, match="before the durable outbox send"):
        lane.bind_telegram_message(
            proposal.id, "telegram-outbox-message", config, chat_id="123",
            rendered_text=rendered, now=NOW,
        )
    assert lane.claim_telegram_display(proposal.id, now=NOW) is True
    assert lane.claim_telegram_display(proposal.id, now=NOW) is False
    recovered = lane.recover_telegram_outbox(now=NOW)
    assert recovered == [{"proposal_id": proposal.id, "status": "manual_review"}]
    outbox = storage.fetch_all(
        "SELECT status,error,telegram_message_id FROM crypto_paper_telegram_outbox WHERE proposal_id=?",
        (proposal.id,),
    )[0]
    assert outbox["status"] == "failed"
    assert "no resend" in outbox["error"]
    assert outbox["telegram_message_id"] is None
    assert storage.fetch_all(
        "SELECT status FROM crypto_paper_proposals WHERE id=?", (proposal.id,)
    )[0]["status"] == "manual_review"
    with storage.connect() as conn:
        with pytest.raises(sqlite3.IntegrityError, match="outbox authority is immutable"):
            conn.execute(
                "UPDATE crypto_paper_telegram_outbox SET rendered_text='tampered' WHERE proposal_id=?",
                (proposal.id,),
            )
    with storage.connect() as conn:
        with pytest.raises(sqlite3.IntegrityError, match="outbox is immutable"):
            conn.execute("DELETE FROM crypto_paper_telegram_outbox WHERE proposal_id=?", (proposal.id,))


def test_crypto_listener_requires_reply_and_explains_buy_sell_text(tmp_path, monkeypatch):
    storage, config, broker, proposal, _rendered = _listener_pending_crypto_proposal(
        tmp_path, monkeypatch
    )
    telegram = _TelegramListenerFake([_crypto_listener_update("BUY ENTRY ETH/USD", reply_to=None)])
    service = TradingService(config, storage, broker, "listener-buy-text-run")
    service.telegram = telegram
    service.listener_started_at = time.time() - 5
    service._crypto_paper_control_providers = lambda: _healthy_providers()

    service.process_telegram()

    assert storage.fetch_all(
        "SELECT status FROM crypto_paper_proposals WHERE id=?", (proposal.id,)
    )[0]["status"] == "pending"
    assert storage.fetch_all("SELECT COUNT(*) n FROM crypto_paper_approvals")[0]["n"] == 0
    assert broker.submit_calls == []
    assert any("not initiated by BUY/SELL text" in message for message, _chat in telegram.sent)


def test_crypto_listener_requires_direct_reply_for_plain_yes(tmp_path, monkeypatch):
    storage, config, broker, proposal, _rendered = _listener_pending_crypto_proposal(
        tmp_path, monkeypatch
    )
    telegram = _TelegramListenerFake([_crypto_listener_update("YES", reply_to=None)])
    service = TradingService(config, storage, broker, "listener-missing-reply-run")
    service.telegram = telegram
    service.listener_started_at = time.time() - 5
    service._crypto_paper_control_providers = lambda: _healthy_providers()

    service.process_telegram()

    assert storage.fetch_all(
        "SELECT status FROM crypto_paper_proposals WHERE id=?", (proposal.id,)
    )[0]["status"] == "pending"
    assert storage.fetch_all("SELECT COUNT(*) n FROM crypto_paper_approvals")[0]["n"] == 0
    assert broker.submit_calls == []
    assert any("requires a direct reply" in message for message, _chat in telegram.sent)


def test_crypto_listener_explains_symbol_suffixed_reply_without_acting(tmp_path, monkeypatch):
    storage, config, broker, proposal, _rendered = _listener_pending_crypto_proposal(
        tmp_path, monkeypatch
    )
    telegram = _TelegramListenerFake([_crypto_listener_update("YES ETH/USD")])
    service = TradingService(config, storage, broker, "listener-symbol-reply-run")
    service.telegram = telegram
    service.listener_started_at = time.time() - 5
    service._crypto_paper_control_providers = lambda: _healthy_providers()

    service.process_telegram()

    assert storage.fetch_all(
        "SELECT status FROM crypto_paper_proposals WHERE id=?", (proposal.id,)
    )[0]["status"] == "pending"
    assert storage.fetch_all("SELECT COUNT(*) n FROM crypto_paper_approvals")[0]["n"] == 0
    assert broker.submit_calls == []
    assert any("Reply directly with YES to approve or NO to reject" in message for message, _chat in telegram.sent)


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
    assert "Reply to this message with:" in rendered
    assert "YES = approve this Buy BTC/USD paper trade" in rendered
    assert "NO = reject this Buy BTC/USD paper trade" in rendered
    assert proposal.id not in rendered
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
    fill = _record_fill(lane,
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
    lifecycle = storage.fetch_all(
        "SELECT id,state,current_quantity,current_quantity_decimal,average_entry_price_decimal,source FROM position_lifecycles WHERE symbol='BTC/USD'"
    )[0]
    lot = storage.fetch_all(
        "SELECT position_lifecycle_id,remaining_quantity_decimal,decimal_provenance FROM position_lots WHERE symbol='BTC/USD'"
    )[0]
    intent_row = storage.fetch_all(
        "SELECT position_lifecycle_id FROM crypto_paper_intents WHERE id=?", (intent.id,)
    )[0]
    assert lifecycle["state"] == "active"
    assert lifecycle["current_quantity_decimal"] == intent.requested_quantity
    assert lifecycle["average_entry_price_decimal"] == "101"
    assert lifecycle["source"] == "crypto_paper_lane"
    assert lot["position_lifecycle_id"] == lifecycle["id"]
    assert lot["remaining_quantity_decimal"] == intent.requested_quantity
    assert lot["decimal_provenance"] == "exact_source_decimal"
    assert intent_row["position_lifecycle_id"] == lifecycle["id"]
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
    first = _record_fill(lane,
        intent.id, "broker-fill-duplicate", intent.requested_quantity, "101", "0.025", config,
        broker_evidence=evidence, occurred_at=NOW,
    )
    duplicate = _record_fill(lane,
        intent.id, "broker-fill-duplicate", intent.requested_quantity, "101", "0.025", config,
        broker_evidence=evidence, occurred_at=NOW,
    )
    assert first["status"] == "recorded"
    assert duplicate["status"] == "duplicate"
    with pytest.raises(CryptoPaperLaneError, match="conflicting duplicate"):
        _record_fill(lane,
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

    first = _record_fill(lane,
        intent.id, "broker-fill-part-1", half, "101", "0.01", config,
        broker_evidence=evidence(half, "0.01"), occurred_at=NOW,
    )
    second = _record_fill(lane,
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


def test_reconciliation_derives_each_partial_fill_price_from_cumulative_average(tmp_path):
    class ReconcileBroker(Broker):
        def __init__(self):
            super().__init__()
            self.order_response = None

        def get_order_by_client_order_id(self, client_order_id):
            return self.order_response

    storage, config, broker, lane, proposal, intent = _ready(
        tmp_path, broker=ReconcileBroker(), request_basis="quantity"
    )
    lane.execute_intent(intent.id, config, broker, now=NOW)
    requested = D(intent.requested_quantity)
    half = requested / D("2")

    def order(cumulative: str, average: str, fees: str, status: str):
        return {
            "id": "crypto-broker-order-1",
            "client_order_id": intent.client_order_id,
            "symbol": "BTC/USD",
            "side": "buy",
            "status": status,
            "qty": intent.requested_quantity,
            "filled_qty": cumulative,
            "filled_avg_price": average,
            "fees": fees,
            "limit_price": "100.1",
        }

    broker.order_response = order(str(half), "100", "0.01", "partially_filled")
    first = lane.reconcile_intent(intent.id, config, broker, now=NOW)
    broker.order_response = order(intent.requested_quantity, "101", "0.02", "filled")
    second = lane.reconcile_intent(intent.id, config, broker, now=NOW)

    assert first["state"] == "partially_filled"
    assert second["state"] == "filled"
    fills = storage.fetch_all(
        "SELECT quantity,price FROM crypto_paper_fills WHERE intent_id=? ORDER BY received_at,id",
        (intent.id,),
    )
    assert [D(row["quantity"]) for row in fills] == [half, half]
    assert [D(row["price"]) for row in fills] == [D("100"), D("102")]


def test_crypto_round_trip_closes_lifecycle_and_enters_attribution_and_strategy_evidence(tmp_path):
    config = _config()
    config["crypto"]["sizing_policy"]["maximum_order_notional_usd"] = "10.10"
    config["effective_config_hash"] = effective_config_hash(config)
    storage, config, broker, lane, _entry_proposal, entry_intent = _ready(
        tmp_path, config=config, request_basis="quantity"
    )
    lane.execute_intent(entry_intent.id, config, broker, now=NOW)
    entry_fill = _record_fill(lane,
        entry_intent.id, "broker-roundtrip-buy", entry_intent.requested_quantity, "101", "0.025", config,
        broker_evidence={
            "verified": True, "broker_order_id": "crypto-broker-order-1",
            "client_order_id": entry_intent.client_order_id, "symbol": "BTC/USD", "side": "buy",
            "status": "filled", "cumulative_filled_quantity": entry_intent.requested_quantity,
            "filled_average_price": "101", "fees": "0.025", "paper_account_id_hash": ACCOUNT_HASH,
            "payload": {"id": "crypto-broker-order-1", "status": "filled"},
        },
        occurred_at=NOW,
    )
    assert entry_fill["state"] == "filled"
    quantity = D(entry_intent.requested_quantity)
    broker.positions = [SimpleNamespace(
        symbol="BTC/USD", asset_class="crypto", side="long", qty=str(quantity),
        market_value=str(quantity * D("101")), current_price="101", avg_entry_price="101",
    )]
    capability = CryptoCapabilityStore(storage).capture(config, broker, "roundtrip-exit", now=NOW)
    storage.execute(
        """INSERT INTO crypto_research_runs(
          id,run_id,status,started_at,symbols,provider,capability_snapshot_id,
          capability_snapshot_fingerprint,capability_authoritative,payload
        ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            "roundtrip-exit-research", "roundtrip-exit", "running", NOW.isoformat(),
            '["BTC/USD"]', "alpaca", capability.id, capability.snapshot_fingerprint,
            int(capability.authoritative), "{}",
        ),
    )
    market = CryptoMarketDataStore(storage).capture(
        config, broker, capability, "roundtrip-exit", "roundtrip-exit-research", "BTC/USD", now=NOW,
    )
    risk = CryptoRiskStore(storage).evaluate(
        config, broker, "roundtrip-exit", capability.id, market.id,
        CryptoSizingRequest(
            source_type="crypto_position_management", source_id="roundtrip-lifecycle",
            source_fingerprint="d" * 64, symbol="BTC/USD", side="sell", action="exit",
            request_basis="quantity", close_entire_position=True,
        ),
        now=NOW,
    )
    assert risk.sizing.eligible, risk.sizing.blockers
    assert risk.risk_eligible, risk.reasons
    sell_proposal = lane.create_proposal(config, None, risk.decision_id, now=NOW)
    lane.bind_telegram_message(sell_proposal.id, "roundtrip-sell-message", config, chat_id="123", now=NOW)
    lane.approve_proposal(
        sell_proposal.id, config, sender_id="operator", allowed_sender_id="operator",
        raw_message=f"YES CRYPTO {sell_proposal.id}", reply_to_message_id="roundtrip-sell-message", chat_id="123", now=NOW,
    )
    sell_intent = lane.create_intent(sell_proposal.id, config, now=NOW)
    lane.execute_intent(sell_intent.id, config, broker, now=NOW)
    sell_fill = _record_fill(lane,
        sell_intent.id, "broker-roundtrip-sell", sell_intent.requested_quantity, "103", "0.025", config,
        broker_evidence={
            "verified": True, "broker_order_id": "crypto-broker-order-1",
            "client_order_id": sell_intent.client_order_id, "symbol": "BTC/USD", "side": "sell",
            "status": "filled", "cumulative_filled_quantity": sell_intent.requested_quantity,
            "filled_average_price": "103", "fees": "0.025", "paper_account_id_hash": ACCOUNT_HASH,
            "payload": {"id": "crypto-broker-order-1", "status": "filled"},
        },
        occurred_at=NOW + timedelta(minutes=1),
    )
    assert sell_fill["state"] == "filled"
    lifecycle = storage.fetch_all(
        "SELECT id,state,current_quantity_decimal,closed_at FROM position_lifecycles WHERE symbol='BTC/USD'"
    )[0]
    assert lifecycle["state"] == "closed"
    assert lifecycle["current_quantity_decimal"] == "0"
    assert lifecycle["closed_at"]
    assert storage.fetch_all(
        "SELECT COUNT(*) n FROM lot_consumptions WHERE position_lifecycle_id=?", (lifecycle["id"],)
    )[0]["n"] == 1
    attribution = storage.fetch_all(
        "SELECT position_lifecycle_id,status,confidence,realized_net_pnl,actual_r_multiple FROM profit_attribution_records WHERE position_lifecycle_id=?",
        (lifecycle["id"],),
    )
    assert len(attribution) == 1
    assert attribution[0]["status"] == "partial"
    assert attribution[0]["confidence"] == "verified_actual_only"
    assert D(attribution[0]["realized_net_pnl"]) > 0
    snapshots = StrategyPerformanceEngine(storage, config, as_of=NOW + timedelta(minutes=1)).refresh_all()
    assert snapshots
    records = storage.fetch_all(
        "SELECT evidence_class,attribution_status,profit_attribution_id,strategy_version FROM strategy_trade_records WHERE position_lifecycle_id=?",
        (lifecycle["id"],),
    )
    assert len(records) == 1
    assert records[0]["evidence_class"] == "actual_paper"
    assert records[0]["attribution_status"] == "partial"
    assert records[0]["profit_attribution_id"]
    assert records[0]["strategy_version"]
    links = storage.fetch_all(
        "SELECT fill_id,position_lifecycle_id,fill_type FROM crypto_performance_links ORDER BY created_at,fill_id"
    )
    assert len(links) == 2
    assert {row["position_lifecycle_id"] for row in links} == {lifecycle["id"]}
    assert {row["fill_type"] for row in links} == {"entry", "exit"}
    actual_outcomes = storage.fetch_all(
        "SELECT evidence_type,status,outcome_class,net_return,actual_lineage_json FROM crypto_profitability_observations WHERE strategy_decision_id=?",
        (str(_entry_proposal.payload["strategy_decision_id"]),),
    )
    assert len(actual_outcomes) == 1
    assert actual_outcomes[0]["evidence_type"] == "actual"
    assert actual_outcomes[0]["status"] == "completed"
    assert actual_outcomes[0]["outcome_class"] == "actual_profit"
    assert D(actual_outcomes[0]["net_return"]) > 0
    assert '"state":"closed"' in actual_outcomes[0]["actual_lineage_json"]
    assert all(value == 0 for value in fixed_point_integrity_report(storage).values())


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
    lane.bind_telegram_message(proposal.id, "telegram-sell-1", config, chat_id="123", now=NOW)
    lane.approve_proposal(
        proposal.id, config, sender_id="operator", allowed_sender_id="operator",
        raw_message=f"YES CRYPTO {proposal.id}", reply_to_message_id="telegram-sell-1", chat_id="123", now=NOW,
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
    fill = _record_fill(lane,
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
    lane.bind_telegram_message(proposal.id, "telegram-sell-2", config, chat_id="123", now=NOW)
    lane.approve_proposal(
        proposal.id, config, sender_id="operator", allowed_sender_id="operator",
        raw_message=f"YES CRYPTO {proposal.id}", reply_to_message_id="telegram-sell-2", chat_id="123", now=NOW,
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


def test_callable_broker_adapter_without_explicit_availability_proof_is_retryable(tmp_path):
    storage, config, broker, lane, proposal, intent = _ready(tmp_path)

    class UnavailableCryptoAdapter(Broker):
        def crypto_submission_available(self):
            return False

    unavailable = UnavailableCryptoAdapter()
    result = lane.execute_intent(intent.id, config, unavailable, now=NOW)
    assert result["state"] == "retryable_pre_submission"
    assert result["broker_invocation_occurred"] == 0
    assert result["broker_call"] is False
    assert unavailable.submit_calls == []


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


def test_unbound_crypto_telegram_display_is_manual_review_not_pending(tmp_path):
    storage = _storage(tmp_path)
    config = _config()
    broker = Broker()
    strategy, risk = _evidence(storage, config, broker)
    lane = CryptoPaperLaneStore(storage, control_providers=_healthy_providers())
    proposal = lane.create_proposal(config, strategy.id, risk.decision_id, now=NOW)
    lane.mark_unbound_manual_review(
        proposal.id, reason="Telegram response did not contain a chat identity", now=NOW
    )
    row = storage.fetch_all(
        "SELECT status,telegram_message_id,telegram_chat_id FROM crypto_paper_proposals WHERE id=?",
        (proposal.id,),
    )[0]
    assert row["status"] == "manual_review"
    assert row["telegram_message_id"] is None
    assert row["telegram_chat_id"] is None
    assert CryptoPaperLaneStore(storage).integrity_report()["crypto_paper_pending_telegram_binding_missing"] == 0


def test_expired_unsubmitted_crypto_intent_terminalises_and_releases_reservation(tmp_path):
    storage, config, broker, lane, proposal, intent = _ready(tmp_path)
    results = lane.expire_pending(now=NOW + timedelta(minutes=4))
    assert any(item.get("id") == intent.id and item["state"] == "expired" for item in results)
    refreshed = storage.fetch_all(
        "SELECT state,broker_invocation_occurred FROM crypto_paper_intents WHERE id=?",
        (intent.id,),
    )[0]
    reservation = storage.fetch_all(
        "SELECT state,active_notional,active_stop_risk FROM crypto_paper_reservations WHERE intent_id=?",
        (intent.id,),
    )[0]
    assert refreshed["state"] == "expired"
    assert refreshed["broker_invocation_occurred"] == 0
    assert reservation["state"] == "released"
    assert reservation["active_notional"] == "0"
    assert broker.submit_calls == []


def test_crypto_cancel_is_marked_before_broker_call_and_reconciles_terminal_state(tmp_path):
    class CancelBroker(Broker):
        def __init__(self):
            super().__init__()
            self.cancel_calls: list[str] = []
            self.order = None

        def crypto_cancellation_available(self):
            return True

        def cancel_crypto_order(self, order_id):
            self.cancel_calls.append(str(order_id))
            return {"id": order_id, "status": "cancelled"}

        def get_order_by_client_order_id(self, client_order_id):
            return self.order

    broker = CancelBroker()
    storage, config, _unused, lane, proposal, intent = _ready(tmp_path, broker=broker)
    submitted = lane.execute_intent(intent.id, config, broker, now=NOW)
    assert submitted["state"] == "submitted"
    cancelled = lane.cancel_intent(
        intent.id, config, broker, reason="proposal expired", now=NOW
    )
    assert cancelled["state"] == "cancel_pending"
    assert cancelled["cancellation_requested"] is True
    assert broker.cancel_calls == ["crypto-broker-order-1"]
    persisted = storage.fetch_all(
        "SELECT state FROM crypto_paper_intents WHERE id=?", (intent.id,)
    )[0]
    assert persisted["state"] == "cancel_pending"
    broker.order = {
        "id": "crypto-broker-order-1", "client_order_id": intent.client_order_id,
        "symbol": "BTC/USD", "side": "buy", "status": "cancelled",
        "qty": intent.requested_quantity, "filled_qty": "0", "fees": "0",
    }
    reconciled = lane.reconcile_intent(intent.id, config, broker, now=NOW)
    assert reconciled["state"] == "cancelled"
    reservation = storage.fetch_all(
        "SELECT state FROM crypto_paper_reservations WHERE intent_id=?", (intent.id,)
    )[0]
    assert reservation["state"] == "released"


def test_crypto_filled_status_without_cumulative_fill_is_reconciliation_required(tmp_path):
    class MalformedBroker(Broker):
        def get_order_by_client_order_id(self, client_order_id):
            return {
                "id": "crypto-broker-order-1", "client_order_id": client_order_id,
                "symbol": "BTC/USD", "side": "buy", "status": "filled",
                "qty": "5", "filled_qty": "0", "fees": "0",
            }

    broker = MalformedBroker()
    storage, config, _unused, lane, proposal, intent = _ready(tmp_path, broker=broker)
    lane.execute_intent(intent.id, config, broker, now=NOW)
    result = lane.reconcile_intent(intent.id, config, broker, now=NOW)
    assert result["state"] == "reconciliation_required"
    assert result["reconciliation"] == "fill_state_invalid"
    assert storage.fetch_all("SELECT COUNT(*) n FROM crypto_paper_fills")[0]["n"] == 0


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
