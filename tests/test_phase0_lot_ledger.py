from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.execution import DurableExecutionStore
from app.lot_ledger import ACCOUNTING_TIMEZONE, LotLedger
from app.order_state import OrderState
from app.risk_engine import RiskEngine
from app.risk_snapshot import RiskSnapshotBuilder
from app.storage import Storage


def database(tmp_path) -> Storage:
    result = Storage(tmp_path / "lots.db")
    result.initialize()
    return result


def intent(db: Storage, identifier: str, side: str, quantity: float, price: float = 10.0):
    store = DurableExecutionStore(db)
    row = store.create_or_get_intent(
        {
            "id": identifier,
            "proposal_id": identifier,
            "symbol": "SPY",
            "side": side,
            "action": "entry" if side == "buy" else "exit",
            "qty": quantity,
            "notional": None,
            "latest_price": price,
            "trading_mode": "paper",
        },
        run_id="run",
        source_type="proposal",
    )
    store.transition(row["id"], OrderState.SUBMITTING, event_type="test")
    store.transition(row["id"], OrderState.SUBMITTED, event_type="test")
    return store, row


def test_fifo_multiple_lots_partial_close_and_fees(tmp_path):
    db = database(tmp_path)
    ledger = LotLedger(db)
    ledger.set_coverage(effective_from="2026-07-05T00:00:00+00:00", confidence="verified", provenance="prospective migration boundary")
    store, first = intent(db, "buy-1", "buy", 2, 10)
    store.record_fill(first["id"], cumulative_quantity=2, fill_price=10, broker_event_key="buy-fill-1", occurred_at="2026-07-06T14:00:00+00:00")
    store, second = intent(db, "buy-2", "buy", 3, 20)
    store.record_fill(second["id"], cumulative_quantity=3, fill_price=20, broker_event_key="buy-fill-2", occurred_at="2026-07-06T15:00:00+00:00")
    store, sell = intent(db, "sell-1", "sell", 4, 30)
    store.record_fill(sell["id"], cumulative_quantity=4, fill_price=30, broker_event_key="sell-fill-1", occurred_at="2026-07-06T16:00:00+00:00", fees=1.0)

    event = db.fetch_all("SELECT * FROM realized_pnl_events")[0]
    assert event["cost_basis"] == pytest.approx(60.0)
    assert event["gross_proceeds"] == pytest.approx(120.0)
    assert event["realized_pl"] == pytest.approx(59.0)
    assert event["remaining_position_quantity"] == pytest.approx(1.0)
    assert event["confidence"] == "verified"
    lots = db.fetch_all("SELECT unit_cost,remaining_quantity FROM position_lots ORDER BY opened_at")
    assert lots == [{"unit_cost": 10.0, "remaining_quantity": 0.0}, {"unit_cost": 20.0, "remaining_quantity": 1.0}]


def test_duplicate_and_out_of_order_fill_do_not_duplicate_lot_or_pnl(tmp_path):
    db = database(tmp_path)
    LotLedger(db).set_coverage(effective_from="2026-07-01T00:00:00+00:00", confidence="verified", provenance="test")
    store, buy = intent(db, "buy", "buy", 5)
    store.record_fill(buy["id"], cumulative_quantity=3, fill_price=10, broker_event_key="buy-1")
    store.record_fill(buy["id"], cumulative_quantity=3, fill_price=10, broker_event_key="buy-1")
    store.record_fill(buy["id"], cumulative_quantity=2, fill_price=9, broker_event_key="older")
    assert db.fetch_all("SELECT COUNT(*) n FROM position_lots")[0]["n"] == 1
    assert db.fetch_all("SELECT remaining_quantity FROM position_lots")[0]["remaining_quantity"] == 3
    store, sell = intent(db, "sell", "sell", 1, 15)
    store.record_fill(sell["id"], cumulative_quantity=1, fill_price=15, broker_event_key="sell-1")
    store.record_fill(sell["id"], cumulative_quantity=1, fill_price=15, broker_event_key="sell-1")
    assert db.fetch_all("SELECT COUNT(*) n FROM realized_pnl_events")[0]["n"] == 1


def test_missing_historical_basis_is_unavailable_not_zero(tmp_path):
    db = database(tmp_path)
    LotLedger(db).set_coverage(effective_from="2026-07-10T00:00:00+00:00", confidence="unavailable", provenance="pre-migration basis absent")
    store, sell = intent(db, "sell-without-basis", "sell", 2, 12)
    store.record_fill(sell["id"], cumulative_quantity=2, fill_price=12, broker_event_key="unknown-basis", occurred_at="2026-07-10T15:00:00+00:00")
    row = db.fetch_all("SELECT realized_pl,cost_basis,confidence FROM realized_pnl_events")[0]
    assert row == {"realized_pl": None, "cost_basis": None, "confidence": "unavailable"}
    summary = LotLedger(db).summary(as_of="2026-07-10T16:00:00+00:00")
    assert summary.daily_realized_pl is None and summary.daily_confidence == "unavailable"


def test_manual_adjustment_records_provenance_and_unknown_cost(tmp_path):
    db = database(tmp_path)
    identifier = LotLedger(db).record_manual_adjustment(
        symbol="spy", quantity=2, unit_cost=None, occurred_at="2026-07-10T14:00:00+00:00",
        provenance="broker position discovered during reconciliation",
    )
    row = db.fetch_all("SELECT * FROM position_lots WHERE id=?", (identifier,))[0]
    assert row["source"] == "manual_adjustment"
    assert row["confidence"] == "unavailable"
    assert "broker position" in row["provenance"]


def test_day_and_week_rollover_use_new_york_boundaries(tmp_path):
    db = database(tmp_path)
    # Sunday evening New York is already Monday UTC; the ledger deliberately
    # follows New York civil boundaries rather than UTC dates.
    ledger = LotLedger(db)
    ledger.set_coverage(effective_from="2026-07-05T04:00:00+00:00", confidence="verified", provenance="verified start")
    store, buy = intent(db, "roll-buy", "buy", 2, 10)
    store.record_fill(buy["id"], cumulative_quantity=2, fill_price=10, broker_event_key="roll-buy-fill", occurred_at="2026-07-06T13:00:00+00:00")
    store, sell = intent(db, "roll-sell", "sell", 1, 12)
    store.record_fill(sell["id"], cumulative_quantity=1, fill_price=12, broker_event_key="roll-sell-fill", occurred_at="2026-07-06T14:00:00+00:00")
    monday = ledger.summary(as_of="2026-07-06T20:00:00+00:00")
    tuesday = ledger.summary(as_of="2026-07-07T20:00:00+00:00")
    next_week = ledger.summary(as_of="2026-07-13T20:00:00+00:00")
    assert monday.accounting_timezone == ACCOUNTING_TIMEZONE
    assert monday.daily_realized_pl == 2 and monday.weekly_realized_pl == 2
    assert tuesday.daily_realized_pl == 0 and tuesday.weekly_realized_pl == 2
    assert next_week.daily_realized_pl == 0 and next_week.weekly_realized_pl == 0


def test_risk_snapshot_never_substitutes_zero_for_unknown_realized_pl(tmp_path):
    db = database(tmp_path)
    snapshot = RiskSnapshotBuilder(db).build([], {"equity": 100, "cash": 100, "buying_power": 100})
    assert snapshot.daily_realized_pl is None and snapshot.weekly_realized_pl is None
    assert snapshot.daily_realized_loss_pct is None and snapshot.weekly_realized_loss_pct is None
    assert snapshot.daily_realized_pl_status == "unavailable"


@pytest.mark.parametrize("status", ["unavailable", "stale", "partially_reconstructed"])
def test_nonverified_realized_loss_blocks_entry_without_reliable_absolute_fallback(status, safe_config, proposal, context):
    context.update(
        daily_realized_pl_status=status,
        weekly_realized_pl_status=status,
        absolute_loss_control_reliable=False,
    )
    decision = RiskEngine(safe_config).evaluate(proposal, context)
    check = next(item for item in decision.checks if item.name == "realized_loss_information")
    assert not check.passed


def test_verified_realized_loss_is_enforced_without_broker_fallback(safe_config, proposal, context):
    context.update(
        daily_loss=None,
        weekly_loss=None,
        daily_realized_pl=-safe_config["risk"]["stop_if_daily_loss_exceeds"],
        weekly_realized_pl=0.0,
        daily_realized_pl_status="verified",
        weekly_realized_pl_status="verified",
        absolute_loss_control_reliable=False,
    )
    decision = RiskEngine(safe_config).evaluate(proposal, context)
    assert not next(item for item in decision.checks if item.name == "daily_loss").passed
    assert next(item for item in decision.checks if item.name == "daily_loss_known").passed


def test_unavailable_realized_loss_does_not_block_risk_reducing_exit(safe_config, proposal, context):
    exit_proposal = {**proposal, "action": "exit", "side": "sell", "qty": 1, "notional": None}
    context.update(
        daily_loss=None,
        weekly_loss=None,
        daily_realized_pl_status="unavailable",
        weekly_realized_pl_status="unavailable",
        absolute_loss_control_reliable=False,
    )
    decision = RiskEngine(safe_config).evaluate(exit_proposal, context)
    relevant = {item.name: item.passed for item in decision.checks}
    assert relevant["realized_loss_information"]
    assert relevant["daily_loss_known"] and relevant["weekly_loss_known"]


def test_absolute_loss_limit_remains_active_and_percentages_are_display_only(safe_config, proposal, context):
    context.update(
        daily_loss=safe_config["risk"]["stop_if_daily_loss_exceeds"],
        weekly_loss=0,
        daily_realized_pl_status="verified",
        weekly_realized_pl_status="verified",
    )
    decision = RiskEngine(safe_config).evaluate(proposal, context)
    assert not next(item for item in decision.checks if item.name == "daily_loss").passed
    assert not any(item.name in {"daily_realized_loss_pct_limit", "weekly_realized_loss_pct_limit"} for item in decision.checks)
