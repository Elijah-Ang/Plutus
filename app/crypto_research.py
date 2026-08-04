from __future__ import annotations

import hashlib
import json
import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .crypto_capabilities import CryptoCapabilitySnapshot, CryptoCapabilityStore
from .crypto_market_data import CryptoMarketDataStore, CryptoMarketEvidence
from .crypto_strategies import CryptoStrategyError, CryptoStrategyStore
from .crypto_paper_lane import CryptoPaperLaneError, CryptoPaperLaneStore, format_crypto_paper_proposal
from .crypto_risk import CryptoRiskError, CryptoRiskStore
from .crypto_sizing import CryptoSizingRequest
from .crypto_outcomes import CryptoCostModel, calculate_shadow_outcome, persist_observation
from .crypto_profitability import CryptoProfitabilityStore
from .cross_asset_allocation import (
    _policy as cross_asset_policy,
    build_crypto_exploration_evidence,
)
from .fixed_point_accounting import RECONSTRUCTED_REAL_PROVENANCE
from .formula_versions import FIXED_POINT_ACCOUNTING_VERSION
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

CRYPTO_MODES = {"research_only", "paper_watch", "paper_proposal", "supervised_paper"}

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
    "crypto_stage3_enablement_requires_separate_approval",
    "crypto_capability_unverified",
    "crypto_market_data_unverified",
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
    capability_snapshot_id: str | None = None
    capability_snapshot_fingerprint: str | None = None
    capability_authoritative: bool = False
    capability_failure_reasons: tuple[str, ...] = ()
    market_evidence_id: str | None = None
    market_evidence_fingerprint: str | None = None
    market_evidence_authoritative: bool = False
    market_execution_eligible: bool = False
    market_evidence_failure_reasons: tuple[str, ...] = ()
    strategy_decision_id: str | None = None
    strategy_decision_fingerprint: str | None = None
    selected_strategy: str | None = None
    strategy_lifecycle: str = "RESEARCH_ONLY"
    strategy_signal_eligible: bool = False
    strategy_blockers: tuple[str, ...] = ()


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


def _telegram_sent_identity(sent: Any, telegram: Any) -> tuple[str | None, str | None]:
    """Extract the exact Telegram message and chat identities for binding."""

    message_id = getattr(sent, "message_id", None)
    chat_id = getattr(sent, "chat_id", None)
    if isinstance(sent, Mapping):
        message_id = message_id or sent.get("message_id") or sent.get("id")
        chat = sent.get("chat") or {}
        if isinstance(chat, Mapping):
            chat_id = chat_id or chat.get("id")
    chat_id = chat_id or getattr(telegram, "chat_id", None)
    if message_id in (None, "") or chat_id in (None, ""):
        return None, None
    return str(message_id), str(chat_id)


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
        suffix = "Stage 3 readiness-report only. No proposals/orders until a separate approved config-change commit."
    elif mode == "supervised_paper":
        suffix = "Supervised paper lane. Every order requires a fresh Telegram display and explicit manual approval."
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

    def run_due(
        self,
        now: datetime | None = None,
        *,
        defer_entry_proposals: bool = False,
    ) -> list[CryptoResearchResult]:
        now = now or datetime.now(UTC)
        cfg = self.config.get("crypto") or {}
        schedule = cfg.get("schedule") or {}
        if not cfg.get("enabled", False) or not schedule.get("enabled", True):
            return []
        if not self._research_due(now):
            return []
        results = self.run_research(
            now=now,
            defer_entry_proposals=defer_entry_proposals,
        )
        if self._digest_due(now) and not crypto_quiet_hours_active(self.config, now):
            self._send_digest(results, now)
        elif crypto_quiet_hours_active(self.config, now):
            self._set_state("crypto_last_digest_suppressed_at", now.isoformat())
        return results

    def run_research(
        self,
        symbols: list[str] | None = None,
        now: datetime | None = None,
        *,
        defer_entry_proposals: bool = False,
    ) -> list[CryptoResearchResult]:
        now = now or datetime.now(UTC)
        cfg = self.config.get("crypto") or {}
        mode = _crypto_mode(self.config)
        enabled_symbols = symbols or configured_crypto_symbols(self.config)
        provider = str(cfg.get("data_source") or "alpaca")
        research_run_id = str(uuid.uuid4())
        capability = CryptoCapabilityStore(self.storage).capture(
            self.config,
            self.broker,
            self.run_id,
            now=now,
        )
        self.storage.execute(
            """INSERT INTO crypto_research_runs(
                   id,run_id,status,started_at,symbols,provider,
                   capability_snapshot_id,capability_snapshot_fingerprint,
                   capability_authoritative,payload
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                research_run_id,
                self.run_id,
                "running",
                now.isoformat(),
                json_dumps(enabled_symbols),
                provider,
                capability.id,
                capability.snapshot_fingerprint,
                int(capability.authoritative),
                json_dumps({
                    "mode": mode,
                    "paper_trading_enabled": cfg.get("paper_trading_enabled", False),
                    "proposals_enabled": cfg.get("proposals_enabled", False),
                    "capability_failure_reasons": capability.failure_reasons,
                }),
            ),
        )
        results: list[CryptoResearchResult] = []
        status = "completed"
        error = None
        for symbol in enabled_symbols:
            result = self._research_symbol(
                symbol,
                research_run_id,
                now,
                provider,
                capability,
                defer_entry_proposals=defer_entry_proposals,
            )
            results.append(result)
            self._persist_result(result, research_run_id, now)
        if mode == "supervised_paper" and self.broker is not None:
            try:
                self._monitor_crypto_positions(results, capability, now)
            except Exception as exc:
                self.storage.audit(
                    self.run_id,
                    "crypto_position_management_failed_closed",
                    {"error": type(exc).__name__},
                )
        self.storage.execute(
            "UPDATE crypto_research_runs SET status=?, ended_at=?, error=? WHERE id=?",
            (status, datetime.now(UTC).isoformat(), error, research_run_id),
        )
        self._set_state("crypto_last_research_at", now.isoformat())
        return results

    def _research_symbol(
        self,
        symbol: str,
        research_run_id: str,
        now: datetime,
        provider: str,
        capability: CryptoCapabilitySnapshot,
        *,
        defer_entry_proposals: bool = False,
    ) -> CryptoResearchResult:
        normalized = normalize_crypto_symbol(symbol)
        if not normalized:
            return self._with_capability(
                self._missing_result(symbol, provider, "unsupported_crypto_symbol"), capability
            )
        market_evidence = CryptoMarketDataStore(self.storage).capture(
            self.config,
            self.broker,
            capability,
            self.run_id,
            research_run_id,
            normalized,
            now=now,
        )
        try:
            bars = self._get_crypto_bars(normalized)
        except Exception as exc:
            result = self._missing_result(normalized, provider, f"provider_unavailable:{type(exc).__name__}")
            self._with_market_evidence(result, market_evidence)
            return self._with_capability(result, capability)
        rows = _bar_rows(bars, normalized)
        if not rows:
            result = self._missing_result(normalized, provider, "missing_crypto_bars")
            self._with_market_evidence(result, market_evidence)
            return self._with_capability(result, capability)

        closes = [float(row["close"]) for row in rows if _is_number(row.get("close"))]
        price = closes[-1] if closes else None
        price_ts = rows[-1].get("timestamp")
        price_timestamp = _iso_timestamp(price_ts)
        crypto_cfg = self.config.get("crypto") or {}
        strategy_cfg = crypto_cfg.get("strategy_policy") or {}
        # ``max_price_age_seconds`` is the quote/evidence freshness gate. The
        # research series is hourly, so its latest-bar gate must come from the
        # strategy cadence policy; otherwise an hourly bar is marked stale
        # after five minutes and the supervised lane can never become
        # proposal-eligible between bar boundaries.
        max_age_seconds = float(
            strategy_cfg.get("maximum_latest_bar_age_seconds")
            or crypto_cfg.get("max_price_age_seconds", 300)
            or 300
        )
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
        spread = (
            float(market_evidence.spread_bps) / 10000.0
            if market_evidence.spread_bps is not None
            else None
        )
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
        result = CryptoResearchResult(
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
        self._with_market_evidence(result, market_evidence)
        if market_evidence.authoritative:
            decision = None
            try:
                decision = CryptoStrategyStore(self.storage).evaluate(
                    self.config, self.run_id, research_run_id,
                    market_evidence.id, bars, now=now,
                )
                result.strategy_decision_id = decision.id
                result.strategy_decision_fingerprint = decision.decision_fingerprint
                result.selected_strategy = decision.selected_strategy
                result.strategy_lifecycle = decision.lifecycle
                result.strategy_signal_eligible = decision.signal_eligible
                result.strategy_blockers = decision.blockers
                result.risk_metrics.update({
                    "strategy_decision_id": decision.id,
                    "strategy_decision_fingerprint": decision.decision_fingerprint,
                    "selected_strategy": decision.selected_strategy,
                    "strategy_lifecycle": decision.lifecycle,
                    "strategy_signal_eligible": decision.signal_eligible,
                    "strategy_blockers": list(decision.blockers),
                    "strategy_stop_price": decision.stop_price,
                    "strategy_target_price": decision.target_price,
                    "strategy_expected_reward_r": decision.expected_reward_r,
                })
            except CryptoStrategyError as exc:
                result.strategy_blockers = ("crypto_strategy_evaluation_failed",)
                result.risk_metrics["crypto_strategy_error"] = str(exc)
            try:
                self._settle_crypto_shadow_outcomes(
                    normalized,
                    rows,
                    now=now,
                    exclude_strategy_decision_id=None if decision is None else decision.id,
                )
            except Exception as exc:
                self.storage.audit(
                    self.run_id,
                    "crypto_shadow_outcome_settlement_failed_closed",
                    {"symbol": normalized, "error": type(exc).__name__},
                )
            if decision is not None and not defer_entry_proposals:
                self._maybe_create_supervised_paper_proposal(
                    result, capability, market_evidence, decision, now,
                )
        return self._with_capability(result, capability)

    def create_deferred_supervised_entry_proposals(
        self,
        results: list[CryptoResearchResult],
        allocation_decisions: Any,
        *,
        now: datetime | None = None,
    ) -> list[str]:
        """Materialise only crypto entries allocated by the current advisory plan.

        The allocator is intentionally non-authoritative.  This method is the
        narrow bridge back into the existing crypto risk/proposal lane: every
        candidate must have a positive supervised-paper allocation from the
        same scan, and the risk engine still rebuilds its own current broker
        authority before a proposal is persisted.
        """

        current = (now or datetime.now(UTC)).astimezone(UTC)
        by_source: dict[str, Any] = {}
        by_candidate: dict[str, Any] = {}
        for raw in allocation_decisions or ():
            if not isinstance(raw, dict):
                continue
            decision = str(raw.get("decision") or "")
            if not decision.startswith("ALLOCATE_SUPERVISED_PAPER_ADVISORY"):
                continue
            if raw.get("order_authority") is not False:
                continue
            try:
                allocated = Decimal(str(raw.get("allocated_notional")))
            except (InvalidOperation, TypeError, ValueError):
                continue
            if not allocated.is_finite() or allocated <= Decimal("0"):
                continue
            source_id = str(raw.get("source_id") or "").strip()
            if source_id:
                by_source[source_id] = raw
            candidate_id = str(raw.get("candidate_id") or "").strip()
            if candidate_id:
                by_candidate[candidate_id] = raw

        proposal_ids: list[str] = []
        for result in results:
            source_id = str(result.strategy_decision_id or "").strip()
            allocation = by_source.get(source_id)
            if allocation is None:
                for action in ("entry", "add"):
                    allocation = by_candidate.get(
                        f"crypto:{result.symbol}:{source_id}:{action}"
                    )
                    if allocation is not None:
                        break
            if allocation is None or not result.strategy_signal_eligible:
                continue
            if not result.capability_snapshot_id or not result.market_evidence_id:
                continue
            try:
                capability = CryptoCapabilityStore(self.storage).load_verified(
                    result.capability_snapshot_id,
                    self.config,
                    now=current,
                )
                market_evidence = CryptoMarketDataStore(self.storage).load_verified(
                    result.market_evidence_id,
                    self.config,
                )
                strategy = CryptoStrategyStore(self.storage).load_verified(
                    source_id,
                    self.config,
                )
                allocation_action = str(allocation.get("action") or "").strip().lower()
                if allocation_action not in {"entry", "add"}:
                    continue
                if (
                    str(allocation.get("asset_class") or "").lower() != "crypto"
                    or str(allocation.get("execution_lane") or "").lower() != "supervised_paper"
                    or str(allocation.get("symbol") or "").upper().replace("-", "/")
                    != str(strategy.symbol).upper().replace("-", "/")
                    or str(allocation.get("strategy_version") or "")
                    != str(strategy.selected_strategy)
                    or str(allocation.get("candidate_id") or "")
                    != f"crypto:{strategy.symbol}:{strategy.id}:{allocation_action}"
                    or allocation.get("manual_approval_required") is not True
                    or allocation.get("order_authority") is not False
                ):
                    continue
                profitability = CryptoProfitabilityStore(self.storage).load_verified(
                    str(allocation.get("source_id") or "")
                )
                if (
                    str(allocation.get("source_type") or "")
                    != "candidate_profitability_decision"
                    or str(allocation.get("source_fingerprint") or "")
                    != profitability.decision_fingerprint
                    or profitability.strategy_decision_id != strategy.id
                    or profitability.config_hash
                    != str(self.config.get("effective_config_hash") or "")
                ):
                    self.storage.audit(
                        self.run_id,
                        "crypto_deferred_proposal_failed_closed",
                        {
                            "symbol": result.symbol,
                            "error": "allocation_source_fingerprint_mismatch",
                        },
                    )
                    continue
                if allocation.get("exploration_eligible") is True:
                    expected_exploration = build_crypto_exploration_evidence(
                        profitability, cross_asset_policy(self.config)
                    )
                    if json_dumps(dict(allocation.get("exploration_evidence") or {})) != json_dumps(expected_exploration):
                        self.storage.audit(
                            self.run_id,
                            "crypto_deferred_proposal_failed_closed",
                            {
                                "symbol": result.symbol,
                                "error": "exploration_evidence_mismatch",
                            },
                        )
                        continue
                requested = Decimal(str(allocation["requested_notional"]))
                allocated = Decimal(str(allocation["allocated_notional"]))
                if (
                    not requested.is_finite()
                    or requested <= Decimal("0")
                    or not allocated.is_finite()
                    or allocated <= Decimal("0")
                    or allocated > requested
                ):
                    continue
                fraction = min(Decimal("1"), max(Decimal("0"), allocated / requested))
                if fraction <= Decimal("0"):
                    continue
                positive_position_symbols: set[str] = set()
                for position in self.broker.get_positions():
                    position_symbol = str(
                        position.get("symbol", "")
                        if isinstance(position, dict)
                        else getattr(position, "symbol", "")
                    ).strip().upper().replace("-", "/")
                    raw_quantity = (
                        position.get("qty", position.get("quantity"))
                        if isinstance(position, dict)
                        else getattr(position, "qty", getattr(position, "quantity", None))
                    )
                    try:
                        position_quantity = Decimal(str(raw_quantity))
                    except (InvalidOperation, TypeError, ValueError) as exc:
                        raise CryptoPaperLaneError("allocation position quantity is invalid") from exc
                    if not position_quantity.is_finite() or position_quantity < Decimal("0"):
                        raise CryptoPaperLaneError("allocation position quantity is outside its safe range")
                    if position_quantity > Decimal("0"):
                        positive_position_symbols.add(position_symbol)
                expected_action = (
                    "add"
                    if str(strategy.symbol).upper().replace("-", "/") in positive_position_symbols
                    else "entry"
                )
                if allocation_action != expected_action:
                    continue
                self._maybe_create_supervised_paper_proposal(
                    result,
                    capability,
                    market_evidence,
                    strategy,
                    current,
                    risk_fraction=fraction,
                )
                proposal_id = str(result.risk_metrics.get("supervised_paper_proposal_id") or "")
                if proposal_id:
                    proposal_ids.append(proposal_id)
            except Exception as exc:
                self.storage.audit(
                    self.run_id,
                    "crypto_deferred_proposal_failed_closed",
                    {"symbol": result.symbol, "error": type(exc).__name__},
                )
        return proposal_ids

    def _monitor_crypto_positions(
        self,
        results: list[CryptoResearchResult],
        capability: CryptoCapabilitySnapshot,
        now: datetime,
    ) -> None:
        """Continuously evaluate held crypto positions and stage approved exits.

        This loop is deliberately one-way: it may persist an evidence-backed
        SELL proposal, but it cannot approve, reserve, submit, add to, or
        rotate a position.  The Telegram listener remains the only authority
        that can turn one of these proposals into a paper intent.
        """

        lane_cfg = (self.config.get("crypto") or {}).get("supervised_paper_lane") or {}
        if lane_cfg.get("enabled") is not True or lane_cfg.get("execution_enabled") is not True:
            return
        get_positions = getattr(self.broker, "get_positions", None)
        if not callable(get_positions):
            self.storage.audit(self.run_id, "crypto_position_management_blocked", {"reason": "positions_adapter_missing"})
            return
        positions = list(get_positions())
        result_by_symbol = {str(result.symbol).upper().replace("-", "/"): result for result in results}
        management_cfg = self.config.get("position_management") or {}
        for raw_position in positions:
            symbol = str(getattr(raw_position, "symbol", "") or (raw_position.get("symbol") if isinstance(raw_position, dict) else "")).upper().replace("-", "/")
            if symbol not in {"BTC/USD", "ETH/USD"}:
                continue
            side = str(getattr(raw_position, "side", "long") or (raw_position.get("side") if isinstance(raw_position, dict) else "long")).lower()
            if side not in {"", "long"}:
                self.storage.audit(self.run_id, "crypto_position_management_blocked", {"symbol": symbol, "reason": "short_position_detected"})
                continue
            quantity = _crypto_decimal(getattr(raw_position, "qty", None) if not isinstance(raw_position, dict) else raw_position.get("qty"), "crypto position quantity")
            if quantity <= Decimal("0"):
                continue
            result = result_by_symbol.get(symbol)
            if result is None or not result.market_evidence_id or not result.market_execution_eligible or not result.capability_authoritative:
                self.storage.audit(self.run_id, "crypto_position_management_blocked", {"symbol": symbol, "reason": "current_market_authority_missing"})
                continue
            quote = self.broker.get_crypto_latest_quote(symbol)
            bid = _crypto_decimal(getattr(quote, "bid_price", None) if not isinstance(quote, dict) else quote.get("bid_price"), "crypto management bid")
            if bid <= Decimal("0"):
                continue
            basis_rows = self.storage.fetch_all(
                "SELECT remaining_quantity,unit_cost,opened_at FROM crypto_paper_lots WHERE symbol=? AND remaining_quantity<>'0' ORDER BY opened_at,id",
                (symbol,),
            )
            basis_quantity = sum((_crypto_decimal(row["remaining_quantity"], "crypto lot quantity") for row in basis_rows), Decimal("0"))
            if basis_quantity < quantity:
                self.storage.audit(
                    self.run_id,
                    "crypto_position_management_blocked",
                    {"symbol": symbol, "reason": "verified_local_basis_below_broker_quantity", "broker_quantity": str(quantity), "verified_basis_quantity": str(basis_quantity)},
                )
                continue
            weighted_cost = sum(
                (_crypto_decimal(row["remaining_quantity"], "crypto lot quantity") * _crypto_decimal(row["unit_cost"], "crypto lot cost") for row in basis_rows),
                Decimal("0"),
            )
            average_entry = weighted_cost / basis_quantity if basis_quantity > Decimal("0") else Decimal("0")
            previous_rows = self.storage.fetch_all(
                "SELECT * FROM crypto_paper_position_management WHERE symbol=?",
                (symbol,),
            )
            previous = previous_rows[0] if previous_rows else None
            prior_peak = _crypto_decimal(previous["peak_price"], "crypto position peak") if previous else Decimal("0")
            peak = max(prior_peak, bid)
            sizing_cfg = (self.config.get("crypto") or {}).get("sizing_policy") or {}
            strategy_cfg = (self.config.get("crypto") or {}).get("strategy_policy") or {}
            stop_distance = _crypto_decimal(sizing_cfg.get("minimum_stop_distance_pct"), "crypto minimum stop distance") / Decimal("100")
            stop_price = average_entry * (Decimal("1") - stop_distance)
            if previous and previous["stop_price"]:
                stop_price = max(stop_price, _crypto_decimal(previous["stop_price"], "crypto persisted stop"))
            target_r = _crypto_decimal(strategy_cfg.get("target_reward_r_multiple"), "crypto target reward")
            profit_target = average_entry + (average_entry - stop_price) * target_r
            try:
                lot_opened_at = _oldest_crypto_lot_opened_at(basis_rows)
            except ValueError:
                self.storage.audit(
                    self.run_id,
                    "crypto_position_management_blocked",
                    {"symbol": symbol, "reason": "crypto_lot_opened_at_missing_or_invalid"},
                )
                continue
            # Position age is anchored to the oldest verified open lot, never
            # to the timestamp of a management row created by the scanner.
            created_at = lot_opened_at.isoformat()
            created_dt = lot_opened_at
            time_stop_cfg = management_cfg.get("time_stop") or {}
            hold_days = _crypto_decimal(time_stop_cfg.get("min_hold_days_before_time_stop") or "3", "crypto time stop days")
            time_stop_at = created_dt + timedelta(seconds=int(hold_days * Decimal("86400")))
            gain_pct = (bid / average_entry - Decimal("1")) * Decimal("100") if average_entry > Decimal("0") else Decimal("0")
            peak_gain_pct = (peak / average_entry - Decimal("1")) * Decimal("100") if average_entry > Decimal("0") else Decimal("0")
            action: str | None = None
            fraction = Decimal("1")
            reason = ""
            trailing_cfg = management_cfg.get("trailing_stop") or {}
            profit_cfg = management_cfg.get("profit_taking") or {}
            if bid <= stop_price:
                action, fraction, reason = "exit", Decimal("1"), "protective_stop"
            elif not result.strategy_signal_eligible and result.strategy_blockers:
                action, fraction, reason = "exit", Decimal("1"), "thesis_invalidation"
            elif now >= time_stop_at and gain_pct <= _crypto_decimal(time_stop_cfg.get("max_unrealized_gain_pct") or "0.5", "crypto time-stop gain"):
                action, fraction, reason = "exit", _crypto_decimal(time_stop_cfg.get("sell_fraction") or "1", "crypto time-stop sell fraction"), "time_stop"
            elif trailing_cfg.get("enabled") is True and peak_gain_pct >= _crypto_decimal(trailing_cfg.get("trailing_stop_start_gain_pct") or "2", "crypto trailing activation"):
                giveback = _crypto_decimal(trailing_cfg.get("trailing_stop_giveback_pct") or "1.5", "crypto trailing giveback") / Decimal("100")
                if bid <= peak * (Decimal("1") - giveback):
                    action, fraction, reason = "reduce", _crypto_decimal(trailing_cfg.get("sell_fraction") or "0.5", "crypto trailing sell fraction"), "trailing_stop"
            elif profit_cfg.get("enabled") is True and bid >= profit_target:
                action, fraction, reason = "reduce", _crypto_decimal(profit_cfg.get("level_1_sell_fraction") or "0.25", "crypto profit sell fraction"), "profit_target"
            fraction = min(Decimal("1"), max(Decimal("0"), fraction))
            requested_quantity = quantity if fraction >= Decimal("1") else quantity * fraction
            if action is not None and requested_quantity <= Decimal("0"):
                action = None
            thesis_fingerprint = result.strategy_decision_fingerprint or _sha256_text(f"{symbol}:{result.strategy_blockers}")
            proposal_id: str | None = None
            if action is not None:
                # Include the current quantity and peak in the idempotency key.
                # A partial/reduction fill changes the remaining quantity and
                # must be eligible for a subsequent approved reduction, while
                # an unchanged position must not be spammed with duplicates.
                action_key = (
                    f"{symbol}:{action}:{reason}:{thesis_fingerprint}:"
                    f"{_decimal_text(requested_quantity)}:{_decimal_text(peak)}"
                )
                pending = self.storage.fetch_all(
                    "SELECT id FROM crypto_paper_proposals WHERE symbol=? AND action=? AND status IN ('pending','approved','intent_created','submitted','partially_filled') LIMIT 1",
                    (symbol, action),
                )
                if not pending and (previous is None or str(previous["last_action"] or "") != action_key):
                    request = CryptoSizingRequest(
                        source_type="crypto_position_management",
                        source_id=action_key,
                        source_fingerprint=_sha256_text(action_key),
                        symbol=symbol,
                        side="sell",
                        action=action,
                        request_basis="quantity",
                        requested_exit_quantity=requested_quantity,
                        close_entire_position=action == "exit" and fraction >= Decimal("1"),
                    )
                    evaluation = CryptoRiskStore(self.storage).evaluate(
                        self.config,
                        self.broker,
                        self.run_id,
                        capability.id,
                        result.market_evidence_id,
                        request,
                        now=now,
                    )
                    if evaluation.risk_eligible:
                        lane = CryptoPaperLaneStore(self.storage)
                        proposal = lane.create_proposal(self.config, None, evaluation.decision_id, now=now)
                        # The proposal itself remains immutable; the stable key
                        # is persisted in the position-management state and in
                        # the audit record for deduplication/recovery.
                        proposal_id = proposal.id
                        rendered = format_crypto_paper_proposal(proposal)
                        self._deliver_crypto_proposal(lane, proposal, rendered, now=now)
                        self.storage.audit(
                            self.run_id,
                            "crypto_position_management_proposal_created",
                            {"symbol": symbol, "action": action, "reason": reason, "proposal_id": proposal.id, "quantity": _decimal_text(requested_quantity)},
                        )
                    else:
                        self.storage.audit(
                            self.run_id,
                            "crypto_position_management_blocked",
                            {"symbol": symbol, "action": action, "reason": reason, "risk_reasons": list(evaluation.reasons)},
                        )
            self.storage.execute(
                """INSERT INTO crypto_paper_position_management(
                   id,symbol,quantity,average_entry_price,peak_price,stop_price,profit_target_price,
                   time_stop_at,thesis_fingerprint,last_action,last_proposal_id,updated_at,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(symbol) DO UPDATE SET quantity=excluded.quantity,
                   average_entry_price=excluded.average_entry_price,peak_price=excluded.peak_price,
                   stop_price=excluded.stop_price,profit_target_price=excluded.profit_target_price,
                   time_stop_at=excluded.time_stop_at,thesis_fingerprint=excluded.thesis_fingerprint,
                   last_action=COALESCE(excluded.last_action,crypto_paper_position_management.last_action),
                   last_proposal_id=COALESCE(excluded.last_proposal_id,crypto_paper_position_management.last_proposal_id),
                   updated_at=excluded.updated_at,created_at=excluded.created_at""",
                (
                    str(uuid.uuid4()), symbol, _decimal_text(quantity), _decimal_text(average_entry), _decimal_text(peak),
                    _decimal_text(stop_price), _decimal_text(profit_target), time_stop_at.isoformat(), thesis_fingerprint,
                    action_key if action else None, proposal_id, now.isoformat(), created_at,
                ),
            )

    def _maybe_create_supervised_paper_proposal(
        self,
        result: CryptoResearchResult,
        capability: CryptoCapabilitySnapshot,
        market_evidence: CryptoMarketEvidence,
        strategy: Any,
        now: datetime,
        *,
        risk_fraction: Decimal | None = None,
    ) -> None:
        """Create an executable paper proposal only for the explicit lane gate.

        The default configuration never enters this method.  When the
        separately reviewed supervised lane is enabled, risk authority is
        rebuilt from the current broker account and the proposal is persisted
        before (and independently of) any Telegram send.  A Telegram failure
        therefore leaves an unapprovable, durable paper proposal rather than
        inventing a fallback or submitting anything.
        """

        lane = (self.config.get("crypto") or {}).get("supervised_paper_lane") or {}
        if lane.get("enabled") is not True or lane.get("execution_enabled") is not True:
            return
        asset = capability.asset(result.symbol)
        if not capability.authoritative or asset is None or not asset.authoritative or not market_evidence.authoritative or not market_evidence.execution_eligible:
            return
        if not strategy.signal_eligible or strategy.action != "entry" or strategy.stop_price is None:
            return
        try:
            action = "entry"
            positions = list(self.broker.get_positions()) if self.broker is not None else []
            matching_positions = []
            for position in positions:
                raw_symbol = str(
                    position.get("symbol") if isinstance(position, dict) else getattr(position, "symbol", "")
                ).strip().upper().replace("-", "/")
                if raw_symbol.replace("/", "") != str(result.symbol).replace("/", ""):
                    continue
                raw_quantity = position.get("qty", position.get("quantity")) if isinstance(position, dict) else getattr(position, "qty", getattr(position, "quantity", None))
                try:
                    quantity = Decimal(str(raw_quantity))
                except (InvalidOperation, TypeError, ValueError):
                    quantity = Decimal("0")
                if quantity > 0:
                    matching_positions.append(position)
            if len(matching_positions) > 1:
                result.risk_metrics["supervised_paper_blockers"] = ["duplicate_crypto_position_evidence"]
                return
            if matching_positions:
                if (self.config.get("crypto") or {}).get("allow_add_to_winner") is not True:
                    result.risk_metrics["supervised_paper_blockers"] = ["crypto_add_to_winner_disabled"]
                    return
                position = matching_positions[0]
                raw_average = (
                    position.get("avg_entry_price", position.get("average_entry_price"))
                    if isinstance(position, dict)
                    else getattr(position, "avg_entry_price", getattr(position, "average_entry_price", None))
                )
                average_entry = Decimal(str(raw_average))
                bid = Decimal(str(market_evidence.bid_price))
                if not average_entry.is_finite() or average_entry <= 0 or not bid.is_finite() or bid <= average_entry:
                    result.risk_metrics["supervised_paper_blockers"] = ["crypto_add_requires_profitable_winner"]
                    return
                action = "add"
            result.risk_metrics["supervised_paper_action"] = action
            account = self.broker.get_account() if self.broker is not None else None
            equity = Decimal(str(getattr(account, "equity", None) or (account or {}).get("equity")))
            stop_risk_pct = Decimal(str(((self.config.get("crypto") or {}).get("risk_policy") or {}).get("maximum_stop_risk_per_trade_pct_equity")))
            requested_risk = equity * stop_risk_pct / Decimal("100")
            if risk_fraction is not None:
                if not risk_fraction.is_finite() or risk_fraction <= Decimal("0"):
                    return
                requested_risk *= min(Decimal("1"), risk_fraction)
            if not requested_risk.is_finite() or requested_risk <= 0:
                return
            request = CryptoSizingRequest(
                source_type="crypto_strategy_decision",
                source_id=f"{strategy.id}:{action}",
                source_fingerprint=hashlib.sha256(f"{strategy.decision_fingerprint}:{action}".encode("utf-8")).hexdigest(),
                symbol=str(strategy.symbol), side="buy", action=action, request_basis="notional",
                requested_stop_risk_dollars=requested_risk, stop_price=str(strategy.stop_price),
            )
            evaluation = CryptoRiskStore(self.storage).evaluate(
                self.config, self.broker, self.run_id, capability.id, market_evidence.id, request, now=now,
            )
            if not evaluation.risk_eligible:
                result.risk_metrics["supervised_paper_blockers"] = list(evaluation.reasons)
                return
            lane_store = CryptoPaperLaneStore(self.storage)
            proposal = lane_store.create_proposal(
                self.config, strategy.id, evaluation.decision_id, now=now,
            )
            result.risk_metrics["supervised_paper_proposal_id"] = proposal.id
            result.status = "supervised_paper"
            result.reason = "fresh_manual_approval_required"
            rendered = format_crypto_paper_proposal(proposal)
            self._deliver_crypto_proposal(lane_store, proposal, rendered, now=now)
        except (CryptoPaperLaneError, CryptoRiskError, InvalidOperation, TypeError, ValueError) as exc:
            result.risk_metrics["supervised_paper_blockers"] = [type(exc).__name__]
        except Exception as exc:
            result.risk_metrics["supervised_paper_blockers"] = [f"unexpected_{type(exc).__name__}"]
            self.storage.audit(
                self.run_id, "crypto_supervised_paper_lane_failed_closed",
                {"symbol": result.symbol, "error": type(exc).__name__},
            )

    def _with_capability(
        self,
        result: CryptoResearchResult,
        capability: CryptoCapabilitySnapshot,
    ) -> CryptoResearchResult:
        asset = capability.asset(result.symbol)
        result.capability_snapshot_id = capability.id
        result.capability_snapshot_fingerprint = capability.snapshot_fingerprint
        result.capability_authoritative = bool(
            capability.authoritative and asset is not None and asset.authoritative
        )
        reasons = list(capability.failure_reasons)
        if asset is None:
            reasons.append(f"{result.symbol}:asset_capability_missing")
        elif not asset.authoritative:
            reasons.extend(f"{result.symbol}:{reason}" for reason in asset.failure_reasons)
        result.capability_failure_reasons = tuple(sorted(set(reasons)))
        return result

    def _with_market_evidence(
        self,
        result: CryptoResearchResult,
        evidence: CryptoMarketEvidence,
    ) -> CryptoResearchResult:
        result.market_evidence_id = evidence.id
        result.market_evidence_fingerprint = evidence.evidence_fingerprint
        result.market_evidence_authoritative = evidence.authoritative
        result.market_execution_eligible = evidence.execution_eligible
        result.market_evidence_failure_reasons = evidence.failure_reasons
        result.risk_metrics.update({
            "market_evidence_id": evidence.id,
            "market_evidence_fingerprint": evidence.evidence_fingerprint,
            "market_evidence_authoritative": evidence.authoritative,
            "market_execution_eligible": evidence.execution_eligible,
            "top_of_book_notional": evidence.top_of_book_notional,
            "quote_timestamp": evidence.quote_timestamp,
            "orderbook_timestamp": evidence.orderbook_timestamp,
            "market_evidence_failure_reasons": list(evidence.failure_reasons),
            "market_evidence_warnings": list(evidence.warnings),
        })
        return result

    def _get_crypto_bars(self, symbol: str) -> Any:
        if self.broker is None or not hasattr(self.broker, "get_crypto_historical_bars"):
            raise RuntimeError("crypto data provider unavailable")
        return self.broker.get_crypto_historical_bars(symbol, "1Hour", 500)

    def _deliver_crypto_proposal(
        self,
        lane: CryptoPaperLaneStore,
        proposal: Any,
        rendered: str,
        *,
        now: datetime,
    ) -> bool:
        """Use a durable exactly-once send claim and bind its returned identity."""

        if self.telegram is None:
            lane.mark_unbound_manual_review(
                proposal.id, reason="Telegram client is unavailable", now=now
            )
            return False
        chat_id = str(getattr(self.telegram, "chat_id", "") or "")
        if not chat_id:
            lane.mark_unbound_manual_review(
                proposal.id, reason="Telegram chat identity is unavailable", now=now
            )
            return False
        outbox = lane.queue_telegram_display(
            proposal.id,
            self.config,
            chat_id=chat_id,
            rendered_text=rendered,
            now=now,
        )
        status = str(outbox.get("status") or "")
        if status == "sent":
            message_id = str(outbox.get("telegram_message_id") or "")
            sent_chat = str(outbox.get("telegram_chat_id") or chat_id)
            if not message_id:
                lane.mark_unbound_manual_review(
                    proposal.id, reason="sent Telegram outbox has no message identity", now=now
                )
                return False
            lane.bind_telegram_message(
                proposal.id,
                message_id,
                self.config,
                chat_id=sent_chat,
                rendered_text=rendered,
                now=now,
            )
            return True
        if status != "queued" or not lane.claim_telegram_display(proposal.id, now=now):
            lane.mark_unbound_manual_review(
                proposal.id,
                reason=f"Telegram outbox is not safely sendable ({status})",
                now=now,
            )
            return False
        try:
            sent = self.telegram.send_message(rendered)
            message_id, returned_chat_id = _telegram_sent_identity(sent, self.telegram)
            if message_id is None or returned_chat_id is None:
                lane.mark_telegram_failed(
                    proposal.id,
                    reason="Telegram send did not return both message and chat identity",
                    now=now,
                )
                lane.mark_unbound_manual_review(
                    proposal.id,
                    reason="Telegram send did not return both message and chat identity",
                    now=now,
                )
                return False
            lane.mark_telegram_sent(
                proposal.id,
                message_id=message_id,
                chat_id=returned_chat_id,
                now=now,
            )
            lane.bind_telegram_message(
                proposal.id,
                message_id,
                self.config,
                chat_id=returned_chat_id,
                rendered_text=rendered,
                now=now,
            )
            return True
        except Exception as exc:
            lane.mark_telegram_failed(
                proposal.id, reason=f"Telegram send failed:{type(exc).__name__}", now=now
            )
            lane.mark_unbound_manual_review(
                proposal.id, reason=f"Telegram display binding failed:{type(exc).__name__}", now=now
            )
            self.storage.audit(
                self.run_id,
                "crypto_paper_proposal_telegram_send_failed",
                {"proposal_id": proposal.id, "error": type(exc).__name__},
            )
            return False

    def _settle_crypto_shadow_outcomes(
        self,
        symbol: str,
        rows: list[dict[str, Any]],
        *,
        now: datetime,
        exclude_strategy_decision_id: str | None,
    ) -> None:
        """Settle prior signal setups against newly observed hourly bars.

        These are real forward shadow observations from the provider's bars;
        no outcome is created when the future horizon is not yet observable.
        The sidecar remains append-only, so a later bar set naturally produces
        a new evidence fingerprint for a maturing setup.
        """

        policy = (self.config.get("crypto") or {}).get("profitability_policy") or {}
        horizon_hours = int(policy.get("outcome_horizon_hours") or 0)
        if horizon_hours <= 0:
            raise ValueError("crypto outcome horizon policy is unavailable")
        sizing = (self.config.get("crypto") or {}).get("sizing_policy") or {}
        cost_model = CryptoCostModel(
            version="crypto_round_trip_cost_v1",
            fee_bps=Decimal(str(sizing.get("conservative_taker_fee_bps_per_side"))),
            spread_bps=Decimal(str((self.config.get("crypto") or {}).get("max_spread_bps"))),
            slippage_bps=Decimal(str(sizing.get("stop_execution_slippage_bps"))),
        )
        bar_evidence = [
            {
                "timestamp": row["timestamp"],
                "high": str(row["high"]),
                "low": str(row["low"]),
                "close": str(row["close"]),
            }
            for row in rows
            if row.get("timestamp") is not None and row.get("high") is not None
            and row.get("low") is not None and row.get("close") is not None
        ]
        decisions = self.storage.fetch_all(
            """
            SELECT id,decision_fingerprint,symbol,selected_strategy,stop_price,target_price,
                   as_of,decision_json
            FROM crypto_strategy_decisions
            WHERE symbol=? AND selected_strategy IS NOT NULL
            ORDER BY created_at DESC LIMIT 200
            """,
            (symbol,),
        )
        for row in decisions:
            if exclude_strategy_decision_id and row["id"] == exclude_strategy_decision_id:
                continue
            try:
                payload = json.loads(row["decision_json"])
                metrics = payload.get("metrics") if isinstance(payload, Mapping) else None
                if not isinstance(metrics, Mapping):
                    continue
                entry = metrics.get("close")
                stop = row["stop_price"]
                target = row["target_price"]
                if entry in (None, "") or stop in (None, "") or target in (None, ""):
                    continue
                entry_decimal = _crypto_decimal(entry, "crypto shadow entry price")
                shadow_notional = _crypto_decimal(
                    sizing.get("maximum_order_notional_usd"),
                    "crypto shadow sizing notional",
                )
                if entry_decimal <= Decimal("0") or shadow_notional <= Decimal("0"):
                    raise ValueError("crypto shadow sizing basis must be positive")
                setup = {
                    "symbol": symbol,
                    "strategy_decision_id": str(row["id"]),
                    "strategy_decision_fingerprint": str(row["decision_fingerprint"]),
                    "research_timestamp": row["as_of"],
                    "entry_price": str(entry),
                    "stop_price": str(stop),
                    "target_price": str(target),
                    "horizon_hours": horizon_hours,
                    "cost_model": cost_model.to_payload(),
                    "side": "long",
                    # Forward shadow outcomes use the configured paper order
                    # notional as a deterministic hypothetical sizing basis.
                    # This is not execution authority; it ensures P&L and
                    # risk-multiple evidence is available for profitability
                    # ranking instead of silently remaining quantity-less.
                    "quantity": _decimal_text(shadow_notional / entry_decimal),
                }
                observation = calculate_shadow_outcome(
                    setup,
                    bar_evidence,
                    horizon_hours=horizon_hours,
                    cost_model=cost_model,
                )
                persist_observation(self.storage, observation)
            except Exception as exc:
                self.storage.audit(
                    self.run_id,
                    "crypto_shadow_outcome_rejected",
                    {"symbol": symbol, "strategy_decision_id": row["id"], "error": type(exc).__name__},
                )

    def _persist_result(self, result: CryptoResearchResult, research_run_id: str, now: datetime) -> None:
        setup_id = self._record_performance_lab(result, now)
        result.setup_id = setup_id
        self.storage.execute(
            """
            INSERT INTO crypto_research_snapshots(
                id,run_id,research_run_id,symbol,lane,price,price_timestamp,data_freshness,return_1h,return_4h,
                return_1d,return_7d,return_20d,realized_volatility,atr_like_volatility,trend_metrics,volume,spread,
                score,score_components,risk_metrics,provider,capability_snapshot_id,
                capability_snapshot_fingerprint,capability_authoritative,market_evidence_id,
                market_evidence_fingerprint,market_evidence_authoritative,market_execution_eligible,
                created_at,payload
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                str(uuid.uuid4()), self.run_id, research_run_id, result.symbol, result.lane, result.price,
                result.price_timestamp, result.data_freshness, result.returns.get("1h"), result.returns.get("4h"),
                result.returns.get("1d"), result.returns.get("7d"), result.returns.get("20d"),
                result.realized_volatility, result.atr_like_volatility, json_dumps(result.trend_metrics),
                result.volume, result.spread, result.score, json_dumps(result.score_components),
                json_dumps(result.risk_metrics), result.provider, result.capability_snapshot_id,
                result.capability_snapshot_fingerprint, int(result.capability_authoritative),
                result.market_evidence_id, result.market_evidence_fingerprint,
                int(result.market_evidence_authoritative), int(result.market_execution_eligible), now.isoformat(),
                json_dumps({
                    "status": result.status,
                    "reason": result.reason,
                    "setup_id": setup_id,
                    "capability_failure_reasons": result.capability_failure_reasons,
                    "market_evidence_id": result.market_evidence_id,
                    "market_evidence_fingerprint": result.market_evidence_fingerprint,
                    "market_evidence_authoritative": result.market_evidence_authoritative,
                    "market_execution_eligible": result.market_execution_eligible,
                    "market_evidence_failure_reasons": result.market_evidence_failure_reasons,
                    "strategy_decision_id": result.strategy_decision_id,
                    "strategy_decision_fingerprint": result.strategy_decision_fingerprint,
                    "selected_strategy": result.selected_strategy,
                    "strategy_lifecycle": result.strategy_lifecycle,
                    "strategy_signal_eligible": result.strategy_signal_eligible,
                    "strategy_blockers": result.strategy_blockers,
                }),
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
                json_dumps({
                    "risk_metrics": result.risk_metrics,
                    "reason": result.reason,
                    "strategy_decision_id": result.strategy_decision_id,
                    "selected_strategy": result.selected_strategy,
                    "strategy_lifecycle": result.strategy_lifecycle,
                    "strategy_signal_eligible": result.strategy_signal_eligible,
                }),
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
        # ``performance_outcomes`` keeps REAL compatibility projections for
        # older reporting consumers, but its Decimal sidecars are still
        # required whenever those projections are populated.  This research
        # row is hypothetical evidence reconstructed from provider/config
        # values, not a broker fill, so the provenance must say so explicitly.
        shadow_entry_price_decimal = None
        shadow_entry_notional_decimal = None
        shadow_entry_quantity_decimal = None
        try:
            if result.price is not None:
                shadow_entry_price_decimal = _crypto_decimal(
                    result.price, "crypto Performance Lab shadow entry price"
                )
            if candidate.get("position_size") is not None:
                shadow_entry_notional_decimal = _crypto_decimal(
                    candidate.get("position_size"),
                    "crypto Performance Lab shadow entry notional",
                )
            if (
                shadow_entry_price_decimal is not None
                and shadow_entry_price_decimal > Decimal("0")
                and shadow_entry_notional_decimal is not None
            ):
                shadow_entry_quantity_decimal = (
                    shadow_entry_notional_decimal / shadow_entry_price_decimal
                )
        except ValueError:
            # Preserve the existing descriptive row and let the read-only
            # integrity report expose malformed source evidence; never invent
            # a Decimal sidecar for an invalid research value.
            shadow_entry_price_decimal = None
            shadow_entry_notional_decimal = None
            shadow_entry_quantity_decimal = None
        proposed = 0
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
                setup_id, now.isoformat(), self.run_id, result.symbol, "crypto", result.lane,
                result.selected_strategy or "hold_watch",
                action_decision, proposed, result.reason, result.score, json_dumps(result.score_components),
                json_dumps({
                    "action": "ENTRY_RESEARCH" if result.strategy_signal_eligible else "RESEARCH",
                    "side": "buy" if result.strategy_signal_eligible else "none",
                    "reason": result.reason, "mode": mode,
                    "strategy_decision_id": result.strategy_decision_id,
                    "strategy": result.selected_strategy,
                    "strategy_lifecycle": result.strategy_lifecycle,
                }), int(result.strategy_signal_eligible), 0, 0,
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
                id,setup_id,run_id,symbol,actual_or_shadow,entry_time,entry_price,entry_notional,entry_qty,status,
                created_at,updated_at,entry_price_decimal,entry_qty_decimal,entry_notional_decimal,
                decimal_provenance,decimal_accounting_version
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                str(uuid.uuid4()), setup_id, self.run_id, result.symbol, "shadow", now.isoformat(), result.price,
                candidate.get("position_size"),
                None
                if shadow_entry_quantity_decimal is None
                else float(shadow_entry_quantity_decimal),
                "pending_forward_returns", now.isoformat(), now.isoformat(),
                None
                if shadow_entry_price_decimal is None
                else _decimal_text(shadow_entry_price_decimal),
                None
                if shadow_entry_quantity_decimal is None
                else _decimal_text(shadow_entry_quantity_decimal),
                None
                if shadow_entry_notional_decimal is None
                else _decimal_text(shadow_entry_notional_decimal),
                RECONSTRUCTED_REAL_PROVENANCE,
                FIXED_POINT_ACCOUNTING_VERSION,
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
            gate_passed, gate_reasons = self._runtime_evidence_gate_passed(now)
            if gate_reasons:
                blockers.extend(("crypto_runtime_evidence_gate_failed", reason) for reason in gate_reasons)
            if blockers or not gate_passed:
                status = "blocked"
            else:
                blockers.append(
                    (
                        "crypto_stage3_enablement_requires_separate_approval",
                        "Stage 3 readiness only; enabling paper proposals requires a separate explicit user-approved config-change task and commit",
                    )
                )
                status = "stage3_ready_report"
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
        if result.strategy_decision_id:
            self.storage.execute(
                "UPDATE crypto_paper_watch_candidates SET strategy_decision_id=? WHERE id=?",
                (result.strategy_decision_id, row_id),
            )
        return row_id

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
        if not result.capability_authoritative:
            blockers.append((
                "crypto_capability_unverified",
                "current Alpaca paper account/pair capability is not authoritative: "
                + ",".join(result.capability_failure_reasons),
            ))
        if not result.market_evidence_authoritative:
            blockers.append((
                "crypto_market_data_unverified",
                "current Alpaca quote/order-book evidence is not authoritative: "
                + ",".join(result.market_evidence_failure_reasons),
            ))
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
        risk_policy = cfg.get("risk_policy") or {}
        max_vol = float(risk_policy.get("volatility_halt_annualized") or 0.0)
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
    sizing_policy = cfg.get("sizing_policy") or {}
    entry_price = float(result.price) if result.price and result.price > 0 else None
    atr_stop_pct = float(result.atr_like_volatility or 0.0) * 2.0
    minimum_stop_fraction = float(sizing_policy.get("minimum_stop_distance_pct") or 0.0) / 100.0
    maximum_stop_fraction = float(sizing_policy.get("maximum_stop_distance_pct") or 0.0) / 100.0
    stop_distance_pct = max(minimum_stop_fraction, atr_stop_pct)
    stop_distance_pct = min(stop_distance_pct, maximum_stop_fraction)
    stop_price = entry_price * (1.0 - stop_distance_pct) if entry_price else None
    min_rr = float(cfg.get("min_risk_reward_ratio", 1.5) or 1.5)
    take_profit_target = entry_price * (1.0 + stop_distance_pct * min_rr) if entry_price else None
    risk_reward_ratio = None
    if entry_price and stop_price and take_profit_target and entry_price > stop_price:
        risk_reward_ratio = (take_profit_target - entry_price) / (entry_price - stop_price)
    max_notional = float(sizing_policy.get("maximum_order_notional_usd") or 0.0)
    # This legacy research record remains descriptive only.  The separately
    # persisted Decimal crypto sizing/risk authority is the sole future source
    # for executable quantity, notional, fees, stop risk and portfolio caps.
    account_risk_notional = max_notional * stop_distance_pct
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
        "sizing_authority": "non_authoritative_float_research_metadata_only",
        "authoritative_sizing_required": "crypto_sizing_decisions",
        "authoritative_portfolio_risk_required": "crypto_risk_decisions",
        "provider_coverage": provider_coverage,
        "capability_snapshot_id": result.capability_snapshot_id,
        "capability_snapshot_fingerprint": result.capability_snapshot_fingerprint,
        "capability_authoritative": result.capability_authoritative,
        "capability_failure_reasons": list(result.capability_failure_reasons),
        "market_evidence_id": result.market_evidence_id,
        "market_evidence_fingerprint": result.market_evidence_fingerprint,
        "market_evidence_authoritative": result.market_evidence_authoritative,
        "market_execution_eligible": result.market_execution_eligible,
        "market_evidence_failure_reasons": list(result.market_evidence_failure_reasons),
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


def _oldest_crypto_lot_opened_at(rows: list[Mapping[str, Any]]) -> datetime | None:
    """Return the oldest opened_at, failing closed on any active-lot defect."""

    opened_at: datetime | None = None
    for row in rows:
        raw_opened_at = row.get("opened_at")
        if raw_opened_at in (None, ""):
            raise ValueError("active crypto lot opened_at is missing")
        try:
            parsed = _parse_dt(str(raw_opened_at))
        except (TypeError, ValueError) as exc:
            raise ValueError("active crypto lot opened_at is invalid") from exc
        opened_at = parsed if opened_at is None else min(opened_at, parsed)
    return opened_at


def _crypto_decimal(value: Any, label: str) -> Decimal:
    if value is None or isinstance(value, bool):
        raise ValueError(f"{label} is missing")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{label} is invalid") from exc
    if not result.is_finite():
        raise ValueError(f"{label} is not finite")
    return result


def _decimal_text(value: Decimal) -> str:
    if value == Decimal("0"):
        return "0"
    return format(value.normalize(), "f")


def _sha256_text(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


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
