from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.data_providers.base import MarketResearchProvider, ProviderResponse
from app.storage import Storage
from app.utils import iso_now, json_dumps

RAW_UNIVERSE = "raw_universe"
RESEARCH_CANDIDATE = "research_candidate"
OBSERVATION = "observation"
PAPER_TRADABLE = "paper_tradable"
DEMOTED = "demoted"
SGT = ZoneInfo("Asia/Singapore")


@dataclass(frozen=True)
class ResearchScore:
    symbol: str
    total_score: float
    liquidity_score: float
    trend_score: float
    relative_strength_score: float
    volatility_quality_score: float
    news_score: float
    sector_theme_score: float
    data_quality_score: float
    block_reason: str | None = None


class DynamicUniverseEngine:
    def __init__(self, config: dict[str, Any], storage: Storage, provider: MarketResearchProvider | None, run_id: str) -> None:
        self.config = config
        self.storage = storage
        self.provider = provider
        self.run_id = run_id
        self.cfg = config.get("dynamic_universe", {})
        self.now = datetime.now(UTC)

    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled", False)) and self.config.get("mode") == "paper"

    def run_due(self, force: bool = False, run_types: list[str] | None = None) -> list[dict[str, Any]]:
        if not self.enabled():
            return []
        run_types = run_types or [
            "daily_deep_research",
            "intraday_light_refresh",
            "event_triggered_refresh",
            "post_market_review",
            "weekly_cleanup",
        ]
        results = []
        for run_type in run_types:
            if force or self._is_due(run_type):
                results.append(self.run_research_cycle(run_type))
        return results

    def run_research_cycle(self, run_type: str = "daily_deep_research") -> dict[str, Any]:
        run_id = str(uuid.uuid4())
        now_iso = self.now.isoformat()
        self.storage.execute(
            "INSERT INTO universe_research_runs(id,run_id,research_type,provider,status,started_at,ended_at,symbols_considered,symbols_promoted,symbols_demoted,detail) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, self.run_id, run_type, self.cfg.get("provider", "eodhd"), "running", now_iso, None, 0, 0, 0, "{}"),
        )

        promoted = []
        demoted = []
        candidates = self._collect_raw_candidates(run_type)
        considered = 0
        try:
            for info in candidates[: int(self.cfg.get("max_research_symbols_per_run", 100))]:
                symbol = self._normalize_symbol(info)
                if not symbol:
                    continue
                considered += 1
                metadata = self._metadata(info)
                current = self._current_symbol(symbol)
                if not current:
                    self._upsert_universe_symbol(symbol, metadata, RAW_UNIVERSE, executable=0, observation_only=1)
                    self._record_membership(symbol, None, RAW_UNIVERSE, "raw candidate discovered", metadata)
                score = self._score_symbol(symbol, metadata)
                self._record_score(score, metadata)
                new_tier = self._decide_tier(symbol, metadata, score)
                old_tier = current.get("tier") if current else None
                if new_tier != old_tier:
                    if new_tier == DEMOTED:
                        demoted.append(symbol)
                        self._record_demotion(symbol, old_tier, score, metadata)
                    elif new_tier in {RESEARCH_CANDIDATE, OBSERVATION, PAPER_TRADABLE}:
                        promoted.append(symbol)
                        self._record_promotion(symbol, old_tier, new_tier, score, metadata)
                    self._record_membership(symbol, old_tier, new_tier, score.block_reason or "research score update", metadata)
                executable = 1 if new_tier == PAPER_TRADABLE and self._asset_execution_allowed(metadata.get("asset_class")) else 0
                observation_only = 0 if executable else 1
                self._upsert_universe_symbol(symbol, metadata, new_tier, executable=executable, observation_only=observation_only, score=score.total_score)
                self._record_trend_snapshot(symbol, metadata, score)
                self._record_news(symbol, metadata)

            self._demote_stale_symbols(demoted)
            status = "completed"
            detail = {"provider_status": "ok", "run_type": run_type}
        except Exception as exc:
            status = "error"
            detail = {"error": type(exc).__name__, "run_type": run_type}
            self.storage.execute(
                "INSERT INTO dynamic_universe_audit(id,run_id,event_type,symbol,detail,created_at) VALUES(?,?,?,?,?,?)",
                (str(uuid.uuid4()), self.run_id, "dynamic_universe_error", None, json_dumps(detail), iso_now()),
            )
        self.storage.execute(
            "UPDATE universe_research_runs SET status=?, ended_at=?, symbols_considered=?, symbols_promoted=?, symbols_demoted=?, detail=? WHERE id=?",
            (status, iso_now(), considered, len(promoted), len(demoted), json_dumps(detail), run_id),
        )
        return {"status": status, "considered": considered, "promoted": promoted, "demoted": demoted, "run_id": run_id}

    def dynamic_scan_symbols(self) -> tuple[list[str], list[str]]:
        if not self.enabled():
            return [], []
        paper = self.storage.fetch_all(
            "SELECT symbol FROM universe_symbols WHERE tier=? AND executable=1 ORDER BY score DESC, symbol LIMIT ?",
            (PAPER_TRADABLE, int(self.cfg.get("max_dynamic_paper_tradable_symbols", 12))),
        )
        obs = self.storage.fetch_all(
            "SELECT symbol FROM universe_symbols WHERE tier=? AND observation_only=1 ORDER BY score DESC, symbol LIMIT ?",
            (OBSERVATION, int(self.cfg.get("max_observation_symbols", 30))),
        )
        return [r["symbol"] for r in paper], [r["symbol"] for r in obs]

    def _is_due(self, run_type: str) -> bool:
        schedules = self.cfg.get("schedules", {})
        if run_type == "daily_deep_research" and not schedules.get("daily_deep_research_enabled", True):
            return False
        if run_type == "intraday_light_refresh" and not schedules.get("intraday_light_refresh_enabled", True):
            return False
        if run_type == "event_triggered_refresh" and not schedules.get("event_triggered_refresh_enabled", True):
            return False
        if run_type == "post_market_review" and not schedules.get("post_market_review_enabled", True):
            return False
        if run_type == "weekly_cleanup" and not schedules.get("weekly_cleanup_enabled", True):
            return False
        if run_type == "daily_deep_research":
            now_sgt = self.now.astimezone(SGT)
            configured = str(schedules.get("daily_deep_research_time_sgt", "20:30"))
            try:
                hour, minute = [int(part) for part in configured.split(":", 1)]
                due_time = time(hour=hour, minute=minute)
            except Exception:
                due_time = time(hour=20, minute=30)
            if now_sgt.time() < due_time:
                return False
            rows = self.storage.fetch_all(
                "SELECT started_at FROM universe_research_runs WHERE research_type=? AND status='completed' AND substr(started_at, 1, 10)=? LIMIT 1",
                (run_type, self.now.date().isoformat()),
            )
            return not bool(rows)
        if run_type == "intraday_light_refresh":
            minutes = int(schedules.get("intraday_light_refresh_minutes", 30))
            cutoff = self.now - timedelta(minutes=minutes)
        elif run_type == "event_triggered_refresh":
            cutoff = self.now - timedelta(minutes=30)
        elif run_type == "weekly_cleanup":
            cutoff = self.now - timedelta(days=7)
        else:
            cutoff = self.now - timedelta(hours=20)
        rows = self.storage.fetch_all(
            "SELECT started_at FROM universe_research_runs WHERE research_type=? AND status='completed' ORDER BY started_at DESC LIMIT 1",
            (run_type,),
        )
        if not rows:
            return True
        try:
            last = datetime.fromisoformat(rows[0]["started_at"].replace("Z", "+00:00")).astimezone(UTC)
        except Exception:
            return True
        return last <= cutoff

    def _collect_raw_candidates(self, run_type: str) -> list[dict[str, Any]]:
        max_raw = int(self.cfg.get("max_raw_symbols_per_research_run", 500))
        candidates: list[dict[str, Any]] = []
        if self.cfg.get("raw_sources", {}).get("existing_static_watchlist", True):
            for profile in self.config.get("market_profiles", {}).values():
                profile_active = profile.get("status") == "active" and profile.get("execution_enabled", False) and profile.get("proposals_enabled", False)
                for symbol in profile.get("watchlist", []):
                    source = "existing_static_watchlist" if profile_active else "existing_static_observation"
                    candidates.append(
                        {
                            "Code": str(symbol).upper(),
                            "Exchange": "US",
                            "Type": "ETF",
                            "source": source,
                            "existing_static": True,
                            "observation": not profile_active,
                        }
                    )
                for symbol in profile.get("observation_watchlist", []):
                    candidates.append({"Code": str(symbol).upper(), "Exchange": "US", "Type": "ETF", "source": "existing_static_observation", "existing_static": True, "observation": True})

        if self.provider and self.cfg.get("raw_sources", {}).get("eodhd_screener", True):
            res = self.provider.get_screener_results(limit=min(max_raw, 100))
            candidates.extend(self._rows_from_response(res, "eodhd_screener"))

        if self.provider and self.cfg.get("raw_sources", {}).get("eodhd_exchange_symbols", True) and run_type == "daily_deep_research":
            res = self.provider.list_symbols("US", limit=max_raw)
            candidates.extend(self._rows_from_response(res, "eodhd_exchange_symbols"))

        deduped: dict[str, dict[str, Any]] = {}
        for row in candidates:
            symbol = self._normalize_symbol(row)
            if symbol and symbol not in deduped:
                deduped[symbol] = row
        return list(deduped.values())[:max_raw]

    def _rows_from_response(self, response: ProviderResponse, source: str) -> list[dict[str, Any]]:
        if response.status != "ok" or not response.data:
            self.storage.execute(
                "INSERT INTO dynamic_universe_audit(id,run_id,event_type,symbol,detail,created_at) VALUES(?,?,?,?,?,?)",
                (str(uuid.uuid4()), self.run_id, "provider_unavailable", None, json_dumps({"source": source, "status": response.status, "error": response.error}), iso_now()),
            )
            return []
        rows = response.data if isinstance(response.data, list) else response.data.get("data", []) if isinstance(response.data, dict) else []
        return [{**row, "source": source} for row in rows if isinstance(row, dict)]

    def _normalize_symbol(self, info: dict[str, Any]) -> str | None:
        symbol = info.get("Code") or info.get("code") or info.get("symbol") or info.get("ticker")
        if not symbol:
            return None
        symbol = str(symbol).upper().strip()
        if "." in symbol and not symbol.endswith(".US"):
            return symbol
        return symbol.replace(".US", "")

    def _metadata(self, info: dict[str, Any]) -> dict[str, Any]:
        symbol = self._normalize_symbol(info) or ""
        raw_type = str(info.get("Type") or info.get("type") or info.get("asset_class") or "equity").lower()
        if "forex" in raw_type or "currency" in raw_type or info.get("Exchange") == "FOREX":
            asset_class = "forex"
        elif "crypto" in raw_type:
            asset_class = "crypto"
        elif "option" in raw_type:
            asset_class = "option"
        elif "bond" in raw_type:
            asset_class = "bond"
        elif "etf" in raw_type:
            asset_class = "etf"
        elif "fund" in raw_type:
            asset_class = "fund"
        elif "index" in raw_type:
            asset_class = "index"
        else:
            asset_class = "equity"
        cluster = self._infer_cluster(symbol, asset_class, info)
        return {
            "symbol": symbol,
            "provider_symbol": info.get("provider_symbol") or (f"{symbol}.US" if "." not in symbol else symbol),
            "exchange": info.get("Exchange") or info.get("exchange") or "US",
            "asset_class": asset_class,
            "sector": info.get("Sector") or info.get("sector"),
            "cluster": cluster,
            "region": info.get("Country") or info.get("country") or "US",
            "currency": info.get("Currency") or info.get("currency") or "USD",
            "source": info.get("source", "unknown"),
            "existing_static": bool(info.get("existing_static")),
            "observation": bool(info.get("observation")),
        }

    def _infer_cluster(self, symbol: str, asset_class: str, info: dict[str, Any]) -> str:
        configured = self.config.get("portfolio_optimizer", {}).get("clusters", {})
        for cluster, symbols in configured.items():
            if symbol.upper() in [str(s).upper() for s in symbols]:
                return cluster
        sector = str(info.get("Sector") or info.get("sector") or "").lower()
        if "semiconductor" in sector:
            return "semiconductors"
        if "financial" in sector:
            return "financials"
        if "energy" in sector:
            return "energy"
        if "health" in sector:
            return "healthcare"
        if asset_class in {"forex", "crypto", "index"}:
            return f"{asset_class}_macro"
        return "unknown_cluster"

    def _score_symbol(self, symbol: str, metadata: dict[str, Any]) -> ResearchScore:
        bars = []
        if self.provider and self.cfg.get("raw_sources", {}).get("eodhd_eod_bars", True) and not metadata.get("existing_static"):
            res = self.provider.get_historical_bars(metadata.get("provider_symbol") or symbol, limit=80)
            if res.status == "ok" and isinstance(res.data, list):
                bars = res.data
        liquidity, liquidity_block = self._liquidity_score(metadata, bars)
        trend = self._trend_score(bars)
        rel = self._relative_strength_score(bars)
        vol = self._volatility_quality_score(bars)
        news = self._news_score(symbol, metadata)
        sector = 5.0 if metadata.get("cluster") != "unknown_cluster" else 3.0
        quality = self._data_quality_score(metadata, bars)
        total = liquidity + trend + rel + vol + news + sector + quality
        block_reason = liquidity_block
        if quality < 2.0 and not metadata.get("existing_static"):
            block_reason = "missing or stale price data"
        return ResearchScore(symbol, min(100.0, total), liquidity, trend, rel, vol, news, sector, quality, block_reason)

    def _liquidity_score(self, metadata: dict[str, Any], bars: list[dict[str, Any]]) -> tuple[float, str | None]:
        if metadata.get("existing_static"):
            return 18.0, None
        if not bars:
            return 0.0, "missing liquidity data"
        closes = [float(b.get("close") or b.get("adjusted_close") or 0) for b in bars[-20:]]
        vols = [float(b.get("volume") or 0) for b in bars[-20:]]
        price = closes[-1] if closes else 0.0
        avg_vol = sum(vols) / len(vols) if vols else 0.0
        dollar_vol = price * avg_vol
        min_price = float(self.cfg.get("min_price", 5.0))
        min_vol = float(self.cfg.get("min_avg_daily_volume", 1_000_000))
        min_dollar = float(self.cfg.get("min_dollar_volume", 10_000_000))
        if price < min_price:
            return 0.0, "price below minimum"
        if avg_vol < min_vol or dollar_vol < min_dollar:
            return 4.0, "liquidity below minimum"
        return min(20.0, 8.0 + min(6.0, avg_vol / min_vol * 3.0) + min(6.0, dollar_vol / min_dollar * 3.0)), None

    def _trend_score(self, bars: list[dict[str, Any]]) -> float:
        closes = [float(b.get("close") or b.get("adjusted_close") or 0) for b in bars if float(b.get("close") or b.get("adjusted_close") or 0) > 0]
        if len(closes) < 50:
            return 10.0 if closes else 0.0
        ma20 = sum(closes[-20:]) / 20
        ma50 = sum(closes[-50:]) / 50
        latest = closes[-1]
        score = 5.0
        if latest > ma20:
            score += 5.0
        if ma20 > ma50:
            score += 5.0
        if latest > ma50:
            score += 5.0
        return min(20.0, score)

    def _relative_strength_score(self, bars: list[dict[str, Any]]) -> float:
        closes = [float(b.get("close") or b.get("adjusted_close") or 0) for b in bars if float(b.get("close") or b.get("adjusted_close") or 0) > 0]
        if len(closes) < 20:
            return 7.5 if closes else 0.0
        ret20 = closes[-1] / closes[-20] - 1.0
        return max(0.0, min(15.0, 7.5 + ret20 * 100))

    def _volatility_quality_score(self, bars: list[dict[str, Any]]) -> float:
        closes = [float(b.get("close") or b.get("adjusted_close") or 0) for b in bars if float(b.get("close") or b.get("adjusted_close") or 0) > 0]
        if len(closes) < 20:
            return 7.5 if closes else 0.0
        returns = [(closes[i] / closes[i - 1] - 1.0) for i in range(1, len(closes))]
        mean = sum(returns) / len(returns)
        variance = sum((r - mean) ** 2 for r in returns) / len(returns)
        daily_vol = math.sqrt(variance)
        if daily_vol > 0.08:
            return 0.0
        if daily_vol < 0.002:
            return 5.0
        return 15.0

    def _news_score(self, symbol: str, metadata: dict[str, Any]) -> float:
        if not self.provider or not self.cfg.get("raw_sources", {}).get("eodhd_news", True) or metadata.get("existing_static"):
            return 7.5
        res = self.provider.get_news(symbol=symbol, limit=5)
        if res.status != "ok" or not isinstance(res.data, list):
            return 7.5
        return min(15.0, 7.5 + len(res.data) * 1.5)

    def _data_quality_score(self, metadata: dict[str, Any], bars: list[dict[str, Any]]) -> float:
        if metadata.get("existing_static"):
            return 5.0
        if len(bars) >= 50:
            return 5.0
        if len(bars) >= 20:
            return 3.0
        return 0.0

    def _decide_tier(self, symbol: str, metadata: dict[str, Any], score: ResearchScore) -> str:
        if metadata.get("existing_static"):
            return OBSERVATION if metadata.get("observation") else PAPER_TRADABLE
        if score.block_reason:
            return RAW_UNIVERSE
        promo = self.cfg.get("promotion", {})
        if score.total_score < float(promo.get("min_research_score", 75)):
            return RAW_UNIVERSE
        current = self._current_symbol(symbol)
        if not current or current.get("tier") == RAW_UNIVERSE:
            return RESEARCH_CANDIDATE
        if current.get("tier") == RESEARCH_CANDIDATE:
            return OBSERVATION
        if current.get("tier") == OBSERVATION:
            cycles = self._score_count(symbol)
            sessions = self._session_count(symbol)
            has_shadow = self._has_shadow_tracking(symbol)
            if (
                cycles >= int(promo.get("min_observation_cycles", 3))
                and sessions >= int(promo.get("min_observation_sessions", 1))
                and has_shadow
                and metadata.get("cluster") != "unknown_cluster"
            ):
                return PAPER_TRADABLE
            return OBSERVATION
        return current.get("tier") or RESEARCH_CANDIDATE

    def _demote_stale_symbols(self, demoted: list[str]) -> None:
        demotion = self.cfg.get("demotion", {})
        max_weak = int(demotion.get("max_weak_cycles", 5))
        rows = self.storage.fetch_all(
            """
            SELECT symbol, tier, source
            FROM universe_symbols
            WHERE tier IN (?, ?, ?)
              AND COALESCE(source, '') NOT IN ('existing_static_watchlist', 'existing_static_observation')
            """,
            (RESEARCH_CANDIDATE, OBSERVATION, PAPER_TRADABLE),
        )
        for row in rows:
            symbol = row["symbol"]
            recent = self.storage.fetch_all(
                "SELECT score FROM symbol_research_scores WHERE symbol=? ORDER BY created_at DESC LIMIT ?",
                (symbol, max_weak),
            )
            if len(recent) >= max_weak and all(float(r["score"] or 0) < 50.0 for r in recent):
                self._upsert_universe_symbol(symbol, self._current_symbol(symbol) or {}, DEMOTED, executable=0, observation_only=1)
                self._record_membership(symbol, row["tier"], DEMOTED, "repeated weak research score", self._current_symbol(symbol) or {})
                self._record_demotion(symbol, row["tier"], None, self._current_symbol(symbol) or {}, "repeated weak research score")
                demoted.append(symbol)

    def _asset_execution_allowed(self, asset_class: str | None) -> bool:
        allowed = self.cfg.get("execution_allowed_asset_classes", {})
        normalized = str(asset_class or "").lower()
        key_map = {
            "stock": "equities",
            "common stock": "equities",
            "equity": "equities",
            "equities": "equities",
            "etf": "etfs",
            "etfs": "etfs",
            "fund": "funds",
            "funds": "funds",
            "index": "indices",
            "indices": "indices",
            "forex": "forex",
            "currency": "forex",
            "crypto": "crypto",
            "option": "options",
            "options": "options",
            "bond": "bonds",
            "bonds": "bonds",
        }
        return bool(allowed.get(key_map.get(normalized, normalized), False))

    def _current_symbol(self, symbol: str) -> dict[str, Any] | None:
        rows = self.storage.fetch_all("SELECT * FROM universe_symbols WHERE symbol=?", (symbol.upper(),))
        return rows[0] if rows else None

    def _score_count(self, symbol: str) -> int:
        return int(self.storage.fetch_all("SELECT COUNT(*) c FROM symbol_research_scores WHERE symbol=?", (symbol.upper(),))[0]["c"])

    def _session_count(self, symbol: str) -> int:
        return int(self.storage.fetch_all("SELECT COUNT(DISTINCT substr(created_at, 1, 10)) c FROM symbol_research_scores WHERE symbol=?", (symbol.upper(),))[0]["c"])

    def _has_shadow_tracking(self, symbol: str) -> bool:
        return bool(self.storage.fetch_all("SELECT 1 FROM shadow_trades WHERE symbol=? LIMIT 1", (symbol.upper(),)))

    def _upsert_universe_symbol(self, symbol: str, metadata: dict[str, Any], tier: str, executable: int, observation_only: int, score: float | None = None) -> None:
        now = iso_now()
        self.storage.execute(
            """
            INSERT INTO universe_symbols(
                id,symbol,provider_symbol,exchange,asset_class,country,region,currency,sector,cluster,tier,state,
                executable,observation_only,score,reason,source,provider,data_quality,last_seen_at,last_promoted_at,last_demoted_at,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(symbol) DO UPDATE SET
                provider_symbol=excluded.provider_symbol,
                exchange=excluded.exchange,
                asset_class=excluded.asset_class,
                country=excluded.country,
                region=excluded.region,
                currency=excluded.currency,
                sector=excluded.sector,
                cluster=excluded.cluster,
                tier=excluded.tier,
                state=excluded.state,
                executable=excluded.executable,
                observation_only=excluded.observation_only,
                score=excluded.score,
                reason=excluded.reason,
                source=excluded.source,
                provider=excluded.provider,
                data_quality=excluded.data_quality,
                last_seen_at=excluded.last_seen_at,
                last_promoted_at=COALESCE(excluded.last_promoted_at, universe_symbols.last_promoted_at),
                last_demoted_at=COALESCE(excluded.last_demoted_at, universe_symbols.last_demoted_at),
                updated_at=excluded.updated_at
            """,
            (
                str(uuid.uuid4()),
                symbol.upper(),
                metadata.get("provider_symbol"),
                metadata.get("exchange"),
                metadata.get("asset_class"),
                metadata.get("region"),
                metadata.get("region"),
                metadata.get("currency"),
                metadata.get("sector"),
                metadata.get("cluster"),
                tier,
                tier,
                executable,
                observation_only,
                score,
                metadata.get("reason"),
                metadata.get("source"),
                self.cfg.get("provider", "eodhd"),
                "ok" if score is not None else "seed",
                now,
                now if tier == PAPER_TRADABLE else None,
                now if tier == DEMOTED else None,
                now,
                now,
            ),
        )

    def _record_membership(self, symbol: str, old_tier: str | None, new_tier: str, reason: str, metadata: dict[str, Any]) -> None:
        self.storage.execute(
            "INSERT INTO universe_membership_history(id,run_id,symbol,old_tier,new_tier,reason,source,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), self.run_id, symbol.upper(), old_tier, new_tier, reason, metadata.get("source"), iso_now()),
        )

    def _record_score(self, score: ResearchScore, metadata: dict[str, Any]) -> None:
        self.storage.execute(
            """
            INSERT INTO symbol_research_scores(
                id,run_id,symbol,provider,score,liquidity_score,trend_score,relative_strength_score,
                volatility_quality_score,news_score,sector_theme_score,data_quality_score,block_reason,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                str(uuid.uuid4()), self.run_id, score.symbol.upper(), self.cfg.get("provider", "eodhd"), score.total_score,
                score.liquidity_score, score.trend_score, score.relative_strength_score, score.volatility_quality_score,
                score.news_score, score.sector_theme_score, score.data_quality_score, score.block_reason, iso_now(),
            ),
        )

    def _record_trend_snapshot(self, symbol: str, metadata: dict[str, Any], score: ResearchScore) -> None:
        self.storage.execute(
            "INSERT INTO symbol_trend_snapshots(id,run_id,symbol,trend_score,relative_strength_score,volatility_quality_score,cluster,created_at,payload) VALUES(?,?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), self.run_id, symbol.upper(), score.trend_score, score.relative_strength_score, score.volatility_quality_score, metadata.get("cluster"), iso_now(), json_dumps(metadata)),
        )
        self.storage.execute(
            "INSERT INTO sector_regime_snapshots(id,run_id,sector,cluster,score,reason,created_at) VALUES(?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), self.run_id, metadata.get("sector"), metadata.get("cluster"), score.sector_theme_score, "symbol research update", iso_now()),
        )

    def _record_news(self, symbol: str, metadata: dict[str, Any]) -> None:
        self.storage.execute(
            "INSERT INTO symbol_news_events(id,run_id,symbol,provider,event_time,headline,sentiment,source,url,relevance_score,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), self.run_id, symbol.upper(), self.cfg.get("provider", "eodhd"), iso_now(), None, "neutral", metadata.get("source"), None, None, iso_now()),
        )

    def _record_promotion(self, symbol: str, old_tier: str | None, new_tier: str, score: ResearchScore, metadata: dict[str, Any]) -> None:
        self.storage.execute(
            "INSERT INTO symbol_promotion_decisions(id,run_id,symbol,from_tier,to_tier,score,reason,deterministic_pass,gpt_summary_used,created_at,payload) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), self.run_id, symbol.upper(), old_tier, new_tier, score.total_score, score.block_reason or "deterministic promotion rule", 1, 0, iso_now(), json_dumps(metadata)),
        )

    def _record_demotion(self, symbol: str, old_tier: str | None, score: ResearchScore | None, metadata: dict[str, Any], reason: str | None = None) -> None:
        self.storage.execute(
            "INSERT INTO symbol_demotion_decisions(id,run_id,symbol,from_tier,to_tier,score,reason,created_at,payload) VALUES(?,?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), self.run_id, symbol.upper(), old_tier, DEMOTED, score.total_score if score else None, reason or (score.block_reason if score else "demotion rule"), iso_now(), json_dumps(metadata)),
        )
