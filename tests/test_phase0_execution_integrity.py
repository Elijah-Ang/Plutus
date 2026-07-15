from __future__ import annotations

import ast
import socket
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.configuration import ConfigurationError, validate_config
from app.approval_workflow import ApprovalWorkflowState, ApprovalWorkflowStore
from app.execution import DurableExecutionStore, Executor
from app.health import HealthMonitor, record_heartbeat
from app.order_state import InvalidOrderTransition, OrderState
from app.position_lifecycle import PositionLifecycleManager
from app.reconciliation import BrokerReconciler
from app.risk_engine import RiskDecision
from app.storage import Storage
from app.utils import PROJECT_ROOT


class PassingRisk:
    def evaluate(self, proposal, context, final=False):
        return RiskDecision(final and context.get("final_revalidation") is True, ())


def proposal(identifier: str = "proposal-1") -> dict:
    now = datetime.now(UTC)
    return {
        "id": identifier, "proposal_id": identifier, "status": "approved", "symbol": "SPY",
        "side": "buy", "action": "entry", "notional": 100.0, "latest_price": 50.0,
        "stop_price": 45.0, "price_at": now.isoformat(), "created_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=5)).isoformat(), "trading_mode": "paper",
        "order_type": "limit", "quote_source": "alpaca_quote", "quote_bid": 49.9,
        "quote_ask": 50.1, "quote_midpoint": 50.0, "quote_timestamp": now.isoformat(),
        "quote_spread_bps": 40.0, "limit_price": 50.23,
    }


def make_storage(tmp_path) -> Storage:
    value = Storage(tmp_path / "phase0.db")
    value.initialize()
    return value


def authorize(db: Storage, candidate: dict, approval_id: str = "approval-1", *, ready: bool = False) -> dict:
    now = datetime.now(UTC).isoformat()
    proposal_id = str(candidate.get("proposal_id") or candidate.get("id"))
    db.execute(
        """INSERT INTO trade_proposals(id,symbol,side,notional,status,created_at,expires_at,strategy_version,payload)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (proposal_id, candidate["symbol"], candidate["side"], candidate.get("notional"), "pending",
         candidate["created_at"], candidate["expires_at"], "rule_based_v2", "{}"),
    )
    workflows = ApprovalWorkflowStore(db)
    workflow = workflows.accept_approval(
        approval_id=approval_id, run_id="run", proposal_id=proposal_id,
        sender_id="owner", raw_message="approve", parsed_action="approve",
        telegram_update_id=None, reply_to_message_id=None, targeting_method="test",
        acknowledgement_status="received", approval_received_at=now,
    )
    assert db.consume_approval(proposal_id, approval_id)
    workflow = workflows.transition(workflow["id"], ApprovalWorkflowState.VALIDATING,
                                    expected_state=ApprovalWorkflowState.TARGET_RESOLVED)
    if ready:
        workflows.transition(workflow["id"], ApprovalWorkflowState.APPROVED_PENDING_INTENT,
                             expected_state=ApprovalWorkflowState.VALIDATING)
    return {**candidate, "id": proposal_id, "proposal_id": proposal_id, "status": "approved"}


class InspectingBroker:
    def __init__(self, store: Storage, failure: BaseException | None = None):
        self.storage, self.failure, self.calls = store, failure, 0

    def submit_order(self, symbol, side, order_args, order_type, limit_price, client_order_id):
        self.calls += 1
        intent = self.storage.fetch_all("SELECT state,client_order_id FROM order_intents")[0]
        reservation = self.storage.fetch_all("SELECT state,active_notional FROM risk_reservations")[0]
        assert intent == {"state": "submitting", "client_order_id": client_order_id}
        assert reservation == {"state": "active", "active_notional": 100.0}
        if self.failure:
            raise self.failure
        return SimpleNamespace(id="broker-order-1", status="submitted")


def test_broker_submission_has_committed_intent_and_reservation_first(tmp_path):
    db = make_storage(tmp_path)
    broker = InspectingBroker(db)
    candidate = authorize(db, proposal())
    result = Executor(broker, PassingRisk(), db, "run").execute(candidate, {"approval_valid": True}, approval_id="approval-1")
    assert result.submitted and result.status == "submitted" and broker.calls == 1
    assert db.fetch_all("SELECT submission_attempt_count FROM order_intents")[0]["submission_attempt_count"] == 1


def test_ambiguous_submission_retains_stable_id_and_reservation_without_duplicate(tmp_path):
    db = make_storage(tmp_path)
    broker = InspectingBroker(db, TimeoutError("lost response"))
    executor = Executor(broker, PassingRisk(), db, "run")
    candidate = authorize(db, proposal())
    first = executor.execute(candidate, {"approval_valid": True}, approval_id="approval-1")
    second = executor.execute(candidate, {"approval_valid": True}, approval_id="approval-1")
    assert first.status == second.status == "unknown"
    assert first.client_order_id == second.client_order_id and broker.calls == 1
    assert DurableExecutionStore(db).active_reservations()["active_reserved_notional"] == 100.0


def test_restart_resumes_reserved_intent_with_same_client_id(tmp_path):
    db = make_storage(tmp_path)
    candidate = authorize(db, proposal(), ready=True)
    intent = DurableExecutionStore(db).create_or_get_intent(candidate, run_id="run", source_type="proposal", approval_id="approval-1")
    broker = InspectingBroker(db)
    result = Executor(broker, PassingRisk(), db, "restart").execute(candidate, {"approval_valid": True})
    assert result.client_order_id == intent["client_order_id"] and broker.calls == 1


def test_partial_fill_is_monotonic_deduplicated_and_releases_proportionally(tmp_path):
    db = make_storage(tmp_path)
    store = DurableExecutionStore(db)
    intent = store.create_or_get_intent({**proposal(), "qty": 10, "notional": None}, run_id="run", source_type="proposal")
    store.transition(intent["id"], OrderState.SUBMITTING, event_type="test")
    store.transition(intent["id"], OrderState.SUBMITTED, event_type="test")
    store.record_fill(intent["id"], cumulative_quantity=4, fill_price=50, broker_event_key="fill-1")
    store.record_fill(intent["id"], cumulative_quantity=4, fill_price=50, broker_event_key="fill-1")
    assert store.get_intent(intent["id"])["filled_quantity"] == 4
    assert db.fetch_all("SELECT active_notional FROM risk_reservations")[0]["active_notional"] == pytest.approx(301.38)
    store.record_fill(intent["id"], cumulative_quantity=10, fill_price=51, broker_event_key="fill-2")
    final = store.get_intent(intent["id"])
    assert (final["filled_quantity"], final["state"]) == (10, "filled")
    assert db.fetch_all("SELECT state,active_notional FROM risk_reservations")[0] == {"state": "released", "active_notional": 0.0}
    assert db.fetch_all("SELECT COUNT(*) n FROM broker_fill_events")[0]["n"] == 2


def test_invalid_transition_and_shadow_intent_fail_closed(tmp_path):
    db = make_storage(tmp_path)
    store = DurableExecutionStore(db)
    intent = store.create_or_get_intent(proposal(), run_id="run", source_type="proposal")
    with pytest.raises(InvalidOrderTransition):
        store.transition(intent["id"], OrderState.FILLED, event_type="invalid")
    with pytest.raises(ValueError):
        store.create_or_get_intent({**proposal("shadow"), "observation_only": True}, run_id="run", source_type="proposal")


def test_two_workers_create_one_intent_and_make_one_submission(tmp_path):
    db = make_storage(tmp_path)
    broker, barrier, results = InspectingBroker(db), threading.Barrier(2), []

    def worker():
        barrier.wait()
        results.append(Executor(broker, PassingRisk(), db, "run").execute(candidate, {"approval_valid": True}, approval_id="approval-1"))

    candidate = authorize(db, proposal())
    threads = [threading.Thread(target=worker), threading.Thread(target=worker)]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    assert broker.calls == 1
    assert db.fetch_all("SELECT COUNT(*) n FROM order_intents")[0]["n"] == 1
    assert len({result.client_order_id for result in results}) == 1


class ReconcileOnlyBroker:
    def __init__(self): self.lookup_calls = 0
    def get_order(self, value):
        self.lookup_calls += 1
        raise AssertionError("blocked local order must not be queried")
    def get_order_by_client_order_id(self, value): return self.get_order(value)
    def get_account(self): return SimpleNamespace(equity="100", cash="100")
    def get_positions(self): return []


def test_reconciliation_excludes_never_submitted_blocked_order(tmp_path):
    db = make_storage(tmp_path)
    now = datetime.now(UTC).isoformat()
    db.execute("INSERT INTO orders(id,client_order_id,symbol,side,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)", ("blocked", "never-sent", "SPY", "buy", "blocked", now, now))
    broker = ReconcileOnlyBroker()
    assert BrokerReconciler(broker, db, "run").reconcile().checked == 0
    assert broker.lookup_calls == 0


def test_reopened_position_gets_clean_lifecycle_and_archives_old_state(tmp_path):
    db = make_storage(tmp_path)
    manager = PositionLifecycleManager(db)
    first = manager.reconcile([SimpleNamespace(symbol="SPY", qty="2", avg_entry_price="50")])["SPY"]
    now = datetime.now(UTC).isoformat()
    db.execute("INSERT INTO position_management_state(id,symbol,position_lifecycle_id,quantity,highest_price_since_entry,take_profit_level_1_hit,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)", ("state-1", "SPY", first, 2, 70, 1, now, now))
    manager.reconcile([])
    assert db.fetch_all("SELECT * FROM position_management_state") == []
    assert "highest_price_since_entry" in db.fetch_all("SELECT management_state_archive FROM position_lifecycles WHERE id=?", (first,))[0]["management_state_archive"]
    second = manager.reconcile([SimpleNamespace(symbol="SPY", qty="1", avg_entry_price="55")])["SPY"]
    assert second != first and db.fetch_all("SELECT * FROM position_management_state") == []


def test_status_is_measured_and_becomes_stale(tmp_path):
    db = make_storage(tmp_path)
    record_heartbeat(db, "scanner", "healthy", completed_at="2020-01-01T00:00:00+00:00", successful_at="2020-01-01T00:00:00+00:00")
    report = HealthMonitor(db, {"health": {"scanner_stale_seconds": 1}}, root=tmp_path).report(datetime.now(UTC) + timedelta(seconds=60))
    assert report.components["scanner"]["state"] == "stale"
    assert not HealthMonitor(db, {}, root=tmp_path).format_status().startswith("Status: Active")


def test_invalid_safety_configuration_fails_before_runtime():
    with pytest.raises(ConfigurationError):
        validate_config({"mode": "live", "live_enabled": True, "auto_execution_enabled": True})


def test_migrations_idempotent_and_integrity_report_read_only(tmp_path):
    db = make_storage(tmp_path)
    db.initialize(); db.initialize()
    assert all(value == 0 for value in DurableExecutionStore(db).integrity_report().values())


def test_production_submit_calls_stay_inside_approved_boundary():
    allowed, offenders = {"execution.py", "broker_alpaca.py", "broker_interface.py", "strategy_ml_shadow.py"}, []
    for path in (PROJECT_ROOT / "app").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "submit_order" for node in ast.walk(tree)) and path.name not in allowed:
            offenders.append(path.name)
    assert offenders == []


def test_test_suite_network_guard_is_active():
    with pytest.raises(BaseException, match="outbound network access is forbidden"):
        socket.create_connection(("example.com", 443))
