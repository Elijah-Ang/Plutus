from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from app.execution_risk_snapshot import (
    REQUIRED_FORMULA_VERSIONS,
    capture_execution_risk_snapshot,
    snapshot_body_from_row,
    verify_execution_risk_snapshot,
)
from app.execution import DurableExecutionStore
from app.storage import Storage


class Broker:
    def __init__(self, *, account_id="paper-123", open_market=True, positions=None, orders=None):
        self.account_id = account_id
        self.open_market = open_market
        self.positions = positions or []
        self.orders = orders or []

    def paper_account_identity(self):
        return {"verified": True, "mode": "paper", "account_id": self.account_id, "sdk_sandbox_evidence": True}

    def get_account(self):
        return {"id": self.account_id, "status": "ACTIVE", "equity": 100_000, "cash": 50_000,
                "buying_power": 50_000, "short_market_value": 0}

    def get_positions(self):
        return list(self.positions)

    def get_open_orders(self):
        return list(self.orders)

    def get_clock(self):
        return {"is_open": self.open_market, "timestamp": datetime.now(UTC).isoformat()}

    def get_loss_metrics(self):
        return {"captured_at": datetime.now(UTC).isoformat(), "provenance": "broker_account_history"}


def candidate(**overrides):
    value = {
        "id": "proposal", "proposal_id": "proposal", "symbol": "ABBV", "side": "sell",
        "action": "exit", "qty": 2.0, "notional": 200.0, "latest_price": 100.0,
        "request_basis": "quantity", "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        "trading_mode": "paper", "config_hash": "cfg", "formula_versions": REQUIRED_FORMULA_VERSIONS,
    }
    value.update(overrides)
    return value


def config():
    return {"effective_config_hash": "cfg", "formula_versions": dict(REQUIRED_FORMULA_VERSIONS)}


def capture(storage, broker, value=None, **kwargs):
    return capture_execution_risk_snapshot(
        storage, broker, proposal_id="proposal", approval_id="approval", run_id="run",
        context=kwargs.pop("context", {}), config=config(), candidate=value or candidate(), **kwargs,
    )


def test_caller_open_market_and_order_hints_cannot_override_broker(tmp_path):
    storage = Storage(tmp_path / "risk.sqlite3"); storage.initialize()
    with pytest.raises(RuntimeError, match="open market"):
        capture(storage, Broker(open_market=False), context={"market_open": True})
    row = capture(
        storage,
        Broker(positions=[{"symbol": "ABBV", "qty": 10, "market_value": 1000}],
               orders=[{"id": "sell-1", "symbol": "ABBV", "side": "sell", "qty": 3, "filled_qty": 0}]),
        context={"conflicting_sell_order": False, "sellable_quantity": 999},
    )
    body = snapshot_body_from_row(row)
    assert body["risk_context"]["conflicting_sell_order"] is True
    assert body["risk_context"]["sellable_quantity"] == 7


def test_durable_sell_intent_contributes_its_pending_quantity(tmp_path):
    storage = Storage(tmp_path / "durable-orders.sqlite3"); storage.initialize()
    DurableExecutionStore(storage).create_or_get_intent(
        candidate(
            id="prior-exit", proposal_id="prior-exit", qty=3.0, notional=300.0,
            approved_quantity_ceiling=3.0, approved_notional_ceiling=300.0,
        ),
        run_id="prior-run",
        source_type="proposal",
    )
    row = capture(
        storage,
        Broker(positions=[{"symbol": "ABBV", "qty": 10, "market_value": 1000}]),
    )
    context = snapshot_body_from_row(row)["risk_context"]
    assert context["conflicting_sell_order"] is True
    assert context["open_sell_quantity"] == 3
    assert context["sellable_quantity"] == 7


def test_caller_holdings_kill_switch_and_loss_hints_are_not_authority(tmp_path):
    storage = Storage(tmp_path / "controls.sqlite3"); storage.initialize()
    row = capture(storage, Broker(positions=[{"symbol": "ABBV", "qty": 1}]),
                  context={"current_holdings_quantity": 100, "kill_switch": False, "daily_loss_pct": 0})
    assert snapshot_body_from_row(row)["risk_context"]["sellable_quantity"] == 1
    with pytest.raises(RuntimeError, match="kill switch"):
        capture(storage, Broker(), trusted_providers={"kill_switch": lambda: True})
    stale = lambda: {"captured_at": (datetime.now(UTC) - timedelta(minutes=6)).isoformat()}
    with pytest.raises(RuntimeError, match="stale"):
        capture(storage, Broker(), trusted_providers={"loss_controls": stale})


def test_account_identity_is_stable_nonempty_and_not_fallback(tmp_path, monkeypatch):
    monkeypatch.delenv("TRADING_AGENT_TESTING", raising=False)
    storage = Storage(tmp_path / "account.sqlite3"); storage.initialize()
    for account_id in ("", "paper-account"):
        with pytest.raises(RuntimeError, match="identity"):
            capture(storage, Broker(account_id=account_id))


def test_snapshot_binds_all_ids_freshness_and_fingerprint(tmp_path):
    storage = Storage(tmp_path / "verify.sqlite3"); storage.initialize()
    row = capture(storage, Broker())
    with storage.connect() as conn:
        for values, message in (
            ({"proposal_id": "wrong", "approval_id": "approval", "run_id": "run"}, "proposal"),
            ({"proposal_id": "proposal", "approval_id": "wrong", "run_id": "run"}, "approval"),
            ({"proposal_id": "proposal", "approval_id": "approval", "run_id": "wrong"}, "run"),
        ):
            with pytest.raises(RuntimeError, match=message):
                verify_execution_risk_snapshot(
                    conn, row["id"], config_hash="cfg", formula_versions=REQUIRED_FORMULA_VERSIONS, **values
                )
        with pytest.raises(RuntimeError, match="expired"):
            verify_execution_risk_snapshot(
                conn, row["id"], proposal_id="proposal", approval_id="approval", run_id="run",
                config_hash="cfg", formula_versions=REQUIRED_FORMULA_VERSIONS,
                now=datetime.now(UTC) + timedelta(minutes=1),
            )
    storage.execute("UPDATE execution_risk_snapshots SET cash=cash+1 WHERE id=?", (row["id"],))
    with storage.connect() as conn, pytest.raises(RuntimeError, match="fingerprint"):
        verify_execution_risk_snapshot(
            conn, row["id"], proposal_id="proposal", approval_id="approval", run_id="run",
            config_hash="cfg", formula_versions=REQUIRED_FORMULA_VERSIONS,
        )


def test_missing_config_or_formula_versions_fail_closed(tmp_path, monkeypatch):
    monkeypatch.delenv("TRADING_AGENT_TESTING", raising=False)
    storage = Storage(tmp_path / "config.sqlite3"); storage.initialize()
    with pytest.raises(RuntimeError, match="configuration hash"):
        capture_execution_risk_snapshot(
            storage, Broker(), proposal_id="proposal", approval_id="approval", run_id="run",
            context={}, config={"formula_versions": REQUIRED_FORMULA_VERSIONS}, candidate=candidate(),
        )
    with pytest.raises(RuntimeError, match="formula"):
        capture_execution_risk_snapshot(
            storage, Broker(), proposal_id="proposal", approval_id="approval", run_id="run",
            context={}, config={"effective_config_hash": "cfg"}, candidate=candidate(),
        )
