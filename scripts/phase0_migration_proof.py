#!/usr/bin/env python3
"""Read-only-source SQLite backup, migration, and restoration proof.

The source is opened with ``mode=ro`` and copied using SQLite's online backup
API. Output is metadata only: no application row values are selected or shown.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.execution import DurableExecutionStore  # noqa: E402
from app.storage import Storage  # noqa: E402


def _utc() -> str:
    return datetime.now(UTC).isoformat()


def _connect_readonly(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _backup(source: Path, destination: Path) -> float:
    started = time.monotonic()
    with _connect_readonly(source) as src, sqlite3.connect(destination) as dst:
        src.backup(dst, pages=2048, sleep=0.01)
    return time.monotonic() - started


def _schema_metadata(path: Path) -> dict[str, object]:
    with _connect_readonly(path) as conn:
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        objects = conn.execute(
            "SELECT type,name,COALESCE(sql,'') sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
        ).fetchall()
        table_names = [str(row["name"]) for row in objects if row["type"] == "table"]
        # Counts are safe shape evidence. Application values are never selected.
        counts = {
            table: int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            for table in table_names
        }
        schema_blob = "\n".join(f"{row['type']}|{row['name']}|{row['sql']}" for row in objects)
        wal_mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0])
        migration_versions = []
        if "schema_migrations" in table_names:
            migration_versions = [str(row[0]) for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]
    return {
        "integrity": integrity,
        "schema_sha256": hashlib.sha256(schema_blob.encode()).hexdigest(),
        "tables": len(table_names),
        "indexes": sum(row["type"] == "index" for row in objects),
        "triggers": sum(row["type"] == "trigger" for row in objects),
        "row_counts": counts,
        "journal_mode": wal_mode,
        "migration_versions": migration_versions,
    }


def run_proof(source: Path, workdir: Path) -> dict[str, object]:
    source = source.resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    source_stat_before = source.stat()
    workdir.mkdir(parents=True, exist_ok=True)
    free_before = shutil.disk_usage(workdir).free
    pre = workdir / "pre_migration.sqlite3"
    migrated = workdir / "migrated.sqlite3"
    restored = workdir / "restored.sqlite3"
    backup_seconds = _backup(source, pre)
    shutil.copyfile(pre, migrated)
    source_meta = _schema_metadata(pre)
    migration_started = _utc()
    start = time.monotonic()
    Storage(migrated).initialize()
    migration_seconds = time.monotonic() - start
    migration_completed = _utc()
    migrated_meta = _schema_metadata(migrated)
    # Repeated startup must be idempotent and cheap relative to the first pass.
    repeat_start = time.monotonic()
    Storage(migrated).initialize()
    repeat_seconds = time.monotonic() - repeat_start
    repeated_meta = _schema_metadata(migrated)
    # Restoration uses SQLite backup too, then verifies schema and every table count.
    restore_seconds = _backup(pre, restored)
    restored_meta = _schema_metadata(restored)
    integrity = DurableExecutionStore(Storage(migrated)).integrity_report()
    return {
        "source_open_mode": "read_only",
        "clone_method": "sqlite_backup_api",
        "source_bytes": source.stat().st_size,
        "clone_bytes_before_migration": pre.stat().st_size,
        "clone_bytes_after_migration": migrated.stat().st_size,
        "disk_growth_bytes": migrated.stat().st_size - pre.stat().st_size,
        "free_disk_before_bytes": free_before,
        "backup_seconds": round(backup_seconds, 6),
        "migration_started_at": migration_started,
        "migration_completed_at": migration_completed,
        "migration_seconds": round(migration_seconds, 6),
        "repeat_startup_seconds": round(repeat_seconds, 6),
        "restore_seconds": round(restore_seconds, 6),
        "source": {k: v for k, v in source_meta.items() if k != "row_counts"},
        "migrated": {k: v for k, v in migrated_meta.items() if k != "row_counts"},
        "migration_repeat_identical": migrated_meta == repeated_meta,
        "restoration_schema_exact": source_meta["schema_sha256"] == restored_meta["schema_sha256"],
        "restoration_counts_exact": source_meta["row_counts"] == restored_meta["row_counts"],
        "phase0_integrity": integrity,
        "source_unchanged_during_proof": (
            source_stat_before.st_size == source.stat().st_size
            and source_stat_before.st_mtime_ns == source.stat().st_mtime_ns
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--workdir", type=Path)
    args = parser.parse_args()
    if args.workdir:
        report = run_proof(args.source, args.workdir)
    else:
        with tempfile.TemporaryDirectory(prefix="phase0-migration-proof-") as temporary:
            report = run_proof(args.source, Path(temporary))
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
