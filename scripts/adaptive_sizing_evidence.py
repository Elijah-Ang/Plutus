#!/usr/bin/env python3
"""Read-only evidence report for operational-paper Phase 4.2B sizing."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from app.adaptive_sizing import evidence_report, trading_state_counts  # noqa: E402


def build_report(database: str | Path) -> dict[str, Any]:
    path = Path(database).expanduser().resolve()
    uri = f"file:{path}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        before = trading_state_counts(conn)
        report = evidence_report(conn)
        after = trading_state_counts(conn)
    report["trading_state_counts_before"] = before
    report["trading_state_counts_after"] = after
    report["trading_state_mutations"] = sum(
        abs(after[name] - before[name]) for name in before
    )
    return report


def format_report(report: dict[str, Any]) -> str:
    return (
        "Phase 4.2B Adaptive Sizing Evidence (operational paper; read-only report)\n"
        f"Decisions: {report['total_decisions']}; complete by stage: {report['complete_counts']}\n"
        f"Modes: {report['deployment_modes']}; opportunity classes: {report['opportunity_classes']}\n"
        f"Comparisons: {report['comparison_directions']}; binding caps: {report['binding_cap_frequency']}\n"
        f"Absolute size difference: median ${report['median_absolute_size_difference']:,.2f}; maximum ${report['maximum_absolute_size_difference']:,.2f}\n"
        f"Proposal-to-approval drift: {report['proposal_to_approval_drift']}\n"
        f"Missing inputs: {report['missing_input_frequency']}; contradictions: {report['contradictory_classifications']}\n"
        f"Hypothetical exposure: {report['hypothetical_exposure']}\n"
        f"Recommendations exceeding a hard limit: {report['recommendations_exceeding_hard_limit']}\n"
        f"Trading-state mutations: {report['trading_state_mutations']} (read-only SQLite connection)"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", help="SQLite database to inspect read-only")
    parser.add_argument(
        "--json", action="store_true", help="emit machine-readable JSON"
    )
    args = parser.parse_args()
    try:
        report = build_report(args.database)
    except (OSError, sqlite3.Error, ValueError):
        print(
            "Adaptive sizing evidence report unavailable: database or required schema could not be read safely."
        )
        return 2
    print(
        json.dumps(report, indent=2, sort_keys=True)
        if args.json
        else format_report(report)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
