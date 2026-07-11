from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import statistics
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import pandas as pd

from .strategy_rule_based import STRATEGY_VERSION, completed_daily_bars, evaluate_symbol
from .utils import json_dumps
from .formula_versions import EVIDENCE_VERSION


PHASE1_SCHEMA_VERSION = "phase1_evidence_validation_v1"
OUTCOME_ENGINE_VERSION = EVIDENCE_VERSION
REGIME_VERSION = "spy_trend_vol_v1"
ELIGIBILITY_VERSION = "phase1_point_in_time_v1"
FEATURE_VERSION = "rule_based_features_v1"


def _utc(value: datetime | str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _session_date(value: Any) -> date:
    if hasattr(value, "to_pydatetime"):
        value = value.to_pydatetime()
    if isinstance(value, datetime):
        return _utc(value).date()
    if isinstance(value, date):
        return value
    return _utc(str(value)).date()


def _observed(day: date) -> date:
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    cursor = date(year, month, 1)
    return cursor + timedelta(days=(weekday - cursor.weekday()) % 7 + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    cursor = date(year + (month == 12), 1 if month == 12 else month + 1, 1) - timedelta(days=1)
    return cursor - timedelta(days=(cursor.weekday() - weekday) % 7)


def _easter(year: int) -> date:
    # Anonymous Gregorian algorithm; NYSE Good Friday is two days earlier.
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f, g = (b + 8) // 25, (b - (b + 8) // 25 + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    return date(year, month, (h + l - 7 * m + 114) % 31 + 1)


class ExchangeSessions:
    """Deterministic regular-session calendar for US equity daily labels.

    It intentionally models only full-day NYSE closures. Early closes remain
    valid sessions for daily outcomes. One-off closures must be supplied in
    ``extra_closures`` and are preserved as provenance in reports.
    """

    version = "nyse_regular_sessions_v1"

    def __init__(self, extra_closures: Iterable[date] = ()) -> None:
        self.extra_closures = frozenset(extra_closures)

    def holidays(self, year: int) -> set[date]:
        days = {
            _observed(date(year, 1, 1)),
            _nth_weekday(year, 1, 0, 3),  # MLK
            _nth_weekday(year, 2, 0, 3),  # Presidents Day
            _easter(year) - timedelta(days=2),
            _last_weekday(year, 5, 0),
            _observed(date(year, 7, 4)),
            _nth_weekday(year, 9, 0, 1),
            _nth_weekday(year, 11, 3, 4),
            _observed(date(year, 12, 25)),
        }
        if year >= 2022:
            days.add(_observed(date(year, 6, 19)))
        # A next-year New Year observation can fall in this calendar year.
        days.add(_observed(date(year + 1, 1, 1)))
        return {d for d in days if d.year == year} | set(self.extra_closures)

    def is_session(self, day: date) -> bool:
        return day.weekday() < 5 and day not in self.holidays(day.year)

    def next_session(self, day: date) -> date:
        cursor = day + timedelta(days=1)
        while not self.is_session(cursor):
            cursor += timedelta(days=1)
        return cursor

    def add_sessions(self, day: date, count: int) -> date:
        cursor = day
        for _ in range(count):
            cursor = self.next_session(cursor)
        return cursor


@dataclass(frozen=True)
class CostModel:
    version: str
    spread_bps: float
    entry_slippage_bps: float
    exit_slippage_bps: float
    commission_bps: float = 0.0
    regulatory_bps: float = 0.0
    delayed_entry_bps: float = 0.0
    source: str = "operator_assumption"
    observed_at: str | None = None

    @property
    def round_trip_bps(self) -> float:
        return (
            self.spread_bps
            + self.entry_slippage_bps
            + self.exit_slippage_bps
            + 2.0 * self.commission_bps
            + self.regulatory_bps
            + self.delayed_entry_bps
        )


@dataclass(frozen=True)
class Opportunity:
    id: str
    symbol: str
    observed_at: datetime
    entry_price: float | None
    direction: str
    execution_type: str
    strategy_version: str
    score: float | None = None
    blocker: str | None = None
    ai_gate: str | None = None
    stop_price: float | None = None
    target_price: float | None = None
    benchmark_entry_price: float | None = None
    actual_exit_price: float | None = None
    feature_version: str = FEATURE_VERSION
    universe_version: str = "unknown"
    regime_version: str = REGIME_VERSION
    eligibility_version: str = ELIGIBILITY_VERSION
    source_id: str | None = None
    source_table: str | None = None


@dataclass(frozen=True)
class HorizonResult:
    horizon_sessions: int
    status: str
    reason: str
    maturity_session: str
    exit_session: str | None = None
    gross_return: float | None = None
    spy_return: float | None = None
    spy_relative_return: float | None = None
    cost_adjusted_return: float | None = None
    mfe: float | None = None
    mae: float | None = None
    gross_r_multiple: float | None = None
    cost_adjusted_r_multiple: float | None = None
    stop_hit: bool | None = None
    target_hit: bool | None = None
    first_barrier: str | None = None
    ordering_quality: str | None = None
    cost_bps: float | None = None
    outcome_class: str = "executable_trade_path"
    holding_period_sessions: int | None = None
    trade_path_gross_return: float | None = None
    trade_path_spy_return: float | None = None
    trade_path_cost_adjusted_return: float | None = None
    fixed_horizon_gross_return: float | None = None
    fixed_horizon_spy_return: float | None = None
    fixed_horizon_cost_adjusted_return: float | None = None


def _bars_by_session(bars: pd.DataFrame) -> dict[date, Mapping[str, Any]]:
    result: dict[date, Mapping[str, Any]] = {}
    if bars is None or bars.empty:
        return result
    for idx, row in bars.sort_index().iterrows():
        result[_session_date(idx)] = row
    return result


class CanonicalOutcomeCalculator:
    def __init__(self, calendar: ExchangeSessions, cost_model: CostModel) -> None:
        self.calendar = calendar
        self.cost_model = cost_model

    def calculate(
        self,
        opportunity: Opportunity,
        asset_bars: pd.DataFrame,
        benchmark_bars: pd.DataFrame,
        *,
        as_of: datetime,
        horizons: Sequence[int] = (1, 5, 20),
    ) -> list[HorizonResult]:
        observed = _utc(opportunity.observed_at)
        observed_day = observed.date()
        asset = _bars_by_session(asset_bars)
        benchmark = _bars_by_session(benchmark_bars)
        entry = opportunity.entry_price
        results: list[HorizonResult] = []
        for horizon in horizons:
            maturity = self.calendar.add_sessions(observed_day, int(horizon))
            if entry is None or not math.isfinite(float(entry)) or float(entry) <= 0:
                results.append(HorizonResult(horizon, "unavailable", "missing_or_invalid_entry_price", maturity.isoformat()))
                continue
            if _utc(as_of).date() < maturity:
                results.append(HorizonResult(horizon, "maturing", "exchange_session_horizon_not_elapsed", maturity.isoformat()))
                continue
            sessions: list[date] = []
            cursor = observed_day
            while len(sessions) < horizon:
                cursor = self.calendar.next_session(cursor)
                sessions.append(cursor)
            missing_asset = [d for d in sessions if d not in asset]
            if missing_asset:
                results.append(HorizonResult(horizon, "unavailable", "asset_session_bars_missing", maturity.isoformat()))
                continue
            window = [(d, asset[d]) for d in sessions]
            direction = -1.0 if opportunity.direction.lower() in {"short", "sell"} else 1.0
            entry_f = float(entry)
            stop, target = opportunity.stop_price, opportunity.target_price
            first_barrier: str | None = None
            ordering_quality: str | None = None
            exit_price = float(window[-1][1]["close"])
            exit_day = window[-1][0]
            stop_hit = False if stop is not None else None
            target_hit = False if target is not None else None
            fixed_observation = str(opportunity.execution_type or "").lower() in {
                "shadow_hypothetical", "observation", "observation_only", "hypothetical", "research_only",
            } or str(opportunity.source_table or "").startswith("shadow")
            if fixed_observation:
                stop_hit = None
                target_hit = None
            if not fixed_observation:
                for day, bar in window:
                    high = float(bar.get("high", bar["close"]))
                    low = float(bar.get("low", bar["close"]))
                    hit_stop = stop is not None and (low <= float(stop) if direction > 0 else high >= float(stop))
                    hit_target = target is not None and (high >= float(target) if direction > 0 else low <= float(target))
                    if hit_stop:
                        stop_hit = True
                    if hit_target:
                        target_hit = True
                    if hit_stop and hit_target:
                        first_barrier, ordering_quality = "stop", "ambiguous_same_daily_bar_conservative_stop_first"
                        exit_price, exit_day = float(stop), day
                        break
                    if hit_stop:
                        first_barrier, ordering_quality = "stop", "daily_bar_ordered"
                        exit_price, exit_day = float(stop), day
                        break
                    if hit_target:
                        first_barrier, ordering_quality = "target", "daily_bar_ordered"
                        exit_price, exit_day = float(target), day
                        break
            exit_index = next((index for index, (day, _bar) in enumerate(window) if day == exit_day), len(window) - 1)
            attribution_window = window[: exit_index + 1]
            highs = [float(row.get("high", row["close"])) for _, row in attribution_window]
            lows = [float(row.get("low", row["close"])) for _, row in attribution_window]
            mfe = ((max(highs) / entry_f) - 1.0) * direction
            mae = ((min(lows) / entry_f) - 1.0) * direction
            if direction < 0:
                mfe, mae = -((min(lows) / entry_f) - 1.0), -((max(highs) / entry_f) - 1.0)
            gross = (exit_price / entry_f - 1.0) * direction
            fixed_exit_price = float(window[-1][1]["close"])
            fixed_gross = (fixed_exit_price / entry_f - 1.0) * direction
            benchmark_return = None
            benchmark_exit_day = sessions[-1] if fixed_observation else exit_day
            if opportunity.benchmark_entry_price is not None and benchmark_exit_day in benchmark:
                benchmark_return = float(benchmark[benchmark_exit_day]["close"]) / float(opportunity.benchmark_entry_price) - 1.0
            fixed_benchmark_return = None
            if opportunity.benchmark_entry_price is not None and sessions[-1] in benchmark:
                fixed_benchmark_return = float(benchmark[sessions[-1]]["close"]) / float(opportunity.benchmark_entry_price) - 1.0
            cost = self.cost_model.round_trip_bps / 10_000.0
            risk = abs(entry_f - float(stop)) / entry_f if stop is not None and float(stop) != entry_f else None
            net = gross - cost
            fixed_net = fixed_gross - cost
            outcome_class = "fixed_horizon_observation" if fixed_observation else "executable_trade_path"
            results.append(
                HorizonResult(
                    horizon_sessions=horizon,
                    status="completed",
                    reason="canonical_fixed_horizon_observation_completed" if fixed_observation else "canonical_session_trade_path_completed",
                    maturity_session=maturity.isoformat(),
                    exit_session=exit_day.isoformat(),
                    gross_return=gross,
                    spy_return=benchmark_return,
                    spy_relative_return=None if benchmark_return is None else gross - benchmark_return,
                    cost_adjusted_return=net,
                    mfe=mfe,
                    mae=mae,
                    gross_r_multiple=None if not risk else gross / risk,
                    cost_adjusted_r_multiple=None if not risk else net / risk,
                    stop_hit=stop_hit,
                    target_hit=target_hit,
                    first_barrier=first_barrier,
                    ordering_quality=ordering_quality or "no_barrier_within_horizon",
                    cost_bps=self.cost_model.round_trip_bps,
                    outcome_class=outcome_class,
                    holding_period_sessions=exit_index + 1,
                    trade_path_gross_return=None if fixed_observation else gross,
                    trade_path_spy_return=None if fixed_observation else benchmark_return,
                    trade_path_cost_adjusted_return=None if fixed_observation else net,
                    fixed_horizon_gross_return=fixed_gross if fixed_observation else None,
                    fixed_horizon_spy_return=fixed_benchmark_return if fixed_observation else None,
                    fixed_horizon_cost_adjusted_return=fixed_net if fixed_observation else None,
                )
            )
        return results


def deterministic_regime(spy_history: pd.DataFrame) -> str:
    """Regime uses only rows supplied at or before the attribution timestamp."""
    if spy_history is None or len(spy_history) < 200:
        return "unknown_insufficient_history"
    closes = spy_history["close"].astype(float)
    last = float(closes.iloc[-1])
    ma50 = float(closes.tail(50).mean())
    ma200 = float(closes.tail(200).mean())
    returns = closes.pct_change().dropna().tail(20)
    vol = float(returns.std(ddof=1) * math.sqrt(252)) if len(returns) >= 2 else math.nan
    if not math.isfinite(vol):
        return "unknown_missing_volatility"
    trend = "uptrend" if last > ma50 > ma200 else "downtrend" if last < ma50 < ma200 else "mixed"
    vol_band = "high_vol" if vol > 0.25 else "normal_vol"
    return f"{trend}_{vol_band}"


class PointInTimeSimulator:
    """Expanding-window simulator that calls production strategy logic directly."""

    def __init__(self, minimum_history: int = 200) -> None:
        self.minimum_history = max(200, int(minimum_history))

    def opportunities(
        self,
        symbol: str,
        bars: pd.DataFrame,
        spy_bars: pd.DataFrame,
        membership: Callable[[str, date], tuple[bool, str]],
        *,
        maximum_volatility_20d: float = 0.50,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        # Runtime and simulation use the same completed daily-bar boundary.
        bars = completed_daily_bars(bars)
        spy_bars = completed_daily_bars(spy_bars)
        for end in range(self.minimum_history, len(bars)):
            decision_day = _session_date(bars.index[end])
            eligible, universe_version = membership(symbol, decision_day)
            if not eligible:
                continue
            history = bars.iloc[:end]  # never includes the outcome/session bar
            signal = evaluate_symbol(
                symbol,
                history,
                market_open=True,
                maximum_volatility_20d=maximum_volatility_20d,
            )
            if signal.action != "ENTRY":
                continue
            entry = float(bars.iloc[end]["open"])
            spy_prefix = spy_bars.loc[[_session_date(i) <= decision_day for i in spy_bars.index]]
            rows.append(
                {
                    "id": hashlib.sha256(f"{symbol}|{decision_day}|{STRATEGY_VERSION}".encode()).hexdigest()[:32],
                    "symbol": symbol,
                    "observed_at": datetime.combine(decision_day, datetime.min.time(), tzinfo=UTC),
                    "entry_price": entry,
                    "benchmark_entry_price": float(spy_prefix.iloc[-1]["close"]) if not spy_prefix.empty else None,
                    "direction": "long",
                    "execution_type": "historical_hypothetical",
                    "strategy_version": signal.strategy_version,
                    "feature_version": FEATURE_VERSION,
                    "universe_version": universe_version,
                    "regime": deterministic_regime(spy_prefix),
                    "regime_version": REGIME_VERSION,
                    "eligibility_version": ELIGIBILITY_VERSION,
                    "signal_reason": signal.reason,
                    "feature_snapshot": signal.indicators,
                }
            )
        return rows


@dataclass(frozen=True)
class Fold:
    fold: int
    train_start: str
    train_end: str
    test_start: str
    test_end: str
    train_ids: tuple[str, ...]
    test_ids: tuple[str, ...]


def walk_forward_folds(
    rows: Sequence[Mapping[str, Any]],
    *,
    train_sessions: int,
    test_sessions: int,
    purge_sessions: int = 20,
    embargo_sessions: int = 1,
) -> list[Fold]:
    ordered = sorted(rows, key=lambda r: (str(r["observed_at"]), str(r["id"])))
    unique_dates = sorted({_utc(r["observed_at"]).date() for r in ordered})
    folds: list[Fold] = []
    cursor = train_sessions
    while cursor + embargo_sessions + test_sessions <= len(unique_dates):
        train_dates = unique_dates[cursor - train_sessions : max(cursor - purge_sessions, cursor - train_sessions)]
        test_dates = unique_dates[cursor + embargo_sessions : cursor + embargo_sessions + test_sessions]
        if train_dates and test_dates:
            train_ids = tuple(str(r["id"]) for r in ordered if _utc(r["observed_at"]).date() in train_dates)
            test_ids = tuple(str(r["id"]) for r in ordered if _utc(r["observed_at"]).date() in test_dates)
            folds.append(Fold(len(folds) + 1, train_dates[0].isoformat(), train_dates[-1].isoformat(), test_dates[0].isoformat(), test_dates[-1].isoformat(), train_ids, test_ids))
        cursor += test_sessions
    return folds


def _mean(values: Sequence[float]) -> float | None:
    return statistics.fmean(values) if values else None


def evidence_metrics(rows: Sequence[Mapping[str, Any]], value_key: str = "cost_adjusted_return") -> dict[str, Any]:
    values = [float(r[value_key]) for r in rows if r.get(value_key) is not None]
    result: dict[str, Any] = {"n": len(values), "mean": _mean(values), "win_rate": None, "sharpe": None, "probabilistic_sharpe": None}
    if not values:
        return result
    result["win_rate"] = sum(v > 0 for v in values) / len(values)
    if len(values) >= 2:
        sd = statistics.stdev(values)
        if sd > 0:
            sr = statistics.fmean(values) / sd
            result["sharpe"] = sr
            z = sr * math.sqrt(max(len(values) - 1, 1))
            result["probabilistic_sharpe"] = 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
    return result


def score_calibration(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    bands = [(0, 59), (60, 69), (70, 79), (80, 89), (90, 100)]
    output: list[dict[str, Any]] = []
    for low, high in bands:
        selected = [r for r in rows if r.get("score") is not None and low <= float(r["score"]) <= high and r.get("cost_adjusted_return") is not None]
        values = [float(r["cost_adjusted_return"]) for r in selected]
        observed = None if not values else sum(v > 0 for v in values) / len(values)
        predicted = None if not selected else statistics.fmean(float(r["score"]) / 100.0 for r in selected)
        brier = None if not selected else statistics.fmean(((float(r["score"]) / 100.0) - (1.0 if float(r["cost_adjusted_return"]) > 0 else 0.0)) ** 2 for r in selected)
        output.append({"score_band": f"{low}-{high}", "n": len(selected), "predicted_probability": predicted, "observed_win_rate": observed, "brier_score": brier})
    return output


def grouped_evidence(rows: Sequence[Mapping[str, Any]], keys: Sequence[str]) -> list[dict[str, Any]]:
    groups: dict[tuple[Any, ...], list[Mapping[str, Any]]] = {}
    for row in rows:
        groups.setdefault(tuple(row.get(k) for k in keys), []).append(row)
    output = []
    for group, members in sorted(groups.items(), key=lambda item: tuple(str(v) for v in item[0])):
        output.append({**dict(zip(keys, group)), **evidence_metrics(members)})
    return output


def bootstrap_mean_interval(values: Sequence[float], *, seed: int = 20260711, draws: int = 2000) -> tuple[float, float] | None:
    if len(values) < 2:
        return None
    import numpy as np

    rng = np.random.default_rng(seed)
    data = np.asarray(values, dtype=float)
    means = rng.choice(data, size=(draws, len(data)), replace=True).mean(axis=1)
    return float(np.quantile(means, 0.025)), float(np.quantile(means, 0.975))


def sensitivity(rows: Sequence[Mapping[str, Any]], base_cost_bps: float, scenarios: Sequence[float] = (0.5, 1.0, 1.5, 2.0)) -> list[dict[str, Any]]:
    result = []
    for multiplier in scenarios:
        values = [float(r["gross_return"]) - base_cost_bps * multiplier / 10_000.0 for r in rows if r.get("gross_return") is not None]
        result.append({"cost_multiplier": multiplier, "cost_bps": base_cost_bps * multiplier, "n": len(values), "mean": _mean(values), "positive": None if not values else _mean(values) > 0})
    return result


def apply_phase1_schema(conn: sqlite3.Connection) -> None:
    schema_sql = """
        CREATE TABLE IF NOT EXISTS research_cost_models(
          version TEXT PRIMARY KEY, parameters_json TEXT NOT NULL, source TEXT NOT NULL,
          observed_at TEXT, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS research_opportunities(
          id TEXT PRIMARY KEY, source_table TEXT, source_id TEXT, symbol TEXT NOT NULL,
          observed_at TEXT NOT NULL, direction TEXT NOT NULL, execution_type TEXT NOT NULL,
          entry_price REAL, stop_price REAL, target_price REAL, benchmark_entry_price REAL, actual_exit_price REAL,
          strategy_version TEXT NOT NULL, score REAL, score_version TEXT,
          feature_version TEXT NOT NULL, feature_snapshot_json TEXT,
          universe_version TEXT NOT NULL, universe_snapshot_json TEXT,
          regime TEXT, regime_version TEXT NOT NULL, eligibility_version TEXT NOT NULL,
          blocker TEXT, blocker_version TEXT, ai_gate TEXT, ai_gate_version TEXT,
          split_label TEXT, provenance_json TEXT NOT NULL, created_at TEXT NOT NULL,
          UNIQUE(source_table, source_id));
        CREATE TABLE IF NOT EXISTS research_outcomes(
          id TEXT PRIMARY KEY, opportunity_id TEXT NOT NULL, horizon_sessions INTEGER NOT NULL,
          status TEXT NOT NULL CHECK(status IN ('completed','maturing','unavailable','failed')),
          reason TEXT NOT NULL, maturity_session TEXT NOT NULL, exit_session TEXT,
          outcome_class TEXT NOT NULL DEFAULT 'executable_trade_path', holding_period_sessions INTEGER,
          gross_return REAL, spy_return REAL, spy_relative_return REAL,
          cost_adjusted_return REAL, mfe REAL, mae REAL, gross_r_multiple REAL,
          cost_adjusted_r_multiple REAL, stop_hit INTEGER, target_hit INTEGER,
          trade_path_gross_return REAL, trade_path_spy_return REAL, trade_path_cost_adjusted_return REAL,
          fixed_horizon_gross_return REAL, fixed_horizon_spy_return REAL, fixed_horizon_cost_adjusted_return REAL,
          first_barrier TEXT, ordering_quality TEXT, cost_model_version TEXT NOT NULL,
          cost_bps REAL, calculation_version TEXT NOT NULL, input_fingerprint TEXT NOT NULL,
          calculated_at TEXT NOT NULL, error_category TEXT, invalidated_at TEXT,
          UNIQUE(opportunity_id, horizon_sessions));
        CREATE TABLE IF NOT EXISTS research_backfill_jobs(
          id TEXT PRIMARY KEY, source_fingerprint TEXT NOT NULL, status TEXT NOT NULL,
          cursor TEXT, processed INTEGER NOT NULL DEFAULT 0, completed INTEGER NOT NULL DEFAULT 0,
          maturing INTEGER NOT NULL DEFAULT 0, unavailable INTEGER NOT NULL DEFAULT 0,
          failed INTEGER NOT NULL DEFAULT 0, started_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          completed_at TEXT, safe_error TEXT, UNIQUE(source_fingerprint));
        CREATE TABLE IF NOT EXISTS research_validation_runs(
          id TEXT PRIMARY KEY, as_of TEXT NOT NULL, status TEXT NOT NULL,
          config_json TEXT NOT NULL, input_fingerprint TEXT NOT NULL,
          assumptions_json TEXT NOT NULL, limitations_json TEXT NOT NULL,
          result_json TEXT NOT NULL, report_path TEXT, created_at TEXT NOT NULL,
          UNIQUE(input_fingerprint));
        CREATE TABLE IF NOT EXISTS research_validation_folds(
          id TEXT PRIMARY KEY, validation_run_id TEXT NOT NULL, fold INTEGER NOT NULL,
          train_start TEXT, train_end TEXT, test_start TEXT, test_end TEXT,
          purge_sessions INTEGER NOT NULL, embargo_sessions INTEGER NOT NULL,
          train_n INTEGER NOT NULL, test_n INTEGER NOT NULL, result_json TEXT NOT NULL,
          UNIQUE(validation_run_id, fold));
        CREATE INDEX IF NOT EXISTS idx_research_outcomes_status_maturity
          ON research_outcomes(status, maturity_session);
        CREATE INDEX IF NOT EXISTS idx_research_opportunities_observed
          ON research_opportunities(observed_at, symbol);
        CREATE INDEX IF NOT EXISTS idx_research_outcomes_opportunity
          ON research_outcomes(opportunity_id, horizon_sessions);
        """
    for statement in schema_sql.split(";"):
        if statement.strip():
            conn.execute(statement)
    opportunity_columns = {row[1] for row in conn.execute("PRAGMA table_info(research_opportunities)")}
    if "benchmark_entry_price" not in opportunity_columns:
        conn.execute("ALTER TABLE research_opportunities ADD COLUMN benchmark_entry_price REAL")
    outcome_columns = {row[1] for row in conn.execute("PRAGMA table_info(research_outcomes)")}
    outcome_additions = {
        "outcome_class": "TEXT DEFAULT 'executable_trade_path'",
        "holding_period_sessions": "INTEGER",
        "trade_path_gross_return": "REAL",
        "trade_path_spy_return": "REAL",
        "trade_path_cost_adjusted_return": "REAL",
        "fixed_horizon_gross_return": "REAL",
        "fixed_horizon_spy_return": "REAL",
        "fixed_horizon_cost_adjusted_return": "REAL",
        "invalidated_at": "TEXT",
    }
    for name, definition in outcome_additions.items():
        if name not in outcome_columns:
            conn.execute(f"ALTER TABLE research_outcomes ADD COLUMN {name} {definition}")
    conn.execute(
        """UPDATE research_outcomes
           SET status='failed', reason=?, error_category=?, invalidated_at=?
           WHERE calculation_version<>? AND status='completed'""",
        (f"invalidated_calculation_version:{OUTCOME_ENGINE_VERSION}", "outcome_engine_version_changed", datetime.now(UTC).isoformat(), OUTCOME_ENGINE_VERSION),
    )
    now = datetime.now(UTC).isoformat()
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations(version,applied_at,detail) VALUES(?,?,?)",
        (PHASE1_SCHEMA_VERSION, now, "additive point-in-time opportunity, canonical outcome, backfill and validation schema"),
    )


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


class ResearchRepository:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def migrate(self) -> None:
        with self.connect() as conn:
            apply_phase1_schema(conn)

    def upsert_opportunity(self, opportunity: Opportunity, **extra: Any) -> None:
        now = datetime.now(UTC).isoformat()
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO research_opportunities(
                  id,source_table,source_id,symbol,observed_at,direction,execution_type,
                  entry_price,stop_price,target_price,benchmark_entry_price,actual_exit_price,strategy_version,score,
                  score_version,feature_version,feature_snapshot_json,universe_version,
                  universe_snapshot_json,regime,regime_version,eligibility_version,blocker,
                  blocker_version,ai_gate,ai_gate_version,split_label,provenance_json,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                  entry_price=excluded.entry_price,stop_price=excluded.stop_price,
                  target_price=excluded.target_price,benchmark_entry_price=excluded.benchmark_entry_price,
                  actual_exit_price=excluded.actual_exit_price,
                  provenance_json=excluded.provenance_json""",
                (
                    opportunity.id, opportunity.source_table, opportunity.source_id, opportunity.symbol,
                    _utc(opportunity.observed_at).isoformat(), opportunity.direction, opportunity.execution_type,
                    opportunity.entry_price, opportunity.stop_price, opportunity.target_price,
                    opportunity.benchmark_entry_price, opportunity.actual_exit_price,
                    opportunity.strategy_version, opportunity.score,
                    extra.get("score_version", "trade_decision_score_v1"), opportunity.feature_version,
                    json_dumps(extra.get("feature_snapshot", {})), opportunity.universe_version,
                    json_dumps(extra.get("universe_snapshot", {})), extra.get("regime"),
                    opportunity.regime_version, opportunity.eligibility_version, opportunity.blocker,
                    extra.get("blocker_version", "performance_blockers_v1"), opportunity.ai_gate,
                    extra.get("ai_gate_version", "ai_review_v1"), extra.get("split_label"),
                    json_dumps(extra.get("provenance", {})), now,
                ),
            )

    def save_results(self, opportunity_id: str, results: Sequence[HorizonResult], cost_model: CostModel, input_fingerprint: str, *, calculated_at: datetime) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO research_cost_models(version,parameters_json,source,observed_at,created_at) VALUES(?,?,?,?,?)",
                (cost_model.version, json_dumps(asdict(cost_model)), cost_model.source, cost_model.observed_at, calculated_at.isoformat()),
            )
            placeholders = ",".join("?" for _ in range(34))
            for result in results:
                conn.execute(
                    f"""INSERT INTO research_outcomes(
                      id,opportunity_id,horizon_sessions,status,reason,maturity_session,exit_session,
                      outcome_class,holding_period_sessions,gross_return,spy_return,spy_relative_return,cost_adjusted_return,mfe,mae,
                      gross_r_multiple,cost_adjusted_r_multiple,stop_hit,target_hit,
                      trade_path_gross_return,trade_path_spy_return,trade_path_cost_adjusted_return,
                      fixed_horizon_gross_return,fixed_horizon_spy_return,fixed_horizon_cost_adjusted_return,
                      first_barrier,ordering_quality,cost_model_version,cost_bps,calculation_version,input_fingerprint,
                      calculated_at,error_category,invalidated_at)
                    VALUES({placeholders})
                    ON CONFLICT(opportunity_id,horizon_sessions) DO UPDATE SET
                      status=excluded.status,reason=excluded.reason,maturity_session=excluded.maturity_session,
                      exit_session=excluded.exit_session,outcome_class=excluded.outcome_class,
                      holding_period_sessions=excluded.holding_period_sessions,gross_return=excluded.gross_return,
                      spy_return=excluded.spy_return,spy_relative_return=excluded.spy_relative_return,
                      cost_adjusted_return=excluded.cost_adjusted_return,mfe=excluded.mfe,mae=excluded.mae,
                      gross_r_multiple=excluded.gross_r_multiple,cost_adjusted_r_multiple=excluded.cost_adjusted_r_multiple,
                      stop_hit=excluded.stop_hit,target_hit=excluded.target_hit,
                      trade_path_gross_return=excluded.trade_path_gross_return,
                      trade_path_spy_return=excluded.trade_path_spy_return,
                      trade_path_cost_adjusted_return=excluded.trade_path_cost_adjusted_return,
                      fixed_horizon_gross_return=excluded.fixed_horizon_gross_return,
                      fixed_horizon_spy_return=excluded.fixed_horizon_spy_return,
                      fixed_horizon_cost_adjusted_return=excluded.fixed_horizon_cost_adjusted_return,
                      first_barrier=excluded.first_barrier,ordering_quality=excluded.ordering_quality,
                      cost_model_version=excluded.cost_model_version,cost_bps=excluded.cost_bps,
                      calculation_version=excluded.calculation_version,input_fingerprint=excluded.input_fingerprint,
                      calculated_at=excluded.calculated_at,error_category=excluded.error_category,
                      invalidated_at=excluded.invalidated_at""",
                    (
                        hashlib.sha256(f"{opportunity_id}|{result.horizon_sessions}".encode()).hexdigest()[:32],
                        opportunity_id, result.horizon_sessions, result.status, result.reason,
                        result.maturity_session, result.exit_session, result.outcome_class, result.holding_period_sessions,
                        result.gross_return, result.spy_return, result.spy_relative_return, result.cost_adjusted_return,
                        result.mfe, result.mae, result.gross_r_multiple, result.cost_adjusted_r_multiple,
                        None if result.stop_hit is None else int(result.stop_hit),
                        None if result.target_hit is None else int(result.target_hit),
                        result.trade_path_gross_return, result.trade_path_spy_return, result.trade_path_cost_adjusted_return,
                        result.fixed_horizon_gross_return, result.fixed_horizon_spy_return, result.fixed_horizon_cost_adjusted_return,
                        result.first_barrier, result.ordering_quality, cost_model.version, result.cost_bps,
                        OUTCOME_ENGINE_VERSION, input_fingerprint, calculated_at.isoformat(),
                        result.reason if result.status == "failed" else None, None,
                    ),
                )


def _safe_json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def import_legacy_opportunities(
    storage: Any,
    repository: ResearchRepository,
    *,
    run_id: str | None = None,
) -> list[Opportunity]:
    """Idempotently normalize every historical Performance Lab/trade row.

    Classification is intentionally descriptive, not ordinal: actual fills,
    proposed-but-unfilled, blocked, shadow, observation-only, and generic
    hypothetical records can never collapse into one result population.
    """
    opportunities: list[Opportunity] = []
    linked_execution_keys: set[tuple[str, str]] = set()
    setups = storage.fetch_all(
        """SELECT ps.*, po.actual_or_shadow, po.entry_time, po.entry_price,
                  GROUP_CONCAT(pb.blocker, '|') AS blockers
           FROM performance_setups ps
           LEFT JOIN performance_outcomes po ON po.setup_id=ps.id
           LEFT JOIN performance_blockers pb ON pb.setup_id=ps.id
           WHERE (? IS NULL OR ps.run_id=?)
           GROUP BY ps.id"""
        , (run_id, run_id)
    )
    for row in setups:
        observed_at = row.get("timestamp") or row.get("created_at")
        if not observed_at:
            continue
        tier = str(row.get("tier") or "unknown")
        if row.get("fill_id") or row.get("fill_price") is not None:
            execution_type = "actual_fill"
        elif int(row.get("proposed") or 0):
            execution_type = "proposal_unfilled"
        elif tier in {"observation", "research_candidate", "raw_universe"}:
            execution_type = "observation_only"
        elif row.get("blockers") or row.get("not_proposed_reason"):
            execution_type = "blocked_hypothetical"
        elif str(row.get("actual_or_shadow") or "") == "shadow":
            execution_type = "shadow_hypothetical"
        else:
            execution_type = "hypothetical"
        signal = _safe_json(row.get("signal_state"))
        score_components = _safe_json(row.get("score_components"))
        entry_price = row.get("fill_price") if execution_type == "actual_fill" else row.get("entry_price") or row.get("current_price")
        source_id = str(row["id"])
        opportunity = Opportunity(
            id=hashlib.sha256(f"performance_setups|{source_id}".encode()).hexdigest()[:32],
            source_table="performance_setups",
            source_id=source_id,
            symbol=str(row["symbol"]),
            observed_at=_utc(observed_at),
            entry_price=None if entry_price is None else float(entry_price),
            direction="short" if str(signal.get("side") or "").lower() == "sell" else "long",
            execution_type=execution_type,
            strategy_version=str(signal.get("strategy_version") or STRATEGY_VERSION),
            score=None if row.get("score") is None else float(row["score"]),
            blocker=row.get("blockers") or row.get("not_proposed_reason"),
            ai_gate=str(signal.get("ai_review_status") or signal.get("gpt_status") or "unknown"),
            stop_price=signal.get("stop_price"),
            target_price=signal.get("target_price") or signal.get("take_profit_price"),
            universe_version=str(signal.get("universe_version") or f"tier:{tier}"),
        )
        repository.upsert_opportunity(
            opportunity,
            feature_snapshot={
                "score_components": score_components,
                "trend_metrics": _safe_json(row.get("trend_metrics")),
                "volatility_metrics": _safe_json(row.get("volatility_metrics")),
                "liquidity_metrics": _safe_json(row.get("liquidity_metrics")),
                "relative_strength_metrics": _safe_json(row.get("relative_strength_metrics")),
            },
            universe_snapshot={"tier": tier, "asset_class": row.get("asset_class")},
            regime=_safe_json(row.get("volatility_metrics")).get("regime") or "unknown",
            provenance={"run_id": row.get("run_id"), "proposal_id": row.get("proposal_id"), "fill_id": row.get("fill_id")},
        )
        opportunities.append(opportunity)
        for key in ("fill_id", "order_id", "proposal_id"):
            if row.get(key):
                linked_execution_keys.add((key, str(row[key])))

    # Full clone backfills normalize the legacy trade-outcome ledger. Runtime
    # cycles import only their newly captured Performance Lab rows; actual fill
    # linkage is synchronized there without rescanning all historical trades.
    trades = storage.fetch_all("SELECT * FROM trade_outcomes") if run_id is None else []
    existing_sources = {(o.source_table, o.source_id) for o in opportunities}
    for row in trades:
        source_id = str(row["id"])
        if ("trade_outcomes", source_id) in existing_sources or not row.get("entry_time"):
            continue
        if any(row.get(key) and (key, str(row[key])) in linked_execution_keys for key in ("fill_id", "order_id", "proposal_id")):
            continue
        actual = str(row.get("actual_or_shadow") or "unknown")
        execution_type = "actual_fill" if actual == "actual" else "shadow_hypothetical" if actual == "shadow" else "hypothetical"
        opportunity = Opportunity(
            id=hashlib.sha256(f"trade_outcomes|{source_id}".encode()).hexdigest()[:32],
            source_table="trade_outcomes",
            source_id=source_id,
            symbol=str(row["symbol"]),
            observed_at=_utc(row["entry_time"]),
            entry_price=None if row.get("entry_price") is None else float(row["entry_price"]),
            direction="long",
            execution_type=execution_type,
            strategy_version=STRATEGY_VERSION,
            score=None if row.get("trade_score") is None else float(row["trade_score"]),
            universe_version="legacy_unknown",
        )
        repository.upsert_opportunity(
            opportunity,
            provenance={"trade_id": row.get("trade_id"), "order_id": row.get("order_id"), "fill_id": row.get("fill_id")},
        )
        opportunities.append(opportunity)
    return opportunities


class BoundedBackfill:
    def __init__(
        self,
        repository: ResearchRepository,
        calculator: CanonicalOutcomeCalculator,
        bar_loader: Callable[[str], pd.DataFrame],
        *,
        as_of: datetime,
    ) -> None:
        self.repository = repository
        self.calculator = calculator
        self.bar_loader = bar_loader
        self.as_of = _utc(as_of)

    def run(self, opportunities: Sequence[Opportunity], *, limit: int = 100, job_key: str = "default") -> dict[str, Any]:
        source_fp = fingerprint({"job_key": job_key, "opportunity_ids": sorted(o.id for o in opportunities), "as_of": self.as_of.isoformat()})
        now = datetime.now(UTC).isoformat()
        with self.repository.connect() as conn:
            existing = conn.execute("SELECT * FROM research_backfill_jobs WHERE source_fingerprint=?", (source_fp,)).fetchone()
            if existing and existing["status"] == "completed":
                return dict(existing)
            job_id = existing["id"] if existing else str(uuid.uuid4())
            conn.execute(
                """INSERT INTO research_backfill_jobs(id,source_fingerprint,status,cursor,started_at,updated_at)
                   VALUES(?,?,'running',NULL,?,?)
                   ON CONFLICT(source_fingerprint) DO UPDATE SET status='running',updated_at=excluded.updated_at,safe_error=NULL""",
                (job_id, source_fp, now, now),
            )
            cursor = existing["cursor"] if existing else None
        ordered = sorted(opportunities, key=lambda o: o.id)
        if cursor:
            ordered = [o for o in ordered if o.id > cursor]
        selected = ordered[: max(1, limit)]
        counts = {"processed": 0, "completed": 0, "maturing": 0, "unavailable": 0, "failed": 0}
        try:
            spy = self.bar_loader("SPY")
            benchmark_error: str | None = None
        except Exception as exc:
            spy = pd.DataFrame()
            benchmark_error = f"benchmark_load_failed:{type(exc).__name__}"
        last_cursor = cursor
        for opportunity in selected:
            asset = pd.DataFrame()
            try:
                asset = self.bar_loader(opportunity.symbol)
                if benchmark_error:
                    raise RuntimeError(benchmark_error)
                results = self.calculator.calculate(opportunity, asset, spy, as_of=self.as_of)
            except Exception as exc:  # each row is interruption-safe and explicit
                results = [
                    HorizonResult(h, "failed", f"calculation_failed:{type(exc).__name__}", self.calculator.calendar.add_sessions(opportunity.observed_at.date(), h).isoformat())
                    for h in (1, 5, 20)
                ]
            self.repository.save_results(
                opportunity.id,
                results,
                self.calculator.cost_model,
                fingerprint({"opportunity": asdict(opportunity), "asset": _frame_fingerprint(asset), "spy": _frame_fingerprint(spy)}),
                calculated_at=self.as_of,
            )
            counts["processed"] += 1
            for result in results:
                counts[result.status] += 1
            last_cursor = opportunity.id
            with self.repository.connect() as conn:
                conn.execute(
                    """UPDATE research_backfill_jobs SET cursor=?,processed=processed+1,
                       completed=completed+?,maturing=maturing+?,unavailable=unavailable+?,failed=failed+?,updated_at=?
                       WHERE source_fingerprint=?""",
                    (
                        last_cursor,
                        sum(r.status == "completed" for r in results),
                        sum(r.status == "maturing" for r in results),
                        sum(r.status == "unavailable" for r in results),
                        sum(r.status == "failed" for r in results),
                        datetime.now(UTC).isoformat(), source_fp,
                    ),
                )
        finished = len(selected) == len(ordered)
        with self.repository.connect() as conn:
            conn.execute(
                "UPDATE research_backfill_jobs SET status=?,completed_at=?,updated_at=? WHERE source_fingerprint=?",
                ("completed" if finished else "partial", datetime.now(UTC).isoformat() if finished else None, datetime.now(UTC).isoformat(), source_fp),
            )
            row = conn.execute("SELECT * FROM research_backfill_jobs WHERE source_fingerprint=?", (source_fp,)).fetchone()
            return dict(row)


def _frame_fingerprint(frame: pd.DataFrame) -> str:
    if frame is None or frame.empty:
        return fingerprint([])
    records = []
    for idx, row in frame.sort_index().iterrows():
        records.append([str(idx), *[None if pd.isna(v) else float(v) if isinstance(v, (int, float)) else str(v) for v in row.tolist()]])
    return fingerprint({"columns": list(frame.columns), "records": records})


def project_canonical_to_legacy(storage: Any) -> None:
    """Compatibility projection only; never calculates a second outcome."""
    performance = storage.fetch_all(
        """SELECT ro.source_id, r.horizon_sessions, r.status, r.reason, r.gross_return,
                  r.mfe, r.mae, r.stop_hit, r.target_hit, r.calculated_at
           FROM research_outcomes r JOIN research_opportunities ro ON ro.id=r.opportunity_id
           WHERE ro.source_table='performance_setups'"""
    )
    for row in performance:
        legacy_status = "complete" if row["status"] == "completed" else row["status"]
        storage.execute(
            """UPDATE performance_forward_returns SET eligible_to_update=?,updated_at=?,forward_return=?,
               max_favorable_excursion=?,max_adverse_excursion=?,hypothetical_stop_hit=?,
               hypothetical_target_hit=?,status=?,reason=? WHERE setup_id=? AND horizon_days=?""",
            (
                int(row["status"] != "maturing"), row["calculated_at"],
                None if row["gross_return"] is None else float(row["gross_return"]) * 100.0,
                None if row["mfe"] is None else float(row["mfe"]) * 100.0,
                None if row["mae"] is None else float(row["mae"]) * 100.0,
                row["stop_hit"], row["target_hit"], legacy_status, row["reason"],
                row["source_id"], row["horizon_sessions"],
            ),
        )
    trades = storage.fetch_all(
        """SELECT ro.source_id, r.horizon_sessions, r.status, r.gross_return,r.mfe,r.mae,
                  r.stop_hit,r.target_hit,r.calculated_at
           FROM research_outcomes r JOIN research_opportunities ro ON ro.id=r.opportunity_id
           WHERE ro.source_table='trade_outcomes'"""
    )
    for row in trades:
        column = {1: "forward_return_1d", 5: "forward_return_5d", 20: "forward_return_20d"}.get(int(row["horizon_sessions"]))
        if not column:
            continue
        storage.execute(
            f"""UPDATE trade_outcomes SET {column}=?,max_favorable_excursion=COALESCE(?,max_favorable_excursion),
                max_adverse_excursion=COALESCE(?,max_adverse_excursion),stop_hit=COALESCE(?,stop_hit),
                target_reached=COALESCE(?,target_reached),outcome_status=?,updated_at=? WHERE id=?""",
            (
                None if row["gross_return"] is None else float(row["gross_return"]) * 100.0,
                None if row["mfe"] is None else float(row["mfe"]) * 100.0,
                None if row["mae"] is None else float(row["mae"]) * 100.0,
                row["stop_hit"], row["target_hit"],
                "complete" if row["status"] == "completed" and int(row["horizon_sessions"]) == 20 else row["status"],
                row["calculated_at"], row["source_id"],
            ),
        )


def update_service_outcomes(
    storage: Any,
    broker: Any,
    *,
    now: datetime,
    max_updates: int = 25,
    run_id: str | None = None,
    bar_cache: Mapping[str, pd.DataFrame] | None = None,
) -> dict[str, Any]:
    """Single bounded runtime entrypoint for legacy and Phase 2 shadow outcomes."""
    repository = ResearchRepository(storage.path)
    repository.migrate()
    import_legacy_opportunities(storage, repository, run_id=run_id)
    with repository.connect() as conn:
        opportunities = [
            Opportunity(
                id=row["id"], source_table=row["source_table"], source_id=row["source_id"],
                symbol=row["symbol"], observed_at=_utc(row["observed_at"]),
                entry_price=row["entry_price"], direction=row["direction"],
                execution_type=row["execution_type"], strategy_version=row["strategy_version"],
                score=row["score"], blocker=row["blocker"], ai_gate=row["ai_gate"],
                stop_price=row["stop_price"], target_price=row["target_price"],
                benchmark_entry_price=row["benchmark_entry_price"], actual_exit_price=row["actual_exit_price"],
                feature_version=row["feature_version"], universe_version=row["universe_version"],
                regime_version=row["regime_version"], eligibility_version=row["eligibility_version"],
            )
            for row in conn.execute("SELECT * FROM research_opportunities ORDER BY observed_at,id")
        ]
    with repository.connect() as conn:
        states: dict[str, list[tuple[str, str]]] = {}
        for row in conn.execute("SELECT opportunity_id,status,maturity_session FROM research_outcomes"):
            states.setdefault(row["opportunity_id"], []).append((row["status"], row["maturity_session"]))
    selected = [
        opportunity
        for opportunity in opportunities
        if opportunity.id not in states
        or any(
            (status == "maturing" and maturity <= now.date().isoformat())
            or status == "failed"
            for status, maturity in states[opportunity.id]
        )
    ]
    if bar_cache is not None:
        available = {str(symbol).upper() for symbol, frame in bar_cache.items() if frame is not None and not frame.empty}
        selected = [opportunity for opportunity in selected if opportunity.symbol.upper() in available]
    selected.sort(key=lambda opportunity: (opportunity.observed_at, opportunity.id))
    def load(symbol: str) -> pd.DataFrame:
        if bar_cache is not None:
            return bar_cache.get(symbol, pd.DataFrame())
        if broker is None:
            return pd.DataFrame()
        from .market_data import normalize_bars

        return normalize_bars(broker.get_historical_bars(symbol, "1Day", 250), symbol)

    model = CostModel("paper_cost_assumption_v1", 4.0, 2.0, 2.0, source="documented conservative paper assumption; replace with observed quote/fill calibration")
    if bar_cache is not None and ("SPY" not in bar_cache or bar_cache["SPY"] is None or bar_cache["SPY"].empty):
        project_canonical_to_legacy(storage)
        return {"status": "deferred", "reason": "cached_spy_bars_unavailable", "selected": len(selected), "provider_calls": 0}
    if not selected:
        project_canonical_to_legacy(storage)
        return {"status": "completed", "processed": 0, "provider_calls": 0}
    runner = BoundedBackfill(repository, CanonicalOutcomeCalculator(ExchangeSessions(), model), load, as_of=now)
    result = runner.run(
        selected,
        limit=max_updates if max_updates > 0 else max(len(selected), 1),
        job_key=f"runtime:{now.date().isoformat()}:{fingerprint([o.id for o in selected[:max_updates or None]])}",
    )
    project_canonical_to_legacy(storage)
    result["provider_calls"] = 0 if bar_cache is not None else None
    return result


def render_evidence_report(
    rows: Sequence[Mapping[str, Any]],
    *,
    as_of: datetime,
    cost_model: CostModel,
    limitations: Sequence[str],
    minimum_oos_n: int = 100,
) -> str:
    completed = [r for r in rows if r.get("status") == "completed"]
    oos = [r for r in completed if r.get("split_label") == "out_of_sample"]
    metrics = evidence_metrics(oos)
    interval = bootstrap_mean_interval([float(r["cost_adjusted_return"]) for r in oos if r.get("cost_adjusted_return") is not None])
    supported = bool(metrics["n"] >= minimum_oos_n and metrics["mean"] is not None and metrics["mean"] > 0 and interval and interval[0] > 0)
    verdict = "supported" if supported else "inconclusive"
    status_counts: dict[str, int] = {}
    reason_counts: dict[tuple[str, str], int] = {}
    for row in rows:
        status = str(row.get("status") or "unknown")
        reason = str(row.get("reason") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
        reason_counts[(status, reason)] = reason_counts.get((status, reason), 0) + 1
    grouped = grouped_evidence(oos, ["strategy_version", "regime", "execution_type"])
    unique_opportunities: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        unique_opportunities.setdefault(str(row.get("opportunity_id") or id(row)), row)
    execution_counts: dict[str, int] = {}
    for row in unique_opportunities.values():
        key = str(row.get("execution_type") or "unknown")
        execution_counts[key] = execution_counts.get(key, 0) + 1
    lines = [
        "# Phase 1 Evidence Report",
        "",
        f"Point-in-time as of: `{_utc(as_of).isoformat()}`  ",
        f"Outcome engine: `{OUTCOME_ENGINE_VERSION}`  ",
        f"Cost model: `{cost_model.version}` ({cost_model.round_trip_bps:.2f} bps round trip; source: {cost_model.source})",
        "",
        "## Evidence verdict",
        "",
        f"Strategy support after costs: **{verdict}**. This report requires at least {minimum_oos_n} OOS observations and a positive 95% bootstrap lower bound.",
        f"OOS n={metrics['n']}; mean={metrics['mean']}; 95% interval={interval}.",
        "Score-based sizing, AI gating, Phase 2 activation, and Phase 3 risk expansion remain unsupported unless separately proven by positive OOS incremental evidence.",
        "",
        "## Coverage",
        "",
        "| status | n |",
        "|---|---:|",
    ]
    lines.extend(f"| {key} | {value} |" for key, value in sorted(status_counts.items()))
    lines += ["", "| status | reason | n |", "|---|---|---:|"]
    lines.extend(f"| {status} | {reason} | {count} |" for (status, reason), count in sorted(reason_counts.items()))
    lines += ["", "## Opportunity types", "", "| execution classification | n |", "|---|---:|"]
    lines.extend(f"| {key} | {value} |" for key, value in sorted(execution_counts.items()))
    lines += ["", "## OOS grouped results", "", "| strategy | regime | execution | n | mean net return | win rate |", "|---|---|---|---:|---:|---:|"]
    for row in grouped:
        lines.append(f"| {row.get('strategy_version')} | {row.get('regime')} | {row.get('execution_type')} | {row['n']} | {row['mean']} | {row['win_rate']} |")
    if not grouped:
        lines.append("| unavailable | unavailable | unavailable | 0 | unavailable | unavailable |")
    lines += ["", "## Score calibration", "", "| score band | n | predicted | observed wins | Brier |", "|---|---:|---:|---:|---:|"]
    for row in score_calibration(oos):
        lines.append(f"| {row['score_band']} | {row['n']} | {row['predicted_probability']} | {row['observed_win_rate']} | {row['brier_score']} |")
    lines += [
        "",
        "## Blocker and AI-gate evidence",
        "",
        "Incremental blocker and AI-gate value is **inconclusive** because no completed OOS labels are available. Unknown gate values remain unknown; they are not treated as passes, failures, or zero returns.",
        "",
        "## Walk-forward, sensitivity, and overfitting",
        "",
        "The implementation supports deterministic expanding-window simulation, purged/embargoed walk-forward folds, cost sensitivity, ablation group comparisons, bootstrap uncertainty, score calibration/Brier analysis, and probabilistic Sharpe. With zero completed OOS observations, numerical walk-forward and sensitivity conclusions are unavailable. Deflated Sharpe and PBO are deliberately unavailable without multiple independently tested configurations; no synthetic value is emitted.",
        "",
        "## Assumptions and data quality",
        "",
        f"- Round-trip costs are {cost_model.round_trip_bps:.2f} bps from `{cost_model.version}`; provenance is `{cost_model.source}`.",
        "- SPY is the benchmark. Benchmark-relative values stay null when aligned SPY sessions are missing.",
        "- All returns are decimal returns in the canonical ledger; legacy report projections use percentage points.",
        "- An unavailable row is terminal for the current immutable input fingerprint. A later data bundle creates a new reproducible calculation fingerprint.",
        "- Maturing rows are not failures; their exchange-session horizon had not elapsed at the report timestamp.",
    ]
    lines += ["", "## Limitations", ""]
    lines.extend(f"- {item}" for item in limitations)
    lines += [
        "",
        "## Integrity controls",
        "",
        "Outcomes use exchange-session horizons, immutable version labels, point-in-time prefixes, explicit unavailable values, conservative daily-bar barrier ordering, traceable costs, and purged/embargoed walk-forward splits. Delisted/corporate-action evidence is unavailable unless supplied by the input bundle; it is never inferred from current membership.",
        "",
    ]
    return "\n".join(lines)
