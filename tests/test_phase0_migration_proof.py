from __future__ import annotations

import sqlite3

import pytest

from app.execution import DurableExecutionStore
from app.storage import Storage
from scripts.phase0_migration_proof import run_proof


def _synthetic_pre_phase0(path):
    storage = Storage(path)
    storage.initialize()
    phase0_tables = (
        "order_events",
        "risk_reservations",
        "broker_fill_events",
        "reconciliation_attempts",
        "telegram_updates",
        "approval_workflows",
        "position_lifecycles",
        "health_heartbeats",
        "risk_snapshots_v2",
        "order_intents",
    )
    with storage.connect() as conn:
        for table in phase0_tables:
            conn.execute(f'DROP TABLE IF EXISTS "{table}"')
        conn.execute("DELETE FROM schema_migrations WHERE version='phase0_execution_integrity_v1'")
        conn.execute(
            "INSERT INTO trade_proposals(id,symbol,side,status,created_at) VALUES('historical','SPY','buy','expired','2020-01-01T00:00:00+00:00')"
        )


def test_online_backup_migration_repeat_and_restoration_proof(tmp_path):
    source = tmp_path / "source.sqlite3"
    _synthetic_pre_phase0(source)
    before_stat = source.stat()
    result = run_proof(source, tmp_path / "proof")
    after_stat = source.stat()
    assert result["source_open_mode"] == "read_only"
    assert result["clone_method"] == "sqlite_backup_api"
    assert result["source"]["integrity"] == result["migrated"]["integrity"] == "ok"
    assert result["migration_repeat_identical"] is True
    assert result["restoration_schema_exact"] is True
    assert result["restoration_counts_exact"] is True
    assert result["source_unchanged_during_proof"] is True
    assert (before_stat.st_size, before_stat.st_mtime_ns) == (after_stat.st_size, after_stat.st_mtime_ns)
    assert all(value == 0 for value in result["phase0_integrity"].values())


def test_interrupted_schema_transaction_rolls_back_every_step(tmp_path):
    path = tmp_path / "interrupted.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE historical(id TEXT PRIMARY KEY)")
    with pytest.raises(RuntimeError, match="injected migration interruption"):
        with sqlite3.connect(path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("CREATE TABLE phase0_step_one(id TEXT PRIMARY KEY)")
            conn.execute("CREATE TABLE phase0_step_two(id TEXT PRIMARY KEY)")
            raise RuntimeError("injected migration interruption")
    with sqlite3.connect(path) as conn:
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert names == {"historical"}


def test_additive_migration_preserves_old_queries_and_enables_new_code(tmp_path):
    path = tmp_path / "compat.sqlite3"
    _synthetic_pre_phase0(path)
    Storage(path).initialize()
    storage = Storage(path)
    assert storage.fetch_all("SELECT id,symbol,side,status FROM trade_proposals WHERE id='historical'") == [
        {"id": "historical", "symbol": "SPY", "side": "buy", "status": "expired"}
    ]
    assert DurableExecutionStore(storage).recovery_sweep().approvals_without_intents == 0


def test_new_execution_store_fails_clearly_before_phase0_migration(tmp_path):
    path = tmp_path / "unmigrated.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE legacy(id TEXT PRIMARY KEY)")
    with pytest.raises(RuntimeError, match="execution schema is unavailable"):
        DurableExecutionStore(Storage(path)).recovery_sweep()


def test_wal_reader_remains_available_during_migration_writer(tmp_path):
    path = tmp_path / "wal.sqlite3"
    Storage(path).initialize()
    reader = sqlite3.connect(path, timeout=0.1)
    reader.execute("PRAGMA journal_mode=WAL")
    reader.execute("BEGIN")
    assert reader.execute("SELECT COUNT(*) FROM trade_proposals").fetchone()[0] == 0
    writer = sqlite3.connect(path, timeout=0.1)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("CREATE TABLE migration_probe(id INTEGER)")
    writer.commit()
    # Existing reader snapshot remains coherent; a fresh read sees the new schema.
    assert reader.execute("SELECT COUNT(*) FROM trade_proposals").fetchone()[0] == 0
    reader.rollback()
    assert reader.execute("SELECT COUNT(*) FROM migration_probe").fetchone()[0] == 0
    reader.close()
    writer.close()
