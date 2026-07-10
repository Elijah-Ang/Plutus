from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager

import pytest

from app.approval_workflow import ApprovalWorkflowConflict, ApprovalWorkflowState, ApprovalWorkflowStore
from app.execution import DurableExecutionStore
from app.order_state import InvalidOrderTransition, OrderState
from app.storage import Storage


def _storage(tmp_path) -> Storage:
    storage = Storage(tmp_path / "concurrency.sqlite3")
    storage.initialize()
    return storage


def _proposal(identifier: str, symbol: str = "SPY", notional: float = 60) -> dict:
    return {
        "id": identifier,
        "proposal_id": identifier,
        "source_id": identifier,
        "status": "approved",
        "symbol": symbol,
        "side": "buy",
        "action": "entry",
        "notional": notional,
        "latest_price": 10,
        "stop_price": 9,
        "trading_mode": "paper",
        "cluster_name": "broad",
    }


def _parallel(count, operation):
    barrier = threading.Barrier(count)
    results, errors = [], []

    def run(index):
        try:
            barrier.wait()
            results.append(operation(index))
        except BaseException as exc:  # asserted by each deterministic case
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(index,)) for index in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()
    return results, errors


def test_concurrency_01_two_handlers_process_same_telegram_update(tmp_path):
    storage = _storage(tmp_path)
    results, errors = _parallel(
        2,
        lambda _: ApprovalWorkflowStore(storage).create_or_get(
            approval_id="same-approval", proposal_id="same-proposal", telegram_update_id=77
        ),
    )
    assert not errors and len(results) == 2
    assert len({row["id"] for row in results}) == 1
    assert storage.fetch_all("SELECT COUNT(*) n FROM approval_workflows")[0]["n"] == 1


def test_concurrency_02_two_handlers_accept_same_proposal(tmp_path):
    storage = _storage(tmp_path)
    results, errors = _parallel(
        2,
        lambda index: ApprovalWorkflowStore(storage).create_or_get(
            approval_id=f"approval-{index}", proposal_id="same-proposal", telegram_update_id=80 + index
        ),
    )
    assert len(results) == 1
    assert len(errors) == 1 and isinstance(errors[0], ApprovalWorkflowConflict)
    assert storage.fetch_all("SELECT COUNT(*) n FROM approval_workflows")[0]["n"] == 1


def test_concurrency_04_two_recovery_workers_claim_same_workflow(tmp_path):
    storage = _storage(tmp_path)
    ApprovalWorkflowStore(storage).create_or_get(
        approval_id="approval", proposal_id="proposal", telegram_update_id=88,
        initial_state=ApprovalWorkflowState.APPROVED_PENDING_INTENT,
    )
    results, errors = _parallel(
        2,
        lambda index: ApprovalWorkflowStore(storage).claim_next(f"worker-{index}", lease_seconds=60),
    )
    assert not errors
    assert sum(row is not None for row in results) == 1


def test_concurrency_03_two_workers_create_same_intent(tmp_path):
    storage = _storage(tmp_path)
    results, errors = _parallel(
        2,
        lambda _: DurableExecutionStore(storage).create_or_get_intent(
            _proposal("same", notional=50), run_id="run", source_type="proposal"
        ),
    )
    assert not errors
    assert len({row["id"] for row in results}) == 1
    assert storage.fetch_all("SELECT COUNT(*) n FROM risk_reservations")[0]["n"] == 1


def test_concurrency_05_two_workers_transition_same_state(tmp_path):
    storage = _storage(tmp_path)
    store = DurableExecutionStore(storage)
    intent = store.create_or_get_intent(_proposal("transition", notional=50), run_id="run", source_type="proposal")
    results, errors = _parallel(
        2,
        lambda _: DurableExecutionStore(storage).transition(
            intent["id"], OrderState.SUBMITTING, event_type="concurrent", expected_state=OrderState.RESERVED
        ),
    )
    assert len(results) == 1
    assert len(errors) == 1 and isinstance(errors[0], InvalidOrderTransition)
    assert storage.fetch_all("SELECT COUNT(*) n FROM order_events WHERE to_state='submitting'")[0]["n"] == 1


def test_concurrency_06_reconciliation_and_stream_deduplicate_fill(tmp_path):
    storage = _storage(tmp_path)
    store = DurableExecutionStore(storage)
    intent = store.create_or_get_intent(_proposal("fill", notional=100), run_id="run", source_type="proposal")
    store.transition(intent["id"], OrderState.SUBMITTING, event_type="test")
    results, errors = _parallel(
        2,
        lambda _: DurableExecutionStore(storage).record_fill(
            intent["id"], cumulative_quantity=5, fill_price=10, broker_event_key="broker-execution-1"
        ),
    )
    assert len(results) == 2 and not errors
    assert storage.fetch_all("SELECT COUNT(*) n FROM broker_fill_events")[0]["n"] == 1
    assert store.get_intent(intent["id"])["filled_quantity"] == 5


def test_concurrency_07_scanner_listener_atomic_capacity_reservation(tmp_path):
    storage = _storage(tmp_path)
    limit = {
        "total_notional_ceiling": 100,
        "symbol_notional_ceiling": 100,
        "cluster_notional_ceiling": 100,
        "open_risk_ceiling": 100,
        "buying_power_ceiling": 100,
    }

    def reserve(index):
        proposal = {**_proposal(f"worker-{index}", symbol=("SPY", "QQQ")[index]), "_reservation_limits": limit}
        return DurableExecutionStore(storage).create_or_get_intent(proposal, run_id="run", source_type="proposal")

    results, errors = _parallel(2, reserve)
    assert len(results) == 1
    assert len(errors) == 1 and "atomic reservation blocked" in str(errors[0])
    active = DurableExecutionStore(storage).active_reservations()
    assert active["active_reserved_notional"] == 60
    assert active["active_reserved_notional"] <= 100


def test_concurrency_08_overlapping_yes_all_logical_actions_win_once(tmp_path):
    storage = _storage(tmp_path)

    def batch(_):
        return [
            DurableExecutionStore(storage).create_or_get_intent(
                _proposal(identifier, symbol, 40), run_id="run", source_type="proposal"
            )["id"]
            for identifier, symbol in (("batch-a", "SPY"), ("batch-b", "QQQ"))
        ]

    results, errors = _parallel(2, batch)
    assert not errors and len(results) == 2
    assert storage.fetch_all("SELECT COUNT(*) n FROM order_intents")[0]["n"] == 2
    assert storage.fetch_all("SELECT COUNT(DISTINCT client_order_id) n FROM order_intents")[0]["n"] == 2


class FastLockStorage(Storage):
    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=0.05)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def test_concurrency_09_write_lock_timeout_is_explicit_and_retryable(tmp_path):
    storage = FastLockStorage(tmp_path / "locked.sqlite3")
    storage.initialize()
    store = DurableExecutionStore(storage)
    intent = store.create_or_get_intent(_proposal("locked", notional=50), run_id="run", source_type="proposal")
    owner = sqlite3.connect(storage.path, timeout=0.05)
    owner.execute("PRAGMA journal_mode=WAL")
    owner.execute("BEGIN IMMEDIATE")
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        store.transition(intent["id"], OrderState.SUBMITTING, event_type="blocked-by-lock")
    owner.rollback()
    owner.close()
    assert store.get_intent(intent["id"])["state"] == "reserved"
    assert storage.fetch_all("SELECT COUNT(*) n FROM order_events WHERE to_state='submitting'")[0]["n"] == 0
    assert store.transition(intent["id"], OrderState.SUBMITTING, event_type="retry")["state"] == "submitting"


def test_concurrency_10_migration_writer_does_not_block_wal_reader_snapshot(tmp_path):
    storage = _storage(tmp_path)
    reader = sqlite3.connect(storage.path)
    reader.execute("BEGIN")
    assert reader.execute("SELECT COUNT(*) FROM trade_proposals").fetchone()[0] == 0
    writer = sqlite3.connect(storage.path)
    writer.execute("CREATE TABLE concurrent_migration_probe(id INTEGER)")
    writer.commit()
    assert reader.execute("SELECT COUNT(*) FROM trade_proposals").fetchone()[0] == 0
    reader.rollback()
    assert reader.execute("SELECT COUNT(*) FROM concurrent_migration_probe").fetchone()[0] == 0
    reader.close()
    writer.close()


def test_concurrency_11_health_reads_during_snapshot_write(tmp_path):
    storage = _storage(tmp_path)
    started = threading.Event()
    release = threading.Event()
    errors = []

    def writer():
        try:
            with storage.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "INSERT INTO health_heartbeats(component,state,updated_at) VALUES('risk_snapshot','healthy','2026-01-01T00:00:00+00:00')"
                )
                started.set()
                assert release.wait(2)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=writer)
    thread.start()
    assert started.wait(2)
    assert storage.fetch_all("SELECT COUNT(*) n FROM health_heartbeats")[0]["n"] == 0
    release.set()
    thread.join(2)
    assert not errors
    assert storage.fetch_all("SELECT COUNT(*) n FROM health_heartbeats")[0]["n"] == 1


def test_concurrency_12_reservation_release_races_late_fill_without_negative_value(tmp_path):
    storage = _storage(tmp_path)
    store = DurableExecutionStore(storage)
    intent = store.create_or_get_intent(_proposal("release", notional=100), run_id="run", source_type="proposal")
    store.transition(intent["id"], OrderState.SUBMITTING, event_type="test")
    store.transition(intent["id"], OrderState.SUBMITTED, event_type="test")
    store.transition(intent["id"], OrderState.CANCEL_PENDING, event_type="cancel-requested")
    results, errors = _parallel(
        2,
        lambda index: (
            DurableExecutionStore(storage).record_fill(
                intent["id"], cumulative_quantity=5, fill_price=10, broker_event_key="late-fill"
            )
            if index == 0
            else DurableExecutionStore(storage).transition(
                intent["id"], OrderState.CANCELLED, event_type="cancel-acknowledged"
            )
        ),
    )
    reservation = storage.fetch_all("SELECT active_notional,active_stop_risk,state FROM risk_reservations")[0]
    current = store.get_intent(intent["id"])
    assert reservation["active_notional"] >= 0 and reservation["active_stop_risk"] >= 0
    assert current["filled_quantity"] == 5
    assert len(results) == 2 and errors == []
