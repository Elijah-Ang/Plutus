from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from app.data_providers.base import ProviderResponse
from app.data_providers.cache import ProviderCache
from app.data_providers.eodhd import EODHDProvider
from app.dynamic_universe import (
    DEMOTED,
    OBSERVATION,
    PAPER_TRADABLE,
    RAW_UNIVERSE,
    RESEARCH_CANDIDATE,
    DynamicUniverseEngine,
)
from app.power import PowerStatus
from app.reports import SHEETS
from app.service import TradingService
from app.utils import load_config
from test_scoring_and_throttling import MockBroker, MockTelegramBot, temp_storage


class FakeProvider:
    name = "fake"

    def __init__(self, rows: list[dict[str, Any]] | None = None, bars: list[dict[str, Any]] | None = None, fail: bool = False) -> None:
        self.rows = rows or []
        self.bars = bars or []
        self.fail = fail
        self.calls: list[str] = []

    def _response(self, endpoint: str, data: Any) -> ProviderResponse:
        self.calls.append(endpoint)
        if self.fail:
            return ProviderResponse("fake", endpoint, "provider_unavailable", None, "offline")
        return ProviderResponse("fake", endpoint, "ok", data)

    def health(self) -> ProviderResponse:
        return self._response("health", {"ok": True})

    def list_symbols(self, exchange: str = "US", limit: int | None = None) -> ProviderResponse:
        return self._response("list_symbols", self.rows[:limit] if limit else self.rows)

    def search_symbols(self, query: str, limit: int | None = None) -> ProviderResponse:
        return self._response("search_symbols", self.rows[:limit] if limit else self.rows)

    def get_historical_bars(self, symbol: str, period: str = "d", limit: int = 250) -> ProviderResponse:
        return self._response("historical_bars", self.bars[-limit:])

    def get_intraday_bars(self, symbol: str, interval: str = "5m", limit: int = 100) -> ProviderResponse:
        return self._response("intraday_bars", [])

    def get_latest_quote(self, symbol: str) -> ProviderResponse:
        return self._response("latest_quote", {"close": 100.0})

    def get_news(self, symbol: str | None = None, topic: str | None = None, limit: int = 10) -> ProviderResponse:
        return self._response("news", [{"title": "filtered catalyst"}])

    def get_fundamentals(self, symbol: str) -> ProviderResponse:
        return self._response("fundamentals", {})

    def get_technical_indicators(self, symbol: str, function: str = "sma", period: int = 50) -> ProviderResponse:
        return self._response("technical", {})

    def get_screener_results(self, filters: dict[str, Any] | None = None, limit: int = 100) -> ProviderResponse:
        return self._response("screener", self.rows[:limit])


def dynamic_config() -> dict[str, Any]:
    cfg = load_config()
    cfg["mode"] = "paper"
    cfg["dynamic_universe"]["max_research_symbols_per_run"] = 20
    cfg["dynamic_universe"]["raw_sources"]["existing_static_watchlist"] = False
    cfg["dynamic_universe"]["raw_sources"]["eodhd_exchange_symbols"] = False
    cfg["dynamic_universe"]["raw_sources"]["eodhd_screener"] = True
    cfg["dynamic_universe"]["raw_sources"]["eodhd_news"] = True
    return cfg


def allow_resilience_environment(monkeypatch, *, internet: bool = True, connected: bool | None = True, battery_pct: float | None = 90.0) -> None:
    monkeypatch.setattr("app.dynamic_universe.internet_available", lambda: internet)
    monkeypatch.setattr("app.dynamic_universe.get_power_status", lambda: PowerStatus(connected, "test", "test power", battery_pct))


def liquid_bars(close: float = 100.0, volume: float = 2_000_000.0, rows: int = 80) -> list[dict[str, Any]]:
    bars: list[dict[str, Any]] = []
    for idx in range(rows):
        price = close + idx * 0.35 + (0.45 if idx % 2 else -0.15)
        bars.append({"close": price, "adjusted_close": price, "volume": volume})
    return bars


def test_provider_cache_round_trip(temp_storage):
    cache = ProviderCache(temp_storage)
    cache.set("eodhd", "screener", {"limit": 5}, [{"Code": "SMH"}], ttl_minutes=60)

    assert cache.get("eodhd", "screener", {"limit": 5}) == [{"Code": "SMH"}]
    assert cache.get("eodhd", "screener", {"limit": 6}) is None


def test_eodhd_key_loaded_from_env_without_network(temp_storage, monkeypatch):
    monkeypatch.setenv("EODHD_API_KEY", "test-secret-token")
    provider = EODHDProvider(load_config(), temp_storage, "run-test")

    assert provider.api_key == "test-secret-token"


def test_provider_failure_degrades_to_audit_without_crash(temp_storage):
    cfg = dynamic_config()
    provider = FakeProvider(fail=True)
    engine = DynamicUniverseEngine(cfg, temp_storage, provider, "run-test")

    result = engine.run_research_cycle("daily_deep_research")

    assert result["status"] == "completed"
    audit = temp_storage.fetch_all("SELECT event_type FROM dynamic_universe_audit WHERE event_type='provider_unavailable'")
    assert audit


def test_dynamic_symbol_starts_non_executable_research_candidate(temp_storage):
    cfg = dynamic_config()
    provider = FakeProvider(
        rows=[{"Code": "SMH", "Type": "ETF", "Exchange": "US", "Sector": "Semiconductors"}],
        bars=liquid_bars(),
    )
    engine = DynamicUniverseEngine(cfg, temp_storage, provider, "run-test")

    result = engine.run_research_cycle("daily_deep_research")
    row = temp_storage.fetch_all("SELECT tier, executable, observation_only FROM universe_symbols WHERE symbol='SMH'")[0]

    assert result["considered"] == 1
    assert row["tier"] == RESEARCH_CANDIDATE
    assert row["executable"] == 0
    assert row["observation_only"] == 1


def test_observation_requires_shadow_tracking_before_paper_tradable(temp_storage):
    cfg = dynamic_config()
    provider = FakeProvider(
        rows=[{"Code": "SMH", "Type": "ETF", "Exchange": "US", "Sector": "Semiconductors"}],
        bars=liquid_bars(),
    )
    engine = DynamicUniverseEngine(cfg, temp_storage, provider, "run-test-1")
    engine.run_research_cycle("daily_deep_research")

    engine = DynamicUniverseEngine(cfg, temp_storage, provider, "run-test-2")
    engine.run_research_cycle("intraday_light_refresh")
    row = temp_storage.fetch_all("SELECT tier, executable FROM universe_symbols WHERE symbol='SMH'")[0]
    assert row["tier"] == OBSERVATION
    assert row["executable"] == 0

    for idx in range(2):
        DynamicUniverseEngine(cfg, temp_storage, provider, f"run-test-{idx+3}").run_research_cycle("intraday_light_refresh")
    row = temp_storage.fetch_all("SELECT tier, executable FROM universe_symbols WHERE symbol='SMH'")[0]
    assert row["tier"] == OBSERVATION
    assert row["executable"] == 0

    temp_storage.execute(
        "INSERT INTO shadow_trades(id,run_id,setup_id,symbol,side,would_have_entry_price,would_have_entry_time,reason_not_executed,score) VALUES(?,?,?,?,?,?,?,?,?)",
        ("shadow-smh", "run-test", "setup-smh", "SMH", "buy", 100.0, datetime.now(UTC).isoformat(), "observation only", 90.0),
    )
    DynamicUniverseEngine(cfg, temp_storage, provider, "run-test-5").run_research_cycle("intraday_light_refresh")
    row = temp_storage.fetch_all("SELECT tier, executable FROM universe_symbols WHERE symbol='SMH'")[0]
    assert row["tier"] == PAPER_TRADABLE
    assert row["executable"] == 1

    promotion = temp_storage.fetch_all("SELECT * FROM symbol_promotion_decisions WHERE symbol='SMH' AND to_tier='paper_tradable'")
    assert promotion


def test_unsupported_asset_class_remains_research_only(temp_storage):
    cfg = dynamic_config()
    provider = FakeProvider(
        rows=[{"Code": "EURUSD", "Type": "forex", "Exchange": "FOREX"}],
        bars=liquid_bars(),
    )
    DynamicUniverseEngine(cfg, temp_storage, provider, "run-test").run_research_cycle("daily_deep_research")

    row = temp_storage.fetch_all("SELECT asset_class, tier, executable FROM universe_symbols WHERE symbol='EURUSD'")[0]
    assert row["asset_class"] == "forex"
    assert row["tier"] in {RAW_UNIVERSE, RESEARCH_CANDIDATE}
    assert row["executable"] == 0


def test_illiquid_or_penny_symbol_does_not_promote(temp_storage):
    cfg = dynamic_config()
    provider = FakeProvider(
        rows=[{"Code": "PENNY", "Type": "Common Stock", "Exchange": "US"}],
        bars=liquid_bars(close=2.0, volume=50_000.0),
    )
    DynamicUniverseEngine(cfg, temp_storage, provider, "run-test").run_research_cycle("daily_deep_research")

    row = temp_storage.fetch_all("SELECT tier, executable, score FROM universe_symbols WHERE symbol='PENNY'")[0]
    assert row["tier"] == RAW_UNIVERSE
    assert row["executable"] == 0


def test_static_dynamic_scan_lists_and_cluster_lookup(temp_storage):
    cfg = dynamic_config()
    cfg["dynamic_universe"]["raw_sources"]["existing_static_watchlist"] = True
    service = TradingService(cfg, temp_storage, MockBroker(), "run-test")
    service.telegram = MockTelegramBot()
    temp_storage.execute(
        "INSERT INTO universe_symbols(id,symbol,asset_class,cluster,tier,executable,observation_only,score,updated_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("u1", "SMH", "etf", "semiconductors", PAPER_TRADABLE, 1, 0, 90.0, datetime.now(UTC).isoformat(), datetime.now(UTC).isoformat()),
    )
    temp_storage.execute(
        "INSERT INTO universe_symbols(id,symbol,exchange,asset_class,cluster,tier,executable,observation_only,score,updated_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("u2", "JPM", "US", "equity", "financials", OBSERVATION, 0, 1, 82.0, datetime.now(UTC).isoformat(), datetime.now(UTC).isoformat()),
    )
    temp_storage.execute(
        "INSERT INTO universe_symbols(id,symbol,exchange,asset_class,cluster,tier,executable,observation_only,score,updated_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("u3", "2800.HK", "HK", "etf", "unknown_cluster", OBSERVATION, 0, 1, 90.0, datetime.now(UTC).isoformat(), datetime.now(UTC).isoformat()),
    )

    active, observation = service._dynamic_universe_scan_symbols()

    assert active == ["SMH"]
    assert observation == ["JPM"]
    assert "2800.HK" not in observation
    assert service._get_symbol_cluster("SMH") == "semiconductors"


def test_observation_market_profile_watchlists_do_not_become_executable(temp_storage):
    cfg = load_config()
    cfg["mode"] = "paper"
    cfg["dynamic_universe"]["raw_sources"]["eodhd_screener"] = False
    cfg["dynamic_universe"]["raw_sources"]["eodhd_exchange_symbols"] = False
    cfg["dynamic_universe"]["raw_sources"]["existing_static_watchlist"] = True
    DynamicUniverseEngine(cfg, temp_storage, FakeProvider(), "run-test").run_research_cycle("daily_deep_research")

    es3 = temp_storage.fetch_all("SELECT tier, executable, observation_only, source FROM universe_symbols WHERE symbol='ES3.SI'")[0]
    hk = temp_storage.fetch_all("SELECT tier, executable, observation_only, source FROM universe_symbols WHERE symbol='2800.HK'")[0]

    assert es3["tier"] == OBSERVATION
    assert es3["executable"] == 0
    assert es3["observation_only"] == 1
    assert es3["source"] == "existing_static_observation"
    assert hk["tier"] == OBSERVATION
    assert hk["executable"] == 0


def test_dynamic_universe_report_sheets_registered():
    sheet_names = {name for name, _ in SHEETS}

    assert "Dynamic Universe Summary" in sheet_names
    assert "Universe Membership" in sheet_names
    assert "Paper-Tradable Symbols" in sheet_names
    assert "Data Provider Health" in sheet_names
    assert "Dynamic Universe Performance" in sheet_names
    assert "Dynamic Universe Schedule State" in sheet_names
    assert "Missed Research Cycles" in sheet_names
    assert "Catch-Up Runs" in sheet_names
    assert "Stale Research Guards" in sheet_names


def test_run_due_respects_paper_only_and_schedule(temp_storage, monkeypatch):
    allow_resilience_environment(monkeypatch)
    cfg = dynamic_config()
    provider = FakeProvider(rows=[{"Code": "SMH", "Type": "ETF", "Exchange": "US", "Sector": "Semiconductors"}], bars=liquid_bars())
    engine = DynamicUniverseEngine(cfg, temp_storage, provider, "run-test")
    engine.now = datetime.now(UTC).replace(hour=13, minute=0)

    results = engine.run_due(force=True, run_types=["event_triggered_refresh"])
    assert len(results) == 1

    cfg["mode"] = "live"
    live_engine = DynamicUniverseEngine(cfg, temp_storage, provider, "run-live")
    assert live_engine.run_due(force=True) == []


def test_missing_provider_key_records_schedule_skip_and_no_promotion(temp_storage, monkeypatch):
    allow_resilience_environment(monkeypatch)
    cfg = dynamic_config()
    provider = FakeProvider(rows=[{"Code": "SMH", "Type": "ETF", "Exchange": "US", "Sector": "Semiconductors"}], bars=liquid_bars())
    provider.api_key = None
    engine = DynamicUniverseEngine(cfg, temp_storage, provider, "run-test")

    result = engine.run_due(force=True, run_types=["daily_deep_research"])[0]

    assert result["status"] == "skipped"
    assert result["reason"] == "missing_api_key"
    assert provider.calls == []
    assert temp_storage.fetch_all("SELECT * FROM universe_symbols WHERE symbol='SMH'") == []
    state = temp_storage.fetch_all("SELECT * FROM dynamic_universe_schedule_state WHERE schedule_name='daily_deep_research'")[0]
    assert state["catchup_required"] == 1
    assert state["last_skip_reason"] == "missing_api_key"
    health = temp_storage.fetch_all("SELECT * FROM data_provider_health WHERE status='provider_unavailable'")
    assert health


def test_no_internet_records_missed_cycle_and_preserves_universe(temp_storage, monkeypatch):
    allow_resilience_environment(monkeypatch, internet=False)
    cfg = dynamic_config()
    temp_storage.execute(
        "INSERT INTO universe_symbols(id,symbol,tier,source,executable,observation_only,score,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
        ("u-existing", "SMH", OBSERVATION, "eodhd_screener", 0, 1, 85.0, datetime.now(UTC).isoformat(), datetime.now(UTC).isoformat()),
    )
    provider = FakeProvider(rows=[{"Code": "QQQ", "Type": "ETF", "Exchange": "US"}], bars=liquid_bars())
    provider.api_key = "configured"

    result = DynamicUniverseEngine(cfg, temp_storage, provider, "run-test").run_due(force=True, run_types=["intraday_light_refresh"])[0]

    assert result["status"] == "skipped"
    assert result["reason"] == "no_internet"
    assert provider.calls == []
    existing = temp_storage.fetch_all("SELECT tier, data_freshness_status, promotion_allowed FROM universe_symbols WHERE symbol='SMH'")[0]
    assert existing["tier"] == OBSERVATION
    assert existing["data_freshness_status"] == "stale"
    assert existing["promotion_allowed"] == 0
    state = temp_storage.fetch_all("SELECT missed_count, catchup_required FROM dynamic_universe_schedule_state WHERE schedule_name='intraday_light_refresh'")[0]
    assert state["missed_count"] == 1
    assert state["catchup_required"] == 1


def test_battery_policy_allows_light_and_blocks_deep(temp_storage, monkeypatch):
    cfg = dynamic_config()
    provider = FakeProvider(rows=[{"Code": "SMH", "Type": "ETF", "Exchange": "US", "Sector": "Semiconductors"}], bars=liquid_bars())
    provider.api_key = "configured"

    allow_resilience_environment(monkeypatch, connected=False, battery_pct=50)
    light = DynamicUniverseEngine(cfg, temp_storage, provider, "run-light").run_due(force=True, run_types=["intraday_light_refresh"])[0]
    assert light["status"] == "completed"

    deep = DynamicUniverseEngine(cfg, temp_storage, provider, "run-deep").run_due(force=True, run_types=["daily_deep_research"])[0]
    assert deep["status"] == "skipped"
    assert deep["reason"] == "deep_research_skipped_on_battery"

    allow_resilience_environment(monkeypatch, connected=False, battery_pct=20)
    low = DynamicUniverseEngine(cfg, temp_storage, provider, "run-low").run_due(force=True, run_types=["intraday_light_refresh"])[0]
    assert low["status"] == "skipped"
    assert low["reason"] == "battery_below_research_threshold"


def test_catchup_runs_once_and_does_not_replay_intraday_cycles(temp_storage, monkeypatch):
    allow_resilience_environment(monkeypatch, internet=False)
    cfg = dynamic_config()
    provider = FakeProvider(rows=[{"Code": "SMH", "Type": "ETF", "Exchange": "US", "Sector": "Semiconductors"}], bars=liquid_bars())
    provider.api_key = "configured"
    engine = DynamicUniverseEngine(cfg, temp_storage, provider, "run-offline")
    engine.run_due(force=True, run_types=["intraday_light_refresh"])
    engine.run_due(force=True, run_types=["intraday_light_refresh"])

    state = temp_storage.fetch_all("SELECT missed_count, catchup_required FROM dynamic_universe_schedule_state WHERE schedule_name='intraday_light_refresh'")[0]
    assert state["missed_count"] == 2
    assert state["catchup_required"] == 1

    allow_resilience_environment(monkeypatch, internet=True)
    recovery_provider = FakeProvider(rows=[{"Code": "SMH", "Type": "ETF", "Exchange": "US", "Sector": "Semiconductors"}], bars=liquid_bars())
    recovery_provider.api_key = "configured"
    results = DynamicUniverseEngine(cfg, temp_storage, recovery_provider, "run-recovery").run_due(force=False, run_types=["intraday_light_refresh"])

    assert len(results) == 1
    assert results[0]["status"] == "completed"
    state = temp_storage.fetch_all("SELECT missed_count, catchup_required, catchup_status FROM dynamic_universe_schedule_state WHERE schedule_name='intraday_light_refresh'")[0]
    assert state["missed_count"] == 0
    assert state["catchup_required"] == 0
    assert state["catchup_status"] == "completed"


def test_stale_dynamic_paper_tradable_blocked_from_buy_scan_but_static_kept(temp_storage, monkeypatch):
    allow_resilience_environment(monkeypatch)
    cfg = dynamic_config()
    cfg["dynamic_universe_resilience"]["stale_data_policy"]["max_age_minutes_for_trade_eligibility"] = 30
    service = TradingService(cfg, temp_storage, MockBroker(), "run-test")
    old = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    fresh = datetime.now(UTC).isoformat()
    temp_storage.execute(
        "INSERT INTO universe_symbols(id,symbol,tier,source,executable,observation_only,score,last_successful_research_at,updated_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("u-dyn-old", "SMH", PAPER_TRADABLE, "eodhd_screener", 1, 0, 95.0, old, old, old),
    )
    temp_storage.execute(
        "INSERT INTO universe_symbols(id,symbol,tier,source,executable,observation_only,score,last_successful_research_at,updated_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("u-static", "SPY", PAPER_TRADABLE, "existing_static_watchlist", 1, 0, 90.0, old, old, old),
    )
    temp_storage.execute(
        "INSERT INTO universe_symbols(id,symbol,tier,source,executable,observation_only,score,last_successful_research_at,updated_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("u-dyn-fresh", "JPM", PAPER_TRADABLE, "eodhd_screener", 1, 0, 80.0, fresh, fresh, fresh),
    )

    active, _ = service._dynamic_universe_scan_symbols()

    assert "SMH" not in active
    assert "SPY" in active
    assert "JPM" in active


def test_provider_unavailable_blocks_demotions_from_missing_data(temp_storage):
    cfg = dynamic_config()
    provider = FakeProvider(fail=True)
    provider.api_key = "configured"
    now = datetime.now(UTC)
    temp_storage.execute(
        "INSERT INTO universe_symbols(id,symbol,tier,source,executable,observation_only,score,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
        ("u-weak", "WEAK", OBSERVATION, "eodhd_screener", 0, 1, 20.0, now.isoformat(), now.isoformat()),
    )
    for idx in range(5):
        temp_storage.execute(
            "INSERT INTO symbol_research_scores(id,run_id,symbol,provider,score,created_at) VALUES(?,?,?,?,?,?)",
            (f"weak-score-{idx}", "run-test", "WEAK", "fake", 20.0, (now + timedelta(minutes=idx)).isoformat()),
        )

    DynamicUniverseEngine(cfg, temp_storage, provider, "run-test").run_research_cycle("intraday_light_refresh")

    row = temp_storage.fetch_all("SELECT tier FROM universe_symbols WHERE symbol='WEAK'")[0]
    assert row["tier"] == OBSERVATION
    audit = temp_storage.fetch_all("SELECT * FROM dynamic_universe_audit WHERE event_type='dynamic_universe_demotions_blocked_provider_unavailable'")
    assert audit


def test_dynamic_universe_digest_mentions_schedule_state(temp_storage, monkeypatch):
    cfg = dynamic_config()
    service = TradingService(cfg, temp_storage, MockBroker(), "run-test")
    now = datetime.now(UTC)
    temp_storage.execute(
        "INSERT INTO dynamic_universe_schedule_state(id,schedule_name,schedule_type,last_skipped_at,last_skip_reason,missed_count,catchup_required,provider_health_status,internet_status,power_status,updated_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        ("state-1", "intraday_light_refresh", "intraday_light", now.isoformat(), "no_internet", 2, 1, "provider_unavailable", "offline", "ac", now.isoformat(), now.isoformat()),
    )
    text = service._dynamic_universe_update_since((now - timedelta(minutes=5)).isoformat())

    assert text is not None
    assert "Research skipped: intraday_light_refresh" in text
    assert "Missed count: 2" in text


def test_demote_repeated_weak_scores_preserves_history(temp_storage):
    cfg = dynamic_config()
    now = datetime.now(UTC)
    temp_storage.execute(
        "INSERT INTO universe_symbols(id,symbol,tier,executable,observation_only,score,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
        ("u-weak", "WEAK", OBSERVATION, 0, 1, 90.0, now.isoformat(), now.isoformat()),
    )
    for idx in range(5):
        temp_storage.execute(
            "INSERT INTO symbol_research_scores(id,run_id,symbol,provider,score,created_at) VALUES(?,?,?,?,?,?)",
            (f"s-{idx}", "run-test", "WEAK", "fake", 20.0, (now + timedelta(minutes=idx)).isoformat()),
        )

    DynamicUniverseEngine(cfg, temp_storage, FakeProvider(), "run-test")._demote_stale_symbols([])

    row = temp_storage.fetch_all("SELECT tier FROM universe_symbols WHERE symbol='WEAK'")[0]
    history = temp_storage.fetch_all("SELECT * FROM universe_membership_history WHERE symbol='WEAK'")
    assert row["tier"] == DEMOTED
    assert history


def test_configured_static_symbols_are_not_demoted_by_dynamic_cleanup(temp_storage):
    cfg = dynamic_config()
    now = datetime.now(UTC)
    temp_storage.execute(
        "INSERT INTO universe_symbols(id,symbol,tier,source,executable,observation_only,score,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
        ("u-static", "SPY", PAPER_TRADABLE, "existing_static_watchlist", 1, 0, 90.0, now.isoformat(), now.isoformat()),
    )
    for idx in range(5):
        temp_storage.execute(
            "INSERT INTO symbol_research_scores(id,run_id,symbol,provider,score,created_at) VALUES(?,?,?,?,?,?)",
            (f"static-score-{idx}", "run-test", "SPY", "fake", 20.0, (now + timedelta(minutes=idx)).isoformat()),
        )

    DynamicUniverseEngine(cfg, temp_storage, FakeProvider(), "run-test")._demote_stale_symbols([])

    row = temp_storage.fetch_all("SELECT tier FROM universe_symbols WHERE symbol='SPY'")[0]
    assert row["tier"] == PAPER_TRADABLE
