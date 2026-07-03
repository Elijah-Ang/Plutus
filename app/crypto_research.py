from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta, timezone
from typing import Any

from .storage import Storage
from .utils import json_dumps


CRYPTO_LANES = {
    "crypto_raw",
    "crypto_research_candidate",
    "crypto_observation",
    "crypto_paper_watch",
    "crypto_paper_tradable",
    "crypto_trade_proposal",
}

CRYPTO_MODES = {"research_only", "paper_watch", "paper_proposal"}

CRYPTO_BLOCKER_REASONS = {
    "crypto_research_only",
    "crypto_paper_disabled",
    "crypto_proposals_disabled",
    "crypto_pair_unsupported",
    "crypto_price_stale",
    "crypto_spread_too_wide",
    "crypto_orderbook_missing",
    "crypto_volatility_extreme",
    "crypto_liquidity_insufficient",
    "crypto_risk_reward_too_low",
    "crypto_stop_distance_invalid",
    "crypto_quiet_hours_notification_suppressed",
    "crypto_existing_position_conflict",
    "crypto_pending_order_conflict",
    "crypto_pending_proposal_conflict",
    "crypto_provider_unavailable",
    "crypto_alpaca_final_price_unavailable",
    "crypto_runtime_evidence_gate_failed",
}


@dataclass
class CryptoResearchResult:
    symbol: str
    lane: str
    price: float | None
    price_timestamp: str | None
    data_freshness: str
    score: float
    score_components: dict[str, Any]
    returns: dict[str, float | None]
    realized_volatility: float | None
    atr_like_volatility: float | None
    trend_metrics: dict[str, Any]
    volume: float | None
    spread: float | None
    risk_metrics: dict[str, Any]
    provider: str
    status: str
    reason: str
    setup_id: str | None = None


def normalize_crypto_symbol(symbol: str) -> str | None:
    raw = str(symbol or "").strip().upper().replace("-", "/")
    if not raw:
        return None
    if "/" in raw:
        base, quote = raw.split("/", 1)
    elif raw.endswith("USD"):
        base, quote = raw[:-3], "USD"
    else:
        return None
    if not base.isalpha() or quote != "USD":
        return None
    if base not in {"BTC", "ETH", "SOL"}:
        return None
    return f"{base}/USD"


def configured_crypto_symbols(config: dict[str, Any]) -> list[str]:
    cfg = config.get("crypto") or {}
    max_symbols = max(0, int(cfg.get("max_symbols", 2) or 0))
    symbols: list[str] = []
    for raw in cfg.get("symbols") or []:
        symbol = normalize_crypto_symbol(raw)
        if symbol and symbol not in symbols:
            symbols.append(symbol)
    return symbols[:max_symbols]


def crypto_quiet_hours_active(config: dict[str, Any], now: datetime | None = None) -> bool:
    cfg = ((config.get("crypto") or {}).get("schedule") or {}).get("quiet_hours_sgt") or {}
    if not cfg.get("enabled", False):
        return False
    now = now or datetime.now(UTC)
    sgt_now = now.astimezone(timezone(timedelta(hours=8))).time()
    start = _parse_hhmm(str(cfg.get("start", "01:00")))
    end = _parse_hhmm(str(cfg.get("end", "08:00")))
    if start <= end:
        return start <= sgt_now < end
    return sgt_now >= start or sgt_now < end


def format_crypto_digest(results: list[CryptoResearchResult]) -> str:
    mode = _crypto_mode_from_results(results)
    if not results:
        return f"Crypto research: no enabled symbols. Mode {mode}. No proposals/orders."
    scores = ", ".join(f"{res.symbol} {res.score:.0f}" for res in results)
    if mode == "paper_watch":
        suffix = "Paper-watch. Hypothetical candidates only. No proposals/orders."
    elif mode == "paper_proposal":
        suffix = "Paper-proposal gated. Manual approval and final validation required."
    else:
        suffix = "Research-only. No proposals/orders."
    return f"Crypto research: {scores}. {suffix}"


class CryptoResearchEngine:
    def __init__(self, config: dict[str, Any], storage: Storage, broker: Any | None = None, telegram: Any | None = None, run_id: str | None = None) -> None:
        self.config = config
        self.storage = storage
        self.broker = broker
        self.telegram = telegram
        self.run_id = run_id or str(uuid.uuid4())

    def run_due(self, now: datetime | None = None) -> list[CryptoResearchResult]:
        now = now or datetime.now(UTC)
        cfg = self.config.get("crypto") or {}
        schedule = cfg.get("schedule") or {}
        if not cfg.get("enabled", False) or not schedule.get("enabled", True):
            return []
        if not self._research_due(now):
            return []
        results = self.run_research(now=now)
        if self._digest_due(now) and not crypto_quiet_hours_active(self.config, now):
            self._send_digest(results, now)
        elif crypto_quiet_hours_active(self.config, now):
            self._set_state("crypto_last_digest_suppressed_at", now.isoformat())
        return results

    def run_research(self, symbols: list[str] | None = None, now: datetime | None = None) -> list[CryptoResearchResult]:
        now = now or datetime.now(UTC)
        cfg = self.config.get("crypto") or {}
        mode = _crypto_mode(self.config)
        enabled_symbols = symbols or configured_crypto_symbols(self.config)
        provider = str(cfg.get("data_source") or "alpaca")
        research_run_id = str(uuid.uuid4())
        self.storage.execute(
            "INSERT INTO crypto_research_runs(id,run_id,status,started_at,symbols,provider,payload) VALUES(?,?,?,?,?,?,?)",
            (
                research_run_id,
                self.run_id,
                "running",
                now.isoformat(),
                json_dumps(enabled_symbols),
                provider,
                json_dumps({"mode": mode, "paper_trading_enabled": cfg.get("paper_trading_enabled", False), "proposals_enabled": cfg.get("proposals_enabled", False)}),
            ),
        )
        results: list[CryptoResearchResult] = []
        status = "completed"
        error = None
        for symbol in enabled_symbols:
            result = self._research_symbol(symbol, research_run_id, now, provider)
            results.append(result)
            self._persist_result(result, research_run_id, now)
        self.storage.execute(
            "UPDATE crypto_research_runs SET status=?, ended_at=?, error=? WHERE id=?",
            (status, datetime.now(UTC).isoformat(), error, research_run_id),
        )
        self._set_state("crypto_last_research_at", now.isoformat())
        return results

    def _research_symbol(self, symbol: str, research_run_id: str, now: datetime, provider: str) -> CryptoResearchResult:
        normalized = normalize_crypto_symbol(symbol)
        if not normalized:
            return self._missing_result(symbol, provider, "unsupported_crypto_symbol")
        try:
            bars = self._get_crypto_bars(normalized)
        except Exception as exc:
            return self._missing_result(normalized, provider, f"provider_unavailable:{type(exc).__name__}")
        rows = _bar_rows(bars, normalized)
        if not rows:
            return self._missing_result(normalized, provider, "missing_crypto_bars")

        closes = [float(row["close"]) for row in rows if _is_number(row.get("close"))]
        price = closes[-1] if closes else None
        price_ts = rows[-1].get("timestamp")
        price_timestamp = _iso_timestamp(price_ts)
        max_age_seconds = float((self.config.get("crypto") or {}).get("max_price_age_seconds", 300) or 300)
        data_freshness = _freshness(price_ts, now, max_age_seconds)

        returns = {
            "1h": _return_at(closes, 1),
            "4h": _return_at(closes, 4),
            "1d": _return_at(closes, 24),
            "7d": _return_at(closes, 24 * 7),
            "20d": _return_at(closes, 24 * 20),
        }
        realized_vol = _realized_volatility(closes)
        atr_like = _atr_like(rows, price)
        trend_metrics = _trend_metrics(closes)
        volume = _last_number(rows, "volume")
        spread = self._safe_spread(normalized)
        score, components, risk_metrics = _score_crypto(
            data_freshness=data_freshness,
            returns=returns,
            realized_volatility=realized_vol,
            atr_like_volatility=atr_like,
            trend_metrics=trend_metrics,
            volume=volume,
            spread=spread,
        )
        lane = _lane_for_score(score, self.config)
        mode = _crypto_mode(self.config)
        reason = "research_only_no_proposals" if mode == "research_only" else f"{mode}_no_actionable_proposal"
        if data_freshness != "fresh":
            reason = "stale_crypto_data_no_proposals"
        return CryptoResearchResult(
            symbol=normalized,
            lane=lane,
            price=price,
            price_timestamp=price_timestamp,
            data_freshness=data_freshness,
            score=score,
            score_components=components,
            returns=returns,
            realized_volatility=realized_vol,
            atr_like_volatility=atr_like,
            trend_metrics=trend_metrics,
            volume=volume,
            spread=spread,
            risk_metrics=risk_metrics,
            provider=provider,
            status=mode,
            reason=reason,
        )

    def _get_crypto_bars(self, symbol: str) -> Any:
        if self.broker is None or not hasattr(self.broker, "get_crypto_historical_bars"):
            raise RuntimeError("crypto data provider unavailable")
        return self.broker.get_crypto_historical_bars(symbol, "1Hour", 500)

    def _safe_spread(self, symbol: str) -> float | None:
        if self.broker is None or not hasattr(self.broker, "get_crypto_latest_quote"):
            return None
        try:
            quote = self.broker.get_crypto_latest_quote(symbol)
            bid = getattr(quote, "bid_price", None) or getattr(quote, "bp", None)
            ask = getattr(quote, "ask_price", None) or getattr(quote, "ap", None)
            if _is_number(bid) and _is_number(ask) and float(ask) > 0:
                mid = (float(bid) + float(ask)) / 2
                return (float(ask) - float(bid)) / mid if mid > 0 else None
        except Exception:
            return None
        return None

    def _persist_result(self, result: CryptoResearchResult, research_run_id: str, now: datetime) -> None:
        setup_id = self._record_performance_lab(result, now)
        result.setup_id = setup_id
        self.storage.execute(
            """
            INSERT INTO crypto_research_snapshots(
                id,run_id,research_run_id,symbol,lane,price,price_timestamp,data_freshness,return_1h,return_4h,
                return_1d,return_7d,return_20d,realized_volatility,atr_like_volatility,trend_metrics,volume,spread,
                score,score_components,risk_metrics,provider,created_at,payload
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                str(uuid.uuid4()), self.run_id, research_run_id, result.symbol, result.lane, result.price,
                result.price_timestamp, result.data_freshness, result.returns.get("1h"), result.returns.get("4h"),
                result.returns.get("1d"), result.returns.get("7d"), result.returns.get("20d"),
                result.realized_volatility, result.atr_like_volatility, json_dumps(result.trend_metrics),
                result.volume, result.spread, result.score, json_dumps(result.score_components),
                json_dumps(result.risk_metrics), result.provider, now.isoformat(),
                json_dumps({"status": result.status, "reason": result.reason, "setup_id": setup_id}),
            ),
        )
        existing = self.storage.fetch_all("SELECT observation_since FROM crypto_observation_state WHERE symbol=?", (result.symbol,))
        observation_since = existing[0]["observation_since"] if existing and existing[0]["observation_since"] else now.isoformat()
        self.storage.execute(
            """
            INSERT INTO crypto_observation_state(
                symbol,lane,score,status,last_price,last_price_timestamp,data_freshness,last_research_at,observation_since,updated_at,payload
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(symbol) DO UPDATE SET
                lane=excluded.lane, score=excluded.score, status=excluded.status, last_price=excluded.last_price,
                last_price_timestamp=excluded.last_price_timestamp, data_freshness=excluded.data_freshness,
                last_research_at=excluded.last_research_at, updated_at=excluded.updated_at, payload=excluded.payload
            """,
            (
                result.symbol, result.lane, result.score, result.status, result.price, result.price_timestamp,
                result.data_freshness, now.isoformat(), observation_since, now.isoformat(),
                json_dumps({"risk_metrics": result.risk_metrics, "reason": result.reason}),
            ),
        )
        self.storage.execute(
            """
            INSERT INTO crypto_counterfactual_outcomes(
                id,run_id,research_run_id,setup_id,symbol,score,would_propose,reason,status,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                str(uuid.uuid4()), self.run_id, research_run_id, setup_id, result.symbol, result.score, 0,
                result.reason, "pending_forward_outcome", now.isoformat(), now.isoformat(),
            ),
        )
        self._record_stage_candidate(result, research_run_id, setup_id, now)

    def _record_performance_lab(self, result: CryptoResearchResult, now: datetime) -> str | None:
        tables = self.storage.fetch_all("SELECT name FROM sqlite_master WHERE type='table' AND name='performance_setups'")
        if not tables:
            return None
        setup_id = str(uuid.uuid4())
        cfg = self.config.get("crypto") or {}
        mode = _crypto_mode(self.config)
        candidate = _build_candidate_metadata(result, self.config, now)
        blockers = self._crypto_blockers(result, candidate, now)
        proposed = 1 if mode == "paper_proposal" and not blockers else 0
        action_decision = "paper_watch" if mode == "paper_watch" else ("paper_proposal" if mode == "paper_proposal" else "research_only")
        self.storage.execute(
            """
            INSERT INTO performance_setups(
                id,timestamp,run_id,symbol,asset_class,tier,setup_type,action_decision,proposed,not_proposed_reason,
                score,score_components,signal_state,entry_signal,exit_signal,add_signal,current_price,price_timestamp,
                data_freshness,trend_metrics,volatility_metrics,liquidity_metrics,relative_strength_metrics,risk_budget,
                hypothetical_notional,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                setup_id, now.isoformat(), self.run_id, result.symbol, "crypto", result.lane, "hold_watch",
                action_decision, proposed, result.reason, result.score, json_dumps(result.score_components),
                json_dumps({"action": "RESEARCH", "side": "none", "reason": result.reason, "mode": mode}), 0, 0, 0,
                result.price, result.price_timestamp, result.data_freshness, json_dumps(result.trend_metrics),
                json_dumps({"realized_volatility": result.realized_volatility, "atr_like_volatility": result.atr_like_volatility}),
                json_dumps({"volume": result.volume, "spread": result.spread}),
                json_dumps({"vs_btc": "btc_baseline" if result.symbol == "BTC/USD" else "pending"}),
                json_dumps(
                    {
                        "crypto_mode": mode,
                        "paper_trading_enabled": bool(cfg.get("paper_trading_enabled", False)),
                        "proposals_enabled": bool(cfg.get("proposals_enabled", False)),
                        "evidence_gate_required": mode == "paper_proposal",
                    }
                ),
                candidate.get("position_size"), now.isoformat(), now.isoformat(),
            ),
        )
        for blocker, reason in blockers:
            self.storage.execute(
                "INSERT INTO performance_blockers(id,setup_id,run_id,symbol,blocker,reason,severity,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), setup_id, self.run_id, result.symbol, blocker, reason, "blocking", now.isoformat()),
            )
        self.storage.execute(
            """
            INSERT INTO performance_outcomes(
                id,setup_id,run_id,symbol,actual_or_shadow,entry_time,entry_price,entry_notional,status,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                str(uuid.uuid4()), setup_id, self.run_id, result.symbol, "shadow", now.isoformat(), result.price,
                candidate.get("position_size"), "pending_forward_returns",
                now.isoformat(), now.isoformat(),
            ),
        )
        for horizon in (1, 5, 20):
            self.storage.execute(
                """
                INSERT INTO performance_forward_returns(
                    id,setup_id,run_id,symbol,horizon_days,due_at,eligible_to_update,status,reason
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    str(uuid.uuid4()), setup_id, self.run_id, result.symbol, horizon,
                    (now + timedelta(days=horizon)).isoformat(), 0, "pending", f"crypto_{mode}_waiting_for_elapsed_horizon",
                ),
            )
        self.storage.execute(
            """
            INSERT INTO performance_counterfactuals(
                id,setup_id,run_id,symbol,counterfactual_type,hypothetical_entry_price,hypothetical_notional,reason,
                comparison_status,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                str(uuid.uuid4()), setup_id, self.run_id, result.symbol, f"crypto_{mode}",
                result.price, candidate.get("position_size"), result.reason,
                "pending_forward_outcome", now.isoformat(), now.isoformat(),
            ),
        )
        return setup_id

    def _record_stage_candidate(self, result: CryptoResearchResult, research_run_id: str, setup_id: str | None, now: datetime) -> str | None:
        mode = _crypto_mode(self.config)
        if mode == "research_only":
            return None
        tables = self.storage.fetch_all("SELECT name FROM sqlite_master WHERE type='table' AND name='crypto_paper_watch_candidates'")
        if not tables:
            return None
        candidate = _build_candidate_metadata(result, self.config, now)
        blockers = self._crypto_blockers(result, candidate, now)
        status = "hypothetical"
        proposal_id = None
        if mode == "paper_proposal":
            actionable_blockers = [item for item in blockers if item[0] != "crypto_quiet_hours_notification_suppressed"]
            if actionable_blockers:
                status = "blocked"
            else:
                gate_passed, gate_reasons = self._runtime_evidence_gate_passed(now)
                if not gate_passed:
                    blockers.extend(("crypto_runtime_evidence_gate_failed", reason) for reason in gate_reasons)
                    status = "blocked"
                else:
                    proposal_id = self._create_crypto_trade_proposal(result, candidate, now)
                    status = "proposal_created" if proposal_id else "blocked"
                    if not proposal_id:
                        blockers.append(("crypto_proposals_disabled", "trade proposal creation did not return a proposal id"))
        row_id = str(uuid.uuid4())
        blocker_labels = [item[0] for item in blockers]
        self.storage.execute(
            """
            INSERT INTO crypto_paper_watch_candidates(
                id,run_id,research_run_id,setup_id,proposal_id,symbol,mode,status,score,entry_price,stop_price,
                take_profit_price,risk_reward_ratio,spread_bps,volatility_regime,position_notional,max_loss_estimate,
                blockers,candidate_metadata,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                row_id, self.run_id, research_run_id, setup_id, proposal_id, result.symbol, mode, status, result.score,
                candidate.get("entry_price"), candidate.get("stop_price"), candidate.get("take_profit_target"),
                candidate.get("risk_reward_ratio"), candidate.get("spread_bps"), candidate.get("volatility_regime"),
                candidate.get("position_size"), candidate.get("max_loss_estimate"), json_dumps(blocker_labels),
                json_dumps({"candidate": candidate, "blockers": blockers, "result_reason": result.reason}),
                now.isoformat(), now.isoformat(),
            ),
        )
        return row_id

    def _create_crypto_trade_proposal(self, result: CryptoResearchResult, candidate: dict[str, Any], now: datetime) -> str | None:
        cfg = self.config.get("crypto") or {}
        if not (cfg.get("paper_trading_enabled", False) and cfg.get("proposals_enabled", False)):
            return None
        if str(cfg.get("default_order_type") or "limit") != "limit" or cfg.get("fallback_market_orders", False):
            return None
        proposal_id = str(uuid.uuid4())
        expiry_minutes = int(cfg.get("proposal_expiry_minutes", 3) or 3)
        payload = {
            "asset_class": "crypto",
            "mode": "paper_proposal",
            "action": "entry",
            "order_type": "limit",
            "limit_price_source": cfg.get("limit_price_source", "midpoint_or_last_with_slippage_cap"),
            "candidate": candidate,
            "approval_max_price_age_seconds": cfg.get("approval_max_price_age_seconds", 30),
            "approval_max_price_move_bps_base": cfg.get("approval_max_price_move_bps_base", 50),
            "approval_max_price_move_bps_hard_cap": cfg.get("approval_max_price_move_bps_hard_cap", 100),
            "requires_manual_telegram_approval": True,
            "requires_alpaca_final_validation": True,
            "eodhd_final_trading_price_allowed": False,
        }
        self.storage.execute(
            """
            INSERT INTO trade_proposals(
                id,run_id,signal_id,symbol,side,notional,status,created_at,expires_at,strategy_version,payload,current_price
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                proposal_id, self.run_id, None, result.symbol, "buy", candidate.get("position_size"), "pending",
                now.isoformat(), (now + timedelta(minutes=expiry_minutes)).isoformat(), "crypto_paper_v1",
                json_dumps(payload), candidate.get("entry_price"),
            ),
        )
        if self.telegram is not None and not crypto_quiet_hours_active(self.config, now):
            try:
                message = (
                    f"Crypto paper proposal: {result.symbol} buy ${candidate.get('position_size'):.2f} "
                    f"limit near {candidate.get('entry_price'):.2f}; stop {candidate.get('stop_price'):.2f}; "
                    f"target {candidate.get('take_profit_target'):.2f}; R/R {candidate.get('risk_reward_ratio'):.2f}. "
                    "Manual approval required."
                )
                response = self.telegram.send_message(message)
                message_id = response.get("message_id") if isinstance(response, dict) else None
                if message_id:
                    self.storage.execute("UPDATE trade_proposals SET telegram_message_id=? WHERE id=?", (str(message_id), proposal_id))
            except Exception as exc:
                self.storage.audit(self.run_id, "crypto_proposal_telegram_send_failed", {"error": type(exc).__name__, "symbol": result.symbol})
        return proposal_id

    def _runtime_evidence_gate_passed(self, now: datetime) -> tuple[bool, list[str]]:
        cfg = self.config.get("crypto") or {}
        gate_cfg = cfg.get("runtime_evidence_gate") or {}
        if not gate_cfg.get("enabled", True):
            return True, []
        min_cycles = int(gate_cfg.get("min_natural_cycles", 3) or 3)
        max_age_hours = float(gate_cfg.get("max_cycle_age_hours", 72) or 72)
        symbols = configured_crypto_symbols(self.config)
        cutoff = (now - timedelta(hours=max_age_hours)).isoformat()
        reasons: list[str] = []
        for symbol in symbols:
            rows = self.storage.fetch_all(
                """
                SELECT COUNT(DISTINCT research_run_id) AS cycles,
                       SUM(CASE WHEN data_freshness='fresh' THEN 1 ELSE 0 END) AS fresh_rows,
                       SUM(CASE WHEN spread IS NOT NULL THEN 1 ELSE 0 END) AS spread_rows
                FROM crypto_research_snapshots
                WHERE symbol=? AND created_at>=?
                """,
                (symbol, cutoff),
            )
            row = rows[0] if rows else {}
            cycles = int(row["cycles"] or 0)
            fresh_rows = int(row["fresh_rows"] or 0)
            spread_rows = int(row["spread_rows"] or 0)
            if cycles < min_cycles:
                reasons.append(f"{symbol}:requires_{min_cycles}_fresh_cycles")
            if fresh_rows < min_cycles:
                reasons.append(f"{symbol}:fresh_alpaca_data_missing")
            if spread_rows < min_cycles:
                reasons.append(f"{symbol}:spread_missing")
        provider_errors = self.storage.fetch_all(
            """
            SELECT COUNT(*) AS cnt
            FROM crypto_research_snapshots
            WHERE created_at>=? AND (
                payload LIKE '%provider_unavailable%' OR
                payload LIKE '%missing_crypto_bars%' OR
                payload LIKE '%crypto_provider_unavailable%'
            )
            """,
            (cutoff,),
        )
        if provider_errors and int(provider_errors[0]["cnt"] or 0) > 0:
            reasons.append("unresolved_crypto_provider_errors")
        proposals = self.storage.fetch_all(
            """
            SELECT COUNT(*) AS cnt
            FROM trade_proposals
            WHERE created_at>=? AND run_id<>? AND (symbol LIKE '%/USD' OR json_extract(payload, '$.asset_class')='crypto')
            """,
            (cutoff, self.run_id),
        )
        if proposals and int(proposals[0]["cnt"] or 0) > 0:
            reasons.append("unexpected_existing_crypto_proposals")
        return not reasons, reasons

    def _crypto_blockers(self, result: CryptoResearchResult, candidate: dict[str, Any], now: datetime) -> list[tuple[str, str]]:
        cfg = self.config.get("crypto") or {}
        mode = _crypto_mode(self.config)
        blockers: list[tuple[str, str]] = []
        if result.symbol not in configured_crypto_symbols(self.config):
            blockers.append(("crypto_pair_unsupported", f"{result.symbol} is not in configured crypto symbols"))
        if mode == "research_only":
            blockers.append(("crypto_research_only", "crypto.mode=research_only"))
        if not cfg.get("paper_trading_enabled", False):
            blockers.append(("crypto_paper_disabled", "crypto.paper_trading_enabled=false"))
        if not cfg.get("proposals_enabled", False):
            blockers.append(("crypto_proposals_disabled", "crypto.proposals_enabled=false"))
        if result.price is None:
            blockers.append(("crypto_alpaca_final_price_unavailable", result.reason))
        if result.data_freshness != "fresh":
            blockers.append(("crypto_price_stale", f"data_freshness={result.data_freshness}"))
        if result.data_freshness == "missing" or "provider_unavailable" in result.reason or "missing_crypto_bars" in result.reason:
            blockers.append(("crypto_provider_unavailable", result.reason))
        max_spread_bps = float(cfg.get("max_spread_bps", 50.0) or 50.0)
        spread_bps = candidate.get("spread_bps")
        if spread_bps is None:
            blockers.append(("crypto_orderbook_missing", "Alpaca crypto quote/spread unavailable"))
        elif float(spread_bps) > max_spread_bps:
            blockers.append(("crypto_spread_too_wide", f"spread_bps={float(spread_bps):.2f} max={max_spread_bps:.2f}"))
        max_vol = float(cfg.get("max_realized_volatility", 1.5) or 1.5)
        if result.realized_volatility is not None and result.realized_volatility > max_vol:
            blockers.append(("crypto_volatility_extreme", f"realized_volatility={result.realized_volatility:.4f}"))
        if not result.volume or result.volume <= 0:
            blockers.append(("crypto_liquidity_insufficient", "latest crypto volume missing or zero"))
        min_rr = float(cfg.get("min_risk_reward_ratio", 1.5) or 1.5)
        rr = candidate.get("risk_reward_ratio")
        if rr is None or rr < min_rr:
            blockers.append(("crypto_risk_reward_too_low", f"risk_reward_ratio={rr} min={min_rr}"))
        stop_distance_pct = candidate.get("stop_distance_pct")
        if stop_distance_pct is None or stop_distance_pct <= 0:
            blockers.append(("crypto_stop_distance_invalid", "stop distance must be positive"))
        if crypto_quiet_hours_active(self.config, now):
            blockers.append(("crypto_quiet_hours_notification_suppressed", "non-urgent Telegram crypto status suppressed"))
        local_pending = self.storage.fetch_all(
            "SELECT COUNT(*) AS cnt FROM trade_proposals WHERE symbol=? AND status IN ('pending','approved','submitted')",
            (result.symbol,),
        )
        if local_pending and int(local_pending[0]["cnt"] or 0) > 0:
            blockers.append(("crypto_pending_proposal_conflict", "local pending crypto proposal exists"))
        local_orders = self.storage.fetch_all(
            "SELECT COUNT(*) AS cnt FROM orders WHERE symbol=? AND status NOT IN ('filled','canceled','cancelled','rejected','expired')",
            (result.symbol,),
        )
        if local_orders and int(local_orders[0]["cnt"] or 0) > 0:
            blockers.append(("crypto_pending_order_conflict", "local pending crypto order exists"))
        local_positions = self.storage.fetch_all(
            "SELECT COUNT(*) AS cnt FROM positions WHERE symbol=? AND qty>0",
            (result.symbol,),
        )
        if local_positions and int(local_positions[0]["cnt"] or 0) > 0:
            blockers.append(("crypto_existing_position_conflict", "local crypto position exists"))
        return _dedupe_blockers(blockers)

    def _missing_result(self, symbol: str, provider: str, reason: str) -> CryptoResearchResult:
        return CryptoResearchResult(
            symbol=str(symbol).upper(),
            lane="crypto_raw",
            price=None,
            price_timestamp=None,
            data_freshness="missing",
            score=0.0,
            score_components={"data_freshness": 0.0, "reason": reason},
            returns={"1h": None, "4h": None, "1d": None, "7d": None, "20d": None},
            realized_volatility=None,
            atr_like_volatility=None,
            trend_metrics={},
            volume=None,
            spread=None,
            risk_metrics={"provider_guard": reason},
            provider=provider,
            status=_crypto_mode(self.config),
            reason=reason,
        )

    def _research_due(self, now: datetime) -> bool:
        last = self.storage.get_control_state("crypto_last_research_at")
        if not last:
            return True
        interval = float(((self.config.get("crypto") or {}).get("schedule") or {}).get("research_interval_minutes", 60) or 60)
        return now - _parse_dt(last) >= timedelta(minutes=interval)

    def _digest_due(self, now: datetime) -> bool:
        last = self.storage.get_control_state("crypto_last_digest_at")
        if not last:
            return True
        interval = float(((self.config.get("crypto") or {}).get("schedule") or {}).get("digest_interval_minutes", 240) or 240)
        return now - _parse_dt(last) >= timedelta(minutes=interval)

    def _send_digest(self, results: list[CryptoResearchResult], now: datetime) -> None:
        if self.telegram is None:
            return
        try:
            self.telegram.send_message(format_crypto_digest(results))
            self._set_state("crypto_last_digest_at", now.isoformat())
        except Exception as exc:
            self.storage.audit(self.run_id, "crypto_digest_send_failed", {"error": type(exc).__name__})

    def _set_state(self, key: str, value: str) -> None:
        self.storage.set_control_state(key, value, "system", "crypto_research", "", None, None, None)


def _crypto_mode(config: dict[str, Any]) -> str:
    mode = str((config.get("crypto") or {}).get("mode") or "research_only")
    return mode if mode in CRYPTO_MODES else "research_only"


def _crypto_mode_from_results(results: list[CryptoResearchResult]) -> str:
    if not results:
        return "research_only"
    status = str(results[0].status or "research_only")
    return status if status in CRYPTO_MODES else "research_only"


def _build_candidate_metadata(result: CryptoResearchResult, config: dict[str, Any], now: datetime) -> dict[str, Any]:
    cfg = config.get("crypto") or {}
    entry_price = float(result.price) if result.price and result.price > 0 else None
    atr_stop_pct = float(result.atr_like_volatility or 0.0) * 2.0
    stop_distance_pct = max(float(cfg.get("min_stop_distance_pct", 0.01) or 0.01), atr_stop_pct)
    stop_distance_pct = min(stop_distance_pct, float(cfg.get("max_stop_distance_pct", 0.08) or 0.08))
    stop_price = entry_price * (1.0 - stop_distance_pct) if entry_price else None
    min_rr = float(cfg.get("min_risk_reward_ratio", 1.5) or 1.5)
    take_profit_target = entry_price * (1.0 + stop_distance_pct * min_rr) if entry_price else None
    risk_reward_ratio = None
    if entry_price and stop_price and take_profit_target and entry_price > stop_price:
        risk_reward_ratio = (take_profit_target - entry_price) / (entry_price - stop_price)
    max_notional = float(cfg.get("max_notional_per_trade", 5.0) or 5.0)
    account_risk_notional = float(cfg.get("max_account_risk_per_trade", max_notional * stop_distance_pct) or (max_notional * stop_distance_pct))
    risk_based_notional = account_risk_notional / stop_distance_pct if stop_distance_pct > 0 else 0.0
    position_size = max(0.0, min(max_notional, risk_based_notional))
    max_loss_estimate = position_size * stop_distance_pct if stop_distance_pct > 0 else None
    spread_bps = float(result.spread) * 10000.0 if result.spread is not None else None
    provider_coverage = {
        "alpaca": {
            "bars": result.data_freshness != "missing",
            "final_price": result.price is not None,
            "quote_spread": result.spread is not None,
            "final_price_timestamp": result.price_timestamp,
            "authority": "final_price_tradability_positions_orders_execution",
        },
        "eodhd": {
            "available": bool(cfg.get("eodhd_research_enabled", True)),
            "authority": "research_context_only",
            "final_trading_price_allowed": False,
        },
    }
    return {
        "entry_price": entry_price,
        "stop_price": stop_price,
        "stop_distance_pct": stop_distance_pct,
        "take_profit_target": take_profit_target,
        "risk_reward_ratio": risk_reward_ratio,
        "spread_bps": spread_bps,
        "volatility_regime": _volatility_regime(result.realized_volatility),
        "position_size": position_size,
        "max_loss_estimate": max_loss_estimate,
        "provider_coverage": provider_coverage,
        "alpaca_final_price_timestamp": result.price_timestamp,
        "long_only_spot": True,
        "allow_margin": False,
        "allow_shorting": False,
        "allow_add_to_winner": bool(cfg.get("allow_add_to_winner", False)),
        "allow_new_entries": bool(cfg.get("allow_new_entries", True)),
        "allow_exits": bool(cfg.get("allow_exits", True)),
        "default_order_type": cfg.get("default_order_type", "limit"),
        "limit_price_source": cfg.get("limit_price_source", "midpoint_or_last_with_slippage_cap"),
        "fallback_market_orders": bool(cfg.get("fallback_market_orders", False)),
        "computed_at": now.isoformat(),
    }


def _volatility_regime(realized_volatility: float | None) -> str:
    if realized_volatility is None:
        return "unknown"
    if realized_volatility > 1.5:
        return "extreme"
    if realized_volatility > 1.0:
        return "high"
    if realized_volatility > 0.6:
        return "elevated"
    if realized_volatility < 0.2:
        return "quiet"
    return "normal"


def _dedupe_blockers(blockers: list[tuple[str, str]]) -> list[tuple[str, str]]:
    seen: set[str] = set()
    deduped: list[tuple[str, str]] = []
    for blocker, reason in blockers:
        if blocker not in CRYPTO_BLOCKER_REASONS:
            blocker = "crypto_provider_unavailable" if "provider" in blocker else blocker
        if blocker in seen:
            continue
        seen.add(blocker)
        deduped.append((blocker, reason))
    return deduped


def _parse_hhmm(value: str) -> time:
    hour, minute = value.split(":", 1)
    return time(int(hour), int(minute))


def _parse_dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _iso_timestamp(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _freshness(value: Any, now: datetime, max_age_seconds: float) -> str:
    if value is None:
        return "missing"
    try:
        ts = _parse_dt(_iso_timestamp(value) or "")
        return "fresh" if abs((now - ts).total_seconds()) <= max_age_seconds else "stale"
    except Exception:
        return "unknown"


def _bar_rows(bars: Any, symbol: str) -> list[dict[str, Any]]:
    if bars is None:
        return []
    if isinstance(bars, list):
        return [_row_from_object(row) for row in bars]
    if hasattr(bars, "empty") and bars.empty:
        return []
    if hasattr(bars, "reset_index"):
        frame = bars
        try:
            if getattr(frame.index, "names", None) and "symbol" in [str(x).lower() for x in frame.index.names if x is not None]:
                frame = frame.reset_index()
            elif frame.index.name:
                frame = frame.reset_index()
            records = []
            for rec in frame.to_dict("records"):
                rec_symbol = str(rec.get("symbol") or rec.get("Symbol") or symbol).upper()
                if rec_symbol == symbol.upper():
                    records.append(_row_from_object(rec))
            return records
        except Exception:
            return []
    return []


def _row_from_object(row: Any) -> dict[str, Any]:
    if isinstance(row, dict):
        source = row
    else:
        source = {
            "timestamp": getattr(row, "timestamp", None),
            "open": getattr(row, "open", None),
            "high": getattr(row, "high", None),
            "low": getattr(row, "low", None),
            "close": getattr(row, "close", None),
            "volume": getattr(row, "volume", None),
        }
    timestamp = source.get("timestamp") or source.get("time") or source.get("t")
    return {
        "timestamp": timestamp,
        "open": source.get("open") or source.get("o"),
        "high": source.get("high") or source.get("h"),
        "low": source.get("low") or source.get("l"),
        "close": source.get("close") or source.get("c"),
        "volume": source.get("volume") or source.get("v"),
    }


def _is_number(value: Any) -> bool:
    try:
        return value is not None and math.isfinite(float(value))
    except Exception:
        return False


def _last_number(rows: list[dict[str, Any]], key: str) -> float | None:
    for row in reversed(rows):
        if _is_number(row.get(key)):
            return float(row[key])
    return None


def _return_at(closes: list[float], periods: int) -> float | None:
    if len(closes) <= periods or closes[-periods - 1] <= 0:
        return None
    return closes[-1] / closes[-periods - 1] - 1.0


def _realized_volatility(closes: list[float]) -> float | None:
    if len(closes) < 25:
        return None
    rets = []
    for prev, cur in zip(closes[-169:-1], closes[-168:]):
        if prev > 0:
            rets.append(cur / prev - 1.0)
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    variance = sum((ret - mean) ** 2 for ret in rets) / (len(rets) - 1)
    return math.sqrt(variance) * math.sqrt(24 * 365)


def _atr_like(rows: list[dict[str, Any]], price: float | None) -> float | None:
    if price is None or price <= 0:
        return None
    ranges = []
    for row in rows[-24:]:
        if _is_number(row.get("high")) and _is_number(row.get("low")):
            ranges.append(float(row["high"]) - float(row["low"]))
    if not ranges:
        return None
    return (sum(ranges) / len(ranges)) / price


def _trend_metrics(closes: list[float]) -> dict[str, Any]:
    latest = closes[-1] if closes else None
    sma_20 = sum(closes[-20:]) / 20 if len(closes) >= 20 else None
    sma_50 = sum(closes[-50:]) / 50 if len(closes) >= 50 else None
    return {
        "close": latest,
        "sma_20": sma_20,
        "sma_50": sma_50,
        "above_sma_20": bool(latest and sma_20 and latest > sma_20),
        "above_sma_50": bool(latest and sma_50 and latest > sma_50),
    }


def _score_crypto(
    *,
    data_freshness: str,
    returns: dict[str, float | None],
    realized_volatility: float | None,
    atr_like_volatility: float | None,
    trend_metrics: dict[str, Any],
    volume: float | None,
    spread: float | None,
) -> tuple[float, dict[str, Any], dict[str, Any]]:
    freshness = 20.0 if data_freshness == "fresh" else 0.0
    trend = 0.0
    if trend_metrics.get("above_sma_20"):
        trend += 12.5
    if trend_metrics.get("above_sma_50"):
        trend += 12.5
    momentum = 0.0
    for key, weight in (("4h", 7.0), ("1d", 7.0), ("7d", 6.0)):
        ret = returns.get(key)
        if ret is not None and ret > 0:
            momentum += weight
    liquidity = 10.0 if volume and volume > 0 else 5.0
    if spread is None:
        liquidity += 3.0
    elif spread <= 0.002:
        liquidity += 5.0
    elif spread <= 0.01:
        liquidity += 2.0
    volatility = 10.0
    if realized_volatility is not None and realized_volatility > 1.5:
        volatility = 4.0
    elif realized_volatility is not None and realized_volatility > 1.0:
        volatility = 7.0
    drawdown_risk = 10.0
    if returns.get("20d") is not None and returns["20d"] < -0.15:
        drawdown_risk = 3.0
    score = max(0.0, min(100.0, freshness + trend + momentum + liquidity + volatility + drawdown_risk))
    components = {
        "freshness": freshness,
        "trend": trend,
        "momentum": momentum,
        "liquidity_spread": liquidity,
        "volatility_regime": volatility,
        "drawdown_risk": drawdown_risk,
    }
    risk_metrics = {
        "realized_volatility": realized_volatility,
        "atr_like_volatility": atr_like_volatility,
        "spread": spread,
        "require_fresh_price": True,
        "allow_margin": False,
        "allow_shorting": False,
    }
    return score, components, risk_metrics


def _lane_for_score(score: float, config: dict[str, Any]) -> str:
    cfg = config.get("crypto") or {}
    mode = _crypto_mode(config)
    if mode == "paper_proposal" and cfg.get("paper_trading_enabled") and cfg.get("proposals_enabled") and score >= float(cfg.get("min_score_for_proposal", 80) or 80):
        return "crypto_paper_tradable"
    if mode == "paper_watch" and score >= float(cfg.get("min_score_for_paper_watch", 70) or 70):
        return "crypto_paper_watch"
    if score >= 65:
        return "crypto_observation"
    if score >= 50:
        return "crypto_research_candidate"
    return "crypto_raw"
