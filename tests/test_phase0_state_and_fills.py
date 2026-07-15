from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json

import pytest

from app.approval_workflow import ApprovalWorkflowState, ApprovalWorkflowStore
from app.execution import DurableExecutionStore, Executor
from app.order_state import ALLOWED_TRANSITIONS, InvalidOrderTransition, OrderState, validate_transition
from app.position_lifecycle import PositionLifecycleManager
from app.risk_engine import RiskDecision
from app.service import TradingService
from app.storage import Storage
from types import SimpleNamespace


def _db(tmp_path) -> Storage:
    storage = Storage(tmp_path / "state.db")
    storage.initialize()
    return storage


def _proposal(identifier: str = "p-state", side: str = "buy") -> dict:
    return {
        "id": identifier,
        "proposal_id": identifier,
        "source_id": identifier,
        "status": "approved",
        "symbol": "SPY",
        "side": side,
        "action": "entry" if side == "buy" else "exit",
        "qty": 10,
        "latest_price": 50,
        "stop_price": 45,
        "trading_mode": "paper",
        "order_type": "limit", "quote_source": "alpaca_quote", "quote_bid": 49.9,
        "quote_ask": 50.1, "quote_midpoint": 50.0,
        "quote_timestamp": datetime.now(UTC).isoformat(), "quote_spread_bps": 40.0,
        "limit_price": 50.23 if side == "buy" else 49.77,
    }


def _authorize(storage: Storage, candidate: dict, approval_id: str = "approval-state") -> dict:
    now = datetime.now(UTC).isoformat()
    expires_at = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    canonical_notional = float(candidate["qty"]) * max(
        float(candidate["latest_price"]), float(candidate["limit_price"])
    )
    candidate = {**candidate, "notional": canonical_notional, "expires_at": expires_at, "strategy_version": "rule_based_v2"}
    storage.execute(
        """INSERT INTO trade_proposals(id,symbol,side,notional,status,created_at,expires_at,strategy_version,payload)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (candidate["id"], candidate["symbol"], candidate["side"], canonical_notional, "pending", now,
         expires_at, "rule_based_v2", json.dumps(candidate)),
    )
    store = ApprovalWorkflowStore(storage)
    workflow = store.accept_approval(
        approval_id=approval_id, run_id="run", proposal_id=candidate["id"], sender_id="owner",
        raw_message="approve", parsed_action="approve", telegram_update_id=None,
        reply_to_message_id=None, targeting_method="test", acknowledgement_status="received",
        approval_received_at=now,
    )
    assert storage.consume_approval(candidate["id"], approval_id)
    store.transition(workflow["id"], ApprovalWorkflowState.VALIDATING,
                     expected_state=ApprovalWorkflowState.TARGET_RESOLVED)
    return candidate


@pytest.mark.parametrize("source", list(OrderState))
@pytest.mark.parametrize("target", list(OrderState))
def test_complete_order_transition_specification(source: OrderState, target: OrderState):
    if source == target or target in ALLOWED_TRANSITIONS[source]:
        assert validate_transition(source, target) == (source, target)
    else:
        with pytest.raises(InvalidOrderTransition):
            validate_transition(source, target)


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (OrderState.FILLED, OrderState.SUBMITTED),
        (OrderState.FILLED, OrderState.PARTIALLY_FILLED),
        (OrderState.CANCELLED, OrderState.SUBMITTED),
        (OrderState.REJECTED, OrderState.SUBMITTED),
        (OrderState.EXPIRED, OrderState.RESERVED),
    ],
)
def test_regressive_terminal_transitions_fail_closed(source: OrderState, target: OrderState):
    with pytest.raises(InvalidOrderTransition):
        validate_transition(source, target)


def test_partial_cancel_preserves_fill_and_releases_only_remaining_reservation(tmp_path):
    storage = _db(tmp_path)
    store = DurableExecutionStore(storage)
    intent = store.create_or_get_intent(_proposal(), run_id="run", source_type="proposal")
    store.transition(intent["id"], OrderState.SUBMITTING, event_type="test")
    store.transition(intent["id"], OrderState.SUBMITTED, event_type="test")
    store.record_fill(intent["id"], cumulative_quantity=4, fill_price=50, broker_event_key="exec-1")
    before = storage.fetch_all("SELECT active_notional FROM risk_reservations")[0]["active_notional"]
    assert before == pytest.approx(301.38)
    store.transition(intent["id"], OrderState.CANCELLED, event_type="broker_cancelled_remainder")
    final = store.get_intent(intent["id"])
    reservation = storage.fetch_all("SELECT initial_notional,active_notional,state FROM risk_reservations")[0]
    assert (final["filled_quantity"], final["state"]) == (4, "cancelled")
    assert reservation["initial_notional"] == pytest.approx(502.3)
    assert reservation["active_notional"] == 0.0
    assert reservation["state"] == "released"


def test_duplicate_and_out_of_order_cumulative_fills_are_monotonic(tmp_path):
    storage = _db(tmp_path)
    store = DurableExecutionStore(storage)
    intent = store.create_or_get_intent(_proposal(), run_id="run", source_type="proposal")
    store.transition(intent["id"], OrderState.SUBMITTING, event_type="test")
    store.record_fill(intent["id"], cumulative_quantity=6, fill_price=51, broker_event_key="exec-2")
    store.record_fill(intent["id"], cumulative_quantity=2, fill_price=49, broker_event_key="exec-1")
    store.record_fill(intent["id"], cumulative_quantity=2, fill_price=49, broker_event_key="exec-1")
    store.record_fill(intent["id"], cumulative_quantity=6, fill_price=51, broker_event_key="exec-2")
    current = store.get_intent(intent["id"])
    assert current["filled_quantity"] == 6
    assert current["average_fill_price"] == 51
    assert storage.fetch_all("SELECT COUNT(*) n FROM broker_fill_events")[0]["n"] == 2
    assert storage.fetch_all("SELECT COUNT(*) n FROM order_events WHERE event_type='out_of_order_fill_ignored'")[0]["n"] == 1
    assert storage.fetch_all("SELECT confidence FROM pnl_ledger_status WHERE scope='prospective'")[0]["confidence"] == "partially_reconstructed"


def test_final_fill_after_partial_reopens_final_notification_eligibility(tmp_path):
    storage = _db(tmp_path)
    store = DurableExecutionStore(storage)
    intent = store.create_or_get_intent(_proposal(), run_id="run", source_type="proposal")
    store.transition(intent["id"], OrderState.SUBMITTING, event_type="test")
    store.record_fill(intent["id"], cumulative_quantity=4, fill_price=50, broker_event_key="exec-1")
    storage.execute(
        "UPDATE fills SET fill_notified_at=?,fill_notification_status='sent' WHERE order_id=?",
        (datetime.now(UTC).isoformat(), intent["id"]),
    )
    store.record_fill(intent["id"], cumulative_quantity=10, fill_price=52, broker_event_key="exec-2")
    fill = storage.fetch_all("SELECT qty,price,fill_notified_at,fill_notification_status FROM fills")[0]
    assert fill["qty"] == 10
    assert fill["price"] == pytest.approx(51.2)
    assert fill["fill_notified_at"] is None
    assert fill["fill_notification_status"] == "pending"


def test_integrity_report_covers_event_fill_and_identity_consistency(tmp_path):
    storage = _db(tmp_path)
    store = DurableExecutionStore(storage)
    intent = store.create_or_get_intent(_proposal(), run_id="run", source_type="proposal")
    store.transition(intent["id"], OrderState.SUBMITTING, event_type="test")
    store.record_fill(intent["id"], cumulative_quantity=1, fill_price=50, broker_event_key="exec-1")
    report = store.integrity_report()
    assert report["state_latest_event_mismatch"] == 0
    assert report["transition_counter_mismatch"] == 0
    assert report["fill_ledger_mismatch"] == 0
    assert report["broker_relevant_missing_identity"] == 0


def test_two_partial_fills_same_price_or_same_delta_remain_distinct(tmp_path):
    storage = _db(tmp_path)
    store = DurableExecutionStore(storage)
    intent = store.create_or_get_intent(_proposal(), run_id="run", source_type="proposal")
    store.transition(intent["id"], OrderState.SUBMITTING, event_type="test")
    store.record_fill(intent["id"], cumulative_quantity=2, fill_price=50, broker_event_key="exec-1")
    store.record_fill(intent["id"], cumulative_quantity=4, fill_price=50, broker_event_key="exec-2")
    store.record_fill(intent["id"], cumulative_quantity=6, fill_price=51, broker_event_key="exec-3")
    fills = storage.fetch_all("SELECT delta_quantity,fill_price FROM broker_fill_events ORDER BY occurred_at,id")
    assert len(fills) == 3
    assert sorted(row["delta_quantity"] for row in fills) == [2, 2, 2]
    assert store.get_intent(intent["id"])["average_fill_price"] == pytest.approx(50 + 1 / 3)


def test_final_fill_before_earlier_partial_never_regresses_or_releases_twice(tmp_path):
    storage = _db(tmp_path)
    store = DurableExecutionStore(storage)
    intent = store.create_or_get_intent(_proposal(), run_id="run", source_type="proposal")
    store.transition(intent["id"], OrderState.SUBMITTING, event_type="test")
    store.record_fill(intent["id"], cumulative_quantity=10, fill_price=50, broker_event_key="final")
    store.record_fill(intent["id"], cumulative_quantity=4, fill_price=49, broker_event_key="late-partial")
    assert store.get_intent(intent["id"])["state"] == "filled"
    assert store.get_intent(intent["id"])["filled_quantity"] == 10
    assert storage.fetch_all("SELECT state,active_notional FROM risk_reservations")[0] == {
        "state": "released", "active_notional": 0.0
    }


@pytest.mark.parametrize("starting", [OrderState.CANCEL_PENDING, OrderState.UNKNOWN, OrderState.RECONCILIATION_REQUIRED])
def test_late_fill_is_accepted_from_nonterminal_ambiguous_states(tmp_path, starting):
    storage = _db(tmp_path)
    store = DurableExecutionStore(storage)
    intent = store.create_or_get_intent(_proposal(), run_id="run", source_type="proposal")
    store.transition(intent["id"], OrderState.SUBMITTING, event_type="test")
    if starting == OrderState.CANCEL_PENDING:
        store.transition(intent["id"], OrderState.SUBMITTED, event_type="test")
        store.transition(intent["id"], starting, event_type="test")
    elif starting == OrderState.UNKNOWN:
        store.transition(intent["id"], starting, event_type="test")
    else:
        store.transition(intent["id"], OrderState.UNKNOWN, event_type="test")
        store.transition(intent["id"], starting, event_type="test")
    updated = store.record_fill(intent["id"], cumulative_quantity=3, fill_price=50, broker_event_key=f"fill-{starting}")
    assert updated["state"] == "partially_filled" and updated["filled_quantity"] == 3
    assert store.active_reservations()["active_reserved_notional"] == pytest.approx(351.61)


def test_restart_replay_entire_fill_stream_is_idempotent(tmp_path):
    storage = _db(tmp_path)
    store = DurableExecutionStore(storage)
    intent = store.create_or_get_intent(_proposal(), run_id="run", source_type="proposal")
    store.transition(intent["id"], OrderState.SUBMITTING, event_type="test")
    stream = [(2, 49, "e1"), (5, 50, "e2"), (10, 51, "e3")]
    for cumulative, price, key in stream:
        store.record_fill(intent["id"], cumulative_quantity=cumulative, fill_price=price, broker_event_key=key)
    fresh = DurableExecutionStore(Storage(storage.path))
    for cumulative, price, key in stream:
        fresh.record_fill(intent["id"], cumulative_quantity=cumulative, fill_price=price, broker_event_key=key)
    final = fresh.get_intent(intent["id"])
    assert final["filled_quantity"] == 10 and final["state"] == "filled"
    assert storage.fetch_all("SELECT COUNT(*) n FROM broker_fill_events")[0]["n"] == 3
    assert storage.fetch_all("SELECT COUNT(*) n FROM position_lots")[0]["n"] == 3


def test_cumulative_average_price_from_rest_snapshot_is_not_double_averaged(tmp_path):
    storage = _db(tmp_path)
    store = DurableExecutionStore(storage)
    intent = store.create_or_get_intent(_proposal(), run_id="run", source_type="proposal")
    store.transition(intent["id"], OrderState.SUBMITTING, event_type="test")
    store.record_fill(intent["id"], cumulative_quantity=2, fill_price=49, broker_event_key="stream-1")
    store.record_fill(intent["id"], cumulative_quantity=5, fill_price=50, broker_event_key="rest-5", price_is_cumulative_average=True)
    assert store.get_intent(intent["id"])["average_fill_price"] == 50
    delta = storage.fetch_all("SELECT fill_price FROM broker_fill_events WHERE broker_event_key='rest-5'")[0]["fill_price"]
    assert delta == pytest.approx(50 + 2 / 3)


def test_immediate_filled_broker_response_records_fill_lot_and_quantity(tmp_path):
    storage = _db(tmp_path)

    class Risk:
        config = {}
        def evaluate(self, proposal, context, final=False): return RiskDecision(True, ())

    class Broker:
        def submit_order(self, *args):
            return SimpleNamespace(id="paper-filled", status="filled", filled_qty="10", filled_avg_price="50", execution_id="exec-immediate", filled_at=datetime.now(UTC).isoformat())

    candidate = _authorize(storage, _proposal())
    result = Executor(Broker(), Risk(), storage, "run").execute(
        candidate, {"approval_valid": True}, approval_id="approval-state"
    )
    assert result.submitted and result.status == "filled"
    assert storage.fetch_all("SELECT state,filled_quantity,average_fill_price FROM order_intents")[0] == {"state": "filled", "filled_quantity": 10.0, "average_fill_price": 50.0}
    assert storage.fetch_all("SELECT COUNT(*) n FROM broker_fill_events")[0]["n"] == 1
    assert storage.fetch_all("SELECT COUNT(*) n FROM position_lots")[0]["n"] == 1


def test_new_broker_position_backfills_prospective_intent_and_lot_lifecycle(tmp_path):
    storage = _db(tmp_path)
    store = DurableExecutionStore(storage)
    intent = store.create_or_get_intent(_proposal(), run_id="run", source_type="proposal")
    store.transition(intent["id"], OrderState.SUBMITTING, event_type="test")
    store.record_fill(intent["id"], cumulative_quantity=10, fill_price=50, broker_event_key="exec")
    lifecycle = PositionLifecycleManager(storage).reconcile([SimpleNamespace(symbol="SPY", qty="10", avg_entry_price="50")])["SPY"]
    assert storage.fetch_all("SELECT position_lifecycle_id FROM order_intents")[0]["position_lifecycle_id"] == lifecycle
    assert storage.fetch_all("SELECT DISTINCT position_lifecycle_id FROM position_lots")[0]["position_lifecycle_id"] == lifecycle


def test_opening_quantity_grows_with_original_entry_but_excludes_later_add(tmp_path):
    storage = _db(tmp_path)
    store = DurableExecutionStore(storage)
    entry = store.create_or_get_intent(_proposal("entry-quantity"), run_id="run", source_type="proposal")
    store.transition(entry["id"], OrderState.SUBMITTING, event_type="test")
    store.record_fill(entry["id"], cumulative_quantity=5, fill_price=50, broker_event_key="entry-partial")

    lifecycle = PositionLifecycleManager(storage).reconcile(
        [SimpleNamespace(symbol="SPY", qty="5", avg_entry_price="50")]
    )["SPY"]
    assert storage.fetch_all("SELECT opening_quantity FROM position_lifecycles WHERE id=?", (lifecycle,))[0]["opening_quantity"] == 5

    store.record_fill(entry["id"], cumulative_quantity=10, fill_price=50, broker_event_key="entry-final")
    PositionLifecycleManager(storage).reconcile(
        [SimpleNamespace(symbol="SPY", qty="10", avg_entry_price="50")]
    )
    assert storage.fetch_all("SELECT opening_quantity FROM position_lifecycles WHERE id=?", (lifecycle,))[0]["opening_quantity"] == 10

    add = store.create_or_get_intent(
        {**_proposal("later-add"), "action": "add", "qty": 2, "is_add": True},
        run_id="run", source_type="proposal",
    )
    store.transition(add["id"], OrderState.SUBMITTING, event_type="test")
    store.record_fill(add["id"], cumulative_quantity=2, fill_price=51, broker_event_key="add-fill")
    PositionLifecycleManager(storage).reconcile(
        [SimpleNamespace(symbol="SPY", qty="12", avg_entry_price="50.17")]
    )

    row = storage.fetch_all(
        "SELECT opening_quantity,current_quantity,opening_quantity_frozen FROM position_lifecycles WHERE id=?",
        (lifecycle,),
    )[0]
    assert row == {"opening_quantity": 10.0, "current_quantity": 12.0, "opening_quantity_frozen": 1}
    service = TradingService.__new__(TradingService)
    service.storage = storage
    seed = service._initial_risk_seed_for_position("SPY")
    assert seed["initial_risk_dollars"] == pytest.approx(50.0)
