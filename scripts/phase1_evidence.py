#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.research_validation import (
    BoundedBackfill,
    CanonicalOutcomeCalculator,
    CostModel,
    ExchangeSessions,
    ResearchRepository,
    evidence_metrics,
    fingerprint,
    import_legacy_opportunities,
    render_evidence_report,
    sensitivity,
    walk_forward_folds,
)
from app.runtime_guard import is_production_path
from app.storage import Storage


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Offline, clone-only Phase 1 evidence pipeline")
    parser.add_argument("--db", required=True, type=Path, help="verified database clone; production paths are refused")
    parser.add_argument("--bars", required=True, type=Path, help="directory of point-in-time SYMBOL.csv files")
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--as-of", required=True, help="reproducible ISO timestamp")
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--spread-bps", type=float, default=4.0)
    parser.add_argument("--entry-slippage-bps", type=float, default=2.0)
    parser.add_argument("--exit-slippage-bps", type=float, default=2.0)
    parser.add_argument("--minimum-oos-n", type=int, default=100)
    return parser.parse_args()


def load_csv(directory: Path, symbol: str) -> pd.DataFrame:
    path = directory / f"{symbol.replace('/', '_')}.csv"
    if not path.exists():
        return pd.DataFrame()
    frame = pd.read_csv(path)
    timestamp_column = next((c for c in ("timestamp", "date", "time") if c in frame.columns), None)
    if timestamp_column is None:
        raise ValueError(f"{path} has no timestamp/date/time column")
    frame[timestamp_column] = pd.to_datetime(frame[timestamp_column], utc=True)
    frame = frame.set_index(timestamp_column).sort_index()
    required = {"open", "high", "low", "close"}
    if not required.issubset(frame.columns):
        raise ValueError(f"{path} missing OHLC columns: {sorted(required - set(frame.columns))}")
    return frame


def main() -> int:
    args = parse_args()
    db = args.db.expanduser().resolve()
    if is_production_path(db):
        raise SystemExit("refusing production database path; create and verify a clone first")
    if not db.exists():
        raise SystemExit(f"database clone does not exist: {db}")
    as_of = datetime.fromisoformat(args.as_of.replace("Z", "+00:00"))
    as_of = as_of.replace(tzinfo=UTC) if as_of.tzinfo is None else as_of.astimezone(UTC)
    repository = ResearchRepository(db)
    repository.migrate()
    storage = Storage(db)
    opportunities = import_legacy_opportunities(storage, repository)
    model = CostModel(
        version="cli_cost_assumption_v1",
        spread_bps=args.spread_bps,
        entry_slippage_bps=args.entry_slippage_bps,
        exit_slippage_bps=args.exit_slippage_bps,
        source="CLI parameters; operator must replace assumptions with observed quote/fill evidence",
        observed_at=as_of.isoformat(),
    )
    runner = BoundedBackfill(
        repository,
        CanonicalOutcomeCalculator(ExchangeSessions(), model),
        lambda symbol: load_csv(args.bars, symbol),
        as_of=as_of,
    )
    job = runner.run(opportunities, limit=args.limit, job_key=f"cli:{args.bars.resolve()}")
    with repository.connect() as conn:
        rows = [dict(row) for row in conn.execute(
            """SELECT r.*,o.strategy_version,o.score,o.execution_type,o.regime,o.blocker,o.ai_gate,o.split_label
               FROM research_outcomes r JOIN research_opportunities o ON o.id=r.opportunity_id
               ORDER BY o.observed_at,o.id,r.horizon_sessions"""
        )]
    limitations = [
        "No delisting or corporate-action adjustment is claimed unless encoded in the supplied point-in-time bar bundle.",
        "Rows without an immutable historical universe snapshot retain an unknown or legacy universe version and are not strong survivorship-bias evidence.",
        "Daily OHLC cannot order a stop and target touched in the same bar; the calculator applies conservative stop-first ordering and labels it ambiguous.",
        "Deflated Sharpe and probability of backtest overfitting are unavailable when the evidence set does not contain multiple independently tested configurations.",
        "The default cost model is an explicit assumption, not observed fill calibration.",
    ]
    report = render_evidence_report(rows, as_of=as_of, cost_model=model, limitations=limitations, minimum_oos_n=args.minimum_oos_n)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(report, encoding="utf-8")
    config = {
        "as_of": as_of.isoformat(), "limit": args.limit, "cost_model": model.__dict__,
        "minimum_oos_n": args.minimum_oos_n, "bars": str(args.bars.resolve()),
    }
    result = {
        "job": job,
        "all": evidence_metrics([r for r in rows if r["status"] == "completed"]),
        "oos": evidence_metrics([r for r in rows if r["status"] == "completed" and r.get("split_label") == "out_of_sample"]),
        "cost_sensitivity": sensitivity(rows, model.round_trip_bps),
    }
    validation_fp = fingerprint({"config": config, "opportunity_ids": sorted(o.id for o in opportunities), "rows": rows})
    with repository.connect() as conn:
        conn.execute(
            """INSERT INTO research_validation_runs(
               id,as_of,status,config_json,input_fingerprint,assumptions_json,limitations_json,
               result_json,report_path,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(input_fingerprint) DO UPDATE SET result_json=excluded.result_json,
               report_path=excluded.report_path""",
            (
                validation_fp[:32], as_of.isoformat(), "complete" if job["status"] == "completed" else "partial",
                json.dumps(config, sort_keys=True), validation_fp,
                json.dumps({"cost_model": model.__dict__}, sort_keys=True), json.dumps(limitations),
                json.dumps(result, sort_keys=True, default=str), str(args.report.resolve()), datetime.now(UTC).isoformat(),
            ),
        )
    print(json.dumps({"report": str(args.report.resolve()), "validation_fingerprint": validation_fp, **result}, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
