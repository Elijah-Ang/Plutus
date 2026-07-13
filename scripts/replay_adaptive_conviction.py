#!/usr/bin/env python3
"""Read-only offline replay for report-only Adaptive Conviction."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any

from app.adaptive_conviction import AdaptiveConvictionEngine
from app.utils import load_config


TRADING_TABLES = ("trade_proposals", "approvals", "risk_reservations", "order_intents", "orders", "fills")


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
    present = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return {table: int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]) for table in TRADING_TABLES if table in present}


def _payload(row: sqlite3.Row) -> dict[str, Any]:
    try:
        return json.loads(row["payload"] or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}


def _proposal_records(conn: sqlite3.Connection, limit: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT id,run_id,signal_id,strategy_version,side,notional,status,payload,
                  performance_snapshot_id,policy_decision_id,strategy_quality_score,strategy_state,permitted_stop_risk_pct
           FROM trade_proposals WHERE lower(side)='buy' ORDER BY created_at DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    records = []
    for row in rows:
        payload = _payload(row)
        action = str(payload.get("action") or "entry").lower()
        if action != "entry":
            continue
        equity = payload.get("portfolio_equity") or (payload.get("indicators") or {}).get("portfolio_equity")
        proposal_pct = (float(row["notional"] or 0.0) / float(equity) * 100.0) if equity else None
        proposed_gross = payload.get("proposed_total_exposure_pct")
        proposed_symbol = payload.get("proposed_symbol_exposure_pct")
        proposed_cluster = payload.get("proposed_cluster_exposure_pct")
        records.append({
            "run_id": row["run_id"], "proposal_id": row["id"], "candidate_id": row["signal_id"],
            "setup_id": payload.get("setup_key"), "strategy_version": row["strategy_version"],
            "policy_decision_id": row["policy_decision_id"], "performance_snapshot_id": row["performance_snapshot_id"],
            "action": "entry", "side": "buy", "strategy_authorized": row["strategy_state"] in {"PROBE", "EXPLORATION", "THROTTLED", "ACTIVE"},
            "strategy_policy_state": row["strategy_state"],
            "evidence_quality": (float(row["strategy_quality_score"]) / 100.0) if row["strategy_quality_score"] is not None else None,
            "evidence_calibrated": row["strategy_state"] in {"EXPLORATION", "THROTTLED", "ACTIVE"} and row["performance_snapshot_id"] is not None,
            "market_regime": payload.get("volatility_regime"), "account_drawdown_pct": payload.get("phase3_account_drawdown_pct"),
            "daily_realized_loss_pct": payload.get("daily_loss_pct"), "weekly_realized_loss_pct": payload.get("weekly_loss_pct"),
            "execution_integrity_ok": payload.get("execution_integrity_ok"), "reconciliation_ok": payload.get("reconciliation_ok"),
            "current_portfolio_heat_pct": payload.get("current_portfolio_heat_pct"),
            "current_gross_exposure_pct": max(0.0, float(proposed_gross) - proposal_pct) if proposed_gross is not None and proposal_pct is not None else None,
            "symbol_exposure_pct": max(0.0, float(proposed_symbol) - proposal_pct) if proposed_symbol is not None and proposal_pct is not None else None,
            "cluster_exposure_pct": max(0.0, float(proposed_cluster) - proposal_pct) if proposed_cluster is not None and proposal_pct is not None else None,
            "correlation_score": payload.get("correlation_score"), "setup_score": payload.get("score"),
            "stop_valid": payload.get("stop_validation_status") == "validated", "stop_distance_pct": payload.get("stop_distance_pct"),
            "reward_to_risk": payload.get("reward_to_risk"), "average_dollar_volume": payload.get("average_dollar_volume"),
            "quote_spread_bps": payload.get("quote_spread_bps"),
            "market_data_fresh": payload.get("proposal_price_age_seconds_at_send") is not None and float(payload["proposal_price_age_seconds_at_send"]) <= 60.0,
            "risk_checks_passed": str(row["status"] or "").lower() not in {"blocked", "rejected"},
            "deterioration_detected": None,
            "operational_stop_risk_pct": row["permitted_stop_risk_pct"],
            "replay_source": "trade_proposal",
        })
    return records


def _research_records(conn: sqlite3.Connection, limit: int) -> list[dict[str, Any]]:
    present = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "research_opportunities" not in present:
        return []
    rows = conn.execute(
        """SELECT r.*,
                  p.id AS replay_policy_id,p.state AS replay_policy_state,p.performance_snapshot_id AS replay_snapshot_id,
                  s.quality_score AS replay_quality_score
           FROM research_opportunities r
           LEFT JOIN strategy_policy_decisions p ON p.id=(
             SELECT p2.id FROM strategy_policy_decisions p2
             WHERE p2.strategy_version=r.strategy_version AND p2.decided_at<=r.created_at
             ORDER BY p2.decided_at DESC,p2.id DESC LIMIT 1)
           LEFT JOIN strategy_performance_snapshots s ON s.id=p.performance_snapshot_id
           WHERE lower(r.direction) IN ('buy','long')
           ORDER BY r.observed_at DESC,r.id DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    records = []
    for row in rows:
        try:
            features = json.loads(row["feature_snapshot_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            features = {}
        entry = row["entry_price"]
        stop = row["stop_price"]
        target = row["target_price"]
        stop_valid = entry is not None and stop is not None and float(entry) > float(stop) > 0
        stop_distance = ((float(entry) - float(stop)) / float(entry) * 100.0) if stop_valid else None
        reward = ((float(target) - float(entry)) / (float(entry) - float(stop))) if stop_valid and target is not None else None
        state = row["replay_policy_state"]
        records.append({
            "run_id": None, "proposal_id": None, "candidate_id": row["id"], "setup_id": row["source_id"],
            "strategy_version": row["strategy_version"], "policy_decision_id": row["replay_policy_id"],
            "performance_snapshot_id": row["replay_snapshot_id"], "action": "entry", "side": "buy",
            "strategy_authorized": state in {"PROBE", "EXPLORATION", "THROTTLED", "ACTIVE"},
            "strategy_policy_state": state, "evidence_quality": (float(row["replay_quality_score"]) / 100.0) if row["replay_quality_score"] is not None else None,
            "evidence_calibrated": state in {"EXPLORATION", "THROTTLED", "ACTIVE"} and row["replay_snapshot_id"] is not None,
            "market_regime": row["regime"], "setup_score": row["score"], "stop_valid": stop_valid,
            "stop_distance_pct": stop_distance, "reward_to_risk": reward,
            "average_dollar_volume": features.get("average_dollar_volume") or features.get("adv20"),
            "quote_spread_bps": features.get("quote_spread_bps"),
            "market_data_fresh": entry is not None, "risk_checks_passed": row["blocker"] is None and stop_valid,
            "deterioration_detected": None, "operational_stop_risk_pct": 0.03 if state == "PROBE" else None,
            "replay_source": "research_opportunity", "source_execution_type": row["execution_type"],
        })
    return records


def replay_database(database: str | Path, *, limit: int = 1000) -> dict[str, Any]:
    uri = f"file:{Path(database).resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        before = _counts(conn)
        proposal_records = _proposal_records(conn, limit)
        research_records = _research_records(conn, limit)
        records = [*proposal_records, *research_records]
        result = AdaptiveConvictionEngine(load_config()).replay(records)
        after = _counts(conn)
        if before != after:
            raise RuntimeError("read-only replay observed a trading-state count change")
        result["source_records"] = {"production_paper_proposals": len(proposal_records), "research_records": len(research_records)}
        result["trading_state_counts_before"] = before
        result["trading_state_counts_after"] = after
        return result
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--output")
    args = parser.parse_args()
    result = replay_database(args.database, limit=args.limit)
    rendered = json.dumps(result, indent=2, sort_keys=True)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
