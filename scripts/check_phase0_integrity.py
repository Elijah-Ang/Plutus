#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.execution import DurableExecutionStore
from app.storage import Storage


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only Phase 0 integrity report")
    parser.add_argument("--db", type=Path, required=True, help="Explicit SQLite database path; no default is provided")
    args = parser.parse_args()
    if not args.db.exists():
        parser.error("database path does not exist")
    report = DurableExecutionStore(Storage(args.db)).integrity_report()
    print(json.dumps(report, indent=2, sort_keys=True))
    return 1 if any(report.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
