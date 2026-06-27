from __future__ import annotations

import math
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.data_providers.base import MarketResearchProvider, ProviderResponse
from app.internet import internet_available
from app.power import get_power_status
from app.storage import Storage
from app.utils import iso_now, json_dumps
from app.broker_interface import BrokerInterface

RAW_UNIVERSE = "raw_universe"
RESEARCH_CANDIDATE = "research_candidate"
OBSERVATION = "observation"
PAPER_TRADABLE = "paper_tradable"
DEMOTED = "demoted"
LANE_ALPACA_US = "alpaca_compatible_us"
LANE_GLOBAL_RESEARCH = "global_research_only"
LANE_EXCLUDED = "excluded_or_low_quality"
PATH_FULL_FRESH = "full_fresh_data"
PATH_CACHED_INTRADAY = "cached_intraday"
PATH_ALPACA_QUOTE_FALLBACK = "alpaca_quote_fallback"
PATH_EOD_ONLY_CLOSED = "eod_only_market_closed"
PATH_NONE = "none"
SGT = ZoneInfo("Asia/Singapore")


@dataclass(frozen=True)
class ResearchScore:
    symbol: str
    total_score: float
    liquidity_score: float
    trend_score: float
    intraday_momentum_score: float
    relative_strength_score: float
    volatility_quality_score: float
    screener_mover_score: float
    news_score: float
    sector_theme_score: float
    data_quality_score: float
    data_confidence: str
    data_confidence_reason: str
    universe_lane: str = LANE_ALPACA_US
    existing_static: bool = False
    block_reason: str | None = None


@dataclass(frozen=True)
class ResearchGate:
    allowed: bool
    reason: str | None
    provider_health_status: str
    internet_status: str
    power_status: str
    battery_pct: float | None
    promotion_allowed: bool
    demotion_allowed: bool
    data_freshness_status: str


class DynamicUniverseEngine:
    def __init__(self, config: dict[str, Any], storage: Storage, provider: MarketResearchProvider | None, run_id: str, broker: BrokerInterface | None = None) -> None:
        self.config = config
        self.storage = storage
        self.provider = provider
        self.run_id = run_id
        self.broker = broker
        self.cfg = config.get("dynamic_universe", {})
        self.resilience_cfg = config.get("dynamic_universe_resilience", {})
        self.now = datetime.now(UTC)
        self._promotion_allowed = True
        self._demotion_allowed = True
        self._provider_failed = False
        self._provider_health_status = "unknown"
        self._data_freshness_status = "fresh"
        self._last_score_candidates: list[ResearchScore] = []

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
        catchups_run = 0
        catchup_cfg = self.resilience_cfg.get("catchup_policy", {})
        max_catchups = int(catchup_cfg.get("max_catchup_runs_per_scanner_cycle", 2))
        for run_type in run_types:
            due = force or self._is_due(run_type)
            catchup_required = self._catchup_required(run_type)
            if not due and not catchup_required:
                continue
            if catchup_required and not self._catchup_allowed(run_type):
                self._record_schedule_skip(run_type, "catchup_not_allowed", catchup_required=True)
                results.append({"status": "skipped", "reason": "catchup_not_allowed", "run_type": run_type})
                continue
            if catchup_required and catchups_run >= max_catchups:
                self._record_schedule_skip(run_type, "catchup_limit_reached", catchup_required=True)
                results.append({"status": "skipped", "reason": "catchup_limit_reached", "run_type": run_type})
                continue
            gate = self._research_gate(run_type, is_catchup=catchup_required)
            self._record_schedule_due(run_type, gate)
            if not gate.allowed:
                self._record_schedule_skip(run_type, gate.reason or "research_gate_blocked", gate, catchup_required=True)
                self._mark_dynamic_symbols_stale(gate.reason or "research skipped", gate)
                results.append({"status": "skipped", "reason": gate.reason, "run_type": run_type})
                continue
            if catchup_required:
                catchups_run += 1
                self._record_catchup_started(run_type, gate)
            result = self.run_research_cycle(run_type, is_catchup=catchup_required, gate=gate)
            results.append(result)
        return results

    def run_research_cycle(self, run_type: str = "daily_deep_research", is_catchup: bool = False, gate: ResearchGate | None = None) -> dict[str, Any]:
        gate = gate or ResearchGate(True, None, "ok", "online", "ac", None, True, True, "fresh")
        self._promotion_allowed = gate.promotion_allowed
        self._demotion_allowed = gate.demotion_allowed
        self._provider_failed = False
        self._provider_health_status = gate.provider_health_status
        self._data_freshness_status = gate.data_freshness_status
        run_id = str(uuid.uuid4())
        now_iso = self.now.isoformat()
        self._record_audit("dynamic_universe_research_started", None, {"run_type": run_type, "catchup": is_catchup})
        self._upsert_schedule_state(
            run_type,
            {
                "last_started_at": now_iso,
                "provider_health_status": gate.provider_health_status,
                "internet_status": gate.internet_status,
                "power_status": gate.power_status,
                "battery_pct": gate.battery_pct,
                "promotion_allowed": 1 if gate.promotion_allowed else 0,
                "demotion_allowed": 1 if gate.demotion_allowed else 0,
                "data_freshness_status": gate.data_freshness_status,
                "notes": json_dumps({"catchup": is_catchup}),
            },
        )
        self.storage.execute(
            "INSERT INTO universe_research_runs(id,run_id,research_type,provider,status,started_at,ended_at,symbols_considered,symbols_promoted,symbols_demoted,detail) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, self.run_id, run_type, self.cfg.get("provider", "eodhd"), "running", now_iso, None, 0, 0, 0, "{}"),
        )

        promoted = []
        demoted = []
        brief_items: list[tuple[ResearchScore, dict[str, Any]]] = []
        self._backfill_unclassified_universe_symbols()
        candidates = self._collect_raw_candidates(run_type)
        considered = 0
        self._last_score_candidates = []
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
                self._last_score_candidates.append(score)
                self._record_score(score, metadata)
                if not metadata.get("existing_static") and (score.block_reason or score.total_score < self._research_threshold()):
                    self._record_candidate_block(score, metadata)
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
                if new_tier == RESEARCH_CANDIDATE:
                    brief_items.append((score, dict(metadata)))
                self._record_trend_snapshot(symbol, metadata, score)
                self._record_news(symbol, metadata)
            for rank, (score, metadata) in enumerate(sorted(brief_items, key=lambda item: item[0].total_score, reverse=True), start=1):
                self._record_candidate_brief(score, metadata, rank, run_type)
            self._record_llm_explanation_usage(brief_items)
            if self._demotion_allowed and not self._provider_failed:
                self._demote_stale_symbols(demoted)
            elif self._provider_failed:
                self._record_audit("dynamic_universe_demotions_blocked_provider_unavailable", None, {"run_type": run_type})
            self.review_observation_maturity(run_type=run_type, fetch_provider=False)
            status = "completed"
            self._provider_health_status = self._provider_summary_status()
            self._record_near_miss_symbols()
            detail = {"provider_status": self._provider_health_status, "run_type": run_type, "catchup": is_catchup, "candidate_briefs": len(brief_items)}
        except Exception as exc:
            status = "error"
            detail = {"error": type(exc).__name__, "run_type": run_type}
            self._record_audit("dynamic_universe_error", None, detail)
        self.storage.execute(
            "UPDATE universe_research_runs SET status=?, ended_at=?, symbols_considered=?, symbols_promoted=?, symbols_demoted=?, detail=? WHERE id=?",
            (status, iso_now(), considered, len(promoted), len(demoted), json_dumps(detail), run_id),
        )
        self._record_schedule_completed(run_type, status, gate, is_catchup)
        self._record_audit(
            "dynamic_universe_catchup_completed" if is_catchup else "dynamic_universe_research_completed",
            None,
            {"run_type": run_type, "status": status, "promoted": promoted, "demoted": demoted},
        )
        return {"status": status, "considered": considered, "promoted": promoted, "demoted": demoted, "run_id": run_id, "run_type": run_type, "candidate_briefs": len(brief_items), "catchup": is_catchup}

    def dynamic_scan_symbols(self) -> tuple[list[str], list[str]]:
        if not self.enabled():
            return [], []
        max_stale = int(self.resilience_cfg.get("stale_data_policy", {}).get("max_age_minutes_for_trade_eligibility", 30))
        freshness_cutoff = (self.now - timedelta(minutes=max_stale)).isoformat()
        paper = self.storage.fetch_all(
            """
            SELECT symbol
            FROM universe_symbols
            WHERE tier=?
              AND executable=1
              AND (
                COALESCE(source, '')='existing_static_watchlist'
                OR COALESCE(last_successful_research_at, last_seen_at, updated_at) >= ?
              )
            ORDER BY score DESC, symbol
            LIMIT ?
            """,
            (PAPER_TRADABLE, freshness_cutoff, int(self.cfg.get("max_dynamic_paper_tradable_symbols", 12))),
        )
        obs = self.storage.fetch_all(
            """
            SELECT symbol
            FROM universe_symbols
            WHERE tier=?
              AND observation_only=1
              AND exchange='US'
              AND COALESCE(universe_lane, 'alpaca_compatible_us')='alpaca_compatible_us'
              AND symbol NOT LIKE '%.%'
              AND asset_class IN ('equity','etf')
            ORDER BY score DESC, symbol
            LIMIT ?
            """,
            (OBSERVATION, int(self.cfg.get("max_observation_symbols", 30))),
        )
        return [r["symbol"] for r in paper], [r["symbol"] for r in obs]

    def generate_current_research_candidate_briefs(self, run_type: str = "report_backfill") -> int:
        """Create deterministic brief rows for current research candidates without provider calls."""
        rows = self.storage.fetch_all(
            """
            SELECT u.*, s.score AS latest_score, s.liquidity_score, s.trend_score, s.intraday_momentum_score,
                   s.relative_strength_score, s.volatility_quality_score, s.screener_mover_score,
                   s.news_score, s.sector_theme_score, s.data_quality_score, s.data_confidence AS score_confidence,
                   s.data_confidence_reason AS score_confidence_reason, s.universe_lane AS score_lane, s.block_reason
            FROM universe_symbols u
            LEFT JOIN (
                SELECT s1.*
                FROM symbol_research_scores s1
                INNER JOIN (
                    SELECT symbol, MAX(created_at) AS max_created_at
                    FROM symbol_research_scores
                    GROUP BY symbol
                ) latest ON latest.symbol=s1.symbol AND latest.max_created_at=s1.created_at
            ) s ON s.symbol=u.symbol
            WHERE u.tier=?
            ORDER BY COALESCE(s.score, u.score) DESC, u.symbol
            """,
            (RESEARCH_CANDIDATE,),
        )
        count = 0
        for rank, row in enumerate(rows, start=1):
            metadata = {
                "symbol": row.get("symbol"),
                "provider_symbol": row.get("provider_symbol"),
                "exchange": row.get("exchange"),
                "asset_class": row.get("asset_class"),
                "sector": row.get("sector"),
                "cluster": row.get("cluster"),
                "region": row.get("region"),
                "currency": row.get("currency"),
                "source": row.get("source"),
                "universe_lane": row.get("universe_lane") or row.get("score_lane") or LANE_ALPACA_US,
                "alpaca_compatible": row.get("alpaca_compatible"),
                "exclusion_reason": row.get("exclusion_reason"),
                "data_confidence": row.get("data_confidence") or row.get("score_confidence"),
                "data_confidence_reason": row.get("data_confidence_reason") or row.get("score_confidence_reason"),
                "endpoint_coverage": self._current_endpoint_coverage(),
                "price_freshness": row.get("data_freshness_status") or "latest stored research score",
                "local_metrics_available": {
                    "ma20": False,
                    "ma50": False,
                    "ma200": False,
                    "rsi": False,
                    "atr": False,
                    "relative_strength": row.get("relative_strength_score") is not None,
                    "liquidity_dollar_volume": False,
                    "volatility": row.get("volatility_quality_score") is not None,
                },
            }
            score = ResearchScore(
                symbol=str(row.get("symbol")).upper(),
                total_score=float(row.get("latest_score") or row.get("score") or 0.0),
                liquidity_score=float(row.get("liquidity_score") or 0.0),
                trend_score=float(row.get("trend_score") or 0.0),
                intraday_momentum_score=float(row.get("intraday_momentum_score") or 0.0),
                relative_strength_score=float(row.get("relative_strength_score") or 0.0),
                volatility_quality_score=float(row.get("volatility_quality_score") or 0.0),
                screener_mover_score=float(row.get("screener_mover_score") or 0.0),
                news_score=float(row.get("news_score") or 0.0),
                sector_theme_score=float(row.get("sector_theme_score") or 0.0),
                data_quality_score=float(row.get("data_quality_score") or 0.0),
                data_confidence=str(row.get("data_confidence") or row.get("score_confidence") or "unknown"),
                data_confidence_reason=str(row.get("data_confidence_reason") or row.get("score_confidence_reason") or "latest stored research score"),
                universe_lane=str(row.get("universe_lane") or row.get("score_lane") or LANE_ALPACA_US),
                block_reason=row.get("block_reason"),
            )
            self._record_candidate_brief(score, metadata, rank, run_type)
            count += 1
        self._record_llm_explanation_usage([])
        return count

    def review_observation_maturity(self, symbols: list[str] | None = None, run_type: str = "observation_maturity_review", fetch_provider: bool = False) -> int:
        review_cfg = self.cfg.get("observation_maturity_review", {})
        if not review_cfg.get("enabled", True):
            return 0
        params: tuple[Any, ...]
        if symbols:
            placeholders = ",".join("?" for _ in symbols)
            where = f"WHERE upper(symbol) IN ({placeholders})"
            params = tuple(s.upper() for s in symbols)
        else:
            where = "WHERE tier IN (?, ?)"
            params = (OBSERVATION, PAPER_TRADABLE)
        rows = self.storage.fetch_all(
            f"""
            SELECT *
            FROM universe_symbols
            {where}
            ORDER BY
                CASE WHEN symbol LIKE 'XL%' THEN 0 ELSE 1 END,
                tier,
                score DESC,
                symbol
            LIMIT ?
            """,
            (*params, int(review_cfg.get("max_symbols_per_review", self.cfg.get("max_observation_symbols", 30)))),
        )
        reviewed = 0
        for row in rows:
            self._record_stage_review(row, run_type, fetch_provider=fetch_provider)
            reviewed += 1
        if reviewed:
            self._record_audit("dynamic_universe_maturity_review_completed", None, {"run_type": run_type, "reviewed": reviewed, "fetch_provider": fetch_provider})
        return reviewed

    def _record_stage_review(self, row: dict[str, Any], run_type: str, fetch_provider: bool = False) -> None:
        symbol = str(row.get("symbol") or "").upper()
        now = iso_now()
        review_cfg = self.cfg.get("observation_maturity_review", {})
        promo = self.cfg.get("promotion", {})
        interval = int(review_cfg.get("review_interval_minutes", 30))
        next_review = (datetime.now(UTC) + timedelta(minutes=interval)).isoformat()
        score_rows = self.storage.fetch_all(
            "SELECT * FROM symbol_research_scores WHERE symbol=? ORDER BY created_at DESC LIMIT 20",
            (symbol,),
        )
        latest_score = score_rows[0] if score_rows else {}
        score = float(latest_score.get("score") if latest_score.get("score") is not None else row.get("score") or 0.0)
        confidence = str(row.get("data_confidence") or latest_score.get("data_confidence") or "unknown")
        cycles = len(score_rows)
        sessions = len({str(r.get("created_at") or "")[:10] for r in score_rows if r.get("created_at")})
        market_open_refreshes = int(self.storage.fetch_all(
            "SELECT COUNT(DISTINCT run_id) c FROM market_memory WHERE symbol=?",
            (symbol,),
        )[0]["c"])
        position_symbols = self._latest_position_symbols()
        cluster = row.get("cluster") or self._infer_cluster(symbol, row.get("asset_class"), row)
        cluster_holdings = self._cluster_holding_symbols(cluster, position_symbols)
        source = str(row.get("source") or "")
        is_static_observation = source == "existing_static_observation"
        is_global = row.get("universe_lane") != LANE_ALPACA_US
        metrics = self._stage_review_metrics(row, fetch_provider)
        fallback = self._promotion_freshness_decision(symbol, row, latest_score, metrics, fetch_provider)
        provider_guard_for_demotion = bool(
            self._provider_failed
            or row.get("provider_health_status") in {"provider_unavailable", "rate_limited"}
            or metrics.get("provider_guard_active")
        )
        provider_guard_for_promotion = bool(
            self._provider_failed
            or row.get("provider_health_status") in {"provider_unavailable", "rate_limited"}
            or (metrics.get("provider_guard_active") and fallback["path"] == PATH_NONE)
        )
        demotion_guard = provider_guard_for_demotion
        eod_available = bool(metrics.get("eod_available"))
        intraday_available = bool(metrics.get("intraday_available"))
        liquidity_ok = float(latest_score.get("liquidity_score") or 0.0) >= 12.0 or row.get("source") == "existing_static_observation"
        trend_ok = float(latest_score.get("trend_score") or 0.0) >= 14.0
        rel_ok = float(latest_score.get("relative_strength_score") or 0.0) >= 8.5
        volatility_ok = float(latest_score.get("volatility_quality_score") or 0.0) >= 5.0
        requirements = {
            "alpaca_compatible_us_lane": not is_global,
            "not_static_observation_placeholder": not is_static_observation,
            "score_at_or_above_paper_tradable_threshold": score >= self._paper_tradable_threshold(),
            "observation_cycles": cycles >= int(review_cfg.get("min_observation_cycles", promo.get("min_observation_cycles", 3))),
            "market_open_refreshes": market_open_refreshes >= int(review_cfg.get("min_market_open_refreshes", 2)),
            "fresh_eod_available": eod_available or not review_cfg.get("require_fresh_eod", True),
            "promotion_freshness_path": fallback["path"] != PATH_NONE,
            "data_confidence_medium_or_high": confidence in {"medium", "high"},
            "liquidity_ok": liquidity_ok,
            "trend_ok": trend_ok,
            "relative_strength_ok": rel_ok,
            "volatility_ok": volatility_ok,
            "cluster_clearance": not cluster_holdings or not review_cfg.get("require_cluster_clearance", True),
            "provider_guard_clear": not provider_guard_for_promotion,
        }
        missing = [key for key, passed in requirements.items() if not passed]
        met = [key for key, passed in requirements.items() if passed]
        demotion_risks: list[str] = []
        if score < float(self.cfg.get("demotion", {}).get("demote_if_score_below", 45)):
            demotion_risks.append("score below demotion threshold")
        if not eod_available and row.get("data_freshness_status") == "stale":
            demotion_risks.append("stale price data")
        if latest_score and float(latest_score.get("trend_score") or 0.0) < 5.0:
            demotion_risks.append("weak trend score")
        if is_global:
            demotion_risks.append("global research-only lane")
        if row.get("tier") == PAPER_TRADABLE and demotion_guard:
            demotion_risks.append("demotion paused by provider guard")
        if row.get("tier") == PAPER_TRADABLE:
            decision = "keep_paper_tradable"
            reason = "paper-tradable eligibility retained; proposal still requires setup, RiskEngine, Telegram approval, and final validation"
            tradable = "tradable"
            proposal_allowed = "blocked_pending_setup_and_risk"
            proposal_block = "proposal requires current ENTRY setup, RiskEngine pass, cluster/exposure clearance, Telegram approval, and final validation"
        elif not missing:
            decision = "promote_to_dynamic_paper_tradable"
            reason = f"maturity review requirements passed using {fallback['path']}; deterministic promotion loop may record paper-tradable promotion"
            tradable = "not_tradable_until_promoted"
            proposal_allowed = "no"
            proposal_block = "proposal blocked until fresh market validation, ENTRY setup, RiskEngine, Telegram approval, and final validation pass"
        elif demotion_risks and not demotion_guard:
            decision = "keep_observation_with_demotion_risk"
            reason = "; ".join(demotion_risks)
            tradable = "not_tradable"
            proposal_allowed = "no"
            proposal_block = "observation-only; demotion risk under review"
        else:
            decision = "keep_observation"
            reason = self._review_reason_from_missing(missing, cluster_holdings)
            tradable = "not_tradable"
            proposal_allowed = "no"
            proposal_block = "observation-only; needs paper-tradable promotion before any proposal"
        if demotion_guard and "provider guard active" not in reason:
            reason = f"provider guard active; {reason}"
        if symbol in set(str(s).upper() for s in review_cfg.get("xl_sector_symbols", [])) and decision.startswith("keep_observation"):
            reason = f"XL-sector ETF remains observation-only: {reason}"
        payload = {
            "requirements": requirements,
            "position_symbols": position_symbols,
            "score_history": [r.get("score") for r in score_rows[:10]],
            "source": source,
            "run_type": run_type,
            "provider_fetch_attempted": fetch_provider,
            "metrics": metrics,
            "promotion_freshness_path": fallback["path"],
            "confidence_adjustment": fallback["confidence_adjustment"],
            "data_limitations": fallback["data_limitations"],
            "proposal_allowed": "no",
            "proposal_block_reason": proposal_block,
            "next_review_time": next_review,
            "safety": "review-only; no proposals or orders created",
        }
        self.storage.execute(
            """
            INSERT INTO dynamic_universe_stage_reviews(
                id,run_id,symbol,current_tier,review_type,decision,reason,score,data_confidence,
                observation_since,observation_cycles,market_open_refreshes,latest_price,price_freshness,
                eod_available,intraday_available,trend_summary,intraday_summary,liquidity_summary,
                volatility_summary,relative_strength_spy,relative_strength_qqq,cluster,cluster_exposure_blocker,
                promotion_requirements_met,promotion_requirements_missing,demotion_risk_reasons,demotion_guard_active,
                current_stage_reason,next_stage_blocker,tradable_status,proposal_allowed_status,proposal_block_reason,
                last_promotion_review_at,last_demotion_review_at,next_promotion_review_at,next_demotion_review_at,created_at,payload,
                promotion_freshness_path,promotion_confidence_adjustment,promotion_data_limitations,proposal_block_reason_after_promotion,
                fallback_used,next_review_time,alpaca_quote_freshness,alpaca_tradability_result,intraday_freshness,eod_freshness
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                str(uuid.uuid4()), self.run_id, symbol, row.get("tier"), run_type, decision, reason, score, confidence,
                row.get("created_at"), cycles, market_open_refreshes, metrics.get("latest_price"), metrics.get("price_freshness"),
                1 if eod_available else 0, 1 if intraday_available else 0, metrics.get("trend_summary"), metrics.get("intraday_summary"),
                metrics.get("liquidity_summary"), metrics.get("volatility_summary"), metrics.get("relative_strength_spy"),
                metrics.get("relative_strength_qqq"), cluster, ", ".join(cluster_holdings),
                json_dumps(met), json_dumps(missing), json_dumps(demotion_risks), 1 if demotion_guard else 0,
                f"{row.get('tier')} from {source or 'unknown source'}", reason, tradable, proposal_allowed, proposal_block,
                now, now, next_review, next_review, now, json_dumps(payload),
                fallback["path"], fallback["confidence_adjustment"], json_dumps(fallback["data_limitations"]),
                proposal_block, fallback["fallback_used"], next_review, fallback["alpaca_quote_freshness"],
                fallback["alpaca_tradability_result"], fallback["intraday_freshness"], fallback["eod_freshness"],
            ),
        )

    def _review_reason_from_missing(self, missing: list[str], cluster_holdings: list[str]) -> str:
        if not missing:
            return "requirements satisfied"
        ordered_missing = list(missing)
        if "provider_guard_clear" in ordered_missing:
            ordered_missing.remove("provider_guard_clear")
            ordered_missing.insert(0, "provider_guard_clear")
        labels = {
            "not_static_observation_placeholder": "static observation profile is not dynamic promotion evidence",
            "score_at_or_above_paper_tradable_threshold": "score below paper-tradable threshold",
            "observation_cycles": "not enough observation cycles",
            "market_open_refreshes": "not enough market-open observation refreshes",
            "shadow_tracking_recorded": "no shadow-tracking record yet",
            "fresh_eod_available": "fresh EOD evidence unavailable",
            "promotion_freshness_path": "no valid promotion freshness path",
            "cluster_clearance": f"cluster overlap with held symbols {', '.join(cluster_holdings)}" if cluster_holdings else "cluster clearance missing",
            "provider_guard_clear": "provider guard active",
            "trend_ok": "trend confirmation missing",
            "relative_strength_ok": "relative strength confirmation missing",
            "liquidity_ok": "liquidity confirmation missing",
            "volatility_ok": "volatility quality not acceptable",
        }
        return "; ".join(labels.get(key, key) for key in ordered_missing[:6])

    def _promotion_freshness_decision(
        self,
        symbol: str,
        row: dict[str, Any],
        latest_score: dict[str, Any],
        metrics: dict[str, Any],
        fetch_provider: bool,
    ) -> dict[str, Any]:
        review_cfg = self.cfg.get("observation_maturity_review", {})
        if row.get("universe_lane") != LANE_ALPACA_US or row.get("source") == "existing_static_watchlist":
            return {
                "path": PATH_NONE,
                "confidence_adjustment": "none",
                "data_limitations": ["not a dynamic Alpaca-compatible promotion candidate"],
                "fallback_used": "no",
                "alpaca_quote_freshness": "not_checked",
                "alpaca_tradability_result": "not_applicable",
                "intraday_freshness": "not_applicable",
                "eod_freshness": "not_applicable",
            }
        eod_ok = bool(metrics.get("eod_available")) or str(row.get("data_freshness_status") or "") == "fresh"
        intraday_ok = bool(metrics.get("intraday_available"))
        data_limitations: list[str] = []
        quote_freshness = "not_checked"
        tradability = "not_checked"

        if eod_ok and intraday_ok:
            path = PATH_FULL_FRESH
            confidence_adjustment = "none"
            fallback_used = "no"
        elif eod_ok and review_cfg.get("allow_cached_intraday_for_promotion", True) and self._cached_intraday_within_window(symbol):
            path = PATH_CACHED_INTRADAY
            confidence_adjustment = "none"
            fallback_used = "yes"
            data_limitations.append("intraday bars served from recent stored score cache")
        else:
            quote_ok = False
            if eod_ok and review_cfg.get("allow_alpaca_quote_fallback_for_promotion", True):
                quote_ok, quote_freshness, tradability = self._alpaca_quote_fallback_check(symbol)
            if eod_ok and quote_ok:
                path = PATH_ALPACA_QUOTE_FALLBACK
                confidence_adjustment = "reduced" if review_cfg.get("reduce_confidence_if_intraday_missing", True) else "none"
                fallback_used = "yes"
                data_limitations.append("EODHD intraday unavailable; Alpaca latest price used only for promotion sanity")
            elif (
                eod_ok
                and review_cfg.get("allow_eod_only_promotion_when_market_closed", True)
                and not self._market_open_for_promotion_review()
            ):
                path = PATH_EOD_ONLY_CLOSED
                confidence_adjustment = "reduced" if review_cfg.get("reduce_confidence_if_intraday_missing", True) else "none"
                fallback_used = "yes"
                data_limitations.append("market closed; EOD-only promotion review")
            else:
                path = PATH_NONE
                confidence_adjustment = "none"
                fallback_used = "no"
                if not eod_ok:
                    data_limitations.append("fresh EOD unavailable")
                if not intraday_ok:
                    data_limitations.append("fresh or acceptable fallback intraday unavailable")

        return {
            "path": path,
            "confidence_adjustment": confidence_adjustment,
            "data_limitations": data_limitations,
            "fallback_used": fallback_used,
            "alpaca_quote_freshness": quote_freshness,
            "alpaca_tradability_result": tradability,
            "intraday_freshness": "fresh" if intraday_ok else ("cached" if path == PATH_CACHED_INTRADAY else "missing"),
            "eod_freshness": "fresh_or_acceptable" if eod_ok else "missing",
        }

    def _cached_intraday_within_window(self, symbol: str) -> bool:
        minutes = int(self.cfg.get("observation_maturity_review", {}).get("intraday_freshness_window_minutes", 30))
        cutoff = (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat()
        rows = self.storage.fetch_all(
            """
            SELECT 1
            FROM symbol_research_scores
            WHERE symbol=?
              AND created_at>=?
              AND COALESCE(intraday_momentum_score, 0) > 0
            LIMIT 1
            """,
            (symbol.upper(), cutoff),
        )
        return bool(rows)

    def _market_open_for_promotion_review(self) -> bool:
        if not self.broker or not hasattr(self.broker, "is_market_open"):
            return False
        try:
            return bool(self.broker.is_market_open())
        except Exception:
            return False

    def _alpaca_quote_fallback_check(self, symbol: str) -> tuple[bool, str, str]:
        if not self._check_alpaca_compatibility(symbol):
            return False, "not_checked", "not_tradable"
        if not self.broker or not hasattr(self.broker, "get_latest_price"):
            return False, "not_available", "tradable"
        try:
            price = self.broker.get_latest_price(symbol)
            return price is not None, "fresh" if price is not None else "missing", "tradable"
        except Exception:
            return False, "error", "tradable"

    def _stage_review_metrics(self, row: dict[str, Any], fetch_provider: bool) -> dict[str, Any]:
        symbol = str(row.get("symbol") or "").upper()
        bars: list[dict[str, Any]] = []
        intraday_available = False
        eod_status = "not_requested"
        intraday_status = "not_requested"
        if fetch_provider and self.provider:
            provider_symbol = row.get("provider_symbol") or (f"{symbol}.US" if "." not in symbol else symbol)
            eod = self.provider.get_historical_bars(provider_symbol, limit=180)
            eod_status = eod.status
            if eod.status == "ok" and isinstance(eod.data, list):
                bars = eod.data
            intraday = self.provider.get_intraday_bars(provider_symbol, limit=60)
            intraday_status = intraday.status
            intraday_available = intraday.status == "ok" and isinstance(intraday.data, list) and len(intraday.data) >= 2
        local = self._local_metric_summary(bars)
        latest_price = local.get("latest_price") if bars else row.get("score")
        trend_summary = "EOD trend unavailable"
        if bars:
            trend_summary = f"trend score {self._trend_score(bars):.1f}; MA20={local.get('ma20')}; MA50={local.get('ma50')}; MA200={local.get('ma200')}"
        intraday_summary = "intraday unavailable"
        if intraday_available:
            intraday_summary = "intraday data available for current/recent session"
        dollar_volume = local.get("dollar_volume")
        liquidity_summary = f"dollar volume {dollar_volume:.0f}" if isinstance(dollar_volume, (int, float)) else "liquidity from latest stored score"
        volatility = local.get("volatility")
        atr = local.get("atr")
        volatility_summary = f"volatility {volatility:.2%}; ATR {atr:.2f}" if isinstance(volatility, (int, float)) and isinstance(atr, (int, float)) else "volatility from latest stored score"
        rel = local.get("relative_strength_20d")
        rel_summary = f"20d return proxy {rel:.2%}" if isinstance(rel, (int, float)) else "relative strength from latest stored score"
        return {
            "latest_price": latest_price,
            "price_freshness": "fresh_eod" if bars else row.get("data_freshness_status") or "stored",
            "eod_available": bool(bars),
            "intraday_available": intraday_available,
            "eod_status": eod_status,
            "intraday_status": intraday_status,
            "provider_guard_active": eod_status in {"provider_unavailable", "rate_limited", "plan_limited"} or intraday_status in {"provider_unavailable", "rate_limited", "plan_limited"},
            "trend_summary": trend_summary,
            "intraday_summary": intraday_summary,
            "liquidity_summary": liquidity_summary,
            "volatility_summary": volatility_summary,
            "relative_strength_spy": rel_summary,
            "relative_strength_qqq": rel_summary if symbol.startswith("XL") else "not applicable",
        }

    def _latest_position_symbols(self) -> list[str]:
        rows = self.storage.fetch_all(
            """
            SELECT symbol
            FROM positions
            WHERE created_at=(SELECT MAX(created_at) FROM positions)
            ORDER BY symbol
            """
        )
        return [str(r["symbol"]).upper() for r in rows if r.get("symbol")]

    def _cluster_holding_symbols(self, cluster: str | None, position_symbols: list[str]) -> list[str]:
        if not cluster:
            return []
        clusters = self.config.get("portfolio_optimizer", {}).get("clusters", {})
        cluster_symbols = {str(s).upper() for s in clusters.get(cluster, [])}
        return sorted(symbol for symbol in position_symbols if symbol in cluster_symbols)

    def _current_endpoint_coverage(self) -> dict[str, bool]:
        rows = self.storage.fetch_all("SELECT endpoint_name, available FROM data_provider_capabilities WHERE provider=?", (self.cfg.get("provider", "eodhd"),))
        coverage = {name: False for name in ("screener", "eod_bars", "intraday_bars", "realtime_quote", "technicals", "news", "fundamentals")}
        for row in rows:
            endpoint = str(row.get("endpoint_name") or "")
            if endpoint in coverage:
                coverage[endpoint] = bool(row.get("available"))
        return coverage

    def _schedule_type(self, run_type: str) -> str:
        return {
            "daily_deep_research": "daily_deep",
            "intraday_light_refresh": "intraday_light",
            "event_triggered_refresh": "event_triggered",
            "post_market_review": "post_market",
            "weekly_cleanup": "weekly_cleanup",
        }.get(run_type, run_type)

    def _record_audit(self, event_type: str, symbol: str | None, detail: dict[str, Any]) -> None:
        self.storage.execute(
            "INSERT INTO dynamic_universe_audit(id,run_id,event_type,symbol,detail,created_at) VALUES(?,?,?,?,?,?)",
            (str(uuid.uuid4()), self.run_id, event_type, symbol, json_dumps(detail), iso_now()),
        )

    def _schedule_state(self, run_type: str) -> dict[str, Any] | None:
        rows = self.storage.fetch_all("SELECT * FROM dynamic_universe_schedule_state WHERE schedule_name=?", (run_type,))
        return rows[0] if rows else None

    def _upsert_schedule_state(self, run_type: str, fields: dict[str, Any]) -> None:
        now = iso_now()
        current = self._schedule_state(run_type)
        data = {
            "id": current.get("id") if current else str(uuid.uuid4()),
            "schedule_name": run_type,
            "schedule_type": self._schedule_type(run_type),
            "due_at": fields.get("due_at") if "due_at" in fields else (current.get("due_at") if current else None),
            "last_started_at": fields.get("last_started_at") if "last_started_at" in fields else (current.get("last_started_at") if current else None),
            "last_completed_at": fields.get("last_completed_at") if "last_completed_at" in fields else (current.get("last_completed_at") if current else None),
            "last_success_at": fields.get("last_success_at") if "last_success_at" in fields else (current.get("last_success_at") if current else None),
            "last_skipped_at": fields.get("last_skipped_at") if "last_skipped_at" in fields else (current.get("last_skipped_at") if current else None),
            "last_skip_reason": fields.get("last_skip_reason") if "last_skip_reason" in fields else (current.get("last_skip_reason") if current else None),
            "missed_count": fields.get("missed_count") if "missed_count" in fields else (current.get("missed_count") if current else 0),
            "catchup_required": fields.get("catchup_required") if "catchup_required" in fields else (current.get("catchup_required") if current else 0),
            "catchup_attempted_at": fields.get("catchup_attempted_at") if "catchup_attempted_at" in fields else (current.get("catchup_attempted_at") if current else None),
            "catchup_completed_at": fields.get("catchup_completed_at") if "catchup_completed_at" in fields else (current.get("catchup_completed_at") if current else None),
            "catchup_status": fields.get("catchup_status") if "catchup_status" in fields else (current.get("catchup_status") if current else None),
            "data_freshness_status": fields.get("data_freshness_status") if "data_freshness_status" in fields else (current.get("data_freshness_status") if current else None),
            "provider_health_status": fields.get("provider_health_status") if "provider_health_status" in fields else (current.get("provider_health_status") if current else None),
            "internet_status": fields.get("internet_status") if "internet_status" in fields else (current.get("internet_status") if current else None),
            "power_status": fields.get("power_status") if "power_status" in fields else (current.get("power_status") if current else None),
            "battery_pct": fields.get("battery_pct") if "battery_pct" in fields else (current.get("battery_pct") if current else None),
            "stale_after_minutes": fields.get("stale_after_minutes") if "stale_after_minutes" in fields else (current.get("stale_after_minutes") if current else None),
            "promotion_allowed": fields.get("promotion_allowed") if "promotion_allowed" in fields else (current.get("promotion_allowed") if current else 0),
            "demotion_allowed": fields.get("demotion_allowed") if "demotion_allowed" in fields else (current.get("demotion_allowed") if current else 0),
            "notes": fields.get("notes") if "notes" in fields else (current.get("notes") if current else None),
            "created_at": current.get("created_at") if current else now,
            "updated_at": now,
        }
        self.storage.execute(
            """
            INSERT INTO dynamic_universe_schedule_state(
                id,schedule_name,schedule_type,due_at,last_started_at,last_completed_at,last_success_at,last_skipped_at,
                last_skip_reason,missed_count,catchup_required,catchup_attempted_at,catchup_completed_at,catchup_status,
                data_freshness_status,provider_health_status,internet_status,power_status,battery_pct,stale_after_minutes,
                promotion_allowed,demotion_allowed,notes,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(schedule_name) DO UPDATE SET
                schedule_type=excluded.schedule_type,
                due_at=excluded.due_at,
                last_started_at=excluded.last_started_at,
                last_completed_at=excluded.last_completed_at,
                last_success_at=excluded.last_success_at,
                last_skipped_at=excluded.last_skipped_at,
                last_skip_reason=excluded.last_skip_reason,
                missed_count=excluded.missed_count,
                catchup_required=excluded.catchup_required,
                catchup_attempted_at=excluded.catchup_attempted_at,
                catchup_completed_at=excluded.catchup_completed_at,
                catchup_status=excluded.catchup_status,
                data_freshness_status=excluded.data_freshness_status,
                provider_health_status=excluded.provider_health_status,
                internet_status=excluded.internet_status,
                power_status=excluded.power_status,
                battery_pct=excluded.battery_pct,
                stale_after_minutes=excluded.stale_after_minutes,
                promotion_allowed=excluded.promotion_allowed,
                demotion_allowed=excluded.demotion_allowed,
                notes=excluded.notes,
                updated_at=excluded.updated_at
            """,
            tuple(data.values()),
        )

    def _provider_available(self) -> tuple[str, str | None]:
        if not self.provider:
            return "provider_unavailable", "provider_not_configured"
        api_key = getattr(self.provider, "api_key", "configured")
        if not api_key:
            return "provider_unavailable", "missing_api_key"
        return "ok", None

    def _record_provider_health(self, status: str, error: str | None = None) -> None:
        self.storage.execute(
            "INSERT INTO data_provider_health(id,run_id,provider,status,checked_at,rate_limit_remaining,error,detail) VALUES(?,?,?,?,?,?,?,?)",
            (
                f"{self.cfg.get('provider', 'eodhd')}-{uuid.uuid4()}",
                self.run_id,
                self.cfg.get("provider", "eodhd"),
                status,
                iso_now(),
                None,
                error,
                json_dumps({"error": error} if error else {}),
            ),
        )

    def _research_gate(self, run_type: str, is_catchup: bool = False) -> ResearchGate:
        res_cfg = self.resilience_cfg
        if not res_cfg.get("enabled", True):
            return ResearchGate(True, None, "ok", "unchecked", "unchecked", None, True, True, "fresh")
        internet_ok = internet_available()
        internet_status = "online" if internet_ok else "offline"
        provider_status, provider_error = self._provider_available()
        if provider_status != "ok":
            self._record_provider_health(provider_status, provider_error)

        power = get_power_status()
        power_status = "ac" if power.connected is True else "battery" if power.connected is False else "unknown"
        battery_pct = power.battery_pct
        policy = res_cfg.get("power_policy", {})
        critical_pct = float(policy.get("skip_all_research_below_battery_pct", 25))
        if battery_pct is not None and battery_pct < critical_pct:
            return ResearchGate(False, "battery_below_research_threshold", provider_status, internet_status, power_status, battery_pct, False, False, "stale")

        deep = run_type in {"daily_deep_research", "weekly_cleanup"}
        light = run_type in {"intraday_light_refresh", "event_triggered_refresh", "post_market_review"}
        if power.connected is False and deep and not policy.get("allow_deep_research_on_battery", False):
            return ResearchGate(False, "deep_research_skipped_on_battery", provider_status, internet_status, power_status, battery_pct, False, False, "stale")
        if power.connected is False and light:
            min_light = float(policy.get("min_battery_pct_for_light_refresh", 35))
            if battery_pct is not None and battery_pct < min_light:
                return ResearchGate(False, "light_research_skipped_low_battery", provider_status, internet_status, power_status, battery_pct, False, False, "stale")
            if not policy.get("allow_light_refresh_on_battery", True):
                return ResearchGate(False, "light_research_skipped_on_battery", provider_status, internet_status, power_status, battery_pct, False, False, "stale")

        if res_cfg.get("internet_required_for_provider_calls", True) and not internet_ok:
            return ResearchGate(False, "no_internet", provider_status, internet_status, power_status, battery_pct, False, False, "stale")
        if provider_status != "ok":
            return ResearchGate(False, provider_error or "provider_unavailable", provider_status, internet_status, power_status, battery_pct, False, False, "stale")

        promotion_allowed = True
        if is_catchup and res_cfg.get("catchup_policy", {}).get("block_new_promotions_during_late_day_catchup", True) and run_type == "daily_deep_research":
            promotion_allowed = False
        return ResearchGate(True, None, provider_status, internet_status, power_status, battery_pct, promotion_allowed, True, "fresh")

    def _catchup_required(self, run_type: str) -> bool:
        if not self.resilience_cfg.get("catchup_policy", {}).get("enabled", True):
            return False
        state = self._schedule_state(run_type)
        return bool(state and int(state.get("catchup_required") or 0) == 1)

    def _catchup_allowed(self, run_type: str) -> bool:
        cfg = self.resilience_cfg.get("catchup_policy", {})
        key = {
            "daily_deep_research": "daily_deep_catchup_allowed",
            "intraday_light_refresh": "intraday_light_catchup_allowed",
            "post_market_review": "post_market_catchup_allowed",
            "weekly_cleanup": "weekly_cleanup_catchup_allowed",
        }.get(run_type)
        if key and not cfg.get(key, True):
            return False
        state = self._schedule_state(run_type)
        attempted = state.get("catchup_attempted_at") if state else None
        if attempted:
            try:
                last = datetime.fromisoformat(str(attempted).replace("Z", "+00:00")).astimezone(UTC)
                min_gap = int(cfg.get("min_minutes_between_catchups", 15))
                if self.now - last < timedelta(minutes=min_gap):
                    return False
            except Exception:
                return True
        return True

    def _record_schedule_due(self, run_type: str, gate: ResearchGate) -> None:
        self._upsert_schedule_state(
            run_type,
            {
                "due_at": self.now.isoformat(),
                "provider_health_status": gate.provider_health_status,
                "internet_status": gate.internet_status,
                "power_status": gate.power_status,
                "battery_pct": gate.battery_pct,
                "promotion_allowed": 1 if gate.promotion_allowed else 0,
                "demotion_allowed": 1 if gate.demotion_allowed else 0,
                "data_freshness_status": gate.data_freshness_status,
            },
        )
        self._record_audit("dynamic_universe_research_due", None, {"run_type": run_type, "gate_allowed": gate.allowed})

    def _record_schedule_skip(self, run_type: str, reason: str, gate: ResearchGate | None = None, catchup_required: bool = True) -> None:
        gate = gate or ResearchGate(False, reason, "unknown", "unknown", "unknown", None, False, False, "stale")
        state = self._schedule_state(run_type)
        missed_count = int(state.get("missed_count") or 0) + 1 if state else 1
        self._upsert_schedule_state(
            run_type,
            {
                "last_skipped_at": iso_now(),
                "last_skip_reason": reason,
                "missed_count": missed_count,
                "catchup_required": 1 if catchup_required else 0,
                "catchup_status": "required" if catchup_required else "not_required",
                "data_freshness_status": "stale",
                "provider_health_status": gate.provider_health_status,
                "internet_status": gate.internet_status,
                "power_status": gate.power_status,
                "battery_pct": gate.battery_pct,
                "promotion_allowed": 0,
                "demotion_allowed": 0,
                "notes": json_dumps({"reason": reason}),
            },
        )
        self._record_audit("dynamic_universe_research_skipped", None, {"run_type": run_type, "reason": reason})
        self._record_audit("dynamic_universe_research_missed", None, {"run_type": run_type, "reason": reason, "missed_count": missed_count})

    def _record_catchup_started(self, run_type: str, gate: ResearchGate) -> None:
        self._upsert_schedule_state(
            run_type,
            {
                "catchup_attempted_at": iso_now(),
                "catchup_status": "running",
                "provider_health_status": gate.provider_health_status,
                "internet_status": gate.internet_status,
                "power_status": gate.power_status,
                "battery_pct": gate.battery_pct,
            },
        )
        self._record_audit("dynamic_universe_catchup_started", None, {"run_type": run_type})

    def _record_schedule_completed(self, run_type: str, status: str, gate: ResearchGate, is_catchup: bool) -> None:
        state = self._schedule_state(run_type)
        fields = {
            "last_completed_at": iso_now(),
            "provider_health_status": self._provider_health_status if self._provider_health_status != "unknown" else gate.provider_health_status,
            "internet_status": gate.internet_status,
            "power_status": gate.power_status,
            "battery_pct": gate.battery_pct,
            "promotion_allowed": 1 if gate.promotion_allowed else 0,
            "demotion_allowed": 1 if gate.demotion_allowed else 0,
            "data_freshness_status": "fresh" if status == "completed" else "stale",
        }
        if status == "completed":
            fields.update(last_success_at=iso_now(), last_skip_reason=None, catchup_required=0, missed_count=0)
            if state and state.get("last_skip_reason") == "missing_api_key":
                self._record_audit(
                    "provider_missing_key_state_recovered",
                    None,
                    {
                        "run_type": run_type,
                        "last_skipped_at": state.get("last_skipped_at"),
                        "recovered_by": "successful_research_completion",
                    },
                )
        if is_catchup:
            fields.update(catchup_completed_at=iso_now(), catchup_status=status)
        self._upsert_schedule_state(run_type, fields)

    def _provider_summary_status(self) -> str:
        if self._provider_failed:
            return self._provider_health_status
        rows = self.storage.fetch_all(
            "SELECT SUM(CASE WHEN available=1 THEN 1 ELSE 0 END) ok_count, SUM(CASE WHEN plan_limited=1 THEN 1 ELSE 0 END) plan_limited_count FROM data_provider_capabilities WHERE provider=?",
            (self.cfg.get("provider", "eodhd"),),
        )
        if not rows:
            return self._provider_health_status
        ok_count = int(rows[0].get("ok_count") or 0)
        plan_limited_count = int(rows[0].get("plan_limited_count") or 0)
        if ok_count > 0 and plan_limited_count > 0:
            return "partial"
        return self._provider_health_status

    def _mark_dynamic_symbols_stale(self, reason: str, gate: ResearchGate) -> None:
        stale_after = int(self.resilience_cfg.get("stale_data_policy", {}).get("max_age_minutes_for_trade_eligibility", 30))
        self.storage.execute(
            """
            UPDATE universe_symbols
            SET data_freshness_status='stale',
                provider_health_status=?,
                promotion_allowed=0,
                demotion_allowed=0,
                stale_after_minutes=?,
                reason=?,
                updated_at=?
            WHERE COALESCE(source, '') NOT IN ('existing_static_watchlist', 'existing_static_observation')
            """,
            (gate.provider_health_status, stale_after, reason, iso_now()),
        )
        self._record_audit("dynamic_universe_stale_data_guard", None, {"reason": reason, "stale_after_minutes": stale_after})

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
        
        # Collect existing dynamically tracked symbols
        try:
            dynamic_rows = self.storage.fetch_all(
                "SELECT symbol, tier, source, provider_symbol, exchange, asset_class, sector, cluster, country, region, currency FROM universe_symbols WHERE tier IN (?, ?, ?)",
                (RESEARCH_CANDIDATE, OBSERVATION, PAPER_TRADABLE)
            )
            for r in dynamic_rows:
                candidates.append({
                    "Code": r["symbol"],
                    "Exchange": r["exchange"] or "US",
                    "exchange": r["exchange"] or "US",
                    "Type": r["asset_class"] or "Common Stock",
                    "asset_class": r["asset_class"] or "Common Stock",
                    "Sector": r["sector"],
                    "sector": r["sector"],
                    "Cluster": r["cluster"],
                    "cluster": r["cluster"],
                    "Country": r["country"] or r["region"] or "US",
                    "country": r["country"] or r["region"] or "US",
                    "Currency": r["currency"] or "USD",
                    "currency": r["currency"] or "USD",
                    "source": r["source"] or "dynamic_universe",
                    "existing_static": False,
                    "observation": r["tier"] == OBSERVATION,
                    "provider_symbol": r["provider_symbol"],
                })
        except Exception:
            pass

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

        if run_type != "intraday_light_refresh":
            if self.provider and self.cfg.get("raw_sources", {}).get("eodhd_screener", True):
                res = self.provider.get_screener_results(limit=min(max_raw, 100))
                candidates.extend(self._rows_from_response(res, "eodhd_screener"))

            if self.provider and self.cfg.get("raw_sources", {}).get("eodhd_news", True):
                res = self.provider.get_news(limit=min(max_raw, 100))
                candidates.extend(self._news_candidate_rows(res))

        if self.provider and self.cfg.get("raw_sources", {}).get("eodhd_exchange_symbols", True) and run_type == "daily_deep_research":
            res = self.provider.list_symbols("US", limit=max_raw)
            candidates.extend(self._rows_from_response(res, "eodhd_exchange_symbols"))

        deduped: dict[str, dict[str, Any]] = {}
        for row in self._prioritize_candidates(candidates):
            symbol = self._normalize_symbol(row)
            if symbol and symbol not in deduped:
                deduped[symbol] = row
        return list(deduped.values())[:max_raw]

    def _news_candidate_rows(self, response: ProviderResponse) -> list[dict[str, Any]]:
        if response.status != "ok" or not isinstance(response.data, list):
            return []
        rows: list[dict[str, Any]] = []
        for item in response.data:
            if not isinstance(item, dict):
                continue
            symbols = item.get("symbols") or item.get("tickers") or item.get("codes") or []
            if isinstance(symbols, str):
                symbols = [symbols]
            for raw in symbols:
                raw_symbol = str(raw).upper().strip()
                if not raw_symbol:
                    continue
                source_exchange = "US" if raw_symbol.endswith(".US") or "." not in raw_symbol else raw_symbol.rsplit(".", 1)[-1]
                symbol = raw_symbol.replace(".US", "")
                rows.append(
                    {
                        "Code": symbol,
                        "Exchange": source_exchange,
                        "Type": "Common Stock",
                        "source": "eodhd_news",
                        "reason": "recent news catalyst",
                        "news_symbol_raw": raw_symbol,
                    }
                )
        return rows

    def _prioritize_candidates(self, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        def priority(row: dict[str, Any]) -> tuple[int, str]:
            metadata = self._metadata(row)
            source = str(row.get("source") or "")
            exchange = str(metadata.get("exchange") or "").upper()
            asset_class = str(metadata.get("asset_class") or "")
            symbol = str(metadata.get("symbol") or "")
            if source == "existing_static_watchlist":
                base = 0
            elif metadata.get("universe_lane") == LANE_EXCLUDED:
                base = 10
            elif metadata.get("universe_lane") == LANE_GLOBAL_RESEARCH:
                base = 7
            elif source == "eodhd_news":
                base = 1
            elif exchange in {"NYSE", "NASDAQ", "NYSE ARCA", "NYSEARCA", "AMEX"} and asset_class in {"equity", "etf"}:
                base = 2
            elif asset_class in {"fund", "index"}:
                base = 8
            elif exchange in {"PINK", "OTC", "OTCQB", "OTCQX"}:
                base = 9
            else:
                base = 5
            return base, symbol

        return sorted(candidates, key=priority)

    def _excluded_candidate(self, row: dict[str, Any]) -> bool:
        metadata = self._metadata(row)
        return metadata.get("universe_lane") == LANE_EXCLUDED

    def _rows_from_response(self, response: ProviderResponse, source: str) -> list[dict[str, Any]]:
        if response.status != "ok" or not response.data:
            if response.status in {"plan_limited", "rate_limited"}:
                self._provider_health_status = "partial" if response.status == "plan_limited" else "rate_limited"
            else:
                self._provider_failed = True
                self._provider_health_status = response.status
                self._promotion_allowed = False
                self._demotion_allowed = False
            self._record_audit("provider_unavailable", None, {"source": source, "status": response.status, "error": response.error})
            if response.status not in {"plan_limited", "rate_limited"}:
                self._record_audit("dynamic_universe_demotions_blocked_provider_unavailable", None, {"source": source, "status": response.status})
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
        exchange = info.get("Exchange") or info.get("exchange") or "US"
        region = info.get("Country") or info.get("country") or "US"
        lane, alpaca_compatible, exclusion_reason = self._classify_symbol_lane(symbol, str(exchange), asset_class, info)
        return {
            "symbol": symbol,
            "provider_symbol": info.get("provider_symbol") or (f"{symbol}.US" if "." not in symbol else symbol),
            "exchange": exchange,
            "asset_class": asset_class,
            "sector": info.get("Sector") or info.get("sector"),
            "cluster": cluster,
            "region": region,
            "currency": info.get("Currency") or info.get("currency") or "USD",
            "source": info.get("source", "unknown"),
            "existing_static": bool(info.get("existing_static")),
            "observation": bool(info.get("observation")),
            "universe_lane": lane,
            "alpaca_compatible": 1 if alpaca_compatible else 0,
            "exclusion_reason": exclusion_reason,
        }

    def _classify_symbol_lane(self, symbol: str, exchange: str, asset_class: str, info: dict[str, Any]) -> tuple[str, bool, str | None]:
        symbol = symbol.upper().strip()
        exchange_upper = str(exchange or "").upper()
        source = str(info.get("source") or "")
        exclusions = self.cfg.get("exclusions", {})
        us_exchanges = {"US", "NYSE", "NASDAQ", "NYSE ARCA", "NYSEARCA", "AMEX", "BATS", "CBOE"}
        execution_allowed = self._asset_execution_allowed(asset_class)
        clean_us_ticker = bool(re.fullmatch(r"[A-Z]{1,5}", symbol))

        if not symbol:
            return LANE_EXCLUDED, False, "invalid_symbol"
        if any(ch in symbol for ch in (":", "/", "\\")):
            return LANE_GLOBAL_RESEARCH, False, "non_us_or_cross_asset_symbol"
        if "-" in symbol and exchange_upper not in us_exchanges:
            return LANE_GLOBAL_RESEARCH, False, "non_us_or_cross_asset_symbol"
        if symbol.isdigit():
            return LANE_GLOBAL_RESEARCH if exchange_upper not in us_exchanges else LANE_EXCLUDED, False, "numeric_symbol_not_us_execution_lane"
        if "." in symbol and not symbol.endswith(".US"):
            return LANE_GLOBAL_RESEARCH, False, "non_us_exchange_suffix"
        if exclusions.get("otc", True) and (exchange_upper in {"PINK", "OTC", "OTCQB", "OTCQX"} or (len(symbol) == 5 and symbol[-1] in {"F", "Y"})):
            return LANE_EXCLUDED, False, "otc_or_adr_like_symbol"
        if asset_class == "fund" and not self.cfg.get("asset_classes_enabled", {}).get("funds", True):
            return LANE_GLOBAL_RESEARCH, False, "fund_research_only"
        if asset_class not in {"equity", "etf", "fund"}:
            return LANE_GLOBAL_RESEARCH, False, "unsupported_asset_class_research_only"
        if exclusions.get("leveraged_etfs", True) and any(token in symbol for token in ("2X", "3X", "ULTRA", "BEAR", "BULL")):
            return LANE_EXCLUDED, False, "leveraged_or_inverse_symbol"
        if exchange_upper not in us_exchanges:
            return LANE_GLOBAL_RESEARCH, False, "non_us_exchange"
        if not clean_us_ticker:
            return LANE_EXCLUDED, False, "unclean_us_ticker_format"
        if not execution_allowed:
            return LANE_GLOBAL_RESEARCH, False, "asset_class_not_execution_enabled"
        return LANE_ALPACA_US, True, None

    def _infer_cluster(self, symbol: str, asset_class: str, info: dict[str, Any]) -> str:
        if info.get("cluster") or info.get("Cluster"):
            return str(info.get("cluster") or info.get("Cluster"))
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
        quote_ok = False
        news_ok = False
        endpoint_coverage = {
            "screener": metadata.get("source") == "eodhd_screener",
            "eod_bars": False,
            "intraday_bars": False,
            "realtime_quote": False,
            "technicals": False,
            "news": False,
            "fundamentals": False,
        }
        lane = metadata.get("universe_lane") or LANE_ALPACA_US
        if lane == LANE_EXCLUDED:
            reason = metadata.get("exclusion_reason") or "excluded_or_low_quality"
            metadata["data_confidence"] = "insufficient"
            metadata["data_confidence_reason"] = reason
            metadata["endpoint_coverage"] = endpoint_coverage
            return ResearchScore(
                symbol=symbol,
                total_score=0.0,
                liquidity_score=0.0,
                trend_score=0.0,
                intraday_momentum_score=0.0,
                relative_strength_score=0.0,
                volatility_quality_score=0.0,
                screener_mover_score=0.0,
                news_score=0.0,
                sector_theme_score=0.0,
                data_quality_score=0.0,
                data_confidence="insufficient",
                data_confidence_reason=reason,
                universe_lane=lane,
                existing_static=bool(metadata.get("existing_static")),
                block_reason=reason,
            )
        if self.provider and self.cfg.get("raw_sources", {}).get("eodhd_eod_bars", True) and not metadata.get("existing_static"):
            res = self.provider.get_historical_bars(metadata.get("provider_symbol") or symbol, limit=80)
            if res.status == "ok" and isinstance(res.data, list):
                bars = res.data
                endpoint_coverage["eod_bars"] = True
            elif res.status not in {"plan_limited", "rate_limited"}:
                self._provider_failed = True
                self._provider_health_status = res.status
        if self.provider and not metadata.get("existing_static") and bars:
            quote = self.provider.get_latest_quote(metadata.get("provider_symbol") or symbol)
            quote_ok = quote.status == "ok" and bool(quote.data)
            endpoint_coverage["realtime_quote"] = quote_ok
            if quote_ok and isinstance(quote.data, dict):
                metadata["quote_payload_available"] = True
        liquidity, liquidity_block = self._liquidity_score(metadata, bars)
        trend = self._trend_score(bars)
        intraday = self._intraday_momentum_score(symbol, metadata, bars, endpoint_coverage)
        rel = self._relative_strength_score(bars)
        vol = self._volatility_quality_score(bars)
        screener = self._screener_mover_score(metadata)
        news, news_ok = self._news_score(symbol, metadata)
        endpoint_coverage["news"] = news_ok
        sector = 5.0 if metadata.get("cluster") != "unknown_cluster" else 2.5
        quality = self._data_quality_score(metadata, bars)
        confidence, confidence_reason = self._data_confidence(metadata, bars, quote_ok, news_ok)
        metadata["data_confidence"] = confidence
        metadata["data_confidence_reason"] = confidence_reason
        metadata["endpoint_coverage"] = endpoint_coverage
        metadata.update(self._local_metric_summary(bars))
        total = liquidity + trend + intraday + rel + vol + screener + news + sector + quality
        block_reason = liquidity_block
        if quality < 2.0 and not metadata.get("existing_static"):
            block_reason = "missing or stale price data"
        if confidence == "insufficient" and not metadata.get("existing_static"):
            block_reason = block_reason or "insufficient data confidence"
        return ResearchScore(
            symbol=symbol,
            total_score=min(100.0, total),
            liquidity_score=liquidity,
            trend_score=trend,
            intraday_momentum_score=intraday,
            relative_strength_score=rel,
            volatility_quality_score=vol,
            screener_mover_score=screener,
            news_score=news,
            sector_theme_score=sector,
            data_quality_score=quality,
            data_confidence=confidence,
            data_confidence_reason=confidence_reason,
            universe_lane=lane,
            existing_static=bool(metadata.get("existing_static")),
            block_reason=block_reason,
        )

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
            return 5.0 if closes else 0.0
        returns = [(closes[i] / closes[i - 1] - 1.0) for i in range(1, len(closes))]
        mean = sum(returns) / len(returns)
        variance = sum((r - mean) ** 2 for r in returns) / len(returns)
        daily_vol = math.sqrt(variance)
        if daily_vol > 0.08:
            return 0.0
        if daily_vol < 0.002:
            return 4.0
        return 10.0

    def _local_metric_summary(self, bars: list[dict[str, Any]]) -> dict[str, Any]:
        closes = [float(b.get("close") or b.get("adjusted_close") or 0) for b in bars if float(b.get("close") or b.get("adjusted_close") or 0) > 0]
        vols = [float(b.get("volume") or 0) for b in bars if b.get("volume") is not None]
        latest = closes[-1] if closes else None
        avg_volume_20 = sum(vols[-20:]) / len(vols[-20:]) if vols[-20:] else None
        dollar_volume = latest * avg_volume_20 if latest is not None and avg_volume_20 is not None else None
        ma20 = sum(closes[-20:]) / 20 if len(closes) >= 20 else None
        ma50 = sum(closes[-50:]) / 50 if len(closes) >= 50 else None
        ma200 = sum(closes[-200:]) / 200 if len(closes) >= 200 else None
        ret20 = closes[-1] / closes[-20] - 1.0 if len(closes) >= 20 else None
        returns = [(closes[i] / closes[i - 1] - 1.0) for i in range(1, len(closes))]
        volatility = None
        atr_proxy = None
        if returns:
            mean = sum(returns) / len(returns)
            volatility = math.sqrt(sum((r - mean) ** 2 for r in returns) / len(returns))
        if len(closes) >= 15:
            ranges = [abs(closes[i] - closes[i - 1]) for i in range(1, len(closes))]
            atr_proxy = sum(ranges[-14:]) / min(14, len(ranges))
        return {
            "latest_price": latest,
            "avg_volume_20": avg_volume_20,
            "dollar_volume": dollar_volume,
            "ma20": ma20,
            "ma50": ma50,
            "ma200": ma200,
            "rsi": None,
            "atr": atr_proxy,
            "relative_strength_20d": ret20,
            "volatility": volatility,
            "price_freshness": "eod_bars" if latest is not None else "unavailable",
            "local_metrics_available": {
                "ma20": ma20 is not None,
                "ma50": ma50 is not None,
                "ma200": ma200 is not None,
                "rsi": False,
                "atr": atr_proxy is not None,
                "relative_strength": ret20 is not None,
                "liquidity_dollar_volume": dollar_volume is not None,
                "volatility": volatility is not None,
            },
        }

    def _intraday_momentum_score(self, symbol: str, metadata: dict[str, Any], bars: list[dict[str, Any]], endpoint_coverage: dict[str, bool] | None = None) -> float:
        if metadata.get("existing_static"):
            return 10.0
        if not self.provider or not self.cfg.get("raw_sources", {}).get("eodhd_intraday_bars", True) or not bars:
            return 7.5 if bars else 0.0
        res = self.provider.get_intraday_bars(metadata.get("provider_symbol") or symbol, limit=60)
        if res.status != "ok" or not isinstance(res.data, list) or len(res.data) < 2:
            return 7.5
        if endpoint_coverage is not None:
            endpoint_coverage["intraday_bars"] = True
        closes = [float(b.get("close") or 0) for b in res.data if float(b.get("close") or 0) > 0]
        vols = [float(b.get("volume") or 0) for b in res.data if b.get("volume") is not None]
        if len(closes) < 2:
            return 7.5
        ret = closes[-1] / closes[0] - 1.0
        metadata["intraday_return"] = ret
        metadata["intraday_points"] = len(closes)
        score = 7.5 + ret * 250
        if len(vols) >= 10:
            recent = sum(vols[-3:]) / 3
            baseline = sum(vols[:-3]) / max(1, len(vols[:-3]))
            if baseline > 0 and recent > baseline * 1.5:
                score += 2.0
        return max(0.0, min(15.0, score))

    def _screener_mover_score(self, metadata: dict[str, Any]) -> float:
        source = str(metadata.get("source") or "")
        if metadata.get("existing_static"):
            return 5.0
        if source == "eodhd_screener":
            return 8.0
        if source == "eodhd_news":
            return 5.0
        if source == "eodhd_exchange_symbols":
            return 3.0
        return 2.5

    def _news_score(self, symbol: str, metadata: dict[str, Any]) -> tuple[float, bool]:
        if not self.provider or not self.cfg.get("raw_sources", {}).get("eodhd_news", True) or metadata.get("existing_static"):
            return 2.5, False
        current = self._current_symbol(symbol)
        current_tier = current.get("tier") if current else None
        if current_tier not in {OBSERVATION, PAPER_TRADABLE}:
            return 2.5, False
        res = self.provider.get_news(symbol=symbol, limit=5)
        if res.status != "ok" or not isinstance(res.data, list):
            metadata["news_unavailable_reason"] = res.status
            return 2.5, False
        return min(5.0, 2.5 + len(res.data) * 0.5), True

    def _data_confidence(self, metadata: dict[str, Any], bars: list[dict[str, Any]], quote_ok: bool, news_ok: bool) -> tuple[str, str]:
        if metadata.get("existing_static"):
            return "high", "static universe symbol"
        if len(bars) < 20:
            return "insufficient", "missing usable EOD price/liquidity data"
        if quote_ok and news_ok:
            return "medium", "EOD bars, realtime quote, and optional news available; fundamentals and technical API are not required"
        if quote_ok:
            return "medium", "EOD bars and realtime quote available; news unavailable neutral"
        return "low", "EOD bars available; realtime quote unavailable"

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
        if self._provider_failed:
            current = self._current_symbol(symbol)
            return current.get("tier") if current else RAW_UNIVERSE
        if metadata.get("universe_lane") == LANE_EXCLUDED:
            return RAW_UNIVERSE
        if metadata.get("universe_lane") != LANE_ALPACA_US and metadata.get("asset_class") not in {"equity", "etf", "fund", "index"}:
            return RAW_UNIVERSE
        if not self._promotion_allowed:
            if score.total_score >= self._research_threshold():
                self._record_audit("dynamic_universe_promotions_blocked_stale_research", symbol, {"score": score.total_score, "provider_health_status": self._provider_health_status})
            current = self._current_symbol(symbol)
            return current.get("tier") if current else RAW_UNIVERSE
        if score.block_reason:
            return RAW_UNIVERSE
        if score.data_confidence == "insufficient":
            return RAW_UNIVERSE
        promo = self.cfg.get("promotion", {})
        if score.total_score < self._research_threshold():
            return RAW_UNIVERSE
        current = self._current_symbol(symbol)
        if not current or current.get("tier") == RAW_UNIVERSE:
            if score.data_confidence not in {"low", "medium", "high"}:
                return RAW_UNIVERSE
            return RESEARCH_CANDIDATE
        if current.get("tier") == RESEARCH_CANDIDATE:
            if score.total_score < self._observation_threshold() or self._positive_component_count(score) < 2:
                return RESEARCH_CANDIDATE
            if score.data_confidence not in {"medium", "high"}:
                return RESEARCH_CANDIDATE
            return OBSERVATION
        if current.get("tier") == OBSERVATION:
            cycles = self._score_count(symbol)
            sessions = self._session_count(symbol)
            confidence_ok = score.data_confidence == "high" or (score.data_confidence == "medium" and bool(promo.get("allow_medium_confidence_paper_tradable", True)))
            freshness = self._score_promotion_freshness(symbol, metadata, score)
            metadata.update(freshness)
            if (
                metadata.get("universe_lane") == LANE_ALPACA_US
                and score.total_score >= self._paper_tradable_threshold()
                and cycles >= int(promo.get("min_observation_cycles", 3))
                and sessions >= int(promo.get("min_observation_sessions", 1))
                and confidence_ok
                and freshness["promotion_freshness_path"] != PATH_NONE
                and self._check_alpaca_compatibility(symbol)
                and not self._has_open_order_conflict(symbol)
            ):
                return PAPER_TRADABLE
            return OBSERVATION
        return current.get("tier") or RESEARCH_CANDIDATE

    def _score_promotion_freshness(self, symbol: str, metadata: dict[str, Any], score: ResearchScore) -> dict[str, Any]:
        metrics = {
            "eod_available": bool((metadata.get("endpoint_coverage") or {}).get("eod_bars")) or metadata.get("price_freshness") in {"eod_bars", "fresh_eod"},
            "intraday_available": bool((metadata.get("endpoint_coverage") or {}).get("intraday_bars")) or bool(metadata.get("intraday_points")),
        }
        fallback = self._promotion_freshness_decision(symbol, metadata, {"score": score.total_score}, metrics, fetch_provider=False)
        return {
            "promotion_freshness_path": fallback["path"],
            "promotion_confidence_adjustment": fallback["confidence_adjustment"],
            "promotion_data_limitations": fallback["data_limitations"],
            "fallback_used": fallback["fallback_used"],
            "alpaca_quote_freshness": fallback["alpaca_quote_freshness"],
            "alpaca_tradability_result": fallback["alpaca_tradability_result"],
            "intraday_freshness": fallback["intraday_freshness"],
            "eod_freshness": fallback["eod_freshness"],
            "proposal_block_reason_after_promotion": "proposal blocked until fresh market validation, ENTRY setup, RiskEngine, Telegram approval, and final validation pass",
        }

    def _check_alpaca_compatibility(self, symbol: str) -> bool:
        if not self.broker or not hasattr(self.broker, "get_asset"):
            return False
        try:
            asset = self.broker.get_asset(symbol)
            if not asset:
                return False
            tradable = getattr(asset, "tradable", False)
            status = getattr(asset, "status", "")
            ac = getattr(asset, "asset_class", "")
            exchange = getattr(asset, "exchange", "")
            if not tradable or status != "active":
                return False
            if ac != "us_equity":
                return False
            if exchange in {"OTC", "PINK", "OTCQB", "OTCQX"}:
                return False
            return True
        except Exception:
            return False

    def _has_open_order_conflict(self, symbol: str) -> bool:
        if not self.broker or not hasattr(self.broker, "get_open_orders"):
            return False
        try:
            for order in self.broker.get_open_orders() or []:
                order_symbol = str(getattr(order, "symbol", "") or (order.get("symbol") if isinstance(order, dict) else "")).upper()
                if order_symbol == symbol.upper():
                    return True
        except Exception:
            return True
        return False

    def _research_threshold(self) -> float:
        promo = self.cfg.get("promotion", {})
        exploration = self.cfg.get("exploration", {})
        if exploration.get("enabled", True):
            return float(exploration.get("min_research_score_for_exploration", promo.get("min_research_score", 55)))
        return float(promo.get("min_research_score", 55))

    def _observation_threshold(self) -> float:
        return float(self.cfg.get("promotion", {}).get("min_observation_score", 65))

    def _paper_tradable_threshold(self) -> float:
        return float(self.cfg.get("promotion", {}).get("min_paper_tradable_score", 75))

    def _positive_component_count(self, score: ResearchScore) -> int:
        checks = [
            score.trend_score >= 14.0,
            score.relative_strength_score >= 8.5,
            score.liquidity_score >= 12.0,
            score.intraday_momentum_score >= 9.0,
            score.screener_mover_score >= 7.0,
            score.news_score > 2.5,
        ]
        return sum(1 for passed in checks if passed)

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

    def _backfill_unclassified_universe_symbols(self) -> None:
        rows = self.storage.fetch_all(
            """
            SELECT symbol, provider_symbol, exchange, asset_class, region, currency, sector, source, reason
            FROM universe_symbols
            WHERE universe_lane IS NULL OR universe_lane=''
            LIMIT 500
            """
        )
        for row in rows:
            info = {
                "Code": row["symbol"],
                "provider_symbol": row["provider_symbol"],
                "Exchange": row["exchange"],
                "Type": row["asset_class"],
                "Country": row["region"],
                "Currency": row["currency"],
                "Sector": row["sector"],
                "source": row["source"],
                "reason": row["reason"],
            }
            metadata = self._metadata(info)
            self.storage.execute(
                """
                UPDATE universe_symbols
                SET universe_lane=?, alpaca_compatible=?, exclusion_reason=?, updated_at=?
                WHERE symbol=?
                """,
                (
                    metadata.get("universe_lane"),
                    metadata.get("alpaca_compatible", 0),
                    metadata.get("exclusion_reason"),
                    iso_now(),
                    row["symbol"],
                ),
            )

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
                universe_lane,alpaca_compatible,exclusion_reason,executable,observation_only,score,reason,source,provider,data_quality,data_confidence,data_confidence_reason,data_freshness_status,
                last_successful_research_at,provider_health_status,promotion_allowed,demotion_allowed,stale_after_minutes,
                promotion_freshness_path,promotion_confidence_adjustment,promotion_data_limitations,proposal_block_reason_after_promotion,
                fallback_used,next_review_time,alpaca_quote_freshness,alpaca_tradability_result,intraday_freshness,eod_freshness,
                last_seen_at,last_promoted_at,last_demoted_at,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                universe_lane=excluded.universe_lane,
                alpaca_compatible=excluded.alpaca_compatible,
                exclusion_reason=excluded.exclusion_reason,
                executable=excluded.executable,
                observation_only=excluded.observation_only,
                score=excluded.score,
                reason=excluded.reason,
                source=excluded.source,
                provider=excluded.provider,
                data_quality=excluded.data_quality,
                data_confidence=excluded.data_confidence,
                data_confidence_reason=excluded.data_confidence_reason,
                data_freshness_status=excluded.data_freshness_status,
                last_successful_research_at=excluded.last_successful_research_at,
                provider_health_status=excluded.provider_health_status,
                promotion_allowed=excluded.promotion_allowed,
                demotion_allowed=excluded.demotion_allowed,
                stale_after_minutes=excluded.stale_after_minutes,
                promotion_freshness_path=excluded.promotion_freshness_path,
                promotion_confidence_adjustment=excluded.promotion_confidence_adjustment,
                promotion_data_limitations=excluded.promotion_data_limitations,
                proposal_block_reason_after_promotion=excluded.proposal_block_reason_after_promotion,
                fallback_used=excluded.fallback_used,
                next_review_time=excluded.next_review_time,
                alpaca_quote_freshness=excluded.alpaca_quote_freshness,
                alpaca_tradability_result=excluded.alpaca_tradability_result,
                intraday_freshness=excluded.intraday_freshness,
                eod_freshness=excluded.eod_freshness,
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
                metadata.get("universe_lane"),
                metadata.get("alpaca_compatible", 0),
                metadata.get("exclusion_reason"),
                executable,
                observation_only,
                score,
                metadata.get("reason"),
                metadata.get("source"),
                self.cfg.get("provider", "eodhd"),
                "ok" if score is not None else "seed",
                metadata.get("data_confidence"),
                metadata.get("data_confidence_reason"),
                self._data_freshness_status,
                now if score is not None and self._data_freshness_status == "fresh" else None,
                self._provider_health_status,
                1 if self._promotion_allowed else 0,
                1 if self._demotion_allowed else 0,
                int(self.resilience_cfg.get("stale_data_policy", {}).get("max_age_minutes_for_trade_eligibility", 30)),
                metadata.get("promotion_freshness_path"),
                metadata.get("promotion_confidence_adjustment"),
                json_dumps(metadata.get("promotion_data_limitations", [])),
                metadata.get("proposal_block_reason_after_promotion"),
                metadata.get("fallback_used"),
                metadata.get("next_review_time"),
                metadata.get("alpaca_quote_freshness"),
                metadata.get("alpaca_tradability_result"),
                metadata.get("intraday_freshness"),
                metadata.get("eod_freshness"),
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
                intraday_momentum_score,volatility_quality_score,screener_mover_score,news_score,sector_theme_score,
                data_quality_score,data_confidence,data_confidence_reason,universe_lane,block_reason,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                str(uuid.uuid4()), self.run_id, score.symbol.upper(), self.cfg.get("provider", "eodhd"), score.total_score,
                score.liquidity_score, score.trend_score, score.relative_strength_score, score.intraday_momentum_score,
                score.volatility_quality_score, score.screener_mover_score, score.news_score, score.sector_theme_score,
                score.data_quality_score, score.data_confidence, score.data_confidence_reason, score.universe_lane,
                score.block_reason, iso_now(),
            ),
        )

    def _record_candidate_block(self, score: ResearchScore, metadata: dict[str, Any]) -> None:
        reason = score.block_reason or "score below research threshold"
        self.storage.execute(
            """
            INSERT INTO research_candidate_block_reasons(
                id,run_id,symbol,score,data_confidence,block_reason,liquidity_score,trend_score,
                intraday_momentum_score,relative_strength_score,volatility_quality_score,screener_mover_score,
                news_score,sector_theme_score,data_quality_score,universe_lane,exclusion_reason,created_at,payload
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                str(uuid.uuid4()), self.run_id, score.symbol.upper(), score.total_score, score.data_confidence, reason,
                score.liquidity_score, score.trend_score, score.intraday_momentum_score, score.relative_strength_score,
                score.volatility_quality_score, score.screener_mover_score, score.news_score, score.sector_theme_score,
                score.data_quality_score, score.universe_lane, metadata.get("exclusion_reason"), iso_now(), json_dumps(metadata),
            ),
        )

    def _record_near_miss_symbols(self) -> None:
        threshold = self._research_threshold()
        near = sorted(
            (s for s in self._last_score_candidates if not s.existing_static and s.universe_lane == LANE_ALPACA_US and s.total_score < threshold),
            key=lambda s: s.total_score,
            reverse=True,
        )[:20]
        if near:
            self._record_audit(
                "dynamic_universe_near_miss_symbols",
                None,
                {
                    "threshold": threshold,
                    "symbols": [
                        {"symbol": s.symbol, "score": s.total_score, "block_reason": s.block_reason or "score below research threshold", "data_confidence": s.data_confidence}
                        for s in near
                    ],
                },
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

    def _score_reason_labels(self, score: ResearchScore) -> list[str]:
        labels: list[str] = []
        if score.liquidity_score >= 12:
            labels.append("liquidity")
        if score.trend_score >= 14:
            labels.append("trend")
        if score.intraday_momentum_score >= 9:
            labels.append("intraday strength")
        if score.relative_strength_score >= 8.5:
            labels.append("relative strength")
        if score.volatility_quality_score >= 8:
            labels.append("controlled volatility")
        if score.screener_mover_score >= 7:
            labels.append("screener momentum")
        if score.news_score > 2.5:
            labels.append("news catalyst")
        return labels or ["score above research threshold"]

    def _observation_requirement_status(self, score: ResearchScore, metadata: dict[str, Any]) -> dict[str, Any]:
        enough_score = score.total_score >= self._observation_threshold()
        component_count = self._positive_component_count(score)
        confidence_ok = score.data_confidence in {"medium", "high"}
        liquidity_ok = score.liquidity_score >= 12.0 and not score.block_reason
        trend_ok = score.trend_score >= 14.0
        rs_ok = score.relative_strength_score >= 8.5
        freshness_ok = self._data_freshness_status == "fresh"
        cluster_clean = bool(metadata.get("cluster")) and metadata.get("cluster") != "unknown_cluster"
        requirements = {
            "score_at_or_above_observation_threshold": enough_score,
            "at_least_two_positive_components": component_count >= 2,
            "data_confidence_medium_or_high": confidence_ok,
            "liquidity_passes": liquidity_ok,
            "trend_passes": trend_ok,
            "relative_strength_passes": rs_ok,
            "provider_data_fresh_enough": freshness_ok,
            "cluster_mapping_clean": cluster_clean,
            "intraday_confirmation_needed": score.intraday_momentum_score < 9.0,
            "enough_observation_cycles_passed": False,
        }
        missing = [key for key, passed in requirements.items() if key != "intraday_confirmation_needed" and not passed]
        satisfied = [key for key, passed in requirements.items() if passed]
        return {
            "requirements": requirements,
            "satisfied": satisfied,
            "missing": missing,
            "current_blockers": missing or ["observation promotion requires a later market-open check"],
        }

    def _paper_requirement_status(self, score: ResearchScore, metadata: dict[str, Any]) -> dict[str, Any]:
        promo = self.cfg.get("promotion", {})
        confidence_ok = score.data_confidence == "high" or (score.data_confidence == "medium" and bool(promo.get("allow_medium_confidence_paper_tradable", True)))
        requirements = {
            "alpaca_compatible_us_lane": metadata.get("universe_lane") == LANE_ALPACA_US,
            "paper_tradable_score_threshold": score.total_score >= self._paper_tradable_threshold(),
            "minimum_observation_cycles": False,
            "minimum_observation_sessions": False,
            "shadow_tracking_recorded": False,
            "cluster_mapping_clean": bool(metadata.get("cluster")) and metadata.get("cluster") != "unknown_cluster",
            "confidence_allowed": confidence_ok,
            "risk_engine_must_pass_later": False,
            "telegram_approval_required_later": False,
            "final_validation_required_later": False,
        }
        return {
            "requirements": requirements,
            "missing": [key for key, passed in requirements.items() if not passed],
        }

    def _next_expected_check(self, run_type: str) -> str:
        if run_type == "daily_deep_research":
            return "market-open refresh/promotion checks"
        if run_type == "intraday_light_refresh":
            minutes = int(self.cfg.get("schedules", {}).get("intraday_light_refresh_minutes", 30))
            return (self.now + timedelta(minutes=minutes)).isoformat()
        return "next scheduled Dynamic Universe check"

    def _record_candidate_brief(self, score: ResearchScore, metadata: dict[str, Any], rank: int, run_type: str) -> None:
        endpoint_coverage = metadata.get("endpoint_coverage") or {}
        local_metrics = metadata.get("local_metrics_available") or {}
        positives = self._score_reason_labels(score)
        missing_neutral = []
        if not endpoint_coverage.get("fundamentals"):
            missing_neutral.append("fundamentals unavailable or not required")
        if not endpoint_coverage.get("news"):
            missing_neutral.append(f"news neutral ({metadata.get('news_unavailable_reason') or 'unavailable or not called'})")
        if not endpoint_coverage.get("technicals"):
            missing_neutral.append("technical API not required; local MA/ATR/volatility used when available")
        if not local_metrics.get("rsi"):
            missing_neutral.append("RSI unavailable")
        obs_status = self._observation_requirement_status(score, metadata)
        paper_status = self._paper_requirement_status(score, metadata)
        blockers = list(obs_status["current_blockers"])
        if score.block_reason:
            blockers.insert(0, score.block_reason)
        latest_price = metadata.get("latest_price")
        avg_volume = metadata.get("avg_volume_20")
        dollar_volume = metadata.get("dollar_volume")
        ma20 = metadata.get("ma20")
        ma50 = metadata.get("ma50")
        ma200 = metadata.get("ma200")
        relative = metadata.get("relative_strength_20d")
        volatility = metadata.get("volatility")
        atr = metadata.get("atr")
        trend_bits = []
        if latest_price is not None and ma20 is not None:
            trend_bits.append("above MA20" if latest_price > ma20 else "below MA20")
        if ma20 is not None and ma50 is not None:
            trend_bits.append("MA20 above MA50" if ma20 > ma50 else "MA20 below MA50")
        if latest_price is not None and ma50 is not None:
            trend_bits.append("above MA50" if latest_price > ma50 else "below MA50")
        if ma200 is None:
            trend_bits.append("MA200 unavailable")
        intraday_return = metadata.get("intraday_return")
        intraday_summary = "intraday bars unavailable or neutral"
        if intraday_return is not None:
            intraday_summary = f"intraday return {intraday_return:.2%} over {metadata.get('intraday_points')} points"
        payload = {
            "observation_promotion": obs_status,
            "paper_tradable_promotion": paper_status,
            "endpoint_coverage": endpoint_coverage,
            "local_metrics_available": local_metrics,
            "llm_boundary": "explanation_only_never_decisioning",
        }
        self.storage.execute(
            """
            INSERT INTO research_candidate_briefs(
                id,run_id,symbol,company_name,current_tier,universe_lane,research_score,rank,data_confidence,
                latest_price,price_freshness,liquidity_metrics,dollar_volume,trend_summary,intraday_summary,
                relative_strength_vs_spy,sector,industry,sector_relative_context,volatility_risk_summary,
                screener_reason,main_positive_reasons,main_blockers,missing_neutral_data,endpoint_coverage,
                before_observation_requirements,before_paper_tradable_requirements,allowed_actions,blocked_actions,
                proposal_order_confirmation,last_pre_market_scan_at,last_candidate_brief_at,last_intraday_refresh_at,
                last_observation_check_at,next_expected_check,current_stage,next_stage_requirements,explanation_source,created_at,payload
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                str(uuid.uuid4()),
                self.run_id,
                score.symbol.upper(),
                metadata.get("Name") or metadata.get("name"),
                RESEARCH_CANDIDATE,
                score.universe_lane,
                score.total_score,
                rank,
                score.data_confidence,
                latest_price,
                metadata.get("price_freshness"),
                json_dumps({"avg_volume_20": avg_volume, "liquidity_score": score.liquidity_score}),
                dollar_volume,
                ", ".join(trend_bits) if trend_bits else "trend metrics unavailable",
                intraday_summary,
                f"20-day return proxy {relative:.2%}" if relative is not None else "relative strength proxy unavailable",
                metadata.get("sector"),
                metadata.get("industry"),
                f"cluster {metadata.get('cluster') or 'unknown_cluster'}",
                f"volatility {volatility:.2%}; ATR proxy {atr:.2f}" if volatility is not None and atr is not None else "volatility or ATR unavailable",
                metadata.get("reason") or metadata.get("source") or "deterministic scan score",
                ", ".join(positives),
                ", ".join(blockers),
                ", ".join(missing_neutral),
                json_dumps(endpoint_coverage),
                json_dumps(obs_status),
                json_dumps(paper_status),
                "research, track, report, explain",
                "trade proposals, orders, manual promotion, RiskEngine bypass",
                "No proposal or order is allowed at research-candidate tier.",
                self.now.isoformat() if run_type == "daily_deep_research" else None,
                iso_now(),
                self.now.isoformat() if run_type == "intraday_light_refresh" else None,
                iso_now(),
                self._next_expected_check(run_type),
                RESEARCH_CANDIDATE,
                json_dumps(obs_status),
                "deterministic",
                iso_now(),
                json_dumps(payload),
            ),
        )

    def _record_llm_explanation_usage(self, brief_items: list[tuple[ResearchScore, dict[str, Any]]]) -> None:
        llm_cfg = self.cfg.get("llm_explanations", {})
        enabled = bool(llm_cfg.get("enabled", False))
        status = "disabled"
        detail: dict[str, Any] = {"candidate_count": len(brief_items)}
        if not enabled:
            status = "llm_explanation_disabled"
        else:
            status = "llm_explanation_disabled_missing_safe_client"
            detail["reason"] = "disabled-by-default stub only; deterministic briefs remain source of truth"
        self.storage.execute(
            "INSERT INTO llm_explanation_usage(id,run_id,enabled,attempted_calls,successful_calls,failed_calls,discarded_invalid,conflicts_ignored,total_estimated_cost,status,created_at,detail) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), self.run_id, 1 if enabled else 0, 0, 0, 0, 0, 0, 0.0, status, iso_now(), json_dumps(detail)),
        )

    def _record_promotion(self, symbol: str, old_tier: str | None, new_tier: str, score: ResearchScore, metadata: dict[str, Any]) -> None:
        reason = score.block_reason or "deterministic promotion rule"
        if new_tier == PAPER_TRADABLE and metadata.get("promotion_freshness_path"):
            reason = f"dynamic promotion using {metadata.get('promotion_freshness_path')}; proposal blocked until fresh market validation"
        self.storage.execute(
            "INSERT INTO symbol_promotion_decisions(id,run_id,symbol,from_tier,to_tier,score,reason,deterministic_pass,gpt_summary_used,created_at,payload) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), self.run_id, symbol.upper(), old_tier, new_tier, score.total_score, reason, 1, 0, iso_now(), json_dumps(metadata)),
        )

    def _record_demotion(self, symbol: str, old_tier: str | None, score: ResearchScore | None, metadata: dict[str, Any], reason: str | None = None) -> None:
        self.storage.execute(
            "INSERT INTO symbol_demotion_decisions(id,run_id,symbol,from_tier,to_tier,score,reason,created_at,payload) VALUES(?,?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), self.run_id, symbol.upper(), old_tier, DEMOTED, score.total_score if score else None, reason or (score.block_reason if score else "demotion rule"), iso_now(), json_dumps(metadata)),
        )
