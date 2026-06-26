from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.error import HTTPError
from typing import Any

from app.data_providers.base import ProviderResponse
from app.data_providers.cache import ProviderCache
from app.data_providers.eodhd import EODHDProvider
from app.data_providers.marketaux import MarketauxNewsProvider
from app.dynamic_universe import (
    DEMOTED,
    LANE_ALPACA_US,
    LANE_EXCLUDED,
    LANE_GLOBAL_RESEARCH,
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


class PartialProvider(FakeProvider):
    def __init__(self, *args: Any, news_status: str = "ok", quote_status: str = "ok", **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.news_status = news_status
        self.quote_status = quote_status

    def get_latest_quote(self, symbol: str) -> ProviderResponse:
        self.calls.append("latest_quote")
        if self.quote_status != "ok":
            return ProviderResponse("fake", "latest_quote", self.quote_status, None, self.quote_status)
        return ProviderResponse("fake", "latest_quote", "ok", {"close": 100.0})

    def get_news(self, symbol: str | None = None, topic: str | None = None, limit: int = 10) -> ProviderResponse:
        self.calls.append("news")
        if self.news_status != "ok":
            return ProviderResponse("fake", "news", self.news_status, None, self.news_status)
        return ProviderResponse("fake", "news", "ok", [{"title": "filtered catalyst"}])


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


def test_eodhd_plan_limited_endpoint_is_cooled_down(temp_storage, monkeypatch):
    cfg = load_config()
    cfg["eodhd"]["max_retries"] = 0
    provider = EODHDProvider(cfg, temp_storage, "run-test", api_key="test-secret")

    def blocked(*args, **kwargs):
        raise HTTPError("https://example.test", 403, "Forbidden", hdrs=None, fp=None)

    monkeypatch.setattr("urllib.request.urlopen", blocked)

    first = provider.get_intraday_bars("SPY")
    calls_after_first = provider.calls_this_run
    second = provider.get_intraday_bars("SPY")

    assert first.status == "plan_limited"
    assert second.status == "plan_limited"
    assert provider.calls_this_run == calls_after_first
    capability = temp_storage.fetch_all("SELECT * FROM data_provider_capabilities WHERE endpoint_name='intraday_bars'")[0]
    assert capability["plan_limited"] == 1
    assert capability["disabled_until"] is not None


def test_provider_capability_reprobe_waits_until_interval(temp_storage):
    cache = ProviderCache(temp_storage)
    cache.record_capability("eodhd", "news", status="plan_limited", run_id="run-1", error_category="forbidden", cooldown_minutes=1440)

    assert cache.capability_disabled("eodhd", "news", reprobe_after_minutes=60) is True


def test_eodhd_retries_stale_plan_limited_capability(temp_storage, monkeypatch):
    cfg = load_config()
    cfg["eodhd"]["max_retries"] = 0
    cfg["eodhd"]["plan_limited_reprobe_minutes"] = 60
    provider = EODHDProvider(cfg, temp_storage, "run-test", api_key="test-secret")
    cache = ProviderCache(temp_storage)
    cache.record_capability("eodhd", "news", status="plan_limited", run_id="run-1", error_category="forbidden", cooldown_minutes=1440)
    stale_at = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    temp_storage.execute(
        "UPDATE data_provider_capabilities SET updated_at=?, last_failure_at=? WHERE provider=? AND endpoint_name=?",
        (stale_at, stale_at, "eodhd", "news"),
    )

    class FakeHTTPResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b'[{\"title\":\"fresh catalyst\"}]'

    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: FakeHTTPResponse())

    result = provider.get_news("SPY")

    assert result.status == "ok"
    assert provider.calls_this_run == 1
    capability = temp_storage.fetch_all("SELECT available, plan_limited, disabled_until FROM data_provider_capabilities WHERE endpoint_name='news'")[0]
    assert capability["available"] == 1
    assert capability["plan_limited"] == 0
    assert capability["disabled_until"] is None


def test_eodhd_symbol_not_found_does_not_disable_endpoint(temp_storage, monkeypatch):
    cfg = load_config()
    cfg["eodhd"]["max_retries"] = 0
    provider = EODHDProvider(cfg, temp_storage, "run-test", api_key="test-secret")
    responses = iter(
        [
            HTTPError("https://example.test", 404, "Not Found", hdrs=None, fp=None),
            b'[{\"close\":600.0,\"adjusted_close\":600.0,\"volume\":1000000}]',
        ]
    )

    class FakeHTTPResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def __init__(self, payload: bytes):
            self.payload = payload

        def read(self):
            return self.payload

    def fake_urlopen(*args, **kwargs):
        next_item = next(responses)
        if isinstance(next_item, Exception):
            raise next_item
        return FakeHTTPResponse(next_item)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    missing = provider.get_historical_bars("BAD.US")
    recovered = provider.get_historical_bars("SPY.US")

    assert missing.status == "plan_limited"
    assert recovered.status == "ok"
    assert provider.calls_this_run == 2
    capability = temp_storage.fetch_all("SELECT available, plan_limited, last_error_category, disabled_until FROM data_provider_capabilities WHERE endpoint_name='eod_bars'")[0]
    assert capability["available"] == 1
    assert capability["plan_limited"] == 0
    assert capability["disabled_until"] is None


def test_provider_capability_recovery_marks_available(temp_storage):
    cache = ProviderCache(temp_storage)
    cache.record_capability("eodhd", "news", status="plan_limited", run_id="run-1", error_category="forbidden", cooldown_minutes=1440)
    cache.record_capability("eodhd", "news", status="ok", run_id="run-2", used_for_scoring=True)

    capability = temp_storage.fetch_all("SELECT available, plan_limited, failure_count, disabled_until FROM data_provider_capabilities WHERE endpoint_name='news'")[0]
    assert capability["available"] == 1
    assert capability["plan_limited"] == 0
    assert capability["failure_count"] == 0
    assert capability["disabled_until"] is None


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
    row = temp_storage.fetch_all("SELECT tier, executable, observation_only, data_confidence FROM universe_symbols WHERE symbol='SMH'")[0]

    assert result["considered"] == 1
    assert row["tier"] == RESEARCH_CANDIDATE
    assert row["executable"] == 0
    assert row["observation_only"] == 1
    assert row["data_confidence"] == "medium"


def test_research_candidate_brief_records_depth_and_safety(temp_storage):
    cfg = dynamic_config()
    provider = FakeProvider(
        rows=[{"Code": "SMH", "Type": "ETF", "Exchange": "US", "Sector": "Semiconductors", "Name": "VanEck Semiconductor ETF"}],
        bars=liquid_bars(),
    )

    result = DynamicUniverseEngine(cfg, temp_storage, provider, "run-test").run_research_cycle("daily_deep_research")

    assert result["candidate_briefs"] == 1
    brief = temp_storage.fetch_all("SELECT * FROM research_candidate_briefs WHERE symbol='SMH'")[0]
    assert brief["current_tier"] == RESEARCH_CANDIDATE
    assert brief["explanation_source"] == "deterministic"
    assert brief["research_score"] is not None
    assert brief["data_confidence"] == "medium"
    assert "liquidity" in brief["main_positive_reasons"]
    assert "trade proposals" in brief["blocked_actions"]
    assert "No proposal or order is allowed" in brief["proposal_order_confirmation"]
    assert "endpoint_coverage" in brief["payload"]
    assert "before_observation_requirements" in set(brief.keys())
    proposals = temp_storage.fetch_all("SELECT * FROM trade_proposals")
    orders = temp_storage.fetch_all("SELECT * FROM orders")
    assert proposals == []
    assert orders == []


def test_stage_semantics_are_seeded_and_block_research_candidate_trading(temp_storage):
    rows = temp_storage.fetch_all("SELECT * FROM dynamic_universe_stage_semantics WHERE tier='research_candidate'")

    assert rows
    row = rows[0]
    assert row["telegram_trade_proposals_allowed"] == 0
    assert row["orders_possible"] == 0
    assert row["llm_explanations_allowed"] == 1
    assert row["llm_can_affect_decisions"] == 0


def test_llm_explanation_stub_disabled_by_default_and_decisionless(temp_storage):
    cfg = dynamic_config()
    provider = FakeProvider(
        rows=[{"Code": "SMH", "Type": "ETF", "Exchange": "US", "Sector": "Semiconductors"}],
        bars=liquid_bars(),
    )

    DynamicUniverseEngine(cfg, temp_storage, provider, "run-test").run_research_cycle("daily_deep_research")

    usage = temp_storage.fetch_all("SELECT * FROM llm_explanation_usage")[0]
    assert usage["enabled"] == 0
    assert usage["attempted_calls"] == 0
    assert usage["successful_calls"] == 0
    assert usage["conflicts_ignored"] == 0
    row = temp_storage.fetch_all("SELECT tier, executable FROM universe_symbols WHERE symbol='SMH'")[0]
    assert row["tier"] == RESEARCH_CANDIDATE
    assert row["executable"] == 0


def test_current_research_candidate_brief_backfill_does_not_promote_or_trade(temp_storage):
    cfg = dynamic_config()
    now = datetime.now(UTC).isoformat()
    temp_storage.execute(
        "INSERT INTO universe_symbols(id,symbol,exchange,asset_class,cluster,tier,universe_lane,alpaca_compatible,executable,observation_only,score,data_confidence,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("u-smh", "SMH", "US", "etf", "semiconductors", RESEARCH_CANDIDATE, LANE_ALPACA_US, 1, 0, 1, 70.0, "medium", now, now),
    )
    temp_storage.execute(
        "INSERT INTO symbol_research_scores(id,run_id,symbol,provider,score,liquidity_score,trend_score,relative_strength_score,intraday_momentum_score,volatility_quality_score,screener_mover_score,news_score,sector_theme_score,data_quality_score,data_confidence,data_confidence_reason,universe_lane,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("score-smh", "run-old", "SMH", "fake", 70.0, 18.0, 15.0, 9.0, 8.0, 10.0, 8.0, 2.5, 5.0, 5.0, "medium", "stored score", LANE_ALPACA_US, now),
    )
    engine = DynamicUniverseEngine(cfg, temp_storage, None, "run-backfill")

    count = engine.generate_current_research_candidate_briefs()

    assert count == 1
    row = temp_storage.fetch_all("SELECT tier, executable FROM universe_symbols WHERE symbol='SMH'")[0]
    assert row["tier"] == RESEARCH_CANDIDATE
    assert row["executable"] == 0
    assert temp_storage.fetch_all("SELECT * FROM trade_proposals") == []
    assert temp_storage.fetch_all("SELECT * FROM orders") == []
    brief = temp_storage.fetch_all("SELECT * FROM research_candidate_briefs WHERE symbol='SMH'")[0]
    assert "No proposal or order is allowed" in brief["proposal_order_confirmation"]


def test_premarket_telegram_wording_uses_scan_and_no_orders_line(temp_storage):
    cfg = dynamic_config()
    cfg["telegram"]["dynamic_universe_premarket_updates_enabled"] = True
    provider = FakeProvider(
        rows=[{"Code": "SMH", "Type": "ETF", "Exchange": "US", "Sector": "Semiconductors"}],
        bars=liquid_bars(),
    )
    result = DynamicUniverseEngine(cfg, temp_storage, provider, "run-test").run_research_cycle("daily_deep_research")
    service = TradingService(cfg, temp_storage, MockBroker(), "run-test")
    service.telegram = MockTelegramBot()

    service.notify_premarket_dynamic_universe_status([result], "market_closed")

    msg = service.telegram.messages[-1]
    assert "pre-market universe scan completed" in msg
    assert "pre-market research completed" not in msg
    assert "Research candidates: 1" in msg
    assert "Briefs: 1" in msg
    assert "Trading remains blocked until market open" in msg
    assert "No trade proposals/orders created" in msg
    assert len(msg) < 900


def test_partial_data_eod_quote_news_can_create_research_candidate(temp_storage):
    cfg = dynamic_config()
    provider = PartialProvider(
        rows=[{"Code": "SMH", "Type": "ETF", "Exchange": "US", "Sector": "Semiconductors"}],
        bars=liquid_bars(),
    )

    DynamicUniverseEngine(cfg, temp_storage, provider, "run-test").run_research_cycle("daily_deep_research")

    row = temp_storage.fetch_all("SELECT tier, data_confidence FROM universe_symbols WHERE symbol='SMH'")[0]
    assert row["tier"] == RESEARCH_CANDIDATE
    assert row["data_confidence"] == "medium"


def test_missing_news_is_neutral_and_low_confidence_research_candidate(temp_storage):
    cfg = dynamic_config()
    provider = PartialProvider(
        rows=[{"Code": "SMH", "Type": "ETF", "Exchange": "US", "Sector": "Semiconductors"}],
        bars=liquid_bars(),
        news_status="plan_limited",
    )

    DynamicUniverseEngine(cfg, temp_storage, provider, "run-test").run_research_cycle("daily_deep_research")

    score = temp_storage.fetch_all("SELECT news_score, data_confidence FROM symbol_research_scores WHERE symbol='SMH'")[0]
    row = temp_storage.fetch_all("SELECT tier, data_confidence FROM universe_symbols WHERE symbol='SMH'")[0]
    assert score["news_score"] == 2.5
    assert score["data_confidence"] == "medium"
    assert row["tier"] == RESEARCH_CANDIDATE


def test_missing_price_liquidity_blocks_research_candidate_and_records_reason(temp_storage):
    cfg = dynamic_config()
    provider = PartialProvider(
        rows=[{"Code": "NODT", "Type": "ETF", "Exchange": "US", "Sector": "Semiconductors"}],
        bars=[],
    )

    DynamicUniverseEngine(cfg, temp_storage, provider, "run-test").run_research_cycle("daily_deep_research")

    row = temp_storage.fetch_all("SELECT tier, data_confidence FROM universe_symbols WHERE symbol='NODT'")[0]
    block = temp_storage.fetch_all("SELECT block_reason, data_confidence FROM research_candidate_block_reasons WHERE symbol='NODT'")[0]
    assert row["tier"] == RAW_UNIVERSE
    assert row["data_confidence"] == "insufficient"
    assert block["block_reason"] in {"missing liquidity data", "missing or stale price data"}
    assert block["data_confidence"] == "insufficient"


def test_symbol_intake_lanes_separate_us_global_and_excluded(temp_storage):
    cfg = dynamic_config()
    provider = FakeProvider(
        rows=[
            {"Code": "SMH", "Type": "ETF", "Exchange": "US", "Sector": "Semiconductors"},
            {"Code": "700", "Type": "Common Stock", "Exchange": "HK"},
            {"Code": "AKRTF", "Type": "Common Stock", "Exchange": "US", "source": "eodhd_news"},
            {"Code": "AKRYY", "Type": "Common Stock", "Exchange": "US", "source": "eodhd_news"},
        ],
        bars=liquid_bars(),
    )

    DynamicUniverseEngine(cfg, temp_storage, provider, "run-test").run_research_cycle("daily_deep_research")

    rows = {r["symbol"]: r for r in temp_storage.fetch_all("SELECT symbol, universe_lane, alpaca_compatible, exclusion_reason FROM universe_symbols")}
    assert rows["SMH"]["universe_lane"] == LANE_ALPACA_US
    assert rows["SMH"]["alpaca_compatible"] == 1
    assert rows["700"]["universe_lane"] == LANE_GLOBAL_RESEARCH
    assert rows["AKRTF"]["universe_lane"] == LANE_EXCLUDED
    assert rows["AKRTF"]["exclusion_reason"] == "otc_or_adr_like_symbol"
    assert rows["AKRYY"]["universe_lane"] == LANE_EXCLUDED


def test_low_quality_symbols_do_not_pollute_us_near_misses(temp_storage):
    cfg = dynamic_config()
    cfg["dynamic_universe"]["exploration"]["min_research_score_for_exploration"] = 80
    provider = FakeProvider(
        rows=[
            {"Code": "CLEAN", "Type": "Common Stock", "Exchange": "US", "Sector": "Financials"},
            {"Code": "700", "Type": "Common Stock", "Exchange": "HK"},
            {"Code": "AKRTF", "Type": "Common Stock", "Exchange": "US", "source": "eodhd_news"},
        ],
        bars=liquid_bars(close=30.0, volume=500_000.0),
    )

    DynamicUniverseEngine(cfg, temp_storage, provider, "run-test").run_research_cycle("daily_deep_research")

    audit = temp_storage.fetch_all("SELECT detail FROM dynamic_universe_audit WHERE event_type='dynamic_universe_near_miss_symbols'")
    assert audit
    detail = audit[-1]["detail"]
    assert "CLEAN" in detail
    assert "AKRTF" not in detail
    assert "700" not in detail


def test_optional_marketaux_news_provider_disabled_safely_without_key(monkeypatch):
    cfg = load_config()
    monkeypatch.delenv("MARKETAUX_API_KEY", raising=False)
    provider = MarketauxNewsProvider(cfg, api_key=None)

    assert provider.enabled() is False
    assert provider.health().status in {"disabled", "disabled_missing_key"}
    assert provider.get_news("SMH").status == "disabled_missing_key"


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
    assert "Dynamic Research Summary" in sheet_names
    assert "Stage Semantics" in sheet_names
    assert "Research Candidate Briefs" in sheet_names
    assert "Candidate Scores" in sheet_names
    assert "Candidate Data Coverage" in sheet_names
    assert "Candidate Endpoint Coverage" in sheet_names
    assert "Candidate Promotion Requirements" in sheet_names
    assert "Candidate Block Reasons" in sheet_names
    assert "Candidate Next Steps" in sheet_names
    assert "Candidate Chart Data" in sheet_names
    assert "Research Funnel Chart Data" in sheet_names
    assert "Data Confidence Chart Data" in sheet_names
    assert "Candidate Score Chart Data" in sheet_names
    assert "Block Reason Chart Data" in sheet_names
    assert "Universe Membership" in sheet_names
    assert "Paper-Tradable Symbols" in sheet_names
    assert "Data Provider Health" in sheet_names
    assert "Dynamic Universe Performance" in sheet_names
    assert "Dynamic Universe Schedule State" in sheet_names
    assert "Missed Research Cycles" in sheet_names
    assert "Catch-Up Runs" in sheet_names
    assert "Stale Research Guards" in sheet_names
    assert "Provider Capabilities" in sheet_names
    assert "Endpoint Availability" in sheet_names
    assert "Research Candidate Blocks" in sheet_names
    assert "Data Confidence" in sheet_names
    assert "Top Near-Miss Symbols" in sheet_names
    assert "Dynamic Universe Source Coverage" in sheet_names
    assert "Symbol Intake Classification" in sheet_names
    assert "Alpaca-Compatible Candidates" in sheet_names
    assert "Global Research-Only Symbols" in sheet_names
    assert "Excluded Symbols" in sheet_names
    assert "Near-Miss US Candidates" in sheet_names
    assert "Optional News Provider Status" in sheet_names


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
