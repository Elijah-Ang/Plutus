"""Deterministic, report-only strategy performance and policy engine.

This module is deliberately downstream of proposal, sizing, approval, order,
reconciliation, and FIFO accounting state.  It reads those records, creates a
canonical strategy-trade projection, and persists a scorecard/policy for
operator review.  The policy is not consulted by any execution or sizing path
in Build 1.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Iterable, Mapping, Sequence

from .formula_versions import (
    ACCOUNTING_VERSION,
    EVIDENCE_VERSION,
    STRATEGY_PERFORMANCE_SCHEMA_VERSION,
    STRATEGY_PERFORMANCE_VERSION,
    STRATEGY_POLICY_VERSION,
)
from .utils import iso_now, json_dumps


PRIMARY_HORIZON_SESSIONS = 20
EVIDENCE_CLASSES = frozenset({"shadow_oos", "actual_paper"})
QUALITY_SCORE_BOUNDARIES = (45.0, 60.0, 75.0)
POLICY_STATES = ("RESEARCH_ONLY", "EXPLORATION", "THROTTLED", "ACTIVE", "SUSPENDED")
_STATE_RANK = {name: index for index, name in enumerate(POLICY_STATES[:-1])}


def _finite(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    return str(value)


def _clamp(value: float | None, lower: float = 0.0, upper: float = 1.0) -> float:
    if value is None or not math.isfinite(float(value)):
        return 0.0
    return max(lower, min(upper, float(value)))


def _mean(values: Iterable[float]) -> float | None:
    values = [float(value) for value in values if _finite(value) is not None]
    return statistics.fmean(values) if values else None


def _value(row: PerformanceObservation | Mapping[str, Any], name: str, default: Any = None) -> Any:
    if isinstance(row, Mapping):
        return row.get(name, default)
    return getattr(row, name, default)


def _ordered(rows: Sequence[PerformanceObservation | Mapping[str, Any]]) -> list[PerformanceObservation | Mapping[str, Any]]:
    return sorted(rows, key=lambda row: (str(_value(row, "exit_session") or _value(row, "entry_session") or ""), str(_value(row, "observation_id") or _value(row, "source_id") or "")))


def _r_values(rows: Sequence[PerformanceObservation | Mapping[str, Any]], key: str = "r_multiple") -> list[float]:
    return [float(value) for row in _ordered(rows) if (value := _finite(_value(row, key))) is not None]


def expectancy_r(rows_or_values: Sequence[PerformanceObservation | Mapping[str, Any] | float]) -> float | None:
    """Arithmetic mean of completed, cost-adjusted trade-path R values."""
    values = []
    for item in rows_or_values:
        value = _finite(item) if isinstance(item, (int, float)) else _finite(_value(item, "r_multiple"))
        if value is not None:
            values.append(value)
    return _mean(values)


net_expectancy_r = expectancy_r


def profit_factor(rows_or_values: Sequence[PerformanceObservation | Mapping[str, Any] | float]) -> float | None:
    values = []
    for item in rows_or_values:
        value = _finite(item) if isinstance(item, (int, float)) else _finite(_value(item, "r_multiple"))
        if value is not None:
            values.append(value)
    gross_profit = sum(value for value in values if value > 0)
    gross_loss = abs(sum(value for value in values if value < 0))
    if gross_profit == 0 and gross_loss == 0:
        return None
    return float("inf") if gross_loss == 0 else gross_profit / gross_loss


def win_rate(rows_or_values: Sequence[PerformanceObservation | Mapping[str, Any] | float]) -> float | None:
    values = []
    for item in rows_or_values:
        value = _finite(item) if isinstance(item, (int, float)) else _finite(_value(item, "r_multiple"))
        if value is not None:
            values.append(value)
    return None if not values else sum(value > 0 for value in values) / len(values)


def average_win_r(rows_or_values: Sequence[PerformanceObservation | Mapping[str, Any] | float]) -> float | None:
    values = [value for value in _r_values(rows_or_values) if value > 0] if rows_or_values and not isinstance(rows_or_values[0], (int, float)) else [float(value) for value in rows_or_values if _finite(value) is not None and float(value) > 0]
    return _mean(values)


def average_loss_r(rows_or_values: Sequence[PerformanceObservation | Mapping[str, Any] | float]) -> float | None:
    values = [value for value in _r_values(rows_or_values) if value < 0] if rows_or_values and not isinstance(rows_or_values[0], (int, float)) else [float(value) for value in rows_or_values if _finite(value) is not None and float(value) < 0]
    return _mean(values)


def payoff_ratio(rows_or_values: Sequence[PerformanceObservation | Mapping[str, Any] | float]) -> float | None:
    win = average_win_r(rows_or_values)
    loss = average_loss_r(rows_or_values)
    return None if win is None or loss in (None, 0) else win / abs(loss)


def maximum_drawdown_r(rows_or_values: Sequence[PerformanceObservation | Mapping[str, Any] | float]) -> float | None:
    values = _r_values(rows_or_values) if rows_or_values and not isinstance(rows_or_values[0], (int, float)) else [float(value) for value in rows_or_values if _finite(value) is not None]
    if not values:
        return None
    curve = 0.0
    peak = 0.0
    drawdown = 0.0
    for value in values:
        curve += value
        peak = max(peak, curve)
        drawdown = max(drawdown, peak - curve)
    return drawdown


max_drawdown_r = maximum_drawdown_r


def worst_losing_streak(rows_or_values: Sequence[PerformanceObservation | Mapping[str, Any] | float]) -> int:
    values = _r_values(rows_or_values) if rows_or_values and not isinstance(rows_or_values[0], (int, float)) else [float(value) for value in rows_or_values if _finite(value) is not None]
    current = 0
    worst = 0
    for value in values:
        current = current + 1 if value < 0 else 0
        worst = max(worst, current)
    return worst


def rolling_window_means(values: Sequence[float], window: int = 20) -> list[float]:
    if window <= 0:
        raise ValueError("rolling window must be positive")
    clean = [float(value) for value in values if _finite(value) is not None]
    return [statistics.fmean(clean[index : index + window]) for index in range(len(clean) - window + 1)]


def recent_expectancy_r(rows_or_values: Sequence[PerformanceObservation | Mapping[str, Any] | float], window: int = 20) -> float | None:
    values = _r_values(rows_or_values) if rows_or_values and not isinstance(rows_or_values[0], (int, float)) else [float(value) for value in rows_or_values if _finite(value) is not None]
    return _mean(values[-window:]) if values else None


def positive_rolling_window_ratio(rows_or_values: Sequence[PerformanceObservation | Mapping[str, Any] | float], window: int = 20) -> float | None:
    values = _r_values(rows_or_values) if rows_or_values and not isinstance(rows_or_values[0], (int, float)) else [float(value) for value in rows_or_values if _finite(value) is not None]
    windows = rolling_window_means(values, window)
    return None if not windows else sum(value > 0 for value in windows) / len(windows)


def regime_metrics(rows: Sequence[PerformanceObservation | Mapping[str, Any]]) -> dict[str, dict[str, float | int | None]]:
    grouped: dict[str, list[PerformanceObservation | Mapping[str, Any]]] = {}
    for row in rows:
        regime = str(_value(row, "regime") or "unknown")
        grouped.setdefault(regime, []).append(row)
    return {
        regime: {"count": len(items), "expectancy_r": expectancy_r(items)}
        for regime, items in sorted(grouped.items())
    }


def positive_regime_ratio(rows: Sequence[PerformanceObservation | Mapping[str, Any]]) -> float | None:
    metrics = regime_metrics(rows)
    return None if not metrics else sum((item["expectancy_r"] or 0) > 0 for item in metrics.values()) / len(metrics)


def worst_regime_expectancy(rows: Sequence[PerformanceObservation | Mapping[str, Any]]) -> float | None:
    values = [float(item["expectancy_r"]) for item in regime_metrics(rows).values() if item["expectancy_r"] is not None]
    return min(values) if values else None


def median_absolute_implementation_shortfall(rows: Sequence[Mapping[str, Any]]) -> float | None:
    values = [abs(float(value)) for row in rows if (value := _finite(row.get("implementation_shortfall_bps"))) is not None]
    return statistics.median(values) if values else None


def cost_drag_ratio(rows: Sequence[PerformanceObservation | Mapping[str, Any]]) -> float | None:
    gross = [(_finite(_value(row, "gross_r")), _finite(_value(row, "r_multiple"))) for row in rows]
    gross = [(before, after) for before, after in gross if before is not None and after is not None]
    if not gross:
        return None
    drag = sum(max(0.0, before - after) for before, after in gross)
    denominator = abs(sum(before for before, _after in gross))
    return None if denominator == 0 else drag / denominator


def top_five_profit_contribution(rows: Sequence[PerformanceObservation | Mapping[str, Any]]) -> float | None:
    profits = sorted((max(0.0, float(_value(row, "net_pnl") if _finite(_value(row, "net_pnl")) is not None else _value(row, "r_multiple") or 0.0)) for row in rows), reverse=True)
    total = sum(profits)
    return None if total == 0 else sum(profits[:5]) / total


def largest_symbol_profit_contribution(rows: Sequence[PerformanceObservation | Mapping[str, Any]]) -> float | None:
    grouped: dict[str, float] = {}
    for row in rows:
        value = _finite(_value(row, "net_pnl"))
        if value is None:
            value = _finite(_value(row, "r_multiple"))
        if value is not None:
            grouped[str(_value(row, "symbol") or "unknown")] = grouped.get(str(_value(row, "symbol") or "unknown"), 0.0) + max(0.0, value)
    total = sum(grouped.values())
    return None if total == 0 else max(grouped.values(), default=0.0) / total


def shadow_paper_expectancy_divergence(rows: Sequence[PerformanceObservation | Mapping[str, Any]]) -> float | None:
    grouped: dict[str, list[PerformanceObservation | Mapping[str, Any]]] = {"shadow_oos": [], "actual_paper": []}
    for row in rows:
        if str(_value(row, "evidence_class")) in grouped:
            grouped[str(_value(row, "evidence_class"))].append(row)
    shadow = expectancy_r(grouped["shadow_oos"])
    paper = expectancy_r(grouped["actual_paper"])
    return None if shadow is None or paper is None else abs(shadow - paper)


def _confidence_score(values: Sequence[str]) -> float:
    scale = {"verified": 1.0, "reconstructed": 0.75, "partially_reconstructed": 0.5, "shadow_deterministic": 1.0, "unavailable": 0.0}
    return _mean(scale.get(str(value), 0.0) for value in values) or 0.0


@dataclass(frozen=True)
class PerformanceObservation:
    observation_id: str = ""
    strategy_version: str = ""
    symbol: str = ""
    evidence_class: str = ""
    entry_session: str | None = None
    exit_session: str | None = None
    regime: str | None = None
    score: float | None = None
    gross_return: float | None = None
    net_return: float | None = None
    gross_r: float | None = None
    r_multiple: float | None = None
    gross_pnl: float | None = None
    net_pnl: float | None = None
    initial_risk_dollars: float | None = None
    implementation_shortfall_bps: float | None = None
    attribution_confidence: str = "unavailable"
    evidence_version: str | None = None
    formula_version: str | None = None
    source_id: str | None = None
    attribution_status: str = "complete"


@dataclass(frozen=True)
class StrategyPerformanceSnapshot:
    strategy_version: str = ""
    as_of: str = ""
    performance_version: str = STRATEGY_PERFORMANCE_VERSION
    policy_version: str = STRATEGY_POLICY_VERSION
    schema_version: str = STRATEGY_PERFORMANCE_SCHEMA_VERSION
    metrics: dict[str, Any] = field(default_factory=dict)
    components: dict[str, float] = field(default_factory=dict)
    raw_inputs: dict[str, Any] = field(default_factory=dict)
    quality_score: float = 0.0
    recommendation_state: str = "RESEARCH_ONLY"
    fingerprint: str = ""
    trade_counts: dict[str, int] = field(default_factory=dict)
    evidence_recency_days: float | None = None
    attribution_confidence: float = 0.0
    version_completeness: float = 0.0
    id: str | None = None


@dataclass(frozen=True)
class StrategyRiskPolicy:
    strategy_version: str = ""
    state: str = "RESEARCH_ONLY"
    quality_score: float = 0.0
    reason: str = ""
    hard_gates: dict[str, bool] = field(default_factory=dict)
    maturity: dict[str, Any] = field(default_factory=dict)
    performance_snapshot_id: str | None = None
    enforcement_enabled: bool = False
    performance_version: str = STRATEGY_PERFORMANCE_VERSION
    policy_version: str = STRATEGY_POLICY_VERSION
    fingerprint: str = ""
    decided_at: str | None = None
    id: str | None = None


def score_components(metrics: Mapping[str, Any], settings: Mapping[str, Any] | None = None) -> tuple[dict[str, float], float, dict[str, float]]:
    """Return weighted components, total quality score, and penalties.

    Each component is normalized to its specified weight.  The inputs are
    intentionally explicit so the persisted score can be replayed without
    consulting current broker or account state.
    """
    cfg = dict(settings or {})
    target_expectancy = float(cfg.get("target_expectancy_r", 0.25))
    target_profit_factor = float(cfg.get("target_profit_factor", 1.75))
    target_drawdown = float(cfg.get("target_drawdown_r", 3.0))
    target_streak = float(cfg.get("target_losing_streak", 4.0))
    target_shortfall = float(cfg.get("target_shortfall_bps", 20.0))
    target_divergence = float(cfg.get("target_divergence_r", 0.50))

    pf = metrics.get("profit_factor")
    pf_quality = 1.0 if pf == float("inf") else _clamp((float(pf) - 1.0) / max(target_profit_factor - 1.0, 1e-9)) if pf is not None else 0.0
    profitability_quality = statistics.fmean(
        [_clamp(float(metrics.get("expectancy_r") or 0.0) / max(target_expectancy, 1e-9)), pf_quality, _clamp(metrics.get("win_rate"))]
    )
    downside_quality = statistics.fmean(
        [1.0 - _clamp(float(metrics.get("maximum_drawdown_r") or 0.0) / max(target_drawdown, 1e-9)), 1.0 - _clamp(float(metrics.get("worst_losing_streak") or 0.0) / max(target_streak, 1e-9))]
    )
    recent_positive = 1.0 if (metrics.get("recent_20_trade_expectancy_r") or 0.0) > 0 else 0.0
    stability_quality = statistics.fmean([recent_positive, _clamp(metrics.get("positive_rolling_20_window_ratio")), 1.0 - _clamp(abs(float(metrics.get("recent_20_trade_expectancy_r") or 0.0) - float(metrics.get("expectancy_r") or 0.0)) / max(target_expectancy, 1e-9))])
    regime_quality = statistics.fmean([_clamp(metrics.get("positive_regime_ratio")), _clamp((float(metrics.get("worst_regime_expectancy_r") or 0.0) + target_expectancy) / max(2.0 * target_expectancy, 1e-9))])
    fill_quality = _clamp(metrics.get("submitted_order_fill_rate")) if metrics.get("submitted_order_fill_rate") is not None else 0.5
    median_shortfall = metrics.get("median_absolute_implementation_shortfall_bps")
    shortfall_quality = 1.0 - _clamp(float(target_shortfall if median_shortfall is None else median_shortfall) / max(target_shortfall, 1e-9))
    drag_quality = 1.0 - _clamp(metrics.get("cost_drag_ratio")) if metrics.get("cost_drag_ratio") is not None else 0.5
    execution_quality = statistics.fmean([fill_quality, shortfall_quality, drag_quality])
    recency_quality = 0.0 if metrics.get("evidence_recency_days") is None else 1.0 - _clamp(float(metrics["evidence_recency_days"]) / max(float(cfg.get("evidence_stale_after_days", 90)), 1e-9))
    evidence_quality = statistics.fmean([_clamp(float(metrics.get("sample_count") or 0) / max(float(cfg.get("minimum_completed_samples", 100)), 1.0)), recency_quality, _clamp(metrics.get("attribution_confidence")), _clamp(metrics.get("version_completeness"))])

    components = {
        "profitability": round(30.0 * profitability_quality, 10),
        "downside": round(20.0 * downside_quality, 10),
        "stability": round(15.0 * stability_quality, 10),
        "regime": round(15.0 * regime_quality, 10),
        "execution": round(10.0 * execution_quality, 10),
        "evidence": round(10.0 * evidence_quality, 10),
    }
    top_five = float(metrics.get("top_five_profit_contribution") or 0.0)
    largest = float(metrics.get("largest_symbol_profit_contribution") or 0.0)
    concentration = min(10.0, 5.0 * _clamp((top_five - 0.60) / 0.30) + 5.0 * _clamp((largest - 0.35) / 0.40))
    divergence = min(10.0, 10.0 * _clamp(float(metrics.get("shadow_paper_expectancy_divergence_r") or 0.0) / max(target_divergence, 1e-9)))
    penalties = {"concentration": round(concentration, 10), "divergence": round(divergence, 10)}
    return components, round(max(0.0, sum(components.values()) - sum(penalties.values())), 10), penalties


def calculate_metrics(
    observations: Sequence[PerformanceObservation],
    *,
    execution_rows: Sequence[Mapping[str, Any]] = (),
    as_of: str | datetime | None = None,
    current_evidence_version: str = EVIDENCE_VERSION,
    current_formula_version: str = ACCOUNTING_VERSION,
    settings: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Calculate all Build 1 metrics from already canonical observations."""
    ordered = _ordered(observations)
    values = _r_values(ordered)
    shadow = [row for row in ordered if row.evidence_class == "shadow_oos"]
    actual = [row for row in ordered if row.evidence_class == "actual_paper"]
    regimes = regime_metrics(ordered)
    latest = max((_text(row.exit_session) or _text(row.entry_session) or "" for row in ordered), default=None)
    now = _parse_datetime(as_of) if as_of is not None else datetime.now(UTC)
    recency_days = None
    if latest:
        latest_dt = _parse_datetime(latest)
        recency_days = max(0.0, (now - latest_dt).total_seconds() / 86400.0)
    confidences = [row.attribution_confidence for row in ordered]
    attribution = _confidence_score(confidences)
    version_complete = _mean(float(row.evidence_version == current_evidence_version and row.formula_version == current_formula_version) for row in ordered) or 0.0
    submitted = [row for row in execution_rows if row.get("submitted")]
    filled = [row for row in submitted if row.get("filled")]
    fill_rate = None if not submitted else len(filled) / len(submitted)
    metrics: dict[str, Any] = {
        "sample_count": len(values),
        "trade_counts": {"shadow_oos": len(shadow), "actual_paper": len(actual), "total": len(values)},
        "net_expectancy_r": expectancy_r(values),
        "expectancy_r": expectancy_r(values),
        "profit_factor": profit_factor(values),
        "win_rate": win_rate(values),
        "average_win_r": average_win_r(values),
        "average_loss_r": average_loss_r(values),
        "payoff_ratio": payoff_ratio(values),
        "maximum_drawdown_r": maximum_drawdown_r(values),
        "worst_losing_streak": worst_losing_streak(values),
        "recent_20_trade_expectancy_r": recent_expectancy_r(values, PRIMARY_HORIZON_SESSIONS),
        "positive_rolling_20_window_ratio": positive_rolling_window_ratio(values, PRIMARY_HORIZON_SESSIONS),
        "regime_metrics": regimes,
        "positive_regime_ratio": positive_regime_ratio(ordered),
        "worst_regime_expectancy_r": worst_regime_expectancy(ordered),
        "submitted_order_fill_rate": fill_rate,
        "median_absolute_implementation_shortfall_bps": median_absolute_implementation_shortfall(execution_rows),
        "cost_drag_ratio": cost_drag_ratio(ordered),
        "top_five_profit_contribution": top_five_profit_contribution(ordered),
        "largest_symbol_profit_contribution": largest_symbol_profit_contribution(ordered),
        "shadow_paper_expectancy_divergence_r": shadow_paper_expectancy_divergence(ordered),
        "evidence_recency_days": recency_days,
        "attribution_confidence": attribution,
        "version_completeness": version_complete,
        "latest_evidence_session": latest,
    }
    raw_inputs = {
        "ordered_r_curve": values,
        "shadow_r": _r_values(shadow),
        "actual_r": _r_values(actual),
        "regimes": regimes,
        "execution_rows": [dict(row) for row in execution_rows],
        "current_evidence_version": current_evidence_version,
        "current_formula_version": current_formula_version,
    }
    return metrics, raw_inputs


def _parse_datetime(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return result.replace(tzinfo=UTC) if result.tzinfo is None else result.astimezone(UTC)


def _json_safe(value: Any) -> Any:
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(_json_safe(value), sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def apply_strategy_performance_schema(conn: sqlite3.Connection, *, record_migration: bool = True) -> None:
    """Add the Build 1 schema without rewriting existing measurement rows."""
    additions = {
        "position_lots": {
            "strategy_version": "TEXT", "entry_proposal_id": "TEXT", "entry_intent_id": "TEXT",
            "entry_regime": "TEXT", "entry_score": "REAL", "initial_risk_dollars": "REAL",
            "config_hash": "TEXT", "evidence_version": "TEXT", "formula_version": "TEXT",
        },
        "order_intents": {
            "strategy_version": "TEXT", "entry_regime": "TEXT", "entry_score": "REAL",
            "initial_risk_dollars": "REAL", "config_hash": "TEXT", "evidence_version": "TEXT", "formula_version": "TEXT",
        },
        "research_opportunities": {"strategy_performance_version": "TEXT"},
        "performance_setups": {"strategy_version": "TEXT", "evidence_version": "TEXT", "formula_version": "TEXT"},
        "trade_outcomes": {"strategy_version": "TEXT", "evidence_version": "TEXT", "formula_version": "TEXT"},
    }
    for table, columns in additions.items():
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if not exists:
            # The ordinary development initializer intentionally does not run
            # deployment migrations.  The explicit release migration creates
            # Phase 1 tables before reaching this function.
            continue
        present = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
        for name, definition in columns.items():
            if name not in present:
                conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {definition}')
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS lot_consumptions(
          id TEXT PRIMARY KEY, broker_event_key TEXT NOT NULL, sell_intent_id TEXT,
          position_lifecycle_id TEXT, lot_id TEXT NOT NULL, strategy_version TEXT,
          quantity REAL NOT NULL CHECK(quantity>0), allocated_proceeds REAL,
          allocated_cost_basis REAL, allocated_buy_fees REAL, allocated_sell_fees REAL,
          realized_pnl REAL, occurred_at TEXT NOT NULL, confidence TEXT NOT NULL,
          accounting_version TEXT NOT NULL, UNIQUE(broker_event_key,lot_id));
        CREATE TABLE IF NOT EXISTS strategy_trade_records(
          id TEXT PRIMARY KEY, source_key TEXT NOT NULL UNIQUE, strategy_version TEXT,
          symbol TEXT, evidence_class TEXT NOT NULL, position_lifecycle_id TEXT,
          source_id TEXT NOT NULL, entry_session TEXT, exit_session TEXT, regime TEXT,
          score REAL, quantity REAL, gross_return REAL, net_return REAL, gross_pnl REAL,
          net_pnl REAL, initial_risk_dollars REAL, gross_r_multiple REAL,
          r_multiple REAL, attribution_status TEXT NOT NULL, attribution_confidence TEXT,
          evidence_version TEXT, formula_version TEXT, reason TEXT, details_json TEXT NOT NULL,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS strategy_performance_snapshots(
          id TEXT PRIMARY KEY, strategy_version TEXT NOT NULL, as_of TEXT NOT NULL,
          performance_version TEXT NOT NULL, policy_version TEXT NOT NULL,
          schema_version TEXT NOT NULL, quality_score REAL NOT NULL,
          recommendation_state TEXT NOT NULL, trade_counts_json TEXT NOT NULL,
          metrics_json TEXT NOT NULL, components_json TEXT NOT NULL,
          raw_inputs_json TEXT NOT NULL, evidence_recency_days REAL,
          attribution_confidence REAL NOT NULL, version_completeness REAL NOT NULL,
          input_fingerprint TEXT NOT NULL, created_at TEXT NOT NULL,
          UNIQUE(strategy_version,input_fingerprint));
        CREATE TABLE IF NOT EXISTS strategy_policy_decisions(
          id TEXT PRIMARY KEY, strategy_version TEXT NOT NULL, decided_at TEXT NOT NULL,
          performance_snapshot_id TEXT NOT NULL, state TEXT NOT NULL, quality_score REAL NOT NULL,
          reason TEXT NOT NULL, hard_gates_json TEXT NOT NULL, maturity_json TEXT NOT NULL,
          components_json TEXT NOT NULL, raw_inputs_json TEXT NOT NULL,
          enforcement_enabled INTEGER NOT NULL CHECK(enforcement_enabled IN (0,1)),
          performance_version TEXT NOT NULL, policy_version TEXT NOT NULL,
          schema_version TEXT NOT NULL, input_fingerprint TEXT NOT NULL,
          UNIQUE(strategy_version,performance_snapshot_id));
        CREATE INDEX IF NOT EXISTS idx_lot_consumptions_lifecycle ON lot_consumptions(position_lifecycle_id,occurred_at);
        CREATE INDEX IF NOT EXISTS idx_strategy_trade_records_strategy ON strategy_trade_records(strategy_version,evidence_class,exit_session);
        CREATE INDEX IF NOT EXISTS idx_strategy_snapshots_latest ON strategy_performance_snapshots(strategy_version,created_at);
        CREATE INDEX IF NOT EXISTS idx_strategy_policies_latest ON strategy_policy_decisions(strategy_version,decided_at);
        """
    )
    if record_migration:
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version,applied_at,detail) VALUES(?,?,?)",
            (STRATEGY_PERFORMANCE_SCHEMA_VERSION, iso_now(), "additive deterministic strategy profitability and FIFO attribution schema"),
        )


class StrategyPerformanceEngine:
    """Build and persist scorecards without changing trading decisions."""

    def __init__(self, storage: Any, config: Mapping[str, Any] | None = None, *, as_of: str | datetime | None = None) -> None:
        self.storage = storage
        self.config = dict(config or {})
        self.cfg = dict(self.config.get("profitability_engine", {}) or {})
        self.as_of = _parse_datetime(as_of or datetime.now(UTC)).isoformat()

    def _settings(self) -> dict[str, Any]:
        phase3 = self.config.get("phase3", {}) or {}
        phase4 = self.config.get("phase4", {}) or {}
        promotion = phase3.get("promotion", {}) or {}
        return {
            "minimum_completed_samples": int(self.cfg.get("minimum_completed_samples", phase4.get("minimum_oos_samples", promotion.get("minimum_completed_oos", 100)))),
            "minimum_regimes": int(self.cfg.get("minimum_regimes", phase4.get("minimum_regimes", promotion.get("minimum_regimes", 2)))),
            "evidence_stale_after_days": float(self.cfg.get("evidence_stale_after_days", phase4.get("evidence_stale_after_days", 90))),
            "maturity_research_only_max": int(self.cfg.get("maturity_research_only_max", 19)),
            "maturity_exploration_max": int(self.cfg.get("maturity_exploration_max", 49)),
            "maturity_throttled_max": int(self.cfg.get("maturity_throttled_max", 99)),
            "score_exploration_threshold": float(self.cfg.get("score_exploration_threshold", 45.0)),
            "score_throttled_threshold": float(self.cfg.get("score_throttled_threshold", 60.0)),
            "score_active_threshold": float(self.cfg.get("score_active_threshold", 75.0)),
            "hard_max_drawdown_r": float(self.cfg.get("hard_max_drawdown_r", 6.0)),
            "hard_max_losing_streak": int(self.cfg.get("hard_max_losing_streak", 8)),
            "hard_max_divergence_r": float(self.cfg.get("hard_max_divergence_r", 1.50)),
            "target_expectancy_r": float(self.cfg.get("target_expectancy_r", 0.25)),
            "target_profit_factor": float(self.cfg.get("target_profit_factor", 1.75)),
            "target_drawdown_r": float(self.cfg.get("target_drawdown_r", 3.0)),
            "target_losing_streak": float(self.cfg.get("target_losing_streak", 4.0)),
            "target_shortfall_bps": float(self.cfg.get("target_shortfall_bps", 20.0)),
            "target_divergence_r": float(self.cfg.get("target_divergence_r", 0.50)),
        }

    def _shadow_observations(self) -> list[PerformanceObservation]:
        rows = self.storage.fetch_all(
            """SELECT ro.id opportunity_id,ro.source_table,ro.source_id,ro.symbol,ro.observed_at,
                      ro.strategy_version,ro.score,ro.regime,ro.split_label,ro.execution_type,
                      ro.regime_version,r.id outcome_id,r.exit_session,r.outcome_class,
                      r.gross_return,r.cost_adjusted_return,r.trade_path_gross_return,
                      r.trade_path_cost_adjusted_return,r.gross_r_multiple,r.cost_adjusted_r_multiple,
                      r.calculation_version,r.cost_model_version
               FROM research_opportunities ro JOIN research_outcomes r ON r.opportunity_id=ro.id
               WHERE r.status='completed' AND r.horizon_sessions=?
                 AND ro.split_label='out_of_sample' AND r.calculation_version=?
               ORDER BY r.exit_session,ro.id,r.id""",
            (PRIMARY_HORIZON_SESSIONS, EVIDENCE_VERSION),
        )
        result: list[PerformanceObservation] = []
        for row in rows:
            is_shadow = str(row.get("execution_type") or "").lower() in {"shadow_hypothetical", "shadow", "shadow_oos"} or str(row.get("source_table") or "").lower().startswith("shadow")
            if not is_shadow or str(row.get("outcome_class") or "") == "fixed_horizon_observation":
                continue
            net = row.get("trade_path_cost_adjusted_return")
            gross = row.get("trade_path_gross_return")
            if net is None or gross is None or not row.get("exit_session"):
                continue
            r_multiple = row.get("cost_adjusted_r_multiple")
            gross_r = row.get("gross_r_multiple")
            if r_multiple is None:
                continue
            result.append(PerformanceObservation(
                observation_id=f"shadow:{row['outcome_id']}", source_id=str(row["outcome_id"]), strategy_version=str(row["strategy_version"]),
                symbol=str(row["symbol"]), evidence_class="shadow_oos", entry_session=str(row["observed_at"]),
                exit_session=str(row["exit_session"]), regime=_text(row.get("regime")), score=_finite(row.get("score")),
                gross_return=_finite(gross), net_return=_finite(net), gross_r=_finite(gross_r), r_multiple=_finite(r_multiple),
                attribution_confidence="shadow_deterministic", evidence_version=EVIDENCE_VERSION,
                formula_version=ACCOUNTING_VERSION,
            ))
        return result

    def _actual_observations(self) -> list[PerformanceObservation]:
        lifecycles = self.storage.fetch_all("SELECT * FROM position_lifecycles WHERE state='closed' AND closed_at IS NOT NULL ORDER BY closed_at,id")
        result: list[PerformanceObservation] = []
        for lifecycle in lifecycles:
            lifecycle_id = str(lifecycle["id"])
            lots = self.storage.fetch_all("SELECT * FROM position_lots WHERE position_lifecycle_id=? ORDER BY opened_at,id", (lifecycle_id,))
            if not lots:
                self._persist_actual_unavailable(lifecycle, "no_attributed_entry_lots")
                continue
            lot_ids = [str(row["id"]) for row in lots]
            placeholders = ",".join("?" for _ in lot_ids)
            consumptions = self.storage.fetch_all(
                f"SELECT * FROM lot_consumptions WHERE position_lifecycle_id=? OR lot_id IN ({placeholders}) ORDER BY occurred_at,id",
                (lifecycle_id, *lot_ids),
            )
            versions = {str(row.get("strategy_version")) for row in lots if row.get("strategy_version")}
            all_strategy = all(row.get("strategy_version") for row in lots)
            all_versioned = all(row.get("evidence_version") == EVIDENCE_VERSION and row.get("formula_version") == ACCOUNTING_VERSION for row in lots)
            all_numeric = bool(consumptions) and all(_finite(row.get("realized_pnl")) is not None for row in consumptions)
            closed_qty = sum(float(row.get("quantity") or 0) for row in consumptions)
            remaining_qty = sum(float(row.get("remaining_quantity") or 0) for row in lots)
            risk = sum(float(row.get("initial_risk_dollars")) for row in lots if _finite(row.get("initial_risk_dollars")) is not None)
            complete = all_strategy and len(versions) == 1 and all_versioned and all_numeric and risk > 0 and remaining_qty <= 1e-8 and closed_qty > 0
            reason = None
            if not complete:
                reason = "mixed_or_missing_strategy_attribution" if len(versions) != 1 or not all_strategy else "incomplete_lifecycle_accounting"
                self._persist_actual_unavailable(lifecycle, reason, strategy_version=next(iter(versions)) if len(versions) == 1 else None, confidence=self._aggregate_confidence(lots, consumptions))
                continue
            strategy_version = next(iter(versions))
            gross_pnl = sum(float(row.get("allocated_proceeds") or 0) - float(row.get("allocated_cost_basis") or 0) for row in consumptions)
            net_pnl = sum(float(row["realized_pnl"]) for row in consumptions)
            gross_return = gross_pnl / max(sum(float(row.get("allocated_cost_basis") or 0) for row in consumptions), 1e-12)
            net_return = net_pnl / max(sum(float(row.get("allocated_cost_basis") or 0) for row in consumptions), 1e-12)
            first = lots[0]
            result.append(PerformanceObservation(
                observation_id=f"actual:{lifecycle_id}", source_id=lifecycle_id, strategy_version=strategy_version,
                symbol=str(lifecycle["symbol"]), evidence_class="actual_paper", entry_session=str(first["opened_at"]),
                exit_session=str(lifecycle["closed_at"]), regime=_text(first.get("entry_regime")), score=_finite(first.get("entry_score")),
                gross_return=gross_return, net_return=net_return, gross_r=gross_pnl / risk, r_multiple=net_pnl / risk,
                gross_pnl=gross_pnl, net_pnl=net_pnl, initial_risk_dollars=risk,
                attribution_confidence=self._aggregate_confidence(lots, consumptions), evidence_version=EVIDENCE_VERSION,
                formula_version=ACCOUNTING_VERSION,
            ))
        return result

    @staticmethod
    def _aggregate_confidence(lots: Sequence[Mapping[str, Any]], consumptions: Sequence[Mapping[str, Any]]) -> str:
        values = {str(row.get("confidence") or "unavailable") for row in (*lots, *consumptions)}
        if "unavailable" in values or "partially_reconstructed" in values:
            return "partially_reconstructed" if values - {"unavailable"} else "unavailable"
        if "reconstructed" in values:
            return "reconstructed"
        return "verified"

    def _persist_actual_unavailable(self, lifecycle: Mapping[str, Any], reason: str, *, strategy_version: str | None = None, confidence: str = "unavailable") -> None:
        now = iso_now()
        source_id = str(lifecycle["id"])
        source_key = f"actual_paper:{source_id}"
        record_id = _fingerprint(source_key)[:32]
        self.storage.execute(
            """INSERT INTO strategy_trade_records(
                 id,source_key,strategy_version,symbol,evidence_class,position_lifecycle_id,source_id,
                 entry_session,exit_session,attribution_status,attribution_confidence,evidence_version,
                 formula_version,reason,details_json,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(source_key) DO UPDATE SET strategy_version=excluded.strategy_version,
                 attribution_status=excluded.attribution_status,attribution_confidence=excluded.attribution_confidence,
                 reason=excluded.reason,updated_at=excluded.updated_at""",
            (record_id, source_key, strategy_version, lifecycle.get("symbol"), "actual_paper", source_id, source_id,
             lifecycle.get("opened_at"), lifecycle.get("closed_at"), "unavailable", confidence, None, None, reason,
             json_dumps({"report_only": True}), now, now),
        )

    def _persist_observation(self, row: PerformanceObservation) -> None:
        now = iso_now()
        source_key = f"{row.evidence_class}:{row.source_id or row.observation_id}"
        self.storage.execute(
            """INSERT INTO strategy_trade_records(
                 id,source_key,strategy_version,symbol,evidence_class,position_lifecycle_id,source_id,
                 entry_session,exit_session,regime,score,quantity,gross_return,net_return,gross_pnl,net_pnl,
                 initial_risk_dollars,gross_r_multiple,r_multiple,attribution_status,attribution_confidence,
                 evidence_version,formula_version,reason,details_json,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(source_key) DO UPDATE SET strategy_version=excluded.strategy_version,
                 symbol=excluded.symbol,entry_session=excluded.entry_session,exit_session=excluded.exit_session,
                 regime=excluded.regime,score=excluded.score,gross_return=excluded.gross_return,net_return=excluded.net_return,
                 gross_pnl=excluded.gross_pnl,net_pnl=excluded.net_pnl,initial_risk_dollars=excluded.initial_risk_dollars,
                 gross_r_multiple=excluded.gross_r_multiple,r_multiple=excluded.r_multiple,
                 attribution_status=excluded.attribution_status,attribution_confidence=excluded.attribution_confidence,
                 evidence_version=excluded.evidence_version,formula_version=excluded.formula_version,
                 details_json=excluded.details_json,updated_at=excluded.updated_at""",
            (_fingerprint(source_key)[:32], source_key, row.strategy_version, row.symbol, row.evidence_class, None, row.source_id or row.observation_id,
             row.entry_session, row.exit_session, row.regime, row.score, None, row.gross_return, row.net_return, row.gross_pnl, row.net_pnl,
             row.initial_risk_dollars, row.gross_r, row.r_multiple, row.attribution_status, row.attribution_confidence,
             row.evidence_version, row.formula_version, None, json_dumps(_json_safe(asdict(row))), now, now),
        )

    def _execution_rows(self, strategy_version: str) -> list[dict[str, Any]]:
        try:
            rows = self.storage.fetch_all(
                """SELECT oi.id,oi.state,oi.requested_quantity,oi.filled_quantity,
                          oi.implementation_shortfall_bps intent_shortfall,
                          o.implementation_shortfall_bps order_shortfall
                   FROM order_intents oi LEFT JOIN orders o ON o.id=oi.id
                   WHERE oi.strategy_version=? ORDER BY oi.created_at,oi.id""",
                (strategy_version,),
            )
        except sqlite3.Error:
            return []
        submitted_states = {"submitting", "submitted", "partially_filled", "filled", "cancel_pending", "cancelled", "unknown", "reconciliation_required"}
        result = []
        for row in rows:
            state = str(row.get("state") or "").lower()
            submitted = state in submitted_states or float(row.get("filled_quantity") or 0) > 0
            result.append({"submitted": submitted, "filled": float(row.get("filled_quantity") or 0) > 0 or state in {"filled", "partially_filled"}, "implementation_shortfall_bps": row.get("order_shortfall") if row.get("order_shortfall") is not None else row.get("intent_shortfall")})
        return result

    def _strategy_versions(self) -> list[str]:
        versions = set(str(value) for value in (self.config.get("approved_strategy_versions") or []) if value)
        versions.update(str(row["strategy_version"]) for row in self.storage.fetch_all("SELECT DISTINCT strategy_version FROM research_opportunities WHERE strategy_version IS NOT NULL") if row.get("strategy_version"))
        versions.update(str(row["strategy_version"]) for row in self.storage.fetch_all("SELECT DISTINCT strategy_version FROM position_lots WHERE strategy_version IS NOT NULL") if row.get("strategy_version"))
        versions.update(str(row["strategy_version"]) for row in self.storage.fetch_all("SELECT DISTINCT strategy_version FROM order_intents WHERE strategy_version IS NOT NULL") if row.get("strategy_version"))
        versions.update(str(row["strategy_version"]) for row in self.storage.fetch_all("SELECT DISTINCT strategy_version FROM trade_proposals WHERE strategy_version IS NOT NULL") if row.get("strategy_version"))
        return sorted(versions)

    def refresh_strategy(self, strategy_version: str) -> StrategyPerformanceSnapshot:
        if not strategy_version:
            raise ValueError("strategy_version is required")
        shadow = [row for row in self._shadow_observations() if row.strategy_version == strategy_version]
        actual = [row for row in self._actual_observations() if row.strategy_version == strategy_version]
        observations = _ordered([*shadow, *actual])
        for row in observations:
            self._persist_observation(row)
        settings = self._settings()
        execution = self._execution_rows(strategy_version)
        metrics, raw_inputs = calculate_metrics(observations, execution_rows=execution, as_of=self.as_of, settings=settings)
        components, quality, penalties = score_components(metrics, settings)
        minimum = settings["minimum_completed_samples"]
        sample_count = int(metrics["sample_count"])
        regime_count = len(metrics["regime_metrics"])
        recency = metrics.get("evidence_recency_days")
        gates = {
            "evidence_present": sample_count > 0,
            "minimum_sample": sample_count >= minimum,
            "minimum_regimes": regime_count >= settings["minimum_regimes"],
            "evidence_fresh": recency is not None and recency <= settings["evidence_stale_after_days"],
            "version_complete": metrics["version_completeness"] >= 1.0,
            "positive_expectancy": (metrics["expectancy_r"] or 0.0) > 0,
            "drawdown_within_hard_limit": (metrics["maximum_drawdown_r"] or 0.0) <= settings["hard_max_drawdown_r"],
            "losing_streak_within_hard_limit": int(metrics["worst_losing_streak"] or 0) <= settings["hard_max_losing_streak"],
            "divergence_within_hard_limit": (metrics["shadow_paper_expectancy_divergence_r"] is None or metrics["shadow_paper_expectancy_divergence_r"] <= settings["hard_max_divergence_r"]),
        }
        maturity = {
            "sample_count": sample_count, "minimum_completed_samples": minimum, "regime_count": regime_count,
            "minimum_regimes": settings["minimum_regimes"], "ceiling": "RESEARCH_ONLY" if sample_count <= settings["maturity_research_only_max"] else "EXPLORATION" if sample_count <= settings["maturity_exploration_max"] else "THROTTLED" if sample_count <= settings["maturity_throttled_max"] else "ACTIVE",
        }
        if not gates["evidence_present"]:
            state, reason = "RESEARCH_ONLY", "no complete current-version evidence"
        else:
            failed_hard = [name for name in ("evidence_fresh", "version_complete", "drawdown_within_hard_limit", "losing_streak_within_hard_limit", "divergence_within_hard_limit") if not gates[name]]
            if failed_hard and sample_count >= settings["maturity_throttled_max"]:
                state, reason = "SUSPENDED", "hard gate failed: " + ", ".join(failed_hard)
            else:
                if quality >= settings["score_active_threshold"]:
                    candidate = "ACTIVE"
                elif quality >= settings["score_throttled_threshold"]:
                    candidate = "THROTTLED"
                elif quality >= settings["score_exploration_threshold"]:
                    candidate = "EXPLORATION"
                else:
                    candidate = "RESEARCH_ONLY"
                ceiling = maturity["ceiling"]
                state = candidate if _STATE_RANK[candidate] <= _STATE_RANK[ceiling] else ceiling
                reason = "quality score and maturity ceiling" if state == candidate else f"maturity ceiling {ceiling}"
                if state == "ACTIVE" and not gates["minimum_sample"]:
                    state, reason = "THROTTLED", "minimum sample hard gate not met"
                if state == "ACTIVE" and not gates["minimum_regimes"]:
                    state, reason = "THROTTLED", "minimum regime hard gate not met"
                if state == "ACTIVE" and not gates["positive_expectancy"]:
                    state, reason = "SUSPENDED", "non-positive mature expectancy"
        raw_inputs = {**raw_inputs, "penalties": penalties, "hard_gates": gates, "maturity": maturity, "settings": settings}
        metrics = {**metrics, "quality_score": quality, "component_weights": {"profitability": 30, "downside": 20, "stability": 15, "regime": 15, "execution": 10, "evidence": 10}, "penalties": penalties}
        fingerprint = _fingerprint({"strategy_version": strategy_version, "observations": [asdict(row) for row in observations], "metrics": metrics, "components": components, "raw_inputs": raw_inputs, "performance_version": STRATEGY_PERFORMANCE_VERSION, "policy_version": STRATEGY_POLICY_VERSION})
        snapshot_id = fingerprint[:32]
        now = iso_now()
        self.storage.execute(
            """INSERT INTO strategy_performance_snapshots(
                 id,strategy_version,as_of,performance_version,policy_version,schema_version,quality_score,
                 recommendation_state,trade_counts_json,metrics_json,components_json,raw_inputs_json,
                 evidence_recency_days,attribution_confidence,version_completeness,input_fingerprint,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(strategy_version,input_fingerprint) DO UPDATE SET
                 as_of=excluded.as_of,quality_score=excluded.quality_score,recommendation_state=excluded.recommendation_state,
                 trade_counts_json=excluded.trade_counts_json,metrics_json=excluded.metrics_json,components_json=excluded.components_json,
                 raw_inputs_json=excluded.raw_inputs_json,evidence_recency_days=excluded.evidence_recency_days,
                 attribution_confidence=excluded.attribution_confidence,version_completeness=excluded.version_completeness""",
            (snapshot_id, strategy_version, self.as_of, STRATEGY_PERFORMANCE_VERSION, STRATEGY_POLICY_VERSION, STRATEGY_PERFORMANCE_SCHEMA_VERSION,
             quality, state, json_dumps(metrics["trade_counts"]), json_dumps(_json_safe(metrics)), json_dumps(_json_safe({**components, "penalties": penalties})),
             json_dumps(_json_safe(raw_inputs)), metrics.get("evidence_recency_days"), metrics.get("attribution_confidence", 0.0), metrics.get("version_completeness", 0.0), fingerprint, now),
        )
        self.storage.execute(
            """INSERT INTO strategy_policy_decisions(
                 id,strategy_version,decided_at,performance_snapshot_id,state,quality_score,reason,
                 hard_gates_json,maturity_json,components_json,raw_inputs_json,enforcement_enabled,
                 performance_version,policy_version,schema_version,input_fingerprint)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(strategy_version,performance_snapshot_id) DO UPDATE SET
                 decided_at=excluded.decided_at,state=excluded.state,quality_score=excluded.quality_score,reason=excluded.reason,
                 hard_gates_json=excluded.hard_gates_json,maturity_json=excluded.maturity_json,components_json=excluded.components_json,
                 raw_inputs_json=excluded.raw_inputs_json""",
            (_fingerprint({"policy": fingerprint, "state": state})[:32], strategy_version, now, snapshot_id, state, quality, reason,
             json_dumps(gates), json_dumps(maturity), json_dumps(_json_safe({**components, "penalties": penalties})), json_dumps(_json_safe(raw_inputs)), int(bool(self.cfg.get("enforcement_enabled", False))),
             STRATEGY_PERFORMANCE_VERSION, STRATEGY_POLICY_VERSION, STRATEGY_PERFORMANCE_SCHEMA_VERSION, fingerprint),
        )
        return StrategyPerformanceSnapshot(strategy_version, self.as_of, STRATEGY_PERFORMANCE_VERSION, STRATEGY_POLICY_VERSION, STRATEGY_PERFORMANCE_SCHEMA_VERSION, metrics, {**components, "concentration_penalty": penalties["concentration"], "divergence_penalty": penalties["divergence"]}, raw_inputs, quality, state, fingerprint, metrics["trade_counts"], metrics.get("evidence_recency_days"), metrics.get("attribution_confidence", 0.0), metrics.get("version_completeness", 0.0), snapshot_id)

    def refresh_all(self) -> dict[str, StrategyPerformanceSnapshot]:
        if self.cfg.get("enabled", True) is False:
            return {}
        return {version: self.refresh_strategy(version) for version in self._strategy_versions()}

    def latest_policy(self, strategy_version: str | None = None) -> StrategyRiskPolicy | dict[str, StrategyRiskPolicy] | None:
        params: tuple[Any, ...] = () if strategy_version is None else (strategy_version,)
        where = "" if strategy_version is None else "WHERE strategy_version=?"
        rows = self.storage.fetch_all(f"SELECT * FROM strategy_policy_decisions {where} ORDER BY decided_at DESC,id DESC", params)
        policies: dict[str, StrategyRiskPolicy] = {}
        for row in rows:
            if row["strategy_version"] in policies:
                continue
            policies[row["strategy_version"]] = StrategyRiskPolicy(
                strategy_version=row["strategy_version"], state=row["state"], quality_score=float(row["quality_score"]), reason=row["reason"],
                hard_gates=json.loads(row["hard_gates_json"] or "{}"), maturity=json.loads(row["maturity_json"] or "{}"),
                performance_snapshot_id=row["performance_snapshot_id"], enforcement_enabled=bool(row["enforcement_enabled"]),
                performance_version=row["performance_version"], policy_version=row["policy_version"], fingerprint=row["input_fingerprint"],
                decided_at=row["decided_at"], id=row["id"],
            )
        if strategy_version is not None:
            return policies.get(strategy_version)
        return policies

    def format_report(self, strategy_version: str | None = None) -> str:
        """Format only persisted values; this method never refreshes or writes."""
        params: tuple[Any, ...] = () if strategy_version is None else (strategy_version,)
        where = "" if strategy_version is None else "WHERE strategy_version=?"
        rows = self.storage.fetch_all(f"SELECT * FROM strategy_performance_snapshots {where} ORDER BY strategy_version,created_at DESC,id DESC", params)
        latest: dict[str, Mapping[str, Any]] = {}
        for row in rows:
            latest.setdefault(row["strategy_version"], row)
        if not latest:
            return "Strategy performance: no persisted scorecard available.\nReport-only; enforcement disabled."
        lines = ["Strategy performance (persisted, report-only)", "Enforcement: disabled"]
        for version, row in latest.items():
            metrics = json.loads(row["metrics_json"] or "{}")
            lines.extend([
                "",
                f"{version}: {row['recommendation_state']} (quality {float(row['quality_score']):.2f})",
                f"Trades: shadow_oos={metrics.get('trade_counts', {}).get('shadow_oos', 0)}, actual_paper={metrics.get('trade_counts', {}).get('actual_paper', 0)}",
                f"Expectancy R: {metrics.get('expectancy_r') if metrics.get('expectancy_r') is not None else 'unavailable'} | PF: {metrics.get('profit_factor') if metrics.get('profit_factor') is not None else 'unavailable'} | Win rate: {metrics.get('win_rate') if metrics.get('win_rate') is not None else 'unavailable'}",
                f"Max drawdown R: {metrics.get('maximum_drawdown_r') if metrics.get('maximum_drawdown_r') is not None else 'unavailable'} | Losing streak: {metrics.get('worst_losing_streak', 0)}",
                f"Fingerprint: {row['input_fingerprint'][:16]}",
            ])
        return "\n".join(lines)


__all__ = [
    "PerformanceObservation", "StrategyPerformanceSnapshot", "StrategyRiskPolicy", "StrategyPerformanceEngine",
    "STRATEGY_PERFORMANCE_VERSION", "STRATEGY_POLICY_VERSION", "STRATEGY_PERFORMANCE_SCHEMA_VERSION",
    "expectancy_r", "net_expectancy_r", "profit_factor", "win_rate", "average_win_r", "average_loss_r",
    "payoff_ratio", "maximum_drawdown_r", "max_drawdown_r", "worst_losing_streak", "rolling_window_means",
    "recent_expectancy_r", "positive_rolling_window_ratio", "regime_metrics", "positive_regime_ratio",
    "worst_regime_expectancy", "median_absolute_implementation_shortfall", "cost_drag_ratio",
    "top_five_profit_contribution", "largest_symbol_profit_contribution", "shadow_paper_expectancy_divergence",
    "score_components", "calculate_metrics", "apply_strategy_performance_schema",
]
