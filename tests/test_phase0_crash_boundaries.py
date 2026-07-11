from __future__ import annotations

import sqlite3
import functools
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.approval_workflow import ApprovalWorkflowState, ApprovalWorkflowStore
from app.execution import DurableExecutionStore, Executor
from app.order_state import InvalidOrderTransition, OrderState
from app.position_lifecycle import PositionLifecycleManager
from app.reconciliation import BrokerReconciler
from app.risk_engine import RiskDecision
from app.run_lock import inspect_lock
from app.storage import Storage


class SimulatedProcessCrash(BaseException):
    pass


class PassingRisk:
    config = {}

    def evaluate(self, proposal, context, final=False):
        return RiskDecision(True, ())


class FakeBroker:
    def __init__(self, *, submit_error=None, submit_status="submitted"):
        self.submit_error = submit_error
        self.submit_status = submit_status
        self.submit_calls = 0
        self.lookup_calls = 0
        self.orders = {}

    def submit_order(self, symbol, side, order_args, order_type, limit_price, client_order_id):
        self.submit_calls += 1
        remote = SimpleNamespace(
            id=f"paper-{client_order_id}", status=self.submit_status, filled_qty="0", filled_avg_price=None
        )
        self.orders[client_order_id] = remote
        if self.submit_error:
            raise self.submit_error
        return remote

    def get_order_by_client_order_id(self, client_order_id):
        self.lookup_calls += 1
        if client_order_id not in self.orders:
            error = RuntimeError("synthetic not found")
            error.status_code = 404
            raise error
        return self.orders[client_order_id]

    def get_order(self, broker_order_id):
        self.lookup_calls += 1
        for order in self.orders.values():
            if order.id == broker_order_id:
                return order
        error = RuntimeError("synthetic not found")
        error.status_code = 404
        raise error

    def get_account(self):
        return SimpleNamespace(equity="1000", cash="1000")

    def get_positions(self):
        return []


def _db(tmp_path, name="crash.sqlite3"):
    storage = Storage(tmp_path / name)
    storage.initialize()
    return storage


def _proposal(identifier="proposal-1", symbol="SPY", side="buy"):
    return {
        "id": identifier,
        "proposal_id": identifier,
        "source_id": identifier,
        "status": "approved",
        "symbol": symbol,
        "side": side,
        "action": "entry" if side == "buy" else "exit",
        "notional": 100 if side == "buy" else None,
        "qty": None if side == "buy" else 10,
        "latest_price": 10,
        "stop_price": 9,
        "trading_mode": "paper",
    }


def _workflow(storage, state=ApprovalWorkflowState.VALIDATING):
    return ApprovalWorkflowStore(storage).create_or_get(
        approval_id="approval-1",
        proposal_id="proposal-1",
        telegram_update_id=101,
        initial_state=state,
    )


def _crash_at(boundary):
    def hook(actual, detail):
        if actual == boundary:
            raise SimulatedProcessCrash(actual)

    return hook


def assert_common_crash_invariants(tmp_path) -> None:
    """Shared postcondition set invoked for every named crash boundary."""
    for path in tmp_path.glob("*.sqlite3"):
        storage = Storage(path)
        assert storage.fetch_all("SELECT COUNT(*) n FROM risk_reservations WHERE active_notional<0 OR active_stop_risk<0")[0]["n"] == 0
        assert storage.fetch_all("SELECT COUNT(*) n FROM order_intents WHERE filled_quantity<0 OR filled_quantity>requested_quantity+0.000000001")[0]["n"] == 0
        report = DurableExecutionStore(storage).integrity_report()
        assert all(value == 0 for value in report.values()), report
        # Every exercised durable action has either an order event or a workflow/
        # reconciliation audit record; tests use only temporary databases/fakes.
        events = storage.fetch_all("SELECT COUNT(*) n FROM order_events")[0]["n"]
        audits = storage.fetch_all("SELECT COUNT(*) n FROM audit_events")[0]["n"]
        assert events > 0 or audits > 0


def crash_case(test):
    @functools.wraps(test)
    def wrapped(*args, **kwargs):
        result = test(*args, **kwargs)
        tmp_path = kwargs.get("tmp_path") or args[0]
        assert_common_crash_invariants(tmp_path)
        return result
    return wrapped


def test_crash_01_failure_before_intent_persistence(tmp_path):
    storage = _db(tmp_path)
    workflow = _workflow(storage)
    with pytest.raises(SimulatedProcessCrash):
        Executor(FakeBroker(), PassingRisk(), storage, "run", _crash_at("before_intent_persistence")).execute(
            _proposal(), {"approval_valid": True}, approval_id="approval-1"
        )
    assert storage.fetch_all("SELECT COUNT(*) n FROM order_intents")[0]["n"] == 0
    assert storage.fetch_all("SELECT COUNT(*) n FROM risk_reservations")[0]["n"] == 0
    assert ApprovalWorkflowStore(storage).get(workflow["id"])["state"] == "approved_pending_intent"


def test_crash_02_after_intent_commit_before_broker_call(tmp_path):
    storage, broker = _db(tmp_path), FakeBroker()
    with pytest.raises(SimulatedProcessCrash):
        Executor(broker, PassingRisk(), storage, "run", _crash_at("after_intent_and_reservation_commit")).execute(
            _proposal(), {"approval_valid": True}
        )
    intent = storage.fetch_all("SELECT * FROM order_intents")[0]
    assert broker.submit_calls == 0 and intent["state"] == "reserved"
    result = Executor(broker, PassingRisk(), Storage(storage.path), "restart").execute(
        _proposal(), {"approval_valid": True}
    )
    assert result.submitted and broker.submit_calls == 1
    assert result.client_order_id == intent["client_order_id"]


def test_crash_03_immediately_before_broker_invocation(tmp_path):
    storage, broker = _db(tmp_path), FakeBroker()
    workflow = _workflow(storage)
    with pytest.raises(SimulatedProcessCrash):
        Executor(broker, PassingRisk(), storage, "run", _crash_at("immediately_before_broker_submit")).execute(
            _proposal(), {"approval_valid": True}, approval_id="approval-1"
        )
    assert broker.submit_calls == 0
    assert storage.fetch_all("SELECT state FROM order_intents")[0]["state"] == "submitting"
    assert ApprovalWorkflowStore(storage).get(workflow["id"])["state"] == "submission_started"
    assert Executor(broker, PassingRisk(), Storage(storage.path), "restart", recovery_proven_no_submit=True).execute(
        _proposal(), {"approval_valid": True}, approval_id="approval-1"
    ).submitted
    assert broker.submit_calls == 1


def test_crash_04_broker_accepts_then_ambiguous_timeout(tmp_path):
    storage, broker = _db(tmp_path), FakeBroker(submit_error=TimeoutError("synthetic timeout"))
    first = Executor(broker, PassingRisk(), storage, "run").execute(_proposal(), {"approval_valid": True})
    second = Executor(broker, PassingRisk(), Storage(storage.path), "restart").execute(
        _proposal(), {"approval_valid": True}
    )
    assert first.status == second.status == "unknown"
    assert first.client_order_id == second.client_order_id and broker.submit_calls == 1
    assert DurableExecutionStore(storage).active_reservations()["active_reserved_notional"] == 100


def test_crash_05_broker_success_before_local_success_update(tmp_path):
    storage, broker = _db(tmp_path), FakeBroker()
    with pytest.raises(SimulatedProcessCrash):
        Executor(broker, PassingRisk(), storage, "run", _crash_at("after_broker_success_before_local_update")).execute(
            _proposal(), {"approval_valid": True}
        )
    assert broker.submit_calls == 1
    assert storage.fetch_all("SELECT state FROM order_intents")[0]["state"] == "submitting"
    result = BrokerReconciler(broker, Storage(storage.path), "restart").reconcile()
    assert result.found == 1 and broker.submit_calls == 1
    assert storage.fetch_all("SELECT state FROM order_intents")[0]["state"] == "submitted"


def test_crash_06_approval_accepted_before_intent_creation(tmp_path):
    storage = _db(tmp_path)
    workflow = _workflow(storage, ApprovalWorkflowState.APPROVED_PENDING_INTENT)
    summary = ApprovalWorkflowStore(Storage(storage.path)).recover(
        owner_token="restart", proposal_loader=lambda _: _proposal(), run_id="restart"
    )
    assert summary.intent_created == 1
    assert storage.fetch_all("SELECT COUNT(*) n FROM order_intents")[0]["n"] == 1
    assert ApprovalWorkflowStore(storage).get(workflow["id"])["state"] == "submission_pending"


def test_crash_07_intent_created_before_workflow_completion(tmp_path):
    storage = _db(tmp_path)
    workflow = _workflow(storage, ApprovalWorkflowState.APPROVED_PENDING_INTENT)
    intent = DurableExecutionStore(storage).create_or_get_intent(
        _proposal(), run_id="run", source_type="telegram", approval_id="approval-1"
    )
    storage.execute("UPDATE approval_workflows SET state='approved_pending_intent',intent_id=NULL WHERE id=?", (workflow["id"],))
    summary = ApprovalWorkflowStore(Storage(storage.path)).recover(
        owner_token="restart", proposal_loader=lambda _: _proposal(), run_id="restart"
    )
    assert summary.existing_intent_linked == 1
    assert storage.fetch_all("SELECT COUNT(*) n FROM order_intents")[0]["n"] == 1
    assert ApprovalWorkflowStore(storage).get(workflow["id"])["intent_id"] == intent["id"]


def test_crash_08_partial_fill_precedes_submitted_status(tmp_path):
    storage, broker = _db(tmp_path), FakeBroker()
    store = DurableExecutionStore(storage)
    intent = store.create_or_get_intent(_proposal(), run_id="run", source_type="proposal")
    store.transition(intent["id"], OrderState.SUBMITTING, event_type="test")
    store.record_fill(intent["id"], cumulative_quantity=4, fill_price=10, broker_event_key="exec-1")
    broker.orders[intent["client_order_id"]] = SimpleNamespace(id="paper-1", status="submitted", filled_qty="0", filled_avg_price=None)
    BrokerReconciler(broker, storage, "run").reconcile()
    assert store.get_intent(intent["id"])["state"] == "partially_filled"
    assert store.get_intent(intent["id"])["filled_quantity"] == 4


def test_crash_09_duplicate_partial_fill_event(tmp_path):
    storage, store = _db(tmp_path), None
    store = DurableExecutionStore(storage)
    intent = store.create_or_get_intent(_proposal(), run_id="run", source_type="proposal")
    store.transition(intent["id"], OrderState.SUBMITTING, event_type="test")
    store.record_fill(intent["id"], cumulative_quantity=4, fill_price=10, broker_event_key="exec-1")
    store.record_fill(intent["id"], cumulative_quantity=4, fill_price=10, broker_event_key="exec-1")
    assert store.get_intent(intent["id"])["filled_quantity"] == 4
    assert storage.fetch_all("SELECT COUNT(*) n FROM broker_fill_events")[0]["n"] == 1
    assert storage.fetch_all("SELECT COUNT(*) n FROM position_lots")[0]["n"] == 1


def test_crash_10_final_fill_after_partial_notification(tmp_path):
    storage, store = _db(tmp_path), None
    store = DurableExecutionStore(storage)
    intent = store.create_or_get_intent(_proposal(), run_id="run", source_type="proposal")
    store.transition(intent["id"], OrderState.SUBMITTING, event_type="test")
    store.record_fill(intent["id"], cumulative_quantity=4, fill_price=10, broker_event_key="exec-1")
    storage.execute("UPDATE fills SET fill_notification_status='sent',fill_notified_at=?", (datetime.now(UTC).isoformat(),))
    store.record_fill(intent["id"], cumulative_quantity=10, fill_price=11, broker_event_key="exec-2")
    assert store.get_intent(intent["id"])["state"] == "filled"
    assert storage.fetch_all("SELECT fill_notification_status,fill_notified_at FROM fills")[0] == {
        "fill_notification_status": "pending", "fill_notified_at": None
    }


def test_crash_11_cancellation_during_partial_fill(tmp_path):
    storage, store = _db(tmp_path), None
    store = DurableExecutionStore(storage)
    intent = store.create_or_get_intent(_proposal(), run_id="run", source_type="proposal")
    store.transition(intent["id"], OrderState.SUBMITTING, event_type="test")
    store.record_fill(intent["id"], cumulative_quantity=4, fill_price=10, broker_event_key="exec-1")
    store.transition(intent["id"], OrderState.CANCELLED, event_type="cancelled_remainder")
    assert store.get_intent(intent["id"])["filled_quantity"] == 4
    assert storage.fetch_all("SELECT state,active_notional FROM risk_reservations")[0] == {
        "state": "released", "active_notional": 0.0
    }


class FastStorage(Storage):
    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=0.05)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def test_crash_12_database_lock_during_approval_processing(tmp_path):
    storage = FastStorage(tmp_path / "locked.sqlite3")
    storage.initialize()
    owner = sqlite3.connect(storage.path)
    owner.execute("PRAGMA journal_mode=WAL")
    owner.execute("BEGIN IMMEDIATE")
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        ApprovalWorkflowStore(storage).create_or_get(approval_id="a", proposal_id="p", telegram_update_id=1)
    owner.rollback(); owner.close()
    workflow = ApprovalWorkflowStore(storage).create_or_get(approval_id="a", proposal_id="p", telegram_update_id=1)
    assert workflow["state"] == "received"
    assert storage.fetch_all("SELECT COUNT(*) n FROM approval_workflows")[0]["n"] == 1


def test_crash_13_database_lock_during_state_transition(tmp_path):
    storage = FastStorage(tmp_path / "state-lock.sqlite3"); storage.initialize()
    store = DurableExecutionStore(storage)
    intent = store.create_or_get_intent(_proposal(), run_id="run", source_type="proposal")
    owner = sqlite3.connect(storage.path); owner.execute("PRAGMA journal_mode=WAL"); owner.execute("BEGIN IMMEDIATE")
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        store.transition(intent["id"], OrderState.SUBMITTING, event_type="locked")
    owner.rollback(); owner.close()
    assert store.get_intent(intent["id"])["state"] == "reserved"
    assert store.transition(intent["id"], OrderState.SUBMITTING, event_type="retry")["state"] == "submitting"


def test_crash_14_restart_with_unknown_intent_is_lookup_only(tmp_path):
    storage, broker = _db(tmp_path), FakeBroker(submit_error=TimeoutError("synthetic"))
    result = Executor(broker, PassingRisk(), storage, "run").execute(_proposal(), {"approval_valid": True})
    assert result.status == "unknown" and broker.submit_calls == 1
    BrokerReconciler(broker, Storage(storage.path), "restart").reconcile()
    assert broker.submit_calls == 1 and broker.lookup_calls == 1
    assert DurableExecutionStore(storage).active_reservations()["active_reserved_notional"] == 100


def test_crash_15_restart_with_accepted_approval_without_decision(tmp_path):
    storage = _db(tmp_path)
    _workflow(storage, ApprovalWorkflowState.APPROVED_PENDING_INTENT)
    first = ApprovalWorkflowStore(Storage(storage.path)).recover(
        owner_token="one", proposal_loader=lambda _: _proposal(), run_id="restart"
    )
    second = ApprovalWorkflowStore(Storage(storage.path)).recover(
        owner_token="two", proposal_loader=lambda _: _proposal(), run_id="restart"
    )
    assert first.intent_created == 1 and second.intent_created == 0
    assert storage.fetch_all("SELECT COUNT(*) n FROM order_intents")[0]["n"] == 1


def test_crash_16_restart_with_stale_listener_lock(tmp_path, monkeypatch):
    lock = tmp_path / "listener.lockdir"; lock.mkdir()
    (lock / "pid").write_text("999999\n")
    (lock / "started_at_epoch").write_text("1\n")
    monkeypatch.setattr("app.run_lock._pid_exists", lambda pid: False)
    result = inspect_lock(lock, now=1000, dead_pid_grace_seconds=10)
    assert result.state == "stale" and result.pid == 999999


def test_crash_17_sequential_yes_all_exhausts_capacity(tmp_path):
    storage, store = _db(tmp_path), None
    store = DurableExecutionStore(storage)
    limits = {"total_notional_ceiling": 150, "buying_power_ceiling": 150}
    first = store.create_or_get_intent({**_proposal("a", "SPY"), "_reservation_limits": limits}, run_id="run", source_type="proposal")
    with pytest.raises(RuntimeError, match="atomic reservation blocked"):
        store.create_or_get_intent({**_proposal("b", "QQQ"), "_reservation_limits": limits}, run_id="run", source_type="proposal")
    assert first["state"] == "reserved"
    assert store.active_reservations()["active_reserved_notional"] == 100


def test_crash_18_emergency_ambiguous_timeout_matches_ordinary(tmp_path):
    storage, broker = _db(tmp_path), FakeBroker(submit_error=TimeoutError("synthetic"))
    emergency = {**_proposal("emergency", side="sell"), "emergency_exit_triggered": 1, "emergency_exit_trigger_reason": "synthetic protective trigger"}
    result = Executor(broker, PassingRisk(), storage, "run").execute(
        emergency, {"approval_valid": True}, source_type="emergency"
    )
    replay = Executor(broker, PassingRisk(), Storage(storage.path), "restart").execute(
        emergency, {"approval_valid": True}, source_type="emergency"
    )
    assert result.status == replay.status == "unknown"
    assert result.client_order_id == replay.client_order_id and broker.submit_calls == 1


def test_crash_19_close_and_reopen_creates_new_lifecycle(tmp_path):
    storage = _db(tmp_path)
    manager = PositionLifecycleManager(storage)
    first = manager.reconcile([SimpleNamespace(symbol="SPY", qty="2", avg_entry_price="10")])["SPY"]
    manager.reconcile([])
    second = manager.reconcile([SimpleNamespace(symbol="SPY", qty="1", avg_entry_price="11")])["SPY"]
    assert first != second
    assert storage.fetch_all("SELECT state FROM position_lifecycles WHERE id=?", (first,))[0]["state"] == "closed"


def test_crash_20_never_submitted_local_order_is_not_reconciled(tmp_path):
    storage, broker = _db(tmp_path), FakeBroker()
    now = datetime.now(UTC).isoformat()
    storage.execute(
        "INSERT INTO orders(id,client_order_id,symbol,side,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
        ("blocked", "never-submitted", "SPY", "buy", "blocked", now, now),
    )
    result = BrokerReconciler(broker, storage, "run").reconcile()
    assert result.checked == 0 and broker.lookup_calls == broker.submit_calls == 0


# Keep the named matrix readable while guaranteeing every row invokes the same
# complete DB-level invariant helper after its case-specific assertions.
for _name, _test in list(globals().items()):
    if _name.startswith("test_crash_"):
        globals()[_name] = crash_case(_test)
