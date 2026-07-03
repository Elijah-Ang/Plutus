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
    "crypto_paper_tradable",
    "crypto_trade_proposal",
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
    if not results:
        return "Crypto research: no enabled symbols. Research-only. No proposals/orders."
    scores = ", ".join(f"{res.symbol} {res.score:.0f}" for res in results)
    return f"Crypto research: {scores}. Research-only. No proposals/orders."


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
                json_dumps({"mode": cfg.get("mode"), "paper_trading_enabled": cfg.get("paper_trading_enabled", False), "proposals_enabled": cfg.get("proposals_enabled", False)}),
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
        reason = "research_only_no_proposals"
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
            status="research_only",
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

    def _record_performance_lab(self, result: CryptoResearchResult, now: datetime) -> str | None:
        tables = self.storage.fetch_all("SELECT name FROM sqlite_master WHERE type='table' AND name='performance_setups'")
        if not tables:
            return None
        setup_id = str(uuid.uuid4())
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
                "research_only", 0, result.reason, result.score, json_dumps(result.score_components),
                json_dumps({"action": "RESEARCH", "side": "none", "reason": result.reason}), 0, 0, 0,
                result.price, result.price_timestamp, result.data_freshness, json_dumps(result.trend_metrics),
                json_dumps({"realized_volatility": result.realized_volatility, "atr_like_volatility": result.atr_like_volatility}),
                json_dumps({"volume": result.volume, "spread": result.spread}),
                json_dumps({"vs_btc": "btc_baseline" if result.symbol == "BTC/USD" else "pending"}),
                json_dumps({"crypto_mode": (self.config.get("crypto") or {}).get("mode"), "paper_trading_enabled": False, "proposals_enabled": False}),
                (self.config.get("crypto") or {}).get("max_notional_per_trade"), now.isoformat(), now.isoformat(),
            ),
        )
        blockers = [("research_only", "crypto proposals disabled by default")]
        if result.price is None or result.data_freshness == "missing" or "provider_unavailable" in result.reason or "missing_crypto_bars" in result.reason:
            blockers.append(("crypto_data_unavailable", result.reason))
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
                (self.config.get("crypto") or {}).get("max_notional_per_trade"), "pending_forward_returns",
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
                    (now + timedelta(days=horizon)).isoformat(), 0, "pending", "crypto_research_only_waiting_for_elapsed_horizon",
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
                str(uuid.uuid4()), setup_id, self.run_id, result.symbol, "crypto_research_only",
                result.price, (self.config.get("crypto") or {}).get("max_notional_per_trade"), result.reason,
                "pending_forward_outcome", now.isoformat(), now.isoformat(),
            ),
        )
        return setup_id

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
            status="research_only",
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
    if cfg.get("paper_trading_enabled") and cfg.get("proposals_enabled") and score >= 80:
        return "crypto_paper_tradable"
    if score >= 65:
        return "crypto_observation"
    if score >= 50:
        return "crypto_research_candidate"
    return "crypto_raw"
