from __future__ import annotations

import threading

import pytest

from app.execution import DurableExecutionStore
from app.order_state import OrderState
from app.storage import Storage


def _store(tmp_path):
    storage = Storage(tmp_path / "batch-reservation.sqlite3")
    storage.initialize()
    return storage, DurableExecutionStore(storage)


def _candidate(identifier, symbol, notional=60, cluster="broad", *, limits=None):
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
        "cluster_name": cluster,
        "trading_mode": "paper",
        "_reservation_limits": limits or {},
    }


def _create(store, candidate):
    return store.create_or_get_intent(candidate, run_id="batch", source_type="proposal")


def test_batch_combined_total_exposure_exhaustion(tmp_path):
    _, store = _store(tmp_path)
    limits = {"base_total_notional": 20, "total_notional_ceiling": 120}
    _create(store, _candidate("a", "SPY", 60, limits=limits))
    with pytest.raises(RuntimeError, match="total exposure"):
        _create(store, _candidate("b", "QQQ", 60, limits=limits))
    assert store.active_reservations()["active_reserved_notional"] == 60


def test_batch_combined_open_risk_exhaustion(tmp_path):
    _, store = _store(tmp_path)
    limits = {"open_risk_ceiling": 15}
    _create(store, _candidate("a", "SPY", 100, limits=limits))
    with pytest.raises(RuntimeError, match="open risk"):
        _create(store, _candidate("b", "QQQ", 100, limits=limits))
    assert store.active_reservations()["active_reserved_stop_risk"] == 10


def test_batch_shared_symbol_is_never_double_reserved(tmp_path):
    _, store = _store(tmp_path)
    _create(store, _candidate("a", "SPY", 40))
    with pytest.raises(RuntimeError, match="conflicting active order intent"):
        _create(store, _candidate("b", "SPY", 40))


@pytest.mark.parametrize(("symbols", "clusters"), [(('SPY', 'QQQ'), ('broad', 'broad')), (('SPY', 'IWM'), ('static-us', 'static-us'))])
def test_batch_same_cluster_capacity_is_shared(tmp_path, symbols, clusters):
    _, store = _store(tmp_path)
    limits = {"cluster_notional_ceiling": 100}
    _create(store, _candidate("a", symbols[0], 60, clusters[0], limits=limits))
    with pytest.raises(RuntimeError, match="cluster exposure"):
        _create(store, _candidate("b", symbols[1], 60, clusters[1], limits=limits))


def test_batch_unknown_first_submission_retains_capacity(tmp_path):
    _, store = _store(tmp_path)
    limits = {"total_notional_ceiling": 100}
    first = _create(store, _candidate("a", "SPY", 60, limits=limits))
    store.transition(first["id"], OrderState.SUBMITTING, event_type="test")
    store.transition(first["id"], OrderState.UNKNOWN, event_type="ambiguous")
    with pytest.raises(RuntimeError, match="total exposure"):
        _create(store, _candidate("b", "QQQ", 60, limits=limits))
    assert store.active_reservations()["active_reserved_notional"] == 60


def test_batch_partial_fill_retains_unfilled_capacity(tmp_path):
    _, store = _store(tmp_path)
    limits = {"total_notional_ceiling": 100}
    first = _create(store, _candidate("a", "SPY", 60, limits=limits))
    store.transition(first["id"], OrderState.SUBMITTING, event_type="test")
    store.record_fill(first["id"], cumulative_quantity=3, fill_price=10, broker_event_key="partial")
    assert store.active_reservations()["active_reserved_notional"] == 30
    with pytest.raises(RuntimeError, match="total exposure"):
        _create(store, _candidate("b", "QQQ", 80, limits={**limits, "base_total_notional": 30}))


@pytest.mark.parametrize("terminal", [OrderState.REJECTED, OrderState.CANCELLED, OrderState.EXPIRED])
def test_batch_terminal_first_submission_releases_capacity_once(tmp_path, terminal):
    storage, store = _store(tmp_path)
    limits = {"total_notional_ceiling": 100}
    first = _create(store, _candidate("a", "SPY", 60, limits=limits))
    if terminal == OrderState.CANCELLED:
        store.transition(first["id"], OrderState.SUBMITTING, event_type="test")
        store.transition(first["id"], OrderState.SUBMITTED, event_type="test")
    store.transition(first["id"], terminal, event_type="terminal")
    assert store.active_reservations()["active_reserved_notional"] == 0
    with pytest.raises(Exception):
        store.transition(first["id"], terminal, event_type="duplicate-terminal", expected_state=OrderState.RESERVED)
    second = _create(store, _candidate("b", "QQQ", 60, limits=limits))
    assert second["state"] == "reserved"
    assert storage.fetch_all("SELECT COUNT(*) n FROM risk_reservations WHERE state='active'")[0]["n"] == 1


def test_batch_duplicate_approval_delivery_reuses_same_intent(tmp_path):
    storage, store = _store(tmp_path)
    first = _create(store, _candidate("same", "SPY", 50))
    duplicate = _create(store, _candidate("same", "SPY", 50))
    assert duplicate["id"] == first["id"]
    assert storage.fetch_all("SELECT COUNT(*) n FROM risk_reservations")[0]["n"] == 1


def test_batch_concurrent_processing_has_one_capacity_winner(tmp_path):
    _, store = _store(tmp_path)
    barrier = threading.Barrier(2)
    results, errors = [], []

    def reserve(index):
        try:
            barrier.wait()
            results.append(_create(DurableExecutionStore(store.storage), _candidate(
                f"c{index}", ("SPY", "QQQ")[index], 60, limits={"total_notional_ceiling": 100}
            )))
        except BaseException as exc:
            errors.append(exc)

    workers = [threading.Thread(target=reserve, args=(index,)) for index in range(2)]
    for worker in workers: worker.start()
    for worker in workers: worker.join()
    assert len(results) == len(errors) == 1
    assert store.active_reservations()["active_reserved_notional"] == 60


def test_batch_policy_permitted_quantity_reduction_uses_only_reduced_reservation(tmp_path):
    _, store = _store(tmp_path)
    first = _create(store, _candidate("a", "SPY", 60, limits={"total_notional_ceiling": 100}))
    reduced = _create(store, _candidate("b", "QQQ", 40, limits={"total_notional_ceiling": 100}))
    assert first["reserved_notional"] == 60 and reduced["reserved_notional"] == 40
    assert store.active_reservations()["active_reserved_notional"] == 100


def test_batch_buying_power_cannot_be_double_spent(tmp_path):
    _, store = _store(tmp_path)
    limits = {"buying_power_ceiling": 100}
    _create(store, _candidate("a", "SPY", 60, limits=limits))
    with pytest.raises(RuntimeError, match="buying power"):
        _create(store, _candidate("b", "QQQ", 60, limits=limits))
