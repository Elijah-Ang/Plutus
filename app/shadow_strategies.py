from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, Mapping, Sequence

import pandas as pd

from .research_validation import ExchangeSessions, REGIME_VERSION, deterministic_regime
from .utils import json_dumps


PHASE2_SCHEMA_VERSION = "phase2_shadow_strategies_v1"
SHADOW_MODE = "SHADOW_ONLY"
FEATURE_VERSION = "phase2_daily_features_v1"
UNIVERSE_VERSION = "phase2_runtime_snapshot_v1"
ELIGIBILITY_VERSION = "phase2_shadow_only_v1"
OUTCOME_ENGINE_VERSION = "phase1_outcome_v1"
STRATEGY_VERSIONS = MappingProxyType({
    "cross_sectional_momentum": "cross_sectional_momentum_v1",
    "time_series_trend": "time_series_trend_v1",
    "pullback_uptrend": "pullback_uptrend_v1",
    "etf_sector_rotation": "etf_sector_rotation_v1",
    "breakout_continuation": "breakout_continuation_v1",
})
SECTOR_ETFS = frozenset({"XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY"})


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


@dataclass(frozen=True)
class ShadowInsight:
    id: str
    run_id: str
    sleeve: str
    strategy_version: str
    mode: str
    symbol: str
    observed_at: str
    direction: str
    signal: str
    score: float
    rank: int | None
    entry_price: float
    regime: str
    regime_version: str
    feature_version: str
    universe_version: str
    eligibility_version: str
    outcome_engine_version: str
    input_fingerprint: str
    feature_snapshot_json: str
    universe_snapshot_json: str
    provenance_json: str

    def __post_init__(self) -> None:
        if self.mode != SHADOW_MODE:
            raise ValueError("Phase 2 insights must be SHADOW_ONLY")
        if self.sleeve not in STRATEGY_VERSIONS or self.strategy_version != STRATEGY_VERSIONS[self.sleeve]:
            raise ValueError("unrecognized or mismatched strategy version")
        if self.direction != "long" or self.signal not in {"active", "inactive"}:
            raise ValueError("Phase 2 v1 supports long shadow attribution only")


def apply_phase2_schema(conn: Any) -> None:
    statements = """
    CREATE TABLE IF NOT EXISTS shadow_insights(
      id TEXT PRIMARY KEY, run_id TEXT NOT NULL, sleeve TEXT NOT NULL,
      strategy_version TEXT NOT NULL, mode TEXT NOT NULL CHECK(mode='SHADOW_ONLY'),
      symbol TEXT NOT NULL, observed_at TEXT NOT NULL, direction TEXT NOT NULL,
      signal TEXT NOT NULL, score REAL NOT NULL, rank INTEGER, entry_price REAL NOT NULL,
      regime TEXT NOT NULL, regime_version TEXT NOT NULL, feature_version TEXT NOT NULL,
      universe_version TEXT NOT NULL, eligibility_version TEXT NOT NULL,
      outcome_engine_version TEXT NOT NULL, input_fingerprint TEXT NOT NULL,
      feature_snapshot_json TEXT NOT NULL, universe_snapshot_json TEXT NOT NULL,
      provenance_json TEXT NOT NULL, created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS shadow_portfolio_observations(
      id TEXT PRIMARY KEY, insight_id TEXT NOT NULL UNIQUE, sleeve TEXT NOT NULL,
      strategy_version TEXT NOT NULL, symbol TEXT NOT NULL, observed_at TEXT NOT NULL,
      target_weight REAL NOT NULL, comparison_portfolio TEXT NOT NULL,
      status TEXT NOT NULL CHECK(status='SHADOW_ONLY'), created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS shadow_overlap_observations(
      id TEXT PRIMARY KEY, run_id TEXT NOT NULL, symbol TEXT NOT NULL, observed_at TEXT NOT NULL,
      active_sleeves_json TEXT NOT NULL, active_sleeve_count INTEGER NOT NULL,
      pair_keys_json TEXT NOT NULL, created_at TEXT NOT NULL,
      UNIQUE(run_id,symbol));
    CREATE TABLE IF NOT EXISTS shadow_promotion_assessments(
      id TEXT PRIMARY KEY, sleeve TEXT NOT NULL, strategy_version TEXT NOT NULL,
      assessed_at TEXT NOT NULL, status TEXT NOT NULL CHECK(status='NOT_ELIGIBLE'),
      gate_version TEXT NOT NULL, completed_oos_n INTEGER NOT NULL DEFAULT 0,
      limitations_json TEXT NOT NULL, created_at TEXT NOT NULL,
      UNIQUE(sleeve,strategy_version));
    CREATE INDEX IF NOT EXISTS idx_shadow_insights_sleeve_time ON shadow_insights(sleeve,observed_at);
    CREATE INDEX IF NOT EXISTS idx_shadow_insights_symbol_time ON shadow_insights(symbol,observed_at);
    """
    for statement in statements.split(";"):
        if statement.strip():
            conn.execute(statement)
    conn.execute("""CREATE TRIGGER IF NOT EXISTS trg_shadow_insights_immutable_update
      BEFORE UPDATE ON shadow_insights BEGIN SELECT RAISE(ABORT,'shadow_insights are immutable'); END""")
    conn.execute("""CREATE TRIGGER IF NOT EXISTS trg_shadow_insights_immutable_delete
      BEFORE DELETE ON shadow_insights BEGIN SELECT RAISE(ABORT,'shadow_insights are immutable'); END""")
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations(version,applied_at,detail) VALUES(?,?,?)",
        (PHASE2_SCHEMA_VERSION, datetime.now(UTC).isoformat(), "additive immutable shadow insights, overlap, portfolios, and promotion gates"),
    )


def _features(bars: pd.DataFrame) -> dict[str, float] | None:
    if bars is None or len(bars) < 200:
        return None
    close = bars["close"].astype(float)
    volume = bars["volume"].astype(float) if "volume" in bars else pd.Series([0.0] * len(bars))
    ret = close.pct_change()
    values = {
        "close": float(close.iloc[-1]),
        "return_20d": float(close.iloc[-1] / close.iloc[-21] - 1.0),
        "return_60d": float(close.iloc[-1] / close.iloc[-61] - 1.0),
        "return_120d": float(close.iloc[-1] / close.iloc[-121] - 1.0),
        "ma_20": float(close.tail(20).mean()),
        "ma_50": float(close.tail(50).mean()),
        "ma_200": float(close.tail(200).mean()),
        "prior_20d_high": float(close.iloc[-21:-1].max()),
        "drawdown_20d_high": float(close.iloc[-1] / close.tail(20).max() - 1.0),
        "volatility_20d": float(ret.tail(20).std(ddof=1) * math.sqrt(252)),
        "volume_ratio_20d": float(volume.iloc[-1] / volume.tail(20).mean()) if float(volume.tail(20).mean()) > 0 else 0.0,
    }
    return values if all(math.isfinite(v) for v in values.values()) else None


class ShadowStrategyEngine:
    """Pure research writer: intentionally has no proposal/risk/approval/broker interfaces."""

    def __init__(self, storage: Any, run_id: str) -> None:
        self.storage = storage
        self.run_id = run_id

    def evaluate(self, snapshots: Sequence[Mapping[str, Any]], *, observed_at: datetime) -> tuple[ShadowInsight, ...]:
        observed_at = observed_at.astimezone(UTC)
        prepared: list[dict[str, Any]] = []
        for item in snapshots:
            symbol = str(item["symbol"]).upper()
            features = _features(item["bars"])
            if features:
                prepared.append({"symbol": symbol, "features": features, "universe_source": item.get("universe_source", "runtime")})
        if not prepared:
            return ()
        spy_item = next((x for x in snapshots if str(x["symbol"]).upper() == "SPY"), None)
        regime = deterministic_regime(spy_item["bars"] if spy_item is not None else pd.DataFrame())
        momentum_order = sorted(prepared, key=lambda x: x["features"]["return_120d"], reverse=True)
        momentum_rank = {x["symbol"]: i + 1 for i, x in enumerate(momentum_order)}
        sector = [x for x in prepared if x["symbol"] in SECTOR_ETFS]
        sector_order = sorted(sector, key=lambda x: (x["features"]["return_60d"], x["features"]["return_20d"]), reverse=True)
        sector_rank = {x["symbol"]: i + 1 for i, x in enumerate(sector_order)}
        insights: list[ShadowInsight] = []
        for item in prepared:
            f, symbol = item["features"], item["symbol"]
            definitions = [
                ("cross_sectional_momentum", momentum_rank[symbol] <= max(1, math.ceil(len(prepared) * 0.2)), f["return_120d"] * 100.0, momentum_rank[symbol]),
                ("time_series_trend", f["close"] > f["ma_50"] > f["ma_200"] and f["return_120d"] > 0, f["return_120d"] * 100.0, None),
                ("pullback_uptrend", f["close"] > f["ma_200"] and f["ma_50"] > f["ma_200"] and -0.08 <= f["drawdown_20d_high"] <= -0.02, -f["drawdown_20d_high"] * 100.0, None),
                ("breakout_continuation", f["close"] > f["prior_20d_high"] and f["return_60d"] > 0 and f["volume_ratio_20d"] >= 1.0, f["return_60d"] * 100.0 + f["volume_ratio_20d"], None),
            ]
            if symbol in sector_rank:
                definitions.append(("etf_sector_rotation", sector_rank[symbol] <= min(3, len(sector_rank)), f["return_60d"] * 100.0, sector_rank[symbol]))
            for sleeve, active, score, rank in definitions:
                version = STRATEGY_VERSIONS[sleeve]
                payload = {"symbol": symbol, "observed_at": observed_at.isoformat(), "sleeve": sleeve, "version": version, "features": f}
                input_fp = _fingerprint(payload)
                insight_id = hashlib.sha256(f"{self.run_id}|{sleeve}|{symbol}|{input_fp}".encode()).hexdigest()[:32]
                provenance = {"source": "runtime_alpaca_daily_bars", "point_in_time_cutoff": observed_at.isoformat(), "adjustment_status": "provider_supplied_unverified", "no_future_rows": True}
                insights.append(ShadowInsight(
                    insight_id, self.run_id, sleeve, version, SHADOW_MODE, symbol,
                    observed_at.isoformat(), "long", "active" if active else "inactive", round(score, 8), rank,
                    f["close"], regime, REGIME_VERSION, FEATURE_VERSION, UNIVERSE_VERSION,
                    ELIGIBILITY_VERSION, OUTCOME_ENGINE_VERSION, input_fp, json_dumps(f),
                    json_dumps({"symbols": sorted(x["symbol"] for x in prepared), "source": item["universe_source"]}),
                    json_dumps(provenance),
                ))
        self._persist(tuple(insights), observed_at)
        return tuple(insights)

    def _persist(self, insights: tuple[ShadowInsight, ...], observed_at: datetime) -> None:
        active = [i for i in insights if i.signal == "active"]
        with self.storage.connect() as conn:
            apply_phase2_schema(conn)
            now = datetime.now(UTC).isoformat()
            for insight in insights:
                conn.execute(
                    "INSERT OR IGNORE INTO shadow_insights VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (*asdict(insight).values(), now),
                )
                if insight.signal != "active":
                    continue
                conn.execute(
                    """INSERT OR IGNORE INTO research_opportunities(
                    id,source_table,source_id,symbol,observed_at,direction,execution_type,entry_price,
                    strategy_version,score,score_version,feature_version,feature_snapshot_json,universe_version,
                    universe_snapshot_json,regime,regime_version,eligibility_version,blocker,blocker_version,
                    ai_gate,ai_gate_version,provenance_json,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (insight.id, "shadow_insights", insight.id, insight.symbol, insight.observed_at, "long",
                     "shadow_hypothetical", insight.entry_price, insight.strategy_version, insight.score,
                     "phase2_sleeve_score_v1", insight.feature_version, insight.feature_snapshot_json,
                     insight.universe_version, insight.universe_snapshot_json, insight.regime,
                     insight.regime_version, insight.eligibility_version, "SHADOW_ONLY", "phase2_structural_boundary_v1",
                     "not_applicable", "phase2_no_ai_gate_v1", insight.provenance_json, now),
                )
                for horizon in (1, 5, 20):
                    maturity = ExchangeSessions().add_sessions(observed_at.date(), horizon).isoformat()
                    outcome_id = hashlib.sha256(f"{insight.id}|{horizon}".encode()).hexdigest()[:32]
                    conn.execute(
                        """INSERT OR IGNORE INTO research_outcomes(
                        id,opportunity_id,horizon_sessions,status,reason,maturity_session,cost_model_version,
                        calculation_version,input_fingerprint,calculated_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?)""",
                        (outcome_id, insight.id, horizon, "maturing", "exchange_session_horizon_not_elapsed", maturity,
                         "paper_cost_assumption_v1", OUTCOME_ENGINE_VERSION, insight.input_fingerprint, now),
                    )
            by_symbol: dict[str, list[ShadowInsight]] = {}
            by_sleeve: dict[str, list[ShadowInsight]] = {}
            for insight in active:
                by_symbol.setdefault(insight.symbol, []).append(insight)
                by_sleeve.setdefault(insight.sleeve, []).append(insight)
            for sleeve, rows in by_sleeve.items():
                for insight in rows:
                    portfolio_id = hashlib.sha256(f"portfolio|{insight.id}".encode()).hexdigest()[:32]
                    conn.execute("INSERT OR IGNORE INTO shadow_portfolio_observations VALUES(?,?,?,?,?,?,?,?,?,?)",
                                 (portfolio_id, insight.id, sleeve, insight.strategy_version, insight.symbol,
                                  insight.observed_at, 1.0 / len(rows), "equal_weight_active_insights_v1", SHADOW_MODE, now))
            for symbol, rows in by_symbol.items():
                sleeves = sorted(i.sleeve for i in rows)
                pairs = [f"{a}|{b}" for n, a in enumerate(sleeves) for b in sleeves[n + 1:]]
                overlap_id = hashlib.sha256(f"overlap|{self.run_id}|{symbol}".encode()).hexdigest()[:32]
                conn.execute("INSERT OR IGNORE INTO shadow_overlap_observations VALUES(?,?,?,?,?,?,?,?)",
                             (overlap_id, self.run_id, symbol, observed_at.isoformat(), json_dumps(sleeves), len(sleeves), json_dumps(pairs), now))
            for sleeve, version in STRATEGY_VERSIONS.items():
                gate_id = hashlib.sha256(f"promotion|{version}".encode()).hexdigest()[:32]
                conn.execute("INSERT OR IGNORE INTO shadow_promotion_assessments VALUES(?,?,?,?,?,?,?,?,?)",
                             (gate_id, sleeve, version, now, "NOT_ELIGIBLE", "phase2_promotion_gate_v1", 0,
                              json_dumps(["insufficient completed out-of-sample outcomes", "Phase 2 is SHADOW_ONLY", "manual promotion is unsupported"]), now))
