from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pandas as pd

from app.service import TradingService
from app.storage import Storage
from app.utils import format_digest_message, load_config


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


def test_performance_forward_returns_wait_until_horizon_elapsed(tmp_path):
    service, storage, _broker = _service(tmp_path)
    now = datetime(2026, 1, 2, 15, 0, tzinfo=UTC)
    res = _result("SPY", LabSignal("ENTRY", "buy", "SPY", "trend passed"), now=now, proposal_generated=False, performance_not_proposed_reason="shadow measurement")
    service._run_performance_lab([res], ["SPY"], [], now, {"portfolio_equity": 1000, "total_exposure_pct": 0, "single_exposures": {}, "cluster_exposures": {}})

    service._update_performance_forward_returns(now + timedelta(hours=12))
    assert {r["status"] for r in storage.fetch_all("SELECT status FROM performance_forward_returns")} == {"pending"}

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
        },
        {"mode": "paper"},
    )

    assert "Performance Lab: tracked 42 setups, proposed 1, suppressed 41, outcomes pending." in msg
    assert "approve" not in msg.lower()


def test_performance_lab_safety_config_invariants():
    config = load_config()
    assert config["mode"] == "paper"
    assert config["live_enabled"] is False
    assert config["auto_execution_enabled"] is False
    assert config["auto_execution_mode"] == "manual_only"
    assert config["telegram"]["approval_enabled"] is True
    assert config["risk"]["require_final_revalidation"] is True
