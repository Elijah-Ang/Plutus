"""Point-in-time strategy research for the supervised spot-crypto lane.

The evaluator deliberately stops at research authority.  It records whether a
setup is eligible for further paper analysis, but it cannot create an ordinary
``trade_proposals`` row, accept an approval, reserve risk, or submit an order.
All calculations use Decimal values derived only from bars at or before the
bound Alpaca market-evidence timestamp.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from .approval_authority import canonical_json
from .crypto_market_data import CryptoMarketDataStore, CryptoMarketEvidence
from .formula_versions import (
    CRYPTO_STRATEGY_FORMULA_VERSION,
    CRYPTO_STRATEGY_SCHEMA_VERSION,
)
from .utils import iso_now, json_dumps


ZERO = Decimal("0")
ONE = Decimal("1")
HUNDRED = Decimal("100")
HOURS_PER_YEAR = Decimal("8760")
SUPPORTED_STRATEGIES = (
    "time_series_trend",
    "breakout_continuation",
    "pullback_in_trend",
    "volatility_adjusted_momentum",
)


class CryptoStrategyError(ValueError):
    """Raised when strategy evidence is malformed or relationally invalid."""


def _hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _valid_hash(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return len(text) == 64 and all(char in "0123456789abcdef" for char in text)


def _decimal(value: Any, label: str, *, positive: bool = False, minimum: Decimal | None = None) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float) or value is None:
        raise CryptoStrategyError(f"{label} must be an exact decimal value")
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise CryptoStrategyError(f"{label} is invalid") from exc
    if not number.is_finite() or (positive and number <= ZERO) or (minimum is not None and number < minimum):
        raise CryptoStrategyError(f"{label} is outside its finite policy range")
    return number


def _trusted_decimal(value: Any, label: str, *, minimum: Decimal = ZERO) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise CryptoStrategyError(f"{label} is missing")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise CryptoStrategyError(f"{label} is invalid") from exc
    if not number.is_finite() or number < minimum:
        raise CryptoStrategyError(f"{label} is outside its finite policy range")
    return number


def _text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    if value == ZERO:
        return "0"
    return format(value.normalize(), "f")


def _utc(value: Any, label: str) -> datetime:
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise CryptoStrategyError(f"{label} timestamp is invalid") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _value(row: Any, name: str) -> Any:
    return row.get(name) if isinstance(row, Mapping) else getattr(row, name, None)


def _bar_rows(raw: Any, symbol: str) -> list[dict[str, Any]]:
    if hasattr(raw, "reset_index") and hasattr(raw, "to_dict"):
        records = raw.reset_index().to_dict("records")
    elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        records = list(raw)
    else:
        raise CryptoStrategyError("crypto strategy bars must be a sequence or dataframe")
    rows: list[dict[str, Any]] = []
    for index, record in enumerate(records):
        raw_symbol = str(_value(record, "symbol") or symbol).strip().upper().replace("-", "/")
        if raw_symbol.replace("/", "") != symbol.replace("/", ""):
            continue
        timestamp = _utc(_value(record, "timestamp"), f"bar {index}")
        close = _trusted_decimal(_value(record, "close"), f"bar {index} close", minimum=Decimal("0.000000001"))
        high_value = _value(record, "high")
        low_value = _value(record, "low")
        high = close if high_value is None else _trusted_decimal(high_value, f"bar {index} high", minimum=close)
        low = close if low_value is None else _trusted_decimal(low_value, f"bar {index} low", minimum=Decimal("0.000000001"))
        if low > close or high < close or high < low:
            raise CryptoStrategyError(f"bar {index} OHLC relationship is invalid")
        rows.append({"timestamp": timestamp, "close": close, "high": high, "low": low})
    rows.sort(key=lambda item: item["timestamp"])
    if len({item["timestamp"] for item in rows}) != len(rows):
        raise CryptoStrategyError("crypto strategy bars contain duplicate timestamps")
    return rows


def _ema(values: Sequence[Decimal], periods: int) -> Decimal:
    if periods <= 1 or len(values) < periods:
        raise CryptoStrategyError("EMA history is insufficient")
    alpha = Decimal("2") / Decimal(periods + 1)
    result = sum(values[:periods], ZERO) / Decimal(periods)
    for value in values[periods:]:
        result = value * alpha + result * (ONE - alpha)
    return result


def _return(values: Sequence[Decimal], hours: int) -> Decimal:
    if hours <= 0 or len(values) <= hours or values[-1 - hours] <= ZERO:
        raise CryptoStrategyError("return history is insufficient")
    return values[-1] / values[-1 - hours] - ONE


def _annualized_volatility(values: Sequence[Decimal], lookback: int) -> Decimal:
    if lookback < 2 or len(values) <= lookback:
        raise CryptoStrategyError("volatility history is insufficient")
    sample = values[-(lookback + 1):]
    returns = [sample[index] / sample[index - 1] - ONE for index in range(1, len(sample))]
    mean = sum(returns, ZERO) / Decimal(len(returns))
    variance = sum(((value - mean) ** 2 for value in returns), ZERO) / Decimal(len(returns) - 1)
    return variance.sqrt() * HOURS_PER_YEAR.sqrt()


def _atr_fraction(rows: Sequence[Mapping[str, Any]], periods: int) -> Decimal:
    if len(rows) <= periods:
        raise CryptoStrategyError("ATR history is insufficient")
    subset = rows[-periods:]
    previous_close = rows[-periods - 1]["close"]
    ranges: list[Decimal] = []
    for row in subset:
        ranges.append(max(row["high"] - row["low"], abs(row["high"] - previous_close), abs(row["low"] - previous_close)))
        previous_close = row["close"]
    close = rows[-1]["close"]
    return (sum(ranges, ZERO) / Decimal(len(ranges))) / close


def _policy(config: Mapping[str, Any]) -> dict[str, Any]:
    cfg = config.get("crypto") or {}
    policy = cfg.get("strategy_policy") or {}
    failures: list[str] = []
    if policy.get("mode") != "research_only":
        failures.append("strategy_policy_mode_not_research_only")
    if policy.get("lifecycle") != "RESEARCH_ONLY":
        failures.append("strategy_lifecycle_not_research_only")
    if policy.get("formula_version") != CRYPTO_STRATEGY_FORMULA_VERSION:
        failures.append("strategy_formula_identity_mismatch")
    if policy.get("schema_version") != CRYPTO_STRATEGY_SCHEMA_VERSION:
        failures.append("strategy_schema_identity_mismatch")
    if str((config.get("formula_versions") or {}).get("crypto_strategy") or "") != CRYPTO_STRATEGY_FORMULA_VERSION:
        failures.append("configuration_strategy_formula_mismatch")
    if cfg.get("mode") != "research_only" or cfg.get("paper_trading_enabled") is not False or cfg.get("proposals_enabled") is not False or cfg.get("live_enabled") is not False:
        failures.append("global_crypto_lane_not_research_only_disabled")
    strategies = tuple(policy.get("strategies") or ())
    if strategies != SUPPORTED_STRATEGIES:
        failures.append("strategy_set_or_order_mismatch")
    try:
        minimum_history = int(policy.get("minimum_history_hours"))
        fast = int(policy.get("trend_fast_hours"))
        slow = int(policy.get("trend_slow_hours"))
        breakout = int(policy.get("breakout_lookback_hours"))
        volatility = int(policy.get("volatility_lookback_hours"))
    except (TypeError, ValueError):
        minimum_history = fast = slow = breakout = volatility = 0
        failures.append("strategy_integer_policy_invalid")
    if not (minimum_history >= 96 and 2 <= fast < slow <= minimum_history and 12 <= breakout < minimum_history and 24 <= volatility < minimum_history):
        failures.append("strategy_history_windows_invalid")
    decimal_names = (
        "trend_minimum_24h_return",
        "breakout_buffer_pct",
        "breakout_minimum_4h_return",
        "pullback_minimum_pct",
        "pullback_maximum_pct",
        "momentum_minimum_vol_adjusted_score",
        "maximum_annualized_volatility",
        "stop_atr_multiple",
        "minimum_stop_distance_pct",
        "maximum_stop_distance_pct",
        "target_reward_r_multiple",
    )
    decimals: dict[str, Decimal] = {}
    for name in decimal_names:
        try:
            decimals[name] = _trusted_decimal(policy.get(name), f"crypto.strategy_policy.{name}")
        except CryptoStrategyError:
            failures.append(f"invalid_{name}")
    if decimals:
        if not (ZERO < decimals["pullback_minimum_pct"] < decimals["pullback_maximum_pct"] < Decimal("0.25")):
            failures.append("pullback_policy_invalid")
        if not (ZERO < decimals["minimum_stop_distance_pct"] <= decimals["maximum_stop_distance_pct"] <= Decimal("0.25")):
            failures.append("strategy_stop_policy_invalid")
        if decimals["target_reward_r_multiple"] < Decimal("1.5"):
            failures.append("strategy_reward_multiple_below_floor")
    if failures:
        raise CryptoStrategyError("invalid crypto strategy policy: " + ", ".join(sorted(set(failures))))
    return {
        **policy,
        **decimals,
        "minimum_history_hours": minimum_history,
        "trend_fast_hours": fast,
        "trend_slow_hours": slow,
        "breakout_lookback_hours": breakout,
        "volatility_lookback_hours": volatility,
    }


def _evaluations(metrics: Mapping[str, Decimal], policy: Mapping[str, Any]) -> list[dict[str, Any]]:
    trend_up = metrics["close"] > metrics["ema_fast"] > metrics["ema_slow"]
    evaluations = [
        {
            "strategy": "time_series_trend",
            "passed": trend_up and metrics["return_24h"] >= policy["trend_minimum_24h_return"],
            "score": max(ZERO, metrics["return_24h"] / max(policy["trend_minimum_24h_return"], Decimal("0.000000001"))),
            "reason": "close above fast/slow crypto EMAs with positive 24-hour trend",
        },
        {
            "strategy": "breakout_continuation",
            "passed": metrics["close"] >= metrics["prior_breakout_high"] * (ONE + policy["breakout_buffer_pct"]) and metrics["return_4h"] >= policy["breakout_minimum_4h_return"],
            "score": max(ZERO, metrics["close"] / metrics["prior_breakout_high"] - ONE) / max(policy["breakout_buffer_pct"], Decimal("0.000000001")),
            "reason": "current close clears the prior crypto range with positive continuation",
        },
        {
            "strategy": "pullback_in_trend",
            "passed": trend_up and policy["pullback_minimum_pct"] <= metrics["pullback_from_recent_high"] <= policy["pullback_maximum_pct"] and metrics["return_1h"] > ZERO,
            "score": max(ZERO, (policy["pullback_maximum_pct"] - metrics["pullback_from_recent_high"]) / max(policy["pullback_maximum_pct"] - policy["pullback_minimum_pct"], Decimal("0.000000001"))),
            "reason": "bounded pullback inside a positive crypto trend with an hourly rebound",
        },
        {
            "strategy": "volatility_adjusted_momentum",
            "passed": metrics["volatility_adjusted_momentum"] >= policy["momentum_minimum_vol_adjusted_score"] and metrics["return_24h"] > ZERO,
            "score": max(ZERO, metrics["volatility_adjusted_momentum"] / max(policy["momentum_minimum_vol_adjusted_score"], Decimal("0.000000001"))),
            "reason": "positive 24-hour momentum remains material after volatility scaling",
        },
    ]
    return [
        {**item, "score": _text(item["score"])}
        for item in evaluations
    ]


@dataclass(frozen=True)
class CryptoStrategyDecision:
    id: str
    run_id: str
    research_run_id: str
    market_evidence_id: str
    market_evidence_fingerprint: str
    symbol: str
    selected_strategy: str | None
    action: str
    lifecycle: str
    signal_eligible: bool
    proposal_authorized: bool
    execution_authorized: bool
    stop_price: str | None
    target_price: str | None
    expected_reward_r: str | None
    blockers: tuple[str, ...]
    config_hash: str
    formula_version: str
    schema_version: str
    as_of: str
    created_at: str
    decision_fingerprint: str
    payload: Mapping[str, Any]


def evaluate_crypto_strategies(
    *,
    decision_id: str,
    run_id: str,
    research_run_id: str,
    market: CryptoMarketEvidence,
    bars: Any,
    config: Mapping[str, Any],
    created_at: datetime,
) -> CryptoStrategyDecision:
    policy = _policy(config)
    config_hash = str(config.get("effective_config_hash") or "").strip().lower()
    if not _valid_hash(config_hash) or market.config_hash != config_hash:
        raise CryptoStrategyError("crypto strategy configuration identity is missing or changed")
    if not market.authoritative or not market.execution_eligible:
        raise CryptoStrategyError("crypto strategy requires authoritative execution-eligible market evidence")
    if market.research_run_id != str(research_run_id):
        raise CryptoStrategyError("crypto strategy research-run binding mismatch")
    as_of = _utc(market.quote_timestamp, "market quote")
    maximum_age = _trusted_decimal(
        (config.get("crypto") or {}).get("max_price_age_seconds"),
        "crypto maximum price age",
    )
    evidence_age = Decimal(str((created_at.astimezone(UTC) - as_of).total_seconds()))
    if evidence_age < Decimal("-1") or evidence_age > maximum_age:
        raise CryptoStrategyError("crypto strategy market evidence is no longer fresh")
    rows = _bar_rows(bars, market.symbol)
    if any(row["timestamp"] > as_of for row in rows):
        raise CryptoStrategyError("crypto strategy bars contain future information")
    rows = [row for row in rows if row["timestamp"] <= as_of]
    if len(rows) < policy["minimum_history_hours"]:
        raise CryptoStrategyError("crypto strategy history is insufficient")
    rows = rows[-policy["minimum_history_hours"]:]
    closes = [row["close"] for row in rows]
    metrics: dict[str, Decimal] = {
        "close": closes[-1],
        "return_1h": _return(closes, 1),
        "return_4h": _return(closes, 4),
        "return_24h": _return(closes, 24),
        "ema_fast": _ema(closes, policy["trend_fast_hours"]),
        "ema_slow": _ema(closes, policy["trend_slow_hours"]),
        "prior_breakout_high": max(row["high"] for row in rows[-policy["breakout_lookback_hours"] - 1:-1]),
        "recent_high": max(row["high"] for row in rows[-24:]),
        "annualized_volatility": _annualized_volatility(closes, policy["volatility_lookback_hours"]),
        "atr_fraction": _atr_fraction(rows, 24),
    }
    metrics["pullback_from_recent_high"] = max(ZERO, ONE - metrics["close"] / metrics["recent_high"])
    metrics["volatility_adjusted_momentum"] = metrics["return_24h"] / max(metrics["annualized_volatility"], Decimal("0.000000001"))
    evaluations = _evaluations(metrics, policy)
    blockers: list[str] = []
    if metrics["annualized_volatility"] >= policy["maximum_annualized_volatility"]:
        blockers.append("crypto_strategy_volatility_above_policy")
    passed = [item for item in evaluations if item["passed"] is True]
    selected = max(passed, key=lambda item: (Decimal(item["score"]), -SUPPORTED_STRATEGIES.index(item["strategy"]))) if passed else None
    if selected is None:
        blockers.append("no_crypto_strategy_signal")
    stop_price: Decimal | None = None
    target_price: Decimal | None = None
    if selected is not None:
        stop_fraction = min(
            policy["maximum_stop_distance_pct"],
            max(policy["minimum_stop_distance_pct"], metrics["atr_fraction"] * policy["stop_atr_multiple"]),
        )
        stop_price = metrics["close"] * (ONE - stop_fraction)
        target_price = metrics["close"] + (metrics["close"] - stop_price) * policy["target_reward_r_multiple"]
    blockers = sorted(set(blockers))
    signal_eligible = not blockers
    canonical_bars = [
        {"timestamp": row["timestamp"].isoformat(), "close": _text(row["close"]), "high": _text(row["high"]), "low": _text(row["low"])}
        for row in rows
    ]
    input_payload = {
        "market_evidence_id": market.id,
        "market_evidence_fingerprint": market.evidence_fingerprint,
        "bar_count": len(rows),
        "bar_start": rows[0]["timestamp"].isoformat(),
        "bar_end": rows[-1]["timestamp"].isoformat(),
        "bar_fingerprint": _hash(canonical_bars),
        "bars": canonical_bars,
        "as_of": as_of.isoformat(),
    }
    payload = {
        "id": str(decision_id),
        "run_id": str(run_id),
        "research_run_id": str(research_run_id),
        "symbol": market.symbol,
        "market_evidence_id": market.id,
        "market_evidence_fingerprint": market.evidence_fingerprint,
        "input": input_payload,
        "input_fingerprint": _hash(input_payload),
        "metrics": {key: _text(value) for key, value in sorted(metrics.items())},
        "evaluations": evaluations,
        "selected_strategy": selected["strategy"] if selected else None,
        "action": "entry" if selected else "hold",
        "lifecycle": "RESEARCH_ONLY",
        "signal_eligible": signal_eligible,
        "proposal_authorized": False,
        "execution_authorized": False,
        "stop_price": _text(stop_price),
        "target_price": _text(target_price),
        "expected_reward_r": _text(policy["target_reward_r_multiple"] if selected else None),
        "blockers": blockers,
        "config_hash": config_hash,
        "formula_version": CRYPTO_STRATEGY_FORMULA_VERSION,
        "schema_version": CRYPTO_STRATEGY_SCHEMA_VERSION,
        "as_of": as_of.isoformat(),
        "created_at": created_at.astimezone(UTC).isoformat(),
    }
    fingerprint = _hash(payload)
    return CryptoStrategyDecision(
        id=str(decision_id), run_id=str(run_id), research_run_id=str(research_run_id),
        market_evidence_id=market.id, market_evidence_fingerprint=market.evidence_fingerprint,
        symbol=market.symbol, selected_strategy=payload["selected_strategy"], action=payload["action"],
        lifecycle="RESEARCH_ONLY", signal_eligible=signal_eligible,
        proposal_authorized=False, execution_authorized=False,
        stop_price=payload["stop_price"], target_price=payload["target_price"],
        expected_reward_r=payload["expected_reward_r"], blockers=tuple(blockers),
        config_hash=config_hash, formula_version=CRYPTO_STRATEGY_FORMULA_VERSION,
        schema_version=CRYPTO_STRATEGY_SCHEMA_VERSION, as_of=payload["as_of"],
        created_at=payload["created_at"], decision_fingerprint=fingerprint, payload=payload,
    )


def apply_crypto_strategy_schema(conn: Any, *, record_migration: bool = True) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS crypto_strategy_decisions(
          id TEXT PRIMARY KEY,run_id TEXT NOT NULL,research_run_id TEXT NOT NULL,
          market_evidence_id TEXT NOT NULL,market_evidence_fingerprint TEXT NOT NULL,
          symbol TEXT NOT NULL,selected_strategy TEXT,action TEXT NOT NULL,lifecycle TEXT NOT NULL,
          signal_eligible INTEGER NOT NULL CHECK(signal_eligible IN (0,1)),
          proposal_authorized INTEGER NOT NULL CHECK(proposal_authorized=0),
          execution_authorized INTEGER NOT NULL CHECK(execution_authorized=0),
          stop_price TEXT,target_price TEXT,expected_reward_r TEXT,
          blockers_json TEXT NOT NULL,input_fingerprint TEXT NOT NULL,
          config_hash TEXT NOT NULL,formula_version TEXT NOT NULL,schema_version TEXT NOT NULL,
          as_of TEXT NOT NULL,created_at TEXT NOT NULL,decision_json TEXT NOT NULL,
          decision_fingerprint TEXT NOT NULL UNIQUE,
          FOREIGN KEY(market_evidence_id) REFERENCES crypto_market_data_evidence(id),
          FOREIGN KEY(research_run_id) REFERENCES crypto_research_runs(id),
          UNIQUE(research_run_id,symbol)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_crypto_strategy_symbol_time ON crypto_strategy_decisions(symbol,created_at)")
    existing = {str(row[1]) for row in conn.execute("PRAGMA table_info(crypto_paper_watch_candidates)").fetchall()}
    if "strategy_decision_id" not in existing:
        conn.execute("ALTER TABLE crypto_paper_watch_candidates ADD COLUMN strategy_decision_id TEXT")
    if record_migration:
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version,applied_at,detail) VALUES(?,?,?)",
            (CRYPTO_STRATEGY_SCHEMA_VERSION, iso_now(), "point-in-time crypto strategy research decisions"),
        )


class CryptoStrategyStore:
    def __init__(self, storage: Any) -> None:
        self.storage = storage

    def evaluate(
        self,
        config: Mapping[str, Any],
        run_id: str,
        research_run_id: str,
        market_evidence_id: str,
        bars: Any,
        *,
        now: datetime | None = None,
    ) -> CryptoStrategyDecision:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        market = CryptoMarketDataStore(self.storage).load_verified(market_evidence_id, config)
        decision = evaluate_crypto_strategies(
            decision_id=str(uuid.uuid4()), run_id=str(run_id), research_run_id=str(research_run_id),
            market=market, bars=bars, config=config, created_at=current,
        )
        with self.storage.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            apply_crypto_strategy_schema(conn, record_migration=False)
            evidence = conn.execute(
                "SELECT evidence_fingerprint,research_run_id,symbol,config_hash FROM crypto_market_data_evidence WHERE id=?",
                (market.id,),
            ).fetchone()
            research = conn.execute(
                "SELECT run_id FROM crypto_research_runs WHERE id=?", (research_run_id,)
            ).fetchone()
            if evidence is None or research is None or research["run_id"] != str(run_id) or evidence["evidence_fingerprint"] != market.evidence_fingerprint or evidence["research_run_id"] != research_run_id or evidence["symbol"] != market.symbol or evidence["config_hash"] != decision.config_hash:
                raise CryptoStrategyError("crypto market evidence changed before strategy persistence")
            conn.execute(
                """INSERT INTO crypto_strategy_decisions(
                  id,run_id,research_run_id,market_evidence_id,market_evidence_fingerprint,
                  symbol,selected_strategy,action,lifecycle,signal_eligible,proposal_authorized,
                  execution_authorized,stop_price,target_price,expected_reward_r,blockers_json,
                  input_fingerprint,config_hash,formula_version,schema_version,as_of,created_at,
                  decision_json,decision_fingerprint
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    decision.id, decision.run_id, decision.research_run_id, decision.market_evidence_id,
                    decision.market_evidence_fingerprint, decision.symbol, decision.selected_strategy,
                    decision.action, decision.lifecycle, int(decision.signal_eligible), 0, 0,
                    decision.stop_price, decision.target_price, decision.expected_reward_r,
                    json_dumps(decision.blockers), decision.payload["input_fingerprint"],
                    decision.config_hash, decision.formula_version, decision.schema_version,
                    decision.as_of, decision.created_at, json_dumps(decision.payload),
                    decision.decision_fingerprint,
                ),
            )
        return decision

    def load_verified(self, decision_id: str, config: Mapping[str, Any]) -> CryptoStrategyDecision:
        rows = self.storage.fetch_all("SELECT * FROM crypto_strategy_decisions WHERE id=?", (decision_id,))
        if len(rows) != 1:
            raise CryptoStrategyError("crypto strategy decision is missing or duplicated")
        row = rows[0]
        try:
            payload = json.loads(row["decision_json"])
            blockers = json.loads(row["blockers_json"])
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CryptoStrategyError("crypto strategy decision JSON is invalid") from exc
        if not isinstance(payload, dict) or not isinstance(blockers, list):
            raise CryptoStrategyError("crypto strategy decision shape is invalid")
        if _hash(payload) != row["decision_fingerprint"]:
            raise CryptoStrategyError("crypto strategy decision fingerprint mismatch")
        if _hash(payload.get("input") or {}) != row["input_fingerprint"] or payload.get("input_fingerprint") != row["input_fingerprint"]:
            raise CryptoStrategyError("crypto strategy input fingerprint mismatch")
        scalar = (
            "id", "run_id", "research_run_id", "market_evidence_id", "market_evidence_fingerprint",
            "symbol", "selected_strategy", "action", "lifecycle", "stop_price", "target_price",
            "expected_reward_r", "config_hash", "formula_version", "schema_version", "as_of", "created_at",
        )
        for key in scalar:
            if row[key] != payload.get(key):
                raise CryptoStrategyError(f"crypto strategy persisted column mismatch: {key}")
        if blockers != payload.get("blockers") or bool(row["signal_eligible"]) != payload.get("signal_eligible"):
            raise CryptoStrategyError("crypto strategy classification mismatch")
        if bool(row["proposal_authorized"]) or bool(row["execution_authorized"]) or payload.get("proposal_authorized") is not False or payload.get("execution_authorized") is not False:
            raise CryptoStrategyError("crypto strategy decision escaped research-only authority")
        if row["config_hash"] != str(config.get("effective_config_hash") or ""):
            raise CryptoStrategyError("crypto strategy configuration identity changed")
        _policy(config)
        if row["formula_version"] != CRYPTO_STRATEGY_FORMULA_VERSION or row["schema_version"] != CRYPTO_STRATEGY_SCHEMA_VERSION:
            raise CryptoStrategyError("crypto strategy decision version is obsolete")
        market = CryptoMarketDataStore(self.storage).load_verified(row["market_evidence_id"], config)
        research = self.storage.fetch_all(
            "SELECT run_id FROM crypto_research_runs WHERE id=?", (row["research_run_id"],)
        )
        if len(research) != 1 or research[0]["run_id"] != row["run_id"] or market.evidence_fingerprint != row["market_evidence_fingerprint"] or market.research_run_id != row["research_run_id"] or market.symbol != row["symbol"]:
            raise CryptoStrategyError("crypto strategy evidence relationship mismatch")
        recomputed = evaluate_crypto_strategies(
            decision_id=row["id"], run_id=row["run_id"], research_run_id=row["research_run_id"],
            market=market, bars=(payload.get("input") or {}).get("bars") or (), config=config,
            created_at=_utc(row["created_at"], "crypto strategy creation"),
        )
        if recomputed.payload != payload or recomputed.decision_fingerprint != row["decision_fingerprint"]:
            raise CryptoStrategyError("crypto strategy independent recomputation mismatch")
        return CryptoStrategyDecision(
            id=row["id"], run_id=row["run_id"], research_run_id=row["research_run_id"],
            market_evidence_id=row["market_evidence_id"], market_evidence_fingerprint=row["market_evidence_fingerprint"],
            symbol=row["symbol"], selected_strategy=row["selected_strategy"], action=row["action"],
            lifecycle=row["lifecycle"], signal_eligible=bool(row["signal_eligible"]),
            proposal_authorized=False, execution_authorized=False, stop_price=row["stop_price"],
            target_price=row["target_price"], expected_reward_r=row["expected_reward_r"],
            blockers=tuple(blockers), config_hash=row["config_hash"], formula_version=row["formula_version"],
            schema_version=row["schema_version"], as_of=row["as_of"], created_at=row["created_at"],
            decision_fingerprint=row["decision_fingerprint"], payload=payload,
        )


__all__ = [
    "CryptoStrategyDecision",
    "CryptoStrategyError",
    "CryptoStrategyStore",
    "SUPPORTED_STRATEGIES",
    "apply_crypto_strategy_schema",
    "evaluate_crypto_strategies",
]
