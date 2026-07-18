from __future__ import annotations

from decimal import Decimal

import pytest

from app.accounting import separate_accounting_components
from app.execution import DurableExecutionStore
from app.fixed_point_accounting import (
    EXACT_DECIMAL_PROVENANCE,
    RECONSTRUCTED_REAL_PROVENANCE,
    apply_fixed_point_accounting_schema,
    fixed_point_integrity_report,
)
from app.formula_versions import (
    FIXED_POINT_ACCOUNTING_SCHEMA_VERSION,
    FIXED_POINT_ACCOUNTING_VERSION,
)
from app.lot_ledger import LotLedger
from app.order_state import OrderState
from app.storage import Storage


def _database(tmp_path) -> Storage:
    storage = Storage(tmp_path / "fixed-point.sqlite3")
    storage.initialize()
    return storage


def _intent(
    storage: Storage,
    identifier: str,
    side: str,
    quantity: str,
    price: str,
    *,
    symbol: str = "SPY",
):
    store = DurableExecutionStore(storage)
    row = store.create_or_get_intent(
        {
            "id": identifier,
            "proposal_id": identifier,
            "symbol": symbol,
            "side": side,
            "action": "entry" if side == "buy" else "exit",
            "qty": float(quantity),
            "notional": None,
            "latest_price": float(price),
            "trading_mode": "paper",
        },
        run_id="fixed-point-run",
        source_type="proposal",
    )
    store.transition(row["id"], OrderState.SUBMITTING, event_type="test")
    store.transition(row["id"], OrderState.SUBMITTED, event_type="test")
    return store, row


def test_exact_fractional_fifo_fees_adjustments_and_residuals(tmp_path):
    storage = _database(tmp_path)
    LotLedger(storage).set_coverage(
        effective_from="2026-07-01T00:00:00+00:00",
        confidence="verified",
        provenance="exact prospective boundary",
    )
    store, first = _intent(storage, "buy-a", "buy", "0.1", "0.2")
    store.record_fill(
        first["id"], cumulative_quantity="0.1", fill_price="0.2",
        broker_event_key="buy-a-fill", fees="0.03",
    )
    store, second = _intent(storage, "buy-b", "buy", "0.2", "0.3")
    store.record_fill(
        second["id"], cumulative_quantity="0.2", fill_price="0.3",
        broker_event_key="buy-b-fill", fees="0.02",
    )
    store, sell = _intent(storage, "sell-a", "sell", "0.25", "0.5")
    store.record_fill(
        sell["id"], cumulative_quantity="0.25", fill_price="0.5",
        broker_event_key="sell-a-fill", fees="0.01", adjustments="0.005",
    )

    event = storage.fetch_all("SELECT * FROM realized_pnl_events")[0]
    assert event["quantity_decimal"] == "0.25"
    assert event["gross_proceeds_decimal"] == "0.125"
    assert event["cost_basis_decimal"] == "0.11"
    assert event["fees_decimal"] == "0.01"
    assert event["adjustments_decimal"] == "0.005"
    assert event["realized_pl_decimal"] == "0.01"
    assert event["remaining_position_quantity_decimal"] == "0.05"
    assert event["decimal_provenance"] == EXACT_DECIMAL_PROVENANCE
    assert event["decimal_accounting_version"] == FIXED_POINT_ACCOUNTING_VERSION
    assert not any(fixed_point_integrity_report(storage).values())


def test_high_precision_quantity_and_price_survive_fifo_round_trip(tmp_path):
    storage = _database(tmp_path)
    LotLedger(storage).set_coverage(
        effective_from="2026-07-01T00:00:00+00:00",
        confidence="verified",
        provenance="exact prospective boundary",
    )
    quantity = "0.000000123456789012345678"
    price = "12345.67890123456789"
    with storage.connect() as conn:
        LotLedger.apply_fill_in_transaction(
            conn,
            intent={
                "id": "crypto-buy",
                "proposal_id": None,
                "position_lifecycle_id": None,
                "symbol": "BTC/USD",
                "side": "buy",
                "requested_quantity": quantity,
            },
            broker_event_key="crypto-buy-fill",
            delta_quantity=quantity,
            fill_price=price,
            occurred_at="2026-07-02T00:00:00+00:00",
        )
        LotLedger.apply_fill_in_transaction(
            conn,
            intent={
                "id": "crypto-sell",
                "proposal_id": None,
                "position_lifecycle_id": None,
                "symbol": "BTC/USD",
                "side": "sell",
                "requested_quantity": quantity,
            },
            broker_event_key="crypto-sell-fill",
            delta_quantity=quantity,
            fill_price=price,
            occurred_at="2026-07-03T00:00:00+00:00",
        )
    lot = storage.fetch_all("SELECT * FROM position_lots")[0]
    event = storage.fetch_all("SELECT * FROM realized_pnl_events")[0]
    assert lot["original_quantity_decimal"] == quantity
    assert lot["unit_cost_decimal"] == price
    assert lot["remaining_quantity_decimal"] == "0"
    assert event["quantity_decimal"] == quantity
    assert event["realized_pl_decimal"] == "0"
    assert not any(fixed_point_integrity_report(storage).values())


@pytest.mark.parametrize(
    ("quantity", "price", "fees", "adjustments"),
    [
        ("NaN", "1", "0", "0"),
        ("Infinity", "1", "0", "0"),
        ("-0.1", "1", "0", "0"),
        ("0.1", "0", "0", "0"),
        ("0.1", "1", "-0.01", "0"),
        ("0.1", "1", "0", "NaN"),
    ],
)
def test_invalid_fill_evidence_fails_before_any_lot_write(
    tmp_path, quantity, price, fees, adjustments
):
    storage = _database(tmp_path)
    with pytest.raises(ValueError):
        with storage.connect() as conn:
            LotLedger.apply_fill_in_transaction(
                conn,
                intent={
                    "id": "invalid",
                    "proposal_id": None,
                    "position_lifecycle_id": None,
                    "symbol": "SPY",
                    "side": "buy",
                    "requested_quantity": "1",
                },
                broker_event_key="invalid-fill",
                delta_quantity=quantity,
                fill_price=price,
                fees=fees,
                adjustments=adjustments,
                occurred_at="2026-07-02T00:00:00+00:00",
            )
    assert storage.fetch_all("SELECT COUNT(*) n FROM position_lots")[0]["n"] == 0


@pytest.mark.parametrize(
    ("quantity", "unit_cost"),
    [("NaN", "1"), ("Infinity", "1"), ("-1", "1"), ("0", "1"), ("1", "0"), ("1", "-1")],
)
def test_manual_adjustment_rejects_nonfinite_or_nonpositive_values(
    tmp_path, quantity, unit_cost
):
    storage = _database(tmp_path)
    with pytest.raises(ValueError):
        LotLedger(storage).record_manual_adjustment(
            symbol="SPY",
            quantity=quantity,
            unit_cost=unit_cost,
            occurred_at="2026-07-02T00:00:00+00:00",
            provenance="test",
        )


def test_migration_backfills_real_as_reconstructed_and_is_idempotent(tmp_path):
    storage = _database(tmp_path)
    storage.execute(
        """INSERT INTO position_lots(
           id,symbol,source_fill_event_key,opened_at,original_quantity,
           remaining_quantity,unit_cost,fees_allocated,source,provenance,
           confidence,created_at,updated_at)
           VALUES('legacy','SPY','legacy-fill','2026-01-01T00:00:00+00:00',
                  0.1,0.1,0.2,0.03,'legacy','legacy','reconstructed',
                  '2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')"""
    )
    with storage.connect() as conn:
        apply_fixed_point_accounting_schema(conn)
        first = dict(conn.execute(
            "SELECT * FROM position_lots WHERE id='legacy'"
        ).fetchone())
        apply_fixed_point_accounting_schema(conn)
        second = dict(conn.execute(
            "SELECT * FROM position_lots WHERE id='legacy'"
        ).fetchone())
        migration_count = conn.execute(
            "SELECT COUNT(*) n FROM schema_migrations WHERE version=?",
            (FIXED_POINT_ACCOUNTING_SCHEMA_VERSION,),
        ).fetchone()["n"]
    assert first == second
    assert first["original_quantity_decimal"] == "0.1"
    assert first["unit_cost_decimal"] == "0.2"
    assert first["decimal_provenance"] == RECONSTRUCTED_REAL_PROVENANCE
    assert migration_count == 1


def test_integrity_detects_canonical_tampering_and_real_projection_tampering(tmp_path):
    storage = _database(tmp_path)
    identifier = LotLedger(storage).record_manual_adjustment(
        symbol="SPY", quantity="1.25", unit_cost="10.2",
        occurred_at="2026-07-02T00:00:00+00:00", provenance="test",
    )
    assert not any(fixed_point_integrity_report(storage).values())
    storage.execute(
        "UPDATE position_lots SET remaining_quantity_decimal='2' WHERE id=?",
        (identifier,),
    )
    report = fixed_point_integrity_report(storage)
    assert report["fixed_point_fifo_geometry_mismatch"] == 1
    assert report["fixed_point_legacy_projection_mismatch"] == 1


def test_integrity_detects_exact_realized_formula_tampering(tmp_path):
    storage = _database(tmp_path)
    LotLedger(storage).set_coverage(
        effective_from="2026-07-01T00:00:00+00:00",
        confidence="verified",
        provenance="test",
    )
    store, buy = _intent(storage, "formula-buy", "buy", "1", "10")
    store.record_fill(
        buy["id"], cumulative_quantity="1", fill_price="10",
        broker_event_key="formula-buy-fill",
    )
    store, sell = _intent(storage, "formula-sell", "sell", "1", "12")
    store.record_fill(
        sell["id"], cumulative_quantity="1", fill_price="12",
        broker_event_key="formula-sell-fill",
    )
    storage.execute(
        "UPDATE realized_pnl_events SET realized_pl_decimal='3',realized_pl=3"
    )
    report = fixed_point_integrity_report(storage)
    assert report["fixed_point_realized_formula_mismatch"] == 1
    assert report["fixed_point_consumption_reconciliation_mismatch"] == 1


def test_account_component_separation_is_decimal_exact():
    result = separate_accounting_components(
        current_equity="0.3",
        previous_equity="0.1",
        current_realized_fifo_pnl="0.2",
        previous_realized_fifo_pnl="0.1",
        current_unrealized_pl="0.1",
        previous_unrealized_pl="0.0",
    )
    assert result.account_equity_change == Decimal("0.2")
    assert result.realized_fifo_pnl == Decimal("0.1")
    assert result.unrealized_change == Decimal("0.1")
    assert result.external_cash_flow == Decimal("0")


def test_broker_fill_and_aggregate_rows_store_canonical_decimal_evidence(tmp_path):
    storage = _database(tmp_path)
    store, buy = _intent(storage, "canonical-fill", "buy", "0.3", "0.2")
    store.record_fill(
        buy["id"], cumulative_quantity="0.1", fill_price="0.2",
        broker_event_key="canonical-fill-1",
    )
    store.record_fill(
        buy["id"], cumulative_quantity="0.3", fill_price="0.25",
        broker_event_key="canonical-fill-2",
    )
    intent = store.get_intent(buy["id"])
    events = storage.fetch_all(
        "SELECT * FROM broker_fill_events ORDER BY occurred_at,broker_event_key"
    )
    fill = storage.fetch_all("SELECT * FROM fills WHERE order_id=?", (buy["id"],))[0]
    assert intent["filled_quantity_decimal"] == "0.3"
    assert intent["average_fill_price_decimal"] == "0.2333333333333333333333333333"
    assert [row["delta_quantity_decimal"] for row in events] == ["0.1", "0.2"]
    assert fill["qty_decimal"] == "0.3"
    assert fill["price_decimal"] == "0.2333333333333333333333333333"
    assert not any(fixed_point_integrity_report(storage).values())


def test_zero_cumulative_fill_event_is_rejected_without_evidence_rows(tmp_path):
    storage = _database(tmp_path)
    store, buy = _intent(storage, "zero-fill", "buy", "1", "10")
    with pytest.raises(ValueError, match="cumulative_quantity must be positive"):
        store.record_fill(
            buy["id"], cumulative_quantity="0", fill_price="10",
            broker_event_key="zero-fill-event",
        )
    assert storage.fetch_all("SELECT * FROM broker_fill_events") == []
    assert storage.fetch_all("SELECT * FROM fills") == []
    assert storage.fetch_all("SELECT * FROM position_lots") == []


def test_zero_delta_event_cannot_smuggle_fees_or_adjustments(tmp_path):
    storage = _database(tmp_path)
    store, buy = _intent(storage, "zero-delta", "buy", "2", "10")
    store.record_fill(
        buy["id"], cumulative_quantity="1", fill_price="10",
        broker_event_key="zero-delta-first",
    )
    with pytest.raises(ValueError, match="zero-delta fill events"):
        store.record_fill(
            buy["id"], cumulative_quantity="1", fill_price="10",
            broker_event_key="zero-delta-second", fees="1",
        )
    assert storage.fetch_all(
        "SELECT broker_event_key FROM broker_fill_events"
    ) == [{"broker_event_key": "zero-delta-first"}]
    assert storage.fetch_all("SELECT fees_allocated_decimal FROM position_lots") == [
        {"fees_allocated_decimal": "0"}
    ]
