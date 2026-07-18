from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pandas as pd

from app.crypto_research import (
    CryptoResearchEngine,
    configured_crypto_symbols,
    crypto_quiet_hours_active,
    format_crypto_digest,
    normalize_crypto_symbol,
)
from app.reports import SHEETS
from app.risk_engine import RiskEngine
from app.service import TradingService
from app.storage import Storage
from app.formula_versions import (
    CRYPTO_CAPABILITY_FORMULA_VERSION,
    CRYPTO_MARKET_DATA_FORMULA_VERSION,
    CRYPTO_RISK_FORMULA_VERSION,
    CRYPTO_RISK_SCHEMA_VERSION,
    CRYPTO_SIZING_FORMULA_VERSION,
    CRYPTO_SIZING_SCHEMA_VERSION,
)


class CryptoBroker:
    def __init__(self, *, stale: bool = False) -> None:
        self.stale = stale
        self.submitted_orders = []

    def get_crypto_historical_bars(self, symbol: str, timeframe: str = "1Hour", limit: int = 500):
        end = datetime(2026, 7, 3, 10, 0, tzinfo=UTC)
        if self.stale:
            end -= timedelta(hours=2)
        start = end - timedelta(hours=limit - 1)
        rows = []
        base = 100.0 if symbol == "BTC/USD" else 50.0
        for idx in range(limit):
            close = base + idx * 0.2
            ts = start + timedelta(hours=idx)
            rows.append(
                {
                    "symbol": symbol,
                    "timestamp": ts,
                    "open": close - 0.5,
                    "high": close + 1.0,
                    "low": close - 1.0,
                    "close": close,
                    "volume": 1000 + idx,
                }
            )
        return pd.DataFrame(rows).set_index(["symbol", "timestamp"])

    def get_crypto_latest_quote(self, symbol: str):
        return SimpleNamespace(
            bid_price=100.0, ask_price=100.1, bid_size=20.0, ask_size=20.0,
            timestamp=datetime(2026, 7, 3, 10, 0, tzinfo=UTC),
        )

    def get_crypto_latest_trade(self, symbol: str):
        return SimpleNamespace(
            price=100.05, size=0.5, timestamp=datetime(2026, 7, 3, 10, 0, tzinfo=UTC)
        )

    def get_crypto_latest_orderbook(self, symbol: str):
        return SimpleNamespace(
            bids=[SimpleNamespace(price=100.0, size=20.0)],
            asks=[SimpleNamespace(price=100.1, size=20.0)],
            timestamp=datetime(2026, 7, 3, 10, 0, tzinfo=UTC),
        )

    def paper_account_identity(self):
        return {
            "verified": True,
            "mode": "paper",
            "endpoint_class": "paper",
            "account_status": "active",
            "account_currency": "USD",
            "account_id_hash": "a" * 64,
        }

    def get_crypto_assets(self):
        return [
            SimpleNamespace(
                id=f"asset-{symbol}", asset_class="crypto", exchange="CRYPTO", symbol=symbol,
                status="active", tradable=True, marginable=False, shortable=False,
                easy_to_borrow=False, fractionable=True,
                min_order_size="0.0001" if symbol == "BTC/USD" else "0.001",
                min_trade_increment="0.0001" if symbol == "BTC/USD" else "0.001",
                price_increment="1" if symbol == "BTC/USD" else "0.1",
            )
            for symbol in ("BTC/USD", "ETH/USD")
        ]

    def is_market_open(self):
        return False

    def submit_order(self, *args, **kwargs):
        self.submitted_orders.append((args, kwargs))
        raise AssertionError("crypto research must not submit orders")

    def get_positions(self):
        raise AssertionError("crypto research must not fetch equity positions")

    def get_open_orders(self):
        raise AssertionError("crypto research must not fetch equity orders")

    def get_account(self):
        raise AssertionError("crypto research must not fetch equity account")


class FailingCryptoBroker(CryptoBroker):
    def get_crypto_historical_bars(self, symbol: str, timeframe: str = "1Hour", limit: int = 500):
        raise RuntimeError("provider unavailable")


class MissingSpreadCryptoBroker(CryptoBroker):
    def get_crypto_latest_quote(self, symbol: str):
        return None


class UntradableCryptoBroker(CryptoBroker):
    def get_crypto_assets(self):
        assets = super().get_crypto_assets()
        assets[0].tradable = False
        return assets


class TelegramSink:
    def __init__(self) -> None:
        self.messages = []

    def send_message(self, text: str):
        self.messages.append(text)


def _config(**overrides):
    config = {
        "mode": "paper",
        "live_enabled": False,
        "auto_execution_enabled": False,
        "auto_execution_mode": "manual_only",
        "effective_config_hash": "a" * 64,
        "formula_versions": {
            "crypto_capability": CRYPTO_CAPABILITY_FORMULA_VERSION,
            "crypto_market_data": CRYPTO_MARKET_DATA_FORMULA_VERSION,
            "crypto_sizing": CRYPTO_SIZING_FORMULA_VERSION,
            "crypto_risk": CRYPTO_RISK_FORMULA_VERSION,
        },
        "watchlist": ["SPY", "QQQ", "DIA", "IWM"],
        "risk_budget": {"max_total_portfolio_exposure_pct": 6.0},
        "crypto": {
            "enabled": True,
            "mode": "research_only",
            "paper_trading_enabled": False,
            "proposals_enabled": False,
            "live_enabled": False,
            "symbols": ["BTC/USD", "ETH/USD"],
            "optional_symbols": ["SOL/USD"],
            "max_symbols": 2,
            "broker": "alpaca_paper_spot",
            "market_profile": "continuous_24_7",
            "data_feed": "us",
            "quote_currency": "USD",
            "capability_contract": {
                "order_types": ["market", "limit", "stop_limit"],
                "time_in_force": ["gtc", "ioc"],
                "request_bases": ["quantity", "notional"],
                "default_time_in_force": "gtc",
                "require_asset_api_verification": True,
                "require_paper_account_identity": True,
                "snapshot_ttl_minutes": 60,
                "maintenance_policy": "fail_closed",
                "weekend_policy": "continuous_same_controls",
                "stablecoin_policy": "reject_stablecoin_base_and_non_usd_quote",
            },
            "allow_margin": False,
            "allow_shorting": False,
            "require_fresh_price": True,
            "max_price_age_seconds": 300,
            "min_score_for_paper_watch": 70,
            "min_score_for_proposal": 80,
            "min_risk_reward_ratio": 1.5,
            "max_spread_bps": 50,
            "minimum_top_of_book_notional_usd": 1000.0,
            "sizing_policy": {
                "mode": "research_only",
                "formula_version": CRYPTO_SIZING_FORMULA_VERSION,
                "schema_version": CRYPTO_SIZING_SCHEMA_VERSION,
                "minimum_buy_notional_usd": 1.0,
                "maximum_order_notional_usd": 5.0,
                "maximum_quantity_decimal_places": 9,
                "maximum_notional_decimal_places": 2,
                "conservative_taker_fee_bps_per_side": 25.0,
                "stop_execution_slippage_bps": 50.0,
                "minimum_stop_distance_pct": 1.0,
                "maximum_stop_distance_pct": 8.0,
                "require_quantity_basis_for_sells": True,
                "allow_full_position_dust_exit": True,
            },
            "risk_policy": {
                "mode": "research_only",
                "formula_version": CRYPTO_RISK_FORMULA_VERSION,
                "schema_version": CRYPTO_RISK_SCHEMA_VERSION,
                "maximum_gross_exposure_pct_equity": 1.0,
                "volatility_halt_annualized": 1.5,
            },
            "proposal_expiry_minutes": 3,
            "approval_max_price_age_seconds": 30,
            "approval_max_price_move_bps_base": 50,
            "approval_max_price_move_bps_hard_cap": 100,
            "default_order_type": "limit",
            "limit_price_source": "midpoint_or_last_with_slippage_cap",
            "fallback_market_orders": False,
            "allow_new_entries": True,
            "allow_add_to_winner": False,
            "allow_exits": True,
            "eodhd_research_enabled": True,
            "data_source": "alpaca",
            "runtime_evidence_gate": {"enabled": True, "min_natural_cycles": 3, "max_cycle_age_hours": 72},
            "schedule": {
                "enabled": True,
                "research_interval_minutes": 60,
                "digest_interval_minutes": 240,
                "quiet_hours_sgt": {"enabled": True, "start": "01:00", "end": "08:00"},
            },
        },
    }
    config.update(overrides)
    return config


def _storage(tmp_path):
    storage = Storage(tmp_path / "crypto.db")
    storage.initialize()
    return storage


def test_btc_and_eth_enter_crypto_research_lane_sol_optional_by_default(tmp_path):
    storage = _storage(tmp_path)
    broker = CryptoBroker()
    results = CryptoResearchEngine(_config(), storage, broker, TelegramSink(), "run-crypto").run_research(
        now=datetime(2026, 7, 3, 10, 0, tzinfo=UTC)
    )

    assert configured_crypto_symbols(_config()) == ["BTC/USD", "ETH/USD"]
    assert {result.symbol for result in results} == {"BTC/USD", "ETH/USD"}
    assert "SOL/USD" not in {result.symbol for result in results}
    assert all(result.lane in {"crypto_research_candidate", "crypto_observation"} for result in results)
    assert all(result.capability_authoritative for result in results)
    assert len({result.capability_snapshot_id for result in results}) == 1
    assert all(result.market_evidence_authoritative for result in results)
    assert all(result.market_execution_eligible for result in results)
    assert len({result.market_evidence_id for result in results}) == 2
    run = storage.fetch_all("SELECT * FROM crypto_research_runs")[0]
    assert int(run["capability_authoritative"]) == 1
    assert run["capability_snapshot_id"] == results[0].capability_snapshot_id
    snapshots = storage.fetch_all("SELECT * FROM crypto_research_snapshots ORDER BY symbol")
    assert all(row["market_evidence_id"] for row in snapshots)
    assert all(int(row["market_evidence_authoritative"]) == 1 for row in snapshots)


def test_crypto_symbols_are_normalized_consistently():
    assert normalize_crypto_symbol("BTC/USD") == "BTC/USD"
    assert normalize_crypto_symbol("BTCUSD") == "BTC/USD"
    assert normalize_crypto_symbol("BTC-USD") == "BTC/USD"
    assert normalize_crypto_symbol("ETHUSD") == "ETH/USD"
    assert normalize_crypto_symbol("DOGE/USD") is None


def test_crypto_research_does_not_require_us_market_open_and_creates_no_proposals_or_orders(tmp_path):
    storage = _storage(tmp_path)
    broker = CryptoBroker()
    service = TradingService(_config(), storage, broker, "run-service")

    service._run_crypto_research_due()

    assert len(storage.fetch_all("SELECT * FROM crypto_research_snapshots")) == 2
    assert storage.fetch_all("SELECT * FROM trade_proposals") == []
    assert storage.fetch_all("SELECT * FROM orders") == []
    assert broker.submitted_orders == []


def test_crypto_quiet_hours_suppress_telegram_but_record_data(tmp_path):
    storage = _storage(tmp_path)
    telegram = TelegramSink()
    now = datetime(2026, 7, 2, 18, 30, tzinfo=UTC)  # 02:30 SGT

    results = CryptoResearchEngine(_config(), storage, CryptoBroker(), telegram, "run-quiet").run_due(now)

    assert crypto_quiet_hours_active(_config(), now)
    assert results
    assert telegram.messages == []
    assert len(storage.fetch_all("SELECT * FROM crypto_research_snapshots")) == 2
    assert len(storage.fetch_all("SELECT * FROM performance_setups WHERE asset_class='crypto'")) == 2


def test_crypto_digest_line_is_compact_and_research_only(tmp_path):
    storage = _storage(tmp_path)
    results = CryptoResearchEngine(_config(), storage, CryptoBroker(), TelegramSink(), "run-digest").run_research(
        now=datetime(2026, 7, 3, 10, 0, tzinfo=UTC)
    )

    line = format_crypto_digest(results)

    assert line.startswith("Crypto research: BTC/USD")
    assert "Research-only. No proposals/orders." in line


def test_crypto_uses_separate_risk_limits_and_does_not_affect_equity_watchlist(tmp_path):
    config = _config()
    storage = _storage(tmp_path)
    broker = CryptoBroker()
    service = TradingService(config, storage, broker, "run-isolated")

    service._run_crypto_research_due()

    assert config["crypto"]["allow_margin"] is False
    assert config["crypto"]["allow_shorting"] is False
    assert config["crypto"]["risk_policy"]["maximum_gross_exposure_pct_equity"] == 1.0
    assert "BTC/USD" not in config["watchlist"]
    assert "ETH/USD" not in config["watchlist"]
    assert storage.fetch_all("SELECT * FROM trade_proposals") == []
    assert storage.fetch_all("SELECT * FROM position_sizing_decisions") == []
    assert storage.fetch_all("SELECT * FROM portfolio_exposure_snapshots") == []


def test_stale_crypto_data_blocks_future_proposal_path_and_records_shadow_only(tmp_path):
    storage = _storage(tmp_path)
    results = CryptoResearchEngine(_config(), storage, CryptoBroker(stale=True), TelegramSink(), "run-stale").run_research(
        now=datetime(2026, 7, 3, 10, 0, tzinfo=UTC)
    )

    assert {result.data_freshness for result in results} == {"stale"}
    assert all(result.reason == "stale_crypto_data_no_proposals" for result in results)
    assert storage.fetch_all("SELECT * FROM trade_proposals") == []
    perf = storage.fetch_all("SELECT asset_class,action_decision,proposed FROM performance_setups")
    assert perf
    assert {row["asset_class"] for row in perf} == {"crypto"}
    assert {row["action_decision"] for row in perf} == {"research_only"}
    assert {row["proposed"] for row in perf} == {0}


def test_crypto_provider_failure_records_data_unavailable_blocker_and_no_orders(tmp_path):
    storage = _storage(tmp_path)
    broker = FailingCryptoBroker()

    results = CryptoResearchEngine(_config(), storage, broker, TelegramSink(), "run-provider-fail").run_research(
        now=datetime(2026, 7, 3, 10, 0, tzinfo=UTC)
    )

    assert {result.symbol for result in results} == {"BTC/USD", "ETH/USD"}
    assert {result.data_freshness for result in results} == {"missing"}
    assert all(result.reason.startswith("provider_unavailable") for result in results)
    assert len(storage.fetch_all("SELECT * FROM crypto_research_snapshots WHERE data_freshness='missing'")) == 2
    blockers = storage.fetch_all("SELECT symbol,blocker,reason FROM performance_blockers WHERE blocker='crypto_provider_unavailable'")
    assert {row["symbol"] for row in blockers} == {"BTC/USD", "ETH/USD"}
    assert storage.fetch_all("SELECT * FROM trade_proposals") == []
    assert storage.fetch_all("SELECT * FROM orders") == []
    assert broker.submitted_orders == []


def test_unverified_current_pair_capability_is_linked_and_blocks_future_stages(tmp_path):
    storage = _storage(tmp_path)
    results = CryptoResearchEngine(
        _config(), storage, UntradableCryptoBroker(), TelegramSink(), "run-untradable"
    ).run_research(now=datetime(2026, 7, 3, 10, 0, tzinfo=UTC))

    assert all(not result.capability_authoritative for result in results)
    rows = storage.fetch_all(
        "SELECT DISTINCT blocker FROM performance_blockers WHERE blocker='crypto_capability_unverified'"
    )
    assert len(rows) == 1
    assert storage.fetch_all("SELECT * FROM trade_proposals") == []
    assert storage.fetch_all("SELECT * FROM orders") == []


def test_crypto_stage_1_creates_only_research_rows(tmp_path):
    storage = _storage(tmp_path)

    CryptoResearchEngine(_config(), storage, CryptoBroker(), TelegramSink(), "run-stage-1").run_research(
        now=datetime(2026, 7, 3, 10, 0, tzinfo=UTC)
    )

    assert len(storage.fetch_all("SELECT * FROM crypto_research_snapshots")) == 2
    assert storage.fetch_all("SELECT * FROM crypto_paper_watch_candidates") == []
    assert storage.fetch_all("SELECT * FROM trade_proposals") == []
    blockers = storage.fetch_all("SELECT DISTINCT blocker FROM performance_blockers WHERE blocker='crypto_research_only'")
    assert blockers


def test_crypto_stage_2_creates_hypothetical_candidates_only(tmp_path):
    storage = _storage(tmp_path)
    config = _config()
    config["crypto"]["mode"] = "paper_watch"

    CryptoResearchEngine(config, storage, CryptoBroker(), TelegramSink(), "run-stage-2").run_research(
        now=datetime(2026, 7, 3, 10, 0, tzinfo=UTC)
    )

    candidates = storage.fetch_all("SELECT * FROM crypto_paper_watch_candidates ORDER BY symbol")
    assert {row["symbol"] for row in candidates} == {"BTC/USD", "ETH/USD"}
    assert {row["mode"] for row in candidates} == {"paper_watch"}
    assert {row["status"] for row in candidates} == {"hypothetical"}
    assert all(row["entry_price"] and row["stop_price"] and row["take_profit_price"] for row in candidates)
    assert all(row["risk_reward_ratio"] >= 1.5 for row in candidates)
    assert all(row["spread_bps"] is not None for row in candidates)
    assert storage.fetch_all("SELECT * FROM trade_proposals") == []
    assert storage.fetch_all("SELECT * FROM proposal_batches") == []
    assert storage.fetch_all("SELECT * FROM approvals") == []
    assert storage.fetch_all("SELECT * FROM orders") == []
    assert storage.fetch_all("SELECT * FROM fills") == []


def test_crypto_stage_3_blocks_without_runtime_evidence_gate(tmp_path):
    storage = _storage(tmp_path)
    config = _config()
    config["crypto"]["mode"] = "paper_proposal"
    config["crypto"]["paper_trading_enabled"] = True
    config["crypto"]["proposals_enabled"] = True

    CryptoResearchEngine(config, storage, CryptoBroker(), TelegramSink(), "run-stage-3-blocked").run_research(
        now=datetime(2026, 7, 3, 10, 0, tzinfo=UTC)
    )

    candidates = storage.fetch_all("SELECT * FROM crypto_paper_watch_candidates")
    assert candidates
    assert {row["status"] for row in candidates} == {"blocked"}
    assert any("crypto_runtime_evidence_gate_failed" in (row["blockers"] or "") for row in candidates)
    assert storage.fetch_all("SELECT * FROM trade_proposals") == []
    assert storage.fetch_all("SELECT * FROM orders") == []


def test_crypto_stage_3_records_readiness_report_only_after_evidence_gate(tmp_path):
    storage = _storage(tmp_path)
    broker = CryptoBroker()
    config = _config()
    historical = _config()
    for idx in range(3):
        CryptoResearchEngine(historical, storage, broker, TelegramSink(), f"run-history-{idx}").run_research(
            now=datetime(2026, 7, 3, 10, 0, tzinfo=UTC)
        )
    config["crypto"]["mode"] = "paper_proposal"
    config["crypto"]["paper_trading_enabled"] = True
    config["crypto"]["proposals_enabled"] = True

    CryptoResearchEngine(config, storage, broker, TelegramSink(), "run-stage-3").run_research(
        now=datetime(2026, 7, 3, 10, 0, tzinfo=UTC)
    )

    candidates = storage.fetch_all("SELECT symbol,status,proposal_id,blockers FROM crypto_paper_watch_candidates WHERE mode='paper_proposal' ORDER BY symbol")
    assert {row["symbol"] for row in candidates} == {"BTC/USD", "ETH/USD"}
    assert {row["status"] for row in candidates} == {"stage3_ready_report"}
    assert {row["proposal_id"] for row in candidates} == {None}
    assert all("crypto_stage3_enablement_requires_separate_approval" in row["blockers"] for row in candidates)
    assert storage.fetch_all("SELECT * FROM trade_proposals") == []
    assert storage.fetch_all("SELECT * FROM proposal_batches") == []
    assert storage.fetch_all("SELECT * FROM approvals") == []
    assert storage.fetch_all("SELECT * FROM orders") == []
    assert storage.fetch_all("SELECT * FROM fills") == []
    assert broker.submitted_orders == []


def test_crypto_stage_3_does_not_send_proposals_during_quiet_hours(tmp_path):
    storage = _storage(tmp_path)
    broker = CryptoBroker()
    telegram = TelegramSink()
    config = _config()
    historical = _config()
    for idx in range(3):
        CryptoResearchEngine(historical, storage, broker, TelegramSink(), f"run-history-quiet-{idx}").run_research(
            now=datetime(2026, 7, 2, 18, 30, tzinfo=UTC)
        )
    config["crypto"]["mode"] = "paper_proposal"
    config["crypto"]["paper_trading_enabled"] = True
    config["crypto"]["proposals_enabled"] = True

    CryptoResearchEngine(config, storage, broker, telegram, "run-stage-3-quiet").run_research(
        now=datetime(2026, 7, 2, 18, 30, tzinfo=UTC)  # 02:30 SGT
    )

    candidates = storage.fetch_all("SELECT status,blockers FROM crypto_paper_watch_candidates WHERE mode='paper_proposal'")
    assert candidates
    assert {row["status"] for row in candidates} == {"blocked"}
    assert all("crypto_quiet_hours_notification_suppressed" in row["blockers"] for row in candidates)
    assert telegram.messages == []
    assert storage.fetch_all("SELECT * FROM trade_proposals") == []
    assert storage.fetch_all("SELECT * FROM orders") == []


def test_crypto_stage_3_hard_blocks_missing_spread(tmp_path):
    storage = _storage(tmp_path)
    broker = MissingSpreadCryptoBroker()
    config = _config()
    historical = _config()
    for idx in range(3):
        CryptoResearchEngine(historical, storage, broker, TelegramSink(), f"run-history-spread-{idx}").run_research(
            now=datetime(2026, 7, 3, 10, 0, tzinfo=UTC)
        )
    config["crypto"]["mode"] = "paper_proposal"
    config["crypto"]["paper_trading_enabled"] = True
    config["crypto"]["proposals_enabled"] = True

    CryptoResearchEngine(config, storage, broker, TelegramSink(), "run-stage-3-no-spread").run_research(
        now=datetime(2026, 7, 3, 10, 0, tzinfo=UTC)
    )

    candidates = storage.fetch_all("SELECT status,blockers FROM crypto_paper_watch_candidates WHERE mode='paper_proposal'")
    assert candidates
    assert {row["status"] for row in candidates} == {"blocked"}
    assert all("crypto_orderbook_missing" in row["blockers"] for row in candidates)
    assert storage.fetch_all("SELECT * FROM trade_proposals") == []
    assert storage.fetch_all("SELECT * FROM orders") == []


def test_risk_engine_keeps_crypto_execution_blocked_in_stage_1_and_stage_2():
    base_config = _config()
    base_config["risk"] = {"max_price_age_seconds": 120, "allowed_order_types": ["market", "limit"]}
    proposal = {
        "symbol": "BTC/USD",
        "side": "buy",
        "notional": 5,
        "asset_class": "crypto",
        "created_at": datetime(2026, 7, 3, 10, 0, tzinfo=UTC).isoformat(),
        "expires_at": datetime(2026, 7, 3, 10, 3, tzinfo=UTC).isoformat(),
        "latest_price": 100.0,
        "price_at": datetime(2026, 7, 3, 10, 0, tzinfo=UTC).isoformat(),
        "historical_bars": 100,
        "volume": 1000,
        "strategy_version": "rule_based_v1",
        "reason": "fixture",
        "order_type": "limit",
    }
    context = {
        "now": datetime(2026, 7, 3, 10, 0, tzinfo=UTC),
        "power_connected": True,
        "internet_available": True,
        "database_writable": True,
        "broker_available": True,
        "telegram_available": True,
        "market_open": True,
        "open_positions": 0,
        "buy_trades_today": 0,
        "duplicate_order": False,
        "same_symbol_position": False,
        "uses_margin": False,
        "daily_loss": 0,
        "weekly_loss": 0,
        "buying_power": 1000,
    }

    stage_1 = RiskEngine(base_config).evaluate(proposal, context)
    stage_2_config = _config()
    stage_2_config["crypto"]["mode"] = "paper_watch"
    stage_2_config["risk"] = base_config["risk"]
    stage_2 = RiskEngine(stage_2_config).evaluate(proposal, context)

    assert not stage_1.passed
    assert not stage_2.passed
    assert any("crypto is blocked" in reason for reason in stage_1.reasons)
    assert any("crypto is blocked" in reason for reason in stage_2.reasons)


def test_crypto_report_sheets_are_registered():
    sheet_names = {name for name, _ in SHEETS}
    assert {
        "Crypto Research Summary",
        "Crypto Candidate Briefs",
        "Crypto Observation State",
        "Crypto Counterfactual Outcomes",
        "Crypto Data Coverage",
        "Crypto Risk Metrics",
        "Crypto Capability",
        "Crypto Pair Precision",
        "Crypto Market Evidence",
    }.issubset(sheet_names)


def test_crypto_live_and_paper_proposals_disabled_by_default():
    config = _config()
    assert config["live_enabled"] is False
    assert config["auto_execution_enabled"] is False
    assert config["crypto"]["live_enabled"] is False
    assert config["crypto"]["paper_trading_enabled"] is False
    assert config["crypto"]["proposals_enabled"] is False
