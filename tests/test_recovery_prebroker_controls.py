from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from app.approval_display import record_display
from app.approval_workflow import ApprovalWorkflowState, ApprovalWorkflowStore
from app.execution import DurableExecutionStore, Executor
from app.execution_risk_snapshot import REQUIRED_FORMULA_VERSIONS, capture_execution_risk_snapshot
from app.risk_engine import RiskEngine
from app.storage import Storage


class SimulatedCrash(BaseException):
    pass


class Broker:
    def __init__(self, *, side: str = "buy") -> None:
        self.account_id = "paper-recovery-account"
        self.positions = [] if side == "buy" else [
            {"symbol": "SPY", "qty": 20.0, "market_value": 200.0, "current_price": 10.0}
        ]
        self.orders: list[dict] = []
        self.market_open = True
        self.submit_calls = 0

    def paper_account_identity(self):
        return {"verified": True, "mode": "paper", "account_id": self.account_id, "sdk_sandbox_evidence": True}

    def get_account(self):
        return {"id": self.account_id, "status": "ACTIVE", "equity": 100_000.0, "cash": 50_000.0,
                "buying_power": 50_000.0, "short_market_value": 0.0}

    def get_positions(self):
        return list(self.positions)

    def get_open_orders(self):
        return list(self.orders)

    def get_clock(self):
        return {"is_open": self.market_open, "timestamp": datetime.now(UTC).isoformat()}

    def get_loss_metrics(self):
        return {
            "daily_loss_dollars": 0.0, "weekly_loss_dollars": 0.0,
            "daily_loss_confidence": "verified", "weekly_loss_confidence": "verified",
            "reference_equity": 100_000.0, "captured_at": datetime.now(UTC).isoformat(),
        }

    def submit_order(self, symbol, side, order_args, order_type, limit_price, client_order_id):
        self.submit_calls += 1
        return {"id": f"paper-{client_order_id}", "status": "submitted"}


class Controls:
    def __init__(self) -> None:
        self.kill = False
        self.power = True
        self.internet = True
        self.database = True
        self.telegram = True
        self.loss_stale = False

    def providers(self):
        return {
            "kill_switch": lambda: self.kill,
            "power": lambda: self.power,
            "internet": lambda: self.internet,
            "database": lambda: self.database,
            "telegram": lambda: self.telegram,
            "loss_controls": lambda: {
                "daily_loss_dollars": 0.0,
                "weekly_loss_dollars": 0.0,
                "daily_loss_confidence": "verified",
                "weekly_loss_confidence": "verified",
                "reference_equity": 100_000.0,
                "captured_at": (
                    datetime.now(UTC) - timedelta(minutes=10)
                    if self.loss_stale else datetime.now(UTC)
                ).isoformat(),
            },
        }


def _config() -> dict:
    return {
        "mode": "paper", "live_enabled": False,
        "effective_config_hash": "cfg-v1",
        "formula_versions": dict(REQUIRED_FORMULA_VERSIONS),
        "watchlist": ["SPY"],
        "approved_strategy_versions": ["rule_based_v2"],
        "portfolio_execution_mode": "risk_budgeted",
        "position_sizing": {"enabled": False},
        "risk": {
            "max_trade_notional_paper": 10_000.0,
            "allowed_order_types": ["limit"],
            "block_new_buys_when_any_position_open": False,
            "block_new_buys_after_buy_order_submitted_today": False,
            "block_same_symbol_rebuy_while_position_open": False,
        },
        "portfolio_behavior": {
            "max_total_portfolio_exposure_pct": 100.0,
            "max_single_symbol_exposure_pct": 100.0,
            "block_new_buy_if_exit_pending": True,
        },
        "portfolio_optimizer": {
            "max_same_cluster_positions": 100,
            "max_same_cluster_exposure_pct": 100.0,
        },
        "risk_budget": {"max_open_risk_pct": 100.0},
    }


def _proposal(side: str = "buy", identifier: str = "recovery-proposal") -> dict:
    now = datetime.now(UTC)
    limit_price = 10.04 if side == "buy" else 9.96
    result = {
        "id": identifier, "proposal_id": identifier, "status": "approved",
        "symbol": "SPY", "side": side, "action": "entry" if side == "buy" else "exit",
        "qty": 10.0, "notional": 10.0 * max(10.0, limit_price), "latest_price": 10.0, "price_at": now.isoformat(),
        "stop_price": 9.0, "stop_distance_dollars": 1.0, "atr_value": 1.0,
        "technical_stop_price": 9.0, "stop_model_used": "atr_technical",
        "stop_validation_status": "validated", "historical_bars": 100, "volume": 1000,
        "reason": "real RiskEngine recovery regression", "strategy_version": "rule_based_v2",
        "created_at": now.isoformat(), "expires_at": (now + timedelta(minutes=10)).isoformat(),
        "trading_mode": "paper", "order_type": "limit", "quote_source": "alpaca_quote",
        "quote_bid": 9.99, "quote_ask": 10.01, "quote_midpoint": 10.0,
        "quote_timestamp": now.isoformat(), "quote_spread_bps": 2.0,
        "limit_price": limit_price,
        "request_basis": "quantity", "config_hash": "cfg-v1",
        "emergency_exit_triggered": 0,
        "formula_versions": dict(REQUIRED_FORMULA_VERSIONS),
    }
    return result


def _approved(storage: Storage, proposal: dict) -> None:
    storage.execute(
        """INSERT INTO trade_proposals(
             id,symbol,side,notional,status,created_at,expires_at,strategy_version,payload,
             formula_versions_json)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (proposal["id"], proposal["symbol"], proposal["side"], proposal["notional"], "pending",
         proposal["created_at"], proposal["expires_at"], proposal["strategy_version"],
         json.dumps(proposal), json.dumps(REQUIRED_FORMULA_VERSIONS)),
    )
    display = record_display(storage, proposal["id"], "700")
    envelope = json.loads(display["displayed_envelope_json"])
    proposal["approved_quantity_ceiling"] = envelope["max_quantity"]
    proposal["approved_notional_ceiling"] = envelope["max_notional"]
    if proposal["action"] in {"entry", "add"}:
        proposal["approved_stop_risk_ceiling"] = envelope["max_stop_risk"]
    workflow = ApprovalWorkflowStore(storage).accept_approval(
        approval_id="approval-1", run_id="run-1", proposal_id=proposal["id"], sender_id="owner",
        raw_message="approve", parsed_action="approve", telegram_update_id=700,
        reply_to_message_id="700", targeting_method="reply", acknowledgement_status="received",
        approval_received_at=datetime.now(UTC).isoformat(),
    )
    assert storage.consume_approval(proposal["id"], "approval-1")
    ApprovalWorkflowStore(storage).transition(
        workflow["id"], ApprovalWorkflowState.VALIDATING,
        expected_state=ApprovalWorkflowState.TARGET_RESOLVED,
    )


def _retryable(tmp_path, *, side: str = "buy"):
    storage = Storage(tmp_path / f"{side}.sqlite3")
    storage.initialize()
    proposal = _proposal(side)
    _approved(storage, proposal)
    broker = Broker(side=side)
    controls = Controls()
    engine = RiskEngine(_config())

    def crash(boundary, _detail):
        if boundary == "after_intent_and_reservation_commit":
            raise SimulatedCrash(boundary)

    with pytest.raises(SimulatedCrash):
        Executor(
            broker, engine, storage, "run-1", fault_hook=crash,
            trusted_evidence_providers=controls.providers(),
        ).execute(proposal, {}, approval_id="approval-1")
    absent = Executor(None, engine, storage, "run-1").execute(proposal, {}, approval_id="approval-1")
    assert absent.status == "retryable_pre_submission"
    return storage, broker, controls, engine, proposal


def _recover(storage, broker, controls, engine, proposal):
    return Executor(
        broker, engine, Storage(storage.path), "run-1",
        trusted_evidence_providers=controls.providers(),
    ).execute(proposal, {}, approval_id="approval-1")


def _insert_unrelated_intent(storage: Storage) -> None:
    with storage.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        intent = dict(conn.execute("SELECT * FROM order_intents LIMIT 1").fetchone())
        intent.update(
            id="unrelated-intent", run_id="other-run", proposal_id="unrelated-proposal",
            approval_id=None, logical_action_key="unrelated-logical-action",
            client_order_id="unrelated-client-order", state="reserved",
        )
        columns = list(intent)
        conn.execute(
            f"INSERT INTO order_intents({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
            tuple(intent[column] for column in columns),
        )
        reservation = dict(conn.execute("SELECT * FROM risk_reservations LIMIT 1").fetchone())
        reservation.update(id="unrelated-reservation", intent_id="unrelated-intent")
        columns = list(reservation)
        conn.execute(
            f"INSERT INTO risk_reservations({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
            tuple(reservation[column] for column in columns),
        )


def test_real_risk_engine_recovered_retryable_buy_submits_once(tmp_path):
    storage, broker, controls, engine, proposal = _retryable(tmp_path, side="buy")
    assert isinstance(engine, RiskEngine)
    assert _recover(storage, broker, controls, engine, proposal).submitted
    assert broker.submit_calls == 1


def test_real_risk_engine_recovered_retryable_sell_submits_once(tmp_path):
    storage, broker, controls, engine, proposal = _retryable(tmp_path, side="sell")
    assert _recover(storage, broker, controls, engine, proposal).submitted
    assert broker.submit_calls == 1


def test_recovered_sell_does_not_subtract_its_own_quantity_twice(tmp_path):
    storage, broker, controls, engine, proposal = _retryable(tmp_path, side="sell")
    assert _recover(storage, broker, controls, engine, proposal).submitted
    snapshots = storage.fetch_all("SELECT risk_context_json FROM execution_risk_snapshots ORDER BY captured_at")
    final = json.loads(snapshots[-1]["risk_context_json"])
    assert final["open_sell_quantity"] == 0 and final["sellable_quantity"] == 20


def test_recovered_buy_reservation_is_counted_exactly_once(tmp_path):
    storage, broker, controls, engine, proposal = _retryable(tmp_path, side="buy")
    assert _recover(storage, broker, controls, engine, proposal).submitted
    final = json.loads(storage.fetch_all(
        "SELECT risk_context_json FROM execution_risk_snapshots ORDER BY captured_at DESC LIMIT 1"
    )[0]["risk_context_json"])
    assert final["active_reserved_exposure"] == pytest.approx(proposal["notional"])
    assert final["proposed_total_exposure_pct"] == pytest.approx(proposal["notional"] / 100_000 * 100)


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_unrelated_same_symbol_durable_intent_still_blocks_recovery(tmp_path, side):
    storage, broker, controls, engine, proposal = _retryable(tmp_path, side=side)
    _insert_unrelated_intent(storage)
    result = _recover(storage, broker, controls, engine, proposal)
    assert not result.submitted and broker.submit_calls == 0
    assert "active" in (result.reason or "") or "blocked" in result.status


def test_broker_open_order_still_blocks_recovered_intent(tmp_path):
    storage, broker, controls, engine, proposal = _retryable(tmp_path, side="buy")
    broker.orders.append({"id": "broker-buy", "symbol": "SPY", "side": "buy", "qty": 1})
    result = _recover(storage, broker, controls, engine, proposal)
    assert not result.submitted and broker.submit_calls == 0


def test_mismatched_recovery_exclusion_authority_fails_closed(tmp_path):
    storage, broker, controls, _engine, proposal = _retryable(tmp_path, side="buy")
    intent = storage.fetch_all("SELECT * FROM order_intents")[0]
    with pytest.raises(RuntimeError, match="does not match"):
        capture_execution_risk_snapshot(
            storage, broker, proposal_id=proposal["id"], approval_id="approval-1", run_id="run-1",
            context={}, config=_config(), candidate=proposal, trusted_providers=controls.providers(),
            recovery_intent_id=intent["id"], recovery_logical_action_key="forged-action-key",
        )


def test_invoked_intent_is_never_resubmitted(tmp_path):
    storage, broker, controls, engine, proposal = _retryable(tmp_path, side="buy")
    storage.execute("UPDATE order_intents SET broker_invocation_occurred=1")
    result = _recover(storage, broker, controls, engine, proposal)
    assert not result.submitted and broker.submit_calls == 0


def test_repeated_restarts_never_duplicate_intent_reservation_or_broker_call(tmp_path):
    storage, broker, controls, engine, proposal = _retryable(tmp_path, side="buy")
    first = _recover(storage, broker, controls, engine, proposal)
    second = _recover(storage, broker, controls, engine, proposal)
    third = _recover(storage, broker, controls, engine, proposal)
    assert first.submitted and not second.submitted and not third.submitted
    assert broker.submit_calls == 1
    assert storage.fetch_all("SELECT COUNT(*) n FROM order_intents")[0]["n"] == 1
    assert storage.fetch_all("SELECT COUNT(*) n FROM risk_reservations")[0]["n"] == 1


@pytest.mark.parametrize(
    "change",
    [
        "kill_switch", "power", "internet", "database", "telegram", "loss_stale",
        "new_sell_order", "holdings_decrease", "reservation_revoked",
        "approval_superseded", "configuration_changed",
    ],
)
def test_every_critical_control_is_rechecked_before_broker_io(tmp_path, change):
    side = "sell" if change in {"new_sell_order", "holdings_decrease"} else "buy"
    storage = Storage(tmp_path / f"prebroker-{change}.sqlite3")
    storage.initialize()
    proposal = _proposal(side)
    _approved(storage, proposal)
    broker, controls, engine = Broker(side=side), Controls(), RiskEngine(_config())

    def mutate(boundary, detail):
        if boundary != "immediately_before_broker_invocation":
            return
        if change == "kill_switch": controls.kill = True
        elif change == "power": controls.power = False
        elif change == "internet": controls.internet = False
        elif change == "database": controls.database = False
        elif change == "telegram": controls.telegram = False
        elif change == "loss_stale": controls.loss_stale = True
        elif change == "new_sell_order": broker.orders.append(
            {"id": "late-sell", "symbol": "SPY", "side": "sell", "qty": 1}
        )
        elif change == "holdings_decrease": broker.positions[0]["qty"] = 5.0
        elif change == "reservation_revoked": storage.execute(
            "UPDATE risk_reservations SET state='released',active_notional=0,active_stop_risk=0"
        )
        elif change == "approval_superseded": storage.execute(
            "UPDATE trade_proposals SET status='rejected' WHERE id=?", (proposal["id"],)
        )
        elif change == "configuration_changed": engine.config["effective_config_hash"] = "cfg-v2"

    result = Executor(
        broker, engine, storage, "run-1", fault_hook=mutate,
        trusted_evidence_providers=controls.providers(),
    ).execute(proposal, {}, approval_id="approval-1")
    assert not result.submitted, (change, result)
    assert broker.submit_calls == 0
    intent = storage.fetch_all("SELECT state,broker_invocation_occurred FROM order_intents")[0]
    assert intent["state"] == "rejected" and intent["broker_invocation_occurred"] == 0
