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

RAW_UNIVERSE = "raw_universe"
RESEARCH_CANDIDATE = "research_candidate"
OBSERVATION = "observation"
PAPER_TRADABLE = "paper_tradable"
DEMOTED = "demoted"
LANE_ALPACA_US = "alpaca_compatible_us"
LANE_GLOBAL_RESEARCH = "global_research_only"
LANE_EXCLUDED = "excluded_or_low_quality"
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
    def __init__(self, config: dict[str, Any], storage: Storage, provider: MarketResearchProvider | None, run_id: str) -> None:
        self.config = config
        self.storage = storage
        self.provider = provider
        self.run_id = run_id
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
                self._record_trend_snapshot(symbol, metadata, score)
                self._record_news(symbol, metadata)
            if self._demotion_allowed and not self._provider_failed:
                self._demote_stale_symbols(demoted)
            elif self._provider_failed:
                self._record_audit("dynamic_universe_demotions_blocked_provider_unavailable", None, {"run_type": run_type})
            status = "completed"
            self._provider_health_status = self._provider_summary_status()
            self._record_near_miss_symbols()
            detail = {"provider_status": self._provider_health_status, "run_type": run_type, "catchup": is_catchup}
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
        return {"status": status, "considered": considered, "promoted": promoted, "demoted": demoted, "run_id": run_id}

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
            fields.update(last_success_at=iso_now(), catchup_required=0, missed_count=0)
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
        lane = metadata.get("universe_lane") or LANE_ALPACA_US
        if lane == LANE_EXCLUDED:
            reason = metadata.get("exclusion_reason") or "excluded_or_low_quality"
            metadata["data_confidence"] = "insufficient"
            metadata["data_confidence_reason"] = reason
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
            elif res.status not in {"plan_limited", "rate_limited"}:
                self._provider_failed = True
                self._provider_health_status = res.status
        if self.provider and not metadata.get("existing_static") and bars:
            quote = self.provider.get_latest_quote(metadata.get("provider_symbol") or symbol)
            quote_ok = quote.status == "ok" and bool(quote.data)
        liquidity, liquidity_block = self._liquidity_score(metadata, bars)
        trend = self._trend_score(bars)
        intraday = self._intraday_momentum_score(symbol, metadata, bars)
        rel = self._relative_strength_score(bars)
        vol = self._volatility_quality_score(bars)
        screener = self._screener_mover_score(metadata)
        news, news_ok = self._news_score(symbol, metadata)
        sector = 5.0 if metadata.get("cluster") != "unknown_cluster" else 2.5
        quality = self._data_quality_score(metadata, bars)
        confidence, confidence_reason = self._data_confidence(metadata, bars, quote_ok, news_ok)
        metadata["data_confidence"] = confidence
        metadata["data_confidence_reason"] = confidence_reason
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

    def _intraday_momentum_score(self, symbol: str, metadata: dict[str, Any], bars: list[dict[str, Any]]) -> float:
        if metadata.get("existing_static"):
            return 10.0
        if not self.provider or not self.cfg.get("raw_sources", {}).get("eodhd_intraday_bars", True) or not bars:
            return 7.5 if bars else 0.0
        res = self.provider.get_intraday_bars(metadata.get("provider_symbol") or symbol, limit=60)
        if res.status != "ok" or not isinstance(res.data, list) or len(res.data) < 2:
            return 7.5
        closes = [float(b.get("close") or 0) for b in res.data if float(b.get("close") or 0) > 0]
        vols = [float(b.get("volume") or 0) for b in res.data if b.get("volume") is not None]
        if len(closes) < 2:
            return 7.5
        ret = closes[-1] / closes[0] - 1.0
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
            has_shadow = self._has_shadow_tracking(symbol)
            confidence_ok = score.data_confidence == "high" or (score.data_confidence == "medium" and bool(promo.get("allow_medium_confidence_paper_tradable", True)))
            if (
                metadata.get("universe_lane") == LANE_ALPACA_US
                and score.total_score >= self._paper_tradable_threshold()
                and cycles >= int(promo.get("min_observation_cycles", 3))
                and sessions >= int(promo.get("min_observation_sessions", 1))
                and has_shadow
                and metadata.get("cluster") != "unknown_cluster"
                and confidence_ok
            ):
                return PAPER_TRADABLE
            return OBSERVATION
        return current.get("tier") or RESEARCH_CANDIDATE

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
                last_seen_at,last_promoted_at,last_demoted_at,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
