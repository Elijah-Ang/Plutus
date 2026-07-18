from __future__ import annotations

import json
from datetime import UTC, datetime

import pandas as pd
import pytest

from app.accounting import separate_accounting_components
from app.broker_alpaca import AlpacaBroker
from app.phase4_allocator import AdaptiveAllocator
from app.position_management import PositionManagementEngine
from app.research_validation import CanonicalOutcomeCalculator, CostModel, ExchangeSessions, Opportunity
from app.runtime_guard import RuntimeGuardError, runtime_database_path
from app.storage import Storage
from app.service import TradingService
from app.utils import load_config
from app.utils import format_proposal_message


def _sizing_config() -> dict:
    return {
        "mode": "paper",
        "live_enabled": False,
        "phase3": {"enabled": False, "active": False},
        "phase4": {"enabled": False, "active": False},
        "position_sizing": {
            "enabled": True,
            "mode": "risk_portfolio",
            "stage": "moderate_paper",
            "use_stage_dollar_cap": False,
            "stage_max_initial_notional_usd": {"moderate_paper": 250.0},
            "stage_max_add_notional_usd": {"moderate_paper": 100.0},
            "risk_per_trade_pct": 0.05,
            "max_trade_notional_pct_equity": 1.0,
            "max_position_notional_pct_equity": 2.0,
            "max_total_portfolio_exposure_pct": 10.0,
            "max_cluster_exposure_pct": 10.0,
            "min_cash_reserve_pct": 10.0,
            "max_cash_usage_pct": 10.0,
            "default_paper_notional_usd": 250.0,
            "default_add_notional_usd": 100.0,
            "minimum_executable_notional_usd": 5.0,
            "add_size_multiplier": 0.5,
            "stop_model": {"atr_multiple": 2.0, "max_stop_pct": 8.0, "min_stop_pct": 1.0},
            "score_multiplier": {"65_74": 1.0, "75_84": 1.0, "85_94": 1.0, "95_100": 1.0},
            "volatility_multiplier": {"normal": 1.0, "extreme": 0.0},
        },
    }


def _sizing_bars(rows: int = 20) -> pd.DataFrame:
    return pd.DataFrame({
        "open": [102.0] * rows,
        "high": [104.0] * rows,
        "low": [100.0] * rows,
        "close": [102.0] * rows,
        "volume": [10000.0] * rows,
    })


def _snapshot(**overrides: float) -> dict:
    snapshot = {
        "portfolio_equity": 100000.0,
        "cash": 90000.0,
        "buying_power": 360000.0,
        "total_exposure_dollars": 0.0,
        "single_exposures": {},
        "cluster_exposures": {},
    }
    snapshot.update(overrides)
    return snapshot


def _pullback_config() -> dict:
    return {
        "position_management": {
            "enabled": True,
            "profit_taking": {"enabled": False},
            "profit_protection": {"enabled": False},
            "trailing_stop": {"enabled": False},
            "time_stop": {"enabled": False},
            "healthy_pullback_add": {
                "enabled": True,
                "minimum_unrealized_profit_pct": 0.5,
                "minimum_trade_score": 85,
                "minimum_score_improvement": 5,
                "max_emergency_exit_score": 40,
                "max_profit_giveback_ratio": 0.35,
                "max_pullback_atr_multiple": 1.5,
                "fallback_max_pullback_pct": 3.0,
                "require_price_above_avg_entry": True,
                "require_price_above_ma50": True,
                "require_price_above_ma200": True,
            },
        },
        "position_sizing": {"default_add_notional_usd": 100.0},
    }


@pytest.mark.parametrize(("rows", "missing"), [(20, "MA50"), (100, "MA200")])
def test_healthy_pullback_add_requires_complete_trend_evidence(rows: int, missing: str):
    result = PositionManagementEngine(_pullback_config()).classify(
        symbol="SPY", current_price=110.0, avg_entry_price=100.0, quantity=2.0,
        bars=_sizing_bars(rows), previous_state={"highest_price_since_entry": 111.0},
        trade_score=90.0, score_improvement=10.0, volatility_regime="normal",
    )
    assert result.decision_type == "HOLD"
    assert any(missing in reason for reason in result.blocking_reasons)


def test_malformed_pending_buy_exposure_blocks_new_sizing(tmp_path):
    storage = Storage(tmp_path / "pending.sqlite3")
    storage.initialize()
    storage.execute(
        "INSERT INTO trade_proposals(id,symbol,side,notional,status,payload,strategy_version,created_at) VALUES(?,?,?,?,?,?,?,?)",
        ("malformed", "SPY", "buy", 250.0, "pending", "{}", "rule_based_v2", datetime.now(UTC).isoformat()),
    )
    result = TradingService(_sizing_config(), storage, None, "pending-test")._calculate_dynamic_size(
        "SPY", 90.0, "normal", 100.0, _sizing_bars(), _snapshot()
    )
    assert result["final_notional"] == 0.0
    assert result["pending_exposure_unknown"] is True
    assert "malformed" in result["pending_exposure_unknown_reason"]


def test_all_returned_sizing_caps_are_monotonic_ceilings():
    config = _sizing_config()
    service = TradingService(config, None, None, "caps-test")
    cases = [
        ({"use_stage_dollar_cap": True, "stage": "moderate_paper"}, _snapshot()),
        ({"max_trade_notional_pct_equity": 0.1}, _snapshot()),
        ({}, _snapshot(cash=10100.0)),
        ({}, _snapshot(buying_power=100.0)),
        ({"max_position_notional_pct_equity": 0.1}, _snapshot(single_exposures={"SPY": 0.0})),
        ({"max_cluster_exposure_pct": 0.1}, _snapshot(cluster_exposures={"us_broad_market": 0.0})),
        ({"max_total_portfolio_exposure_pct": 0.1}, _snapshot()),
    ]
    for sizing_updates, snapshot in cases:
        cfg = dict(config)
        cfg["position_sizing"] = {**config["position_sizing"], **sizing_updates}
        result = TradingService(cfg, None, None, "caps-test")._calculate_dynamic_size(
            "SPY", 90.0, "normal", 90.0, _sizing_bars(), snapshot
        )
        assert all(result["final_notional"] <= value + 1e-9 for value in result["sizing_caps"].values())
        assert result["final_notional"] <= result["stop_risk_cap"] + 1e-9
        assert result["final_notional"] <= result["cash_cap"] + 1e-9
        assert result["final_notional"] <= result["buying_power_cap"] + 1e-9


def test_production_testing_mode_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("TRADING_AGENT_RUNTIME", "production-paper")
    monkeypatch.setenv("TRADING_AGENT_TESTING", "1")
    with pytest.raises(RuntimeGuardError, match="TRADING_AGENT_TESTING=1"):
        runtime_database_path({"storage": {"sqlite_path": str(tmp_path / "test.sqlite3")}})


def test_fake_test_proposal_formatter_is_test_only(monkeypatch):
    monkeypatch.delenv("TRADING_AGENT_TESTING", raising=False)
    with pytest.raises(RuntimeError, match="isolated tests"):
        format_proposal_message({"symbol": "TEST", "expires_at": "2026-07-12T00:00:00+00:00"}, {"mode": "paper"}, is_fake_test=True)


def test_paper_identity_uses_public_account_and_endpoint_evidence():
    broker = AlpacaBroker({"mode": "paper", "alpaca": {"paper_trading_endpoint": "https://paper-api.alpaca.markets", "equity_realtime_data_feed": "iex"}}, "key", "secret")
    broker.trading._base_url = ""
    broker.trading._sandbox = None
    identity = broker.paper_account_identity()
    assert identity["verified"] is True
    assert identity["account_id_present"] is True
    assert identity["configured_endpoint_paper"] is True


def test_early_exit_attribution_stops_all_metrics_at_exit_session():
    index = pd.bdate_range("2026-01-05", periods=3, tz="UTC")
    asset = pd.DataFrame({
        "open": [101.0, 96.0, 115.0], "high": [102.0, 103.0, 120.0],
        "low": [100.0, 94.0, 114.0], "close": [101.0, 96.0, 115.0],
    }, index=index)
    benchmark = pd.DataFrame({"close": [501.0, 503.0, 510.0]}, index=index)
    opportunity = Opportunity(
        id="early-exit", symbol="SPY", observed_at=datetime(2026, 1, 2, tzinfo=UTC),
        entry_price=100.0, direction="long", execution_type="actual_fill", strategy_version="rule_based_v2",
        stop_price=95.0, target_price=120.0, benchmark_entry_price=500.0,
    )
    result = CanonicalOutcomeCalculator(
        ExchangeSessions(), CostModel("cost-v1", 4, 2, 2),
    ).calculate(opportunity, asset, benchmark, as_of=datetime(2026, 2, 1, tzinfo=UTC), horizons=(3,))[0]
    assert result.exit_session == "2026-01-06"
    assert result.holding_period_sessions == 2
    assert result.mfe == pytest.approx(0.03)
    assert result.mae == pytest.approx(-0.06)
    assert result.spy_return == pytest.approx(0.006)
    assert result.cost_bps == 8
    assert result.fixed_horizon_gross_return is None
    assert result.trade_path_gross_return == pytest.approx(-0.05)


def test_fixed_horizon_observations_are_non_operational_and_separate():
    index = pd.bdate_range("2026-01-05", periods=2, tz="UTC")
    bars = pd.DataFrame({"high": [102.0, 110.0], "low": [98.0, 90.0], "close": [101.0, 105.0]}, index=index)
    opportunity = Opportunity(
        id="shadow", symbol="SPY", observed_at=datetime(2026, 1, 2, tzinfo=UTC),
        entry_price=100.0, direction="long", execution_type="shadow_hypothetical", strategy_version="shadow_v1",
        stop_price=95.0, target_price=103.0, benchmark_entry_price=500.0, source_table="shadow_insights",
    )
    result = CanonicalOutcomeCalculator(ExchangeSessions(), CostModel("cost-v1", 0, 0, 0)).calculate(
        opportunity, bars, pd.DataFrame({"close": [501.0, 502.0]}, index=index),
        as_of=datetime(2026, 2, 1, tzinfo=UTC), horizons=(2,),
    )[0]
    assert result.outcome_class == "fixed_horizon_observation"
    assert result.trade_path_gross_return is None
    assert result.fixed_horizon_gross_return == pytest.approx(0.05)


def test_phase4_audit_records_truthful_operational_fields(tmp_path):
    storage = Storage(tmp_path / "phase4-audit.sqlite3")
    storage.initialize()
    config = load_config()
    result = AdaptiveAllocator(storage, config, "phase4-audit").run(
        regime="normal", drawdown_pct=0.0,
        as_of="2026-07-14T08:00:00+00:00",
        portfolio_snapshot={
            "portfolio_equity": 100_000.0, "as_of": "2026-07-14T08:00:00+00:00", "equity_as_of": "2026-07-14T08:00:00+00:00",
            "heat_before_pct": 0.20, "gross_exposure_before_pct": 3.0,
            "symbol_exposure_before": {"SPY": 1.0}, "cluster_exposure_before": {"us_broad_market": 1.5},
            "pending_risk": 12.0, "reserved_risk": 4.0,
        },
    )
    row = storage.fetch_all("SELECT * FROM phase4_allocation_decisions WHERE id=?", (result["allocation_id"],))[0]
    assert row["operational_kelly_used"] == 0
    assert row["allocation_class"] == "exploration"
    assert row["heat_before_pct"] == pytest.approx(0.20)
    assert row["heat_after_pct"] == pytest.approx(0.20)
    assert row["gross_exposure_before_pct"] == pytest.approx(3.0)
    assert row["gross_exposure_after_pct"] == pytest.approx(3.0)
    assert row["pending_risk"] == pytest.approx(12.0)
    assert row["reserved_risk"] == pytest.approx(4.0)
    assert json.loads(row["binding_caps_json"])["exploration_heat_pct"] == pytest.approx(0.25)
    assert row["formula_version"]
    assert json.loads(row["evidence_versions_json"])


def test_accounting_separates_realized_loss_from_equity_change():
    components = separate_accounting_components(
        current_equity=1100.0, previous_equity=1000.0,
        current_realized_fifo_pnl=-20.0, previous_realized_fifo_pnl=0.0,
        current_unrealized_pl=30.0, previous_unrealized_pl=10.0,
    )
    assert components.account_equity_change == pytest.approx(100.0)
    assert components.realized_fifo_pnl == pytest.approx(-20.0)
    assert components.unrealized_change == pytest.approx(20.0)
    assert components.external_cash_flow == pytest.approx(100.0)
    assert components.confidence == "verified"
