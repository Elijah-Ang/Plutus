from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from app.service import TradingService
from app.storage import Storage
from app.execution import DurableExecutionStore
from app.formula_versions import PERFORMANCE_LAB_CLASSIFICATION_SCHEMA_VERSION
from app.performance_lab import classify_performance_outcome
from app.utils import format_digest_message, load_config
from app.reports import SHEETS


@dataclass(frozen=True)
class LabSignal:
    action: str
    side: str | None
    symbol: str
    reason: str
    confidence: float = 0.7
    indicators: dict | None = None
    strategy_version: str = "rule_based_v1"


class LabBroker:
    def __init__(self) -> None:
        self.submitted_orders = []

    def get_historical_bars(self, symbol, timeframe, limit):
        start = datetime(2026, 1, 1, tzinfo=UTC)
        rows = []
        for idx in range(limit):
            close = 100 + idx
            rows.append(
                {
                    "timestamp": start + timedelta(days=idx),
                    "open": close - 0.5,
                    "high": close + 1.0,
                    "low": close - 1.0,
                    "close": close,
                    "volume": 10000,
                }
            )
        return pd.DataFrame(rows).set_index("timestamp")

    def submit_order(self, *args, **kwargs):
        self.submitted_orders.append((args, kwargs))
        raise AssertionError("Performance Lab must not submit orders")


def _config() -> dict:
    return {
        "mode": "paper",
        "live_enabled": False,
        "auto_execution_enabled": False,
        "auto_execution_mode": "manual_only",
        "ai": {"ai_review_min_score": 65},
        "risk": {"max_price_age_seconds": 120, "require_final_revalidation": True},
        "telegram": {"approval_enabled": True},
        "dynamic_universe": {"enabled": False, "runtime_orchestration": {"max_forward_outcome_updates_per_cycle": 25}},
    }


def _service(tmp_path):
    storage = Storage(tmp_path / "lab.db")
    storage.initialize()
    broker = LabBroker()
    service = TradingService(_config(), storage, broker, "run-lab")
    return service, storage, broker


def _result(symbol: str, signal: LabSignal, **overrides):
    now = overrides.pop("now", datetime(2026, 1, 2, 15, 0, tzinfo=UTC))
    base = {
        "symbol": symbol,
        "signal": signal,
        "score": 72.0,
        "asset_score": 80.0,
        "price": 100.0,
        "price_at": now,
        "volatility_regime": "normal",
        "vol_20": 0.12,
        "volume": 10000,
        "setup_key": f"{symbol}-entry",
        "proposal_allowed": False,
        "proposal_generated": False,
        "performance_action_decision": "suppressed",
        "performance_not_proposed_reason": "no entry signal",
        "performance_price_age_seconds": 10,
        "performance_decision_reasons": [],
        "final_notional": 5.0,
        "suggested_shares": 0.05,
        "score_vol": 15.0,
        "has_position": False,
        "is_add": False,
    }
    base.update(overrides)
    return base


def test_performance_lab_records_qualified_suppressed_and_placeholders(tmp_path):
    service, storage, broker = _service(tmp_path)
    now = datetime(2026, 1, 2, 15, 0, tzinfo=UTC)
    res = _result("SPY", LabSignal("HOLD", None, "SPY", "no entry signal"), now=now)

    service._run_performance_lab([res], ["SPY"], [], now, {"portfolio_equity": 1000, "total_exposure_pct": 0, "single_exposures": {}, "cluster_exposures": {}})

    setups = storage.fetch_all("SELECT * FROM performance_setups")
    assert len(setups) == 1
    assert setups[0]["action_decision"] == "suppressed"
    assert setups[0]["not_proposed_reason"] == "no entry signal"
    assert storage.fetch_all("SELECT blocker FROM performance_blockers")
    assert len(storage.fetch_all("SELECT * FROM performance_forward_returns")) == 3
    assert storage.fetch_all("SELECT * FROM performance_counterfactuals")
    assert storage.fetch_all("SELECT * FROM trade_proposals") == []
    assert storage.fetch_all("SELECT * FROM orders") == []
    assert broker.submitted_orders == []


def test_performance_lab_records_proposed_setup_linked_to_proposal(tmp_path):
    service, storage, _broker = _service(tmp_path)
    now = datetime(2026, 1, 2, 15, 0, tzinfo=UTC)
    storage.execute(
        "INSERT INTO trade_proposals(id,run_id,symbol,side,notional,status,created_at,expires_at,strategy_version) VALUES(?,?,?,?,?,?,?,?,?)",
        ("prop-1", "run-lab", "QQQ", "buy", 5.0, "pending", now.isoformat(), (now + timedelta(minutes=10)).isoformat(), "rule_based_v1"),
    )
    res = _result(
        "QQQ",
        LabSignal("ENTRY", "buy", "QQQ", "trend passed"),
        now=now,
        proposal_generated=True,
        proposed=True,
        proposal_id="prop-1",
        performance_action_decision="proposed",
        performance_not_proposed_reason=None,
        performance_proposed_notional=5.0,
    )

    service._run_performance_lab([res], ["QQQ"], [], now, {"portfolio_equity": 1000, "total_exposure_pct": 0, "single_exposures": {}, "cluster_exposures": {}})

    row = storage.fetch_all("SELECT proposed, proposal_id, not_proposed_reason FROM performance_setups")[0]
    assert row == {"proposed": 1, "proposal_id": "prop-1", "not_proposed_reason": None}
    outcome = storage.fetch_all("SELECT actual_or_shadow FROM performance_outcomes")[0]
    assert outcome["actual_or_shadow"] == "proposal_unfilled"
    summary = storage.fetch_all("SELECT total_actual_trades FROM performance_lab_summaries")[0]
    assert summary["total_actual_trades"] == 0


def test_performance_lab_links_actual_order_and_fill(tmp_path):
    service, storage, _broker = _service(tmp_path)
    now = datetime(2026, 1, 2, 15, 0, tzinfo=UTC)
    storage.execute(
        "INSERT INTO trade_proposals(id,run_id,symbol,side,notional,status,created_at,expires_at,strategy_version) VALUES(?,?,?,?,?,?,?,?,?)",
        ("prop-1", "run-lab", "QQQ", "buy", 5.0, "submitted", now.isoformat(), (now + timedelta(minutes=10)).isoformat(), "rule_based_v1"),
    )
    res = _result("QQQ", LabSignal("ENTRY", "buy", "QQQ", "trend passed"), now=now, proposal_generated=True, proposal_id="prop-1", performance_action_decision="proposed", performance_proposed_notional=5.0)
    service._run_performance_lab([res], ["QQQ"], [], now, {"portfolio_equity": 1000, "total_exposure_pct": 0, "single_exposures": {}, "cluster_exposures": {}})
    storage.execute(
        "INSERT INTO orders(id,run_id,proposal_id,broker_order_id,client_order_id,symbol,side,notional,qty,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        ("order-1", "run-lab", "prop-1", "broker-1", "client-1", "QQQ", "buy", 5.0, 0.05, "filled", now.isoformat(), now.isoformat()),
    )
    storage.execute("INSERT INTO fills(run_id,order_id,qty,price,filled_at) VALUES(?,?,?,?,?)", ("run-lab", "order-1", 0.05, 101.0, now.isoformat()))

    service._sync_performance_lab_order_links()

    linked = storage.fetch_all("SELECT order_id, broker_order_id, fill_id, fill_price, fill_qty FROM performance_setups")[0]
    assert linked["order_id"] == "order-1"
    assert linked["broker_order_id"] == "broker-1"
    assert linked["fill_id"] is not None
    assert linked["fill_price"] == 101.0
    assert linked["fill_qty"] == 0.05
    outcome = storage.fetch_all(
        "SELECT actual_or_shadow,entry_price,entry_qty,entry_notional FROM performance_outcomes"
    )[0]
    assert outcome["actual_or_shadow"] == "actual_fill"
    assert outcome["entry_price"] == 101.0
    assert outcome["entry_qty"] == 0.05
    assert outcome["entry_notional"] == pytest.approx(5.05)
    summary = storage.fetch_all("SELECT total_actual_trades FROM performance_lab_summaries")[0]
    assert summary["total_actual_trades"] == 1
    assert DurableExecutionStore(storage).integrity_report()["performance_lab_actual_without_fill"] == 0
    service._sync_performance_lab_order_links()
    assert storage.fetch_all("SELECT total_actual_trades FROM performance_lab_summaries")[0]["total_actual_trades"] == 1


@pytest.mark.parametrize(
    ("fill_id", "fill_price", "fill_qty"),
    [
        (None, 101.0, 0.05),
        ("fill-1", None, 0.05),
        ("fill-1", 101.0, None),
        ("fill-1", 101.0, 0.0),
        ("fill-1", float("inf"), 0.05),
    ],
)
def test_partial_or_invalid_fill_evidence_is_never_actual(
    fill_id, fill_price, fill_qty
):
    assert classify_performance_outcome(
        proposal_status="filled",
        order_status="filled",
        authorized_approval=True,
        fill_id=fill_id,
        fill_price=fill_price,
        fill_qty=fill_qty,
    ) == "invalid_fill_evidence"


def test_unrelated_newer_order_cannot_downgrade_durable_fill(tmp_path):
    service, storage, _broker = _service(tmp_path)
    now = datetime(2026, 1, 2, 15, 0, tzinfo=UTC)
    storage.execute(
        "INSERT INTO trade_proposals(id,run_id,symbol,side,notional,status,created_at,expires_at,strategy_version) VALUES(?,?,?,?,?,?,?,?,?)",
        ("prop-1", "run-lab", "QQQ", "buy", 5.0, "submitted", now.isoformat(), (now + timedelta(minutes=10)).isoformat(), "rule_based_v1"),
    )
    res = _result(
        "QQQ", LabSignal("ENTRY", "buy", "QQQ", "trend passed"), now=now,
        proposal_generated=True, proposal_id="prop-1",
        performance_action_decision="proposed", performance_proposed_notional=5.0,
    )
    service._run_performance_lab([res], ["QQQ"], [], now, {"portfolio_equity": 1000, "total_exposure_pct": 0, "single_exposures": {}, "cluster_exposures": {}})
    storage.execute(
        "INSERT INTO orders(id,run_id,proposal_id,broker_order_id,client_order_id,symbol,side,notional,qty,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        ("filled-order", "run-lab", "prop-1", "paper-filled", "client-filled", "QQQ", "buy", 5.0, 0.05, "filled", now.isoformat(), now.isoformat()),
    )
    storage.execute(
        "INSERT INTO fills(run_id,order_id,qty,price,filled_at) VALUES(?,?,?,?,?)",
        ("run-lab", "filled-order", 0.05, 101.0, now.isoformat()),
    )
    storage.execute(
        "INSERT INTO trade_proposals(id,run_id,symbol,side,notional,status,created_at,expires_at,strategy_version) VALUES(?,?,?,?,?,?,?,?,?)",
        ("prop-2", "run-lab", "QQQ", "buy", 5.0, "submitted", now.isoformat(), (now + timedelta(minutes=10)).isoformat(), "rule_based_v1"),
    )
    storage.execute(
        "INSERT INTO orders(id,run_id,proposal_id,broker_order_id,client_order_id,symbol,side,notional,qty,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        ("newer-unfilled-order", "run-lab", "prop-2", "paper-newer", "client-newer", "QQQ", "buy", 5.0, 0.05, "submitted", now.isoformat(), (now + timedelta(minutes=1)).isoformat()),
    )

    service._sync_performance_lab_order_links()
    service._sync_performance_lab_order_links()

    outcome = storage.fetch_all(
        "SELECT actual_or_shadow,order_id,entry_price,entry_qty FROM performance_outcomes"
    )[0]
    assert outcome == {
        "actual_or_shadow": "actual_fill",
        "order_id": "filled-order",
        "entry_price": 101.0,
        "entry_qty": 0.05,
    }
    assert storage.fetch_all("SELECT total_actual_trades FROM performance_lab_summaries")[0]["total_actual_trades"] == 1


@pytest.mark.parametrize(
    ("proposal_status", "approval", "expected"),
    [
        ("pending", False, "proposal_unfilled"),
        ("approved", True, "approved_unfilled"),
        ("blocked", False, "blocked_unfilled"),
        ("blocked", True, "approved_blocked"),
        ("rejected", False, "rejected_unfilled"),
        ("expired", False, "expired_unfilled"),
        ("superseded", False, "superseded_unfilled"),
        ("filled", True, "filled_missing_fill_evidence"),
        ("unknown", True, "ambiguous_submission"),
    ],
)
def test_performance_lab_unfilled_proposal_lifecycle_is_not_actual(
    tmp_path, proposal_status, approval, expected
):
    service, storage, _broker = _service(tmp_path)
    now = datetime(2026, 1, 2, 15, 0, tzinfo=UTC)
    storage.execute(
        "INSERT INTO trade_proposals(id,run_id,symbol,side,notional,status,created_at,expires_at,strategy_version) VALUES(?,?,?,?,?,?,?,?,?)",
        ("prop-1", "run-lab", "QQQ", "buy", 5.0, proposal_status, now.isoformat(), (now + timedelta(minutes=10)).isoformat(), "rule_based_v1"),
    )
    if approval:
        storage.execute(
            "INSERT INTO approvals(id,run_id,proposal_id,authorized,status,created_at,consumed_at) VALUES(?,?,?,?,?,?,?)",
            ("approval-1", "run-lab", "prop-1", 1, "consumed", now.isoformat(), now.isoformat()),
        )
    res = _result(
        "QQQ", LabSignal("ENTRY", "buy", "QQQ", "trend passed"), now=now,
        proposal_generated=True, proposal_id="prop-1",
        performance_action_decision="proposed", performance_proposed_notional=5.0,
    )
    service._run_performance_lab([res], ["QQQ"], [], now, {"portfolio_equity": 1000, "total_exposure_pct": 0, "single_exposures": {}, "cluster_exposures": {}})
    service._sync_performance_lab_order_links()
    row = storage.fetch_all("SELECT actual_or_shadow FROM performance_outcomes")[0]
    assert row["actual_or_shadow"] == expected
    assert storage.fetch_all("SELECT total_actual_trades FROM performance_lab_summaries")[0]["total_actual_trades"] == 0


def test_migration_reclassifies_legacy_proposal_as_nonactual_idempotently(tmp_path):
    service, storage, _broker = _service(tmp_path)
    now = datetime(2026, 1, 2, 15, 0, tzinfo=UTC)
    storage.execute(
        "INSERT INTO trade_proposals(id,run_id,symbol,side,notional,status,created_at,expires_at,strategy_version) VALUES(?,?,?,?,?,?,?,?,?)",
        ("prop-1", "run-lab", "QQQ", "buy", 5.0, "expired", now.isoformat(), now.isoformat(), "rule_based_v1"),
    )
    res = _result(
        "QQQ", LabSignal("ENTRY", "buy", "QQQ", "trend passed"), now=now,
        proposal_generated=True, proposal_id="prop-1",
        performance_action_decision="proposed", performance_proposed_notional=5.0,
    )
    service._run_performance_lab([res], ["QQQ"], [], now, {"portfolio_equity": 1000, "total_exposure_pct": 0, "single_exposures": {}, "cluster_exposures": {}})
    storage.execute("UPDATE performance_outcomes SET actual_or_shadow='actual'")
    storage.execute("UPDATE performance_lab_summaries SET total_actual_trades=1")
    assert DurableExecutionStore(storage).integrity_report()["performance_lab_actual_without_fill"] == 1
    storage.apply_explicit_migrations()
    first = storage.fetch_all("SELECT actual_or_shadow,updated_at FROM performance_outcomes")[0]
    assert first["actual_or_shadow"] == "expired_unfilled"
    assert storage.fetch_all("SELECT total_actual_trades FROM performance_lab_summaries")[0]["total_actual_trades"] == 0
    assert PERFORMANCE_LAB_CLASSIFICATION_SCHEMA_VERSION in storage.schema_versions()
    storage.apply_explicit_migrations()
    assert storage.fetch_all("SELECT actual_or_shadow,updated_at FROM performance_outcomes")[0] == first
    assert all(value == 0 for value in DurableExecutionStore(storage).integrity_report().values())


def test_migration_promotes_only_matching_durable_fill_and_repairs_links(tmp_path):
    service, storage, _broker = _service(tmp_path)
    now = datetime(2026, 1, 2, 15, 0, tzinfo=UTC)
    storage.execute(
        "INSERT INTO trade_proposals(id,run_id,symbol,side,notional,status,created_at,expires_at,strategy_version) VALUES(?,?,?,?,?,?,?,?,?)",
        ("prop-1", "run-lab", "QQQ", "buy", 5.05, "filled", now.isoformat(), now.isoformat(), "rule_based_v1"),
    )
    res = _result(
        "QQQ", LabSignal("ENTRY", "buy", "QQQ", "trend passed"), now=now,
        proposal_generated=True, proposal_id="prop-1",
        performance_action_decision="proposed", performance_proposed_notional=5.05,
    )
    service._run_performance_lab([res], ["QQQ"], [], now, {"portfolio_equity": 1000, "total_exposure_pct": 0, "single_exposures": {}, "cluster_exposures": {}})
    storage.execute(
        "INSERT INTO orders(id,run_id,proposal_id,broker_order_id,client_order_id,symbol,side,notional,qty,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        ("order-1", "run-lab", "prop-1", "paper-1", "client-1", "QQQ", "buy", 5.05, 0.05, "filled", now.isoformat(), now.isoformat()),
    )
    storage.execute(
        "INSERT INTO fills(run_id,order_id,qty,price,filled_at) VALUES(?,?,?,?,?)",
        ("run-lab", "order-1", 0.05, 101.0, now.isoformat()),
    )
    storage.execute(
        "UPDATE performance_outcomes SET entry_price=NULL,entry_qty=NULL,entry_notional=NULL"
    )
    report = DurableExecutionStore(storage).integrity_report()
    assert report["performance_lab_fill_not_actual"] == 1

    storage.apply_explicit_migrations()

    first = storage.fetch_all(
        "SELECT actual_or_shadow,order_id,broker_order_id,fill_id,entry_price,entry_qty,entry_notional,updated_at FROM performance_outcomes"
    )[0]
    assert first["actual_or_shadow"] == "actual_fill"
    assert first["order_id"] == "order-1"
    assert first["broker_order_id"] == "paper-1"
    assert first["fill_id"] is not None
    assert first["entry_price"] == 101.0
    assert first["entry_qty"] == 0.05
    assert first["entry_notional"] == pytest.approx(5.05)
    setup = storage.fetch_all(
        "SELECT order_id,broker_order_id,fill_id,fill_price,fill_qty FROM performance_setups"
    )[0]
    assert setup["order_id"] == "order-1"
    assert setup["broker_order_id"] == "paper-1"
    assert setup["fill_id"] == first["fill_id"]
    assert setup["fill_price"] == 101.0
    assert setup["fill_qty"] == 0.05
    assert storage.fetch_all("SELECT total_actual_trades FROM performance_lab_summaries")[0]["total_actual_trades"] == 1
    assert all(value == 0 for value in DurableExecutionStore(storage).integrity_report().values())
    storage.apply_explicit_migrations()
    assert storage.fetch_all(
        "SELECT actual_or_shadow,order_id,broker_order_id,fill_id,entry_price,entry_qty,entry_notional,updated_at FROM performance_outcomes"
    )[0] == first


def test_migration_preserves_blocked_shadow_candidate_ids_but_flags_claimed_proposal(
    tmp_path,
):
    _service_instance, storage, _broker = _service(tmp_path)
    now = datetime(2026, 1, 2, 15, 0, tzinfo=UTC).isoformat()
    for setup_id, proposed in (("shadow-candidate", 0), ("orphaned-proposal", 1)):
        storage.execute(
            """INSERT INTO performance_setups(
                 id,timestamp,run_id,symbol,asset_class,tier,setup_type,
                 action_decision,proposed,proposal_id,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                setup_id, now, "run-lab", "QQQ", "etf", "paper_tradable",
                "new_entry", "blocked", proposed, f"candidate-{setup_id}", now, now,
            ),
        )
        storage.execute(
            """INSERT INTO performance_outcomes(
                 id,setup_id,run_id,symbol,proposal_id,actual_or_shadow,status,
                 created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                f"outcome-{setup_id}", setup_id, "run-lab", "QQQ",
                f"candidate-{setup_id}", "shadow", "pending_forward_returns",
                now, now,
            ),
        )

    storage.apply_explicit_migrations()

    assert storage.fetch_all(
        "SELECT actual_or_shadow FROM performance_outcomes WHERE setup_id='shadow-candidate'"
    )[0]["actual_or_shadow"] == "shadow"
    report = DurableExecutionStore(storage).integrity_report()
    assert report["performance_lab_orphaned_proposal_link"] == 1


@pytest.mark.parametrize(
    ("order_status", "expected"),
    [
        ("submitted", "submitted_unfilled"),
        ("reserved", "intent_unsubmitted"),
        ("cancelled", "submitted_cancelled_unfilled"),
        ("filled", "filled_missing_fill_evidence"),
        ("submitting", "ambiguous_submission"),
        ("unknown", "ambiguous_submission"),
        ("reconciliation_required", "ambiguous_submission"),
    ],
)
def test_performance_lab_submitted_without_fill_remains_nonactual(
    tmp_path, order_status, expected
):
    service, storage, _broker = _service(tmp_path)
    now = datetime(2026, 1, 2, 15, 0, tzinfo=UTC)
    storage.execute(
        "INSERT INTO trade_proposals(id,run_id,symbol,side,notional,status,created_at,expires_at,strategy_version) VALUES(?,?,?,?,?,?,?,?,?)",
        ("prop-1", "run-lab", "QQQ", "buy", 5.0, "submitted", now.isoformat(), (now + timedelta(minutes=10)).isoformat(), "rule_based_v1"),
    )
    res = _result(
        "QQQ", LabSignal("ENTRY", "buy", "QQQ", "trend passed"), now=now,
        proposal_generated=True, proposal_id="prop-1",
        performance_action_decision="proposed", performance_proposed_notional=5.0,
    )
    service._run_performance_lab([res], ["QQQ"], [], now, {"portfolio_equity": 1000, "total_exposure_pct": 0, "single_exposures": {}, "cluster_exposures": {}})
    storage.execute(
        "INSERT INTO orders(id,run_id,proposal_id,broker_order_id,client_order_id,symbol,side,notional,qty,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        ("order-1", "run-lab", "prop-1", "broker-1", "client-1", "QQQ", "buy", 5.0, 0.05, order_status, now.isoformat(), now.isoformat()),
    )
    service._sync_performance_lab_order_links()
    assert storage.fetch_all("SELECT actual_or_shadow FROM performance_outcomes")[0]["actual_or_shadow"] == expected
    assert storage.fetch_all("SELECT total_actual_trades FROM performance_lab_summaries")[0]["total_actual_trades"] == 0


def test_performance_forward_returns_wait_until_horizon_elapsed(tmp_path):
    service, storage, _broker = _service(tmp_path)
    now = datetime(2026, 1, 2, 15, 0, tzinfo=UTC)
    res = _result("SPY", LabSignal("ENTRY", "buy", "SPY", "trend passed"), now=now, proposal_generated=False, performance_not_proposed_reason="shadow measurement")
    service._run_performance_lab([res], ["SPY"], [], now, {"portfolio_equity": 1000, "total_exposure_pct": 0, "single_exposures": {}, "cluster_exposures": {}})

    service._update_performance_forward_returns(now + timedelta(hours=12))
    assert {r["status"] for r in storage.fetch_all("SELECT status FROM performance_forward_returns")} == {"maturing"}

    service._update_performance_forward_returns(datetime(2026, 1, 10, tzinfo=UTC))
    rows = storage.fetch_all("SELECT horizon_days, status, forward_return, max_favorable_excursion, max_adverse_excursion FROM performance_forward_returns")
    complete = {r["horizon_days"]: r for r in rows if r["status"] == "complete"}
    assert 1 in complete and 5 in complete
    assert complete[1]["forward_return"] is not None
    assert complete[5]["max_favorable_excursion"] >= complete[5]["max_adverse_excursion"]


def test_performance_lab_digest_line_is_compact():
    msg = format_digest_message(
        {
            "market_open_status": "Open",
            "window_start": datetime(2026, 1, 2, 15, 0, tzinfo=UTC),
            "window_end": datetime(2026, 1, 2, 15, 30, tzinfo=UTC),
            "symbols_list": [],
            "weakest_symbol": "SPY",
            "weakest_score": 0,
            "weakest_classification": "No action",
            "actions": {"proposals": 0, "orders": 0, "fills": 0, "gpt_calls": 0, "expired": 0},
            "summary": "No proposals.",
            "performance_lab": {"tracked": 42, "proposed": 1, "suppressed": 41, "outcome_status": "outcomes pending"},
            "exit_watch": "Exit watch: no exit triggers.",
            "proposal_capacity": "Setup tracking: 18 suppressed or observation-only. Top blocker: no_entry_signal. Proposal count is uncapped.",
        },
        {"mode": "paper"},
    )

    assert "Performance Lab: tracked 42 setups, proposed 1, suppressed 41, outcomes pending." in msg
    assert "Exit watch: no exit triggers." in msg
    assert "Setup tracking: 18 suppressed or observation-only. Top blocker: no_entry_signal. Proposal count is uncapped." in msg
    assert "Proposal capacity" not in msg
    assert "approve" not in msg.lower()


def test_proposal_frequency_report_sheets_exist():
    sheet_names = {name for name, _query in SHEETS}

    assert "Proposal Activity Status" in sheet_names
    assert "Proposal Bottleneck Summary" in sheet_names
    assert "Suppressed Setup Blockers" in sheet_names
    assert "Risk or Workflow Blockers" in sheet_names
    assert "Proposal Frequency Audit" in sheet_names


def test_performance_lab_capacity_blockers_are_classified(tmp_path):
    service, _storage, _broker = _service(tmp_path)
    signal = LabSignal("ENTRY", "buy", "SPY", "trend passed")
    res = _result("SPY", signal, score=80.0, cooldown_applied=1, cooldown_reason="pending_proposal_exists")

    blockers = service._performance_lab_blockers(
        res,
        signal,
        "suppressed due to pending proposal limit and cooldown",
        {"SPY"},
        "fresh",
    )
    blocker_names = {name for name, _reason in blockers}

    assert "pending_proposal_limit" not in blocker_names
    assert "cooldown" in blocker_names


def test_digest_setup_tracking_never_invents_proposal_capacity(tmp_path):
    service, _storage, _broker = _service(tmp_path)
    line = service._proposal_capacity_digest_line(
        "2026-01-02T15:00:00+00:00",
        "2026-01-02T15:30:00+00:00",
        {"suppressed": 18},
    )
    assert line == "Setup tracking: 18 suppressed or observation-only. Top blocker: none. Proposal count is uncapped."
    assert "capacity" not in line.lower()


def test_performance_lab_safety_config_invariants():
    config = load_config()
    assert config["mode"] == "paper"
    assert config["live_enabled"] is False
    assert config["auto_execution_enabled"] is False
    assert config["auto_execution_mode"] == "manual_only"
    assert config["telegram"]["approval_enabled"] is True
    assert config["risk"]["require_final_revalidation"] is True
