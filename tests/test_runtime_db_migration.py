from __future__ import annotations

import json
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import scripts.migrate_runtime_db as migration


def _database(path: Path) -> Path:
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE schema_migrations("
            "version TEXT PRIMARY KEY, applied_at TEXT, detail TEXT)"
        )
        conn.execute("CREATE TABLE sample(id INTEGER PRIMARY KEY, value TEXT)")
        conn.executemany(
            "INSERT INTO sample(value) VALUES(?)",
            [("alpha",), ("beta",), ("gamma",)],
        )
    return path


def _valid_manifest() -> dict[str, object]:
    commit = "a" * 40
    tree = "b" * 40
    source_digest = "c" * 64
    return {
        "release_id": commit[:12],
        "release_commit": commit,
        "mode": "paper",
        "manual_approval_only": True,
        "live_capability": False,
        "tests_verified": True,
        "python_version": migration.REQUIRED_PYTHON,
        "schema_version": migration.REQUIRED_SCHEMA_VERSION,
        "required_schema_versions": sorted(migration.REQUIRED_SCHEMA_VERSIONS),
        "git_tree_sha": tree,
        "tracked_source_inventory_digest": source_digest,
        "release_authority": {
            "mode": "forward",
            "source_tree_sha": tree,
            "tracked_source_inventory_digest": source_digest,
        },
        "ci": {
            "workflow_name": "CI",
            "run_id": 12345,
            "head_sha": commit,
        },
    }


def _evidence(*, page_count: int = 4, file_sha256: str = "digest") -> dict:
    return {
        "bytes": 16384,
        "file_sha256": file_sha256,
        "quick_check": ["ok"],
        "integrity_check": ["ok"],
        "foreign_key_violations": 0,
        "tables": 2,
        "row_counts": {"sample": 3, "schema_migrations": 0},
        "schema_sha256": "schema",
        "versions": [],
        "page_count": page_count,
        "page_size": 4096,
        "logical_bytes": page_count * 4096,
        "freelist_count": 0,
    }


def test_metadata_records_restoration_evidence(tmp_path):
    database = _database(tmp_path / "source.sqlite3")

    evidence = migration.metadata(database)

    assert evidence["quick_check"] == ["ok"]
    assert evidence["integrity_check"] == ["ok"]
    assert evidence["foreign_key_violations"] == 0
    assert evidence["row_counts"]["sample"] == 3
    assert evidence["tables"] == 2
    assert evidence["page_count"] > 0
    assert evidence["page_size"] > 0
    assert evidence["logical_bytes"] == (
        evidence["page_count"] * evidence["page_size"]
    )
    assert len(evidence["schema_sha256"]) == 64
    assert len(evidence["file_sha256"]) == 64


def test_consistent_backups_are_exclusive_and_preserve_source(tmp_path):
    database = _database(tmp_path / "source.sqlite3")
    backups = tmp_path / "backups"
    source_evidence = migration.metadata(database)

    first, first_evidence = migration.create_consistent_backup(
        database,
        backups,
        source_evidence=source_evidence,
    )
    first_bytes = first.read_bytes()
    second, second_evidence = migration.create_consistent_backup(
        database,
        backups,
        source_evidence=source_evidence,
    )

    assert first != second
    assert first.exists() and second.exists()
    assert first.read_bytes() == first_bytes
    assert migration._logical_identity(first_evidence) == migration._logical_identity(
        source_evidence
    )
    assert migration._logical_identity(second_evidence) == migration._logical_identity(
        source_evidence
    )
    assert stat.S_IMODE(first.stat().st_mode) == 0o600
    assert stat.S_IMODE(backups.stat().st_mode) == 0o700


def test_backup_capacity_is_checked_before_creating_a_file(tmp_path, monkeypatch):
    database = _database(tmp_path / "source.sqlite3")
    backups = tmp_path / "backups"
    source_evidence = migration.metadata(database)
    monkeypatch.setattr(
        migration.shutil,
        "disk_usage",
        lambda _path: type("DiskUsage", (), {"free": 0})(),
    )

    with pytest.raises(RuntimeError, match="insufficient free disk"):
        migration.create_consistent_backup(
            database,
            backups,
            source_evidence=source_evidence,
        )

    assert list(backups.iterdir()) == []


def test_backup_failure_removes_partial_file(tmp_path, monkeypatch):
    database = _database(tmp_path / "source.sqlite3")
    backups = tmp_path / "backups"
    original_metadata = migration.metadata

    def fail_metadata(path: Path):
        if path.resolve() != database.resolve():
            raise RuntimeError("backup verification failed")
        return original_metadata(database)

    monkeypatch.setattr(migration, "metadata", fail_metadata)

    with pytest.raises(RuntimeError, match="backup verification failed"):
        migration.create_consistent_backup(database, backups)

    assert list(backups.iterdir()) == []


def test_writer_probe_requires_both_launchd_services_absent(monkeypatch):
    def stopped(command, **_kwargs):
        label = command[-1]
        return subprocess.CompletedProcess(
            command,
            113,
            "",
            f'Could not find service "{label}" in domain for user',
        )

    monkeypatch.setattr(migration.subprocess, "run", stopped)

    assert migration.require_writers_stopped() == {
        label: "stopped" for label in migration.WRITER_LABELS
    }


@pytest.mark.parametrize(
    ("returncode", "stderr", "message"),
    [
        (0, "", "still loaded"),
        (1, "permission denied", "unverifiable"),
    ],
)
def test_writer_probe_fails_closed(returncode, stderr, message, monkeypatch):
    monkeypatch.setattr(
        migration.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command, returncode, "", stderr
        ),
    )

    with pytest.raises(RuntimeError, match=message):
        migration.require_writers_stopped()


def test_release_manifest_binds_paper_controls_ci_and_source_tree():
    manifest = _valid_manifest()

    migration.validate_release_manifest(manifest)

    manifest["live_capability"] = True
    with pytest.raises(RuntimeError, match="live capability"):
        migration.validate_release_manifest(manifest)


def test_release_manifest_rejects_mismatched_ci_and_source_authority():
    manifest = _valid_manifest()
    manifest["ci"]["head_sha"] = "b" * 40
    with pytest.raises(RuntimeError, match="CI identity"):
        migration.validate_release_manifest(manifest)

    manifest = _valid_manifest()
    manifest["release_authority"]["source_tree_sha"] = "different-tree"
    with pytest.raises(RuntimeError, match="source-tree authority"):
        migration.validate_release_manifest(manifest)


def test_release_manifest_must_resolve_inside_release_root(tmp_path, monkeypatch):
    release_root = tmp_path / "releases"
    release = release_root / "release-a"
    release.mkdir(parents=True)
    manifest_path = release / "release-manifest.json"
    manifest_path.write_text(json.dumps(_valid_manifest()), encoding="utf-8")
    monkeypatch.setattr(migration, "RELEASE_ROOT", release_root)

    assert migration.load_release_manifest(manifest_path)["release_id"] == "a" * 12

    outside = tmp_path / "release-manifest.json"
    outside.write_text(json.dumps(_valid_manifest()), encoding="utf-8")
    with pytest.raises(RuntimeError, match="inside an immutable release"):
        migration.load_release_manifest(outside)


def test_release_interpreter_must_match_immutable_release_venv(
    tmp_path, monkeypatch
):
    release = tmp_path / "release"
    manifest_path = release / "release-manifest.json"
    (release / ".venv").mkdir(parents=True)
    manifest_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(migration.sys, "prefix", str(release / ".venv"))
    monkeypatch.setattr(
        migration.platform, "python_version", lambda: migration.REQUIRED_PYTHON
    )

    assert migration.require_release_interpreter(manifest_path) == {
        "python_version": migration.REQUIRED_PYTHON,
        "environment": str((release / ".venv").resolve()),
    }

    monkeypatch.setattr(migration.sys, "prefix", str(tmp_path / "other"))
    with pytest.raises(RuntimeError, match="exact immutable release interpreter"):
        migration.require_release_interpreter(manifest_path)


def test_main_requires_both_explicit_production_authorities(monkeypatch):
    monkeypatch.delenv(
        "TRADINGAGENT_ALLOW_PRODUCTION_DB_MIGRATION", raising=False
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "migrate_runtime_db.py",
            "--database",
            "/does/not/exist.sqlite3",
            "--release-manifest",
            "/does/not/exist.json",
        ],
    )

    with pytest.raises(
        SystemExit, match="explicit production migration authorization is required"
    ):
        migration.main()


def test_real_migration_is_structurally_idempotent_on_temporary_database(
    tmp_path, monkeypatch
):
    database = tmp_path / "runtime.sqlite3"
    sqlite3.connect(database).close()
    monkeypatch.delenv("TRADING_AGENT_TESTING", raising=False)

    first = migration._apply_and_verify(database)
    second = migration._apply_and_verify(database)

    assert migration._logical_identity(first) == migration._logical_identity(second)
    assert migration.REQUIRED_SCHEMA_VERSION in second["versions"]
    with sqlite3.connect(database) as conn:
        assert (
            conn.execute(
                "SELECT value FROM runtime_metadata WHERE key='environment'"
            ).fetchone()[0]
            == "production-paper"
        )


def test_main_runs_two_passes_and_retains_verified_backup(
    tmp_path, monkeypatch, capsys
):
    state_root = tmp_path / "state"
    database = state_root / "database" / "runtime.sqlite3"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"database")
    backup = state_root / "backups" / "backup.sqlite3"
    backup.parent.mkdir(parents=True)
    backup.write_bytes(b"backup")
    manifest_path = tmp_path / "release-manifest.json"
    manifest_path.write_text("{}", encoding="utf-8")
    evidence = _evidence()
    passes: list[Path] = []
    writer_checks: list[bool] = []

    monkeypatch.setattr(migration, "STATE_ROOT", state_root)
    monkeypatch.setattr(migration, "load_release_manifest", lambda _path: _valid_manifest())
    monkeypatch.setattr(
        migration,
        "require_release_interpreter",
        lambda _path: {
            "python_version": migration.REQUIRED_PYTHON,
            "environment": "/release/.venv",
        },
    )
    monkeypatch.setattr(
        migration,
        "require_writers_stopped",
        lambda: writer_checks.append(True)
        or {label: "stopped" for label in migration.WRITER_LABELS},
    )
    monkeypatch.setattr(migration, "metadata", lambda _path: dict(evidence))
    monkeypatch.setattr(
        migration,
        "create_consistent_backup",
        lambda *_args, **_kwargs: (backup, dict(evidence)),
    )
    monkeypatch.setattr(
        migration,
        "_apply_and_verify",
        lambda path: passes.append(path) or dict(evidence),
    )
    monkeypatch.setenv(
        "TRADINGAGENT_ALLOW_PRODUCTION_DB_MIGRATION", "YES_I_AM_DEPLOYING"
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "migrate_runtime_db.py",
            "--database",
            str(database),
            "--release-manifest",
            str(manifest_path),
            "--allow-production-migration",
        ],
    )

    assert migration.main() == 0

    output = json.loads(capsys.readouterr().out)
    assert output["idempotent"] is True
    assert output["backup"] == str(backup)
    assert passes == [database.resolve(), database.resolve()]
    assert len(writer_checks) == 2


def test_main_fails_before_backup_if_a_writer_is_loaded(
    tmp_path, monkeypatch
):
    state_root = tmp_path / "state"
    database = state_root / "database" / "runtime.sqlite3"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"database")
    manifest_path = tmp_path / "release-manifest.json"
    manifest_path.write_text("{}", encoding="utf-8")
    backup_called = False

    monkeypatch.setattr(migration, "STATE_ROOT", state_root)
    monkeypatch.setattr(migration, "load_release_manifest", lambda _path: _valid_manifest())
    monkeypatch.setattr(
        migration,
        "require_release_interpreter",
        lambda _path: {
            "python_version": migration.REQUIRED_PYTHON,
            "environment": "/release/.venv",
        },
    )
    monkeypatch.setattr(
        migration,
        "require_writers_stopped",
        lambda: (_ for _ in ()).throw(RuntimeError("writer still loaded")),
    )

    def unexpected_backup(*_args, **_kwargs):
        nonlocal backup_called
        backup_called = True

    monkeypatch.setattr(migration, "create_consistent_backup", unexpected_backup)
    monkeypatch.setenv(
        "TRADINGAGENT_ALLOW_PRODUCTION_DB_MIGRATION", "YES_I_AM_DEPLOYING"
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "migrate_runtime_db.py",
            "--database",
            str(database),
            "--release-manifest",
            str(manifest_path),
            "--allow-production-migration",
        ],
    )

    with pytest.raises(SystemExit, match="writer still loaded"):
        migration.main()

    assert backup_called is False


def test_main_fails_closed_when_second_pass_is_not_idempotent(
    tmp_path, monkeypatch
):
    state_root = tmp_path / "state"
    database = state_root / "database" / "runtime.sqlite3"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"database")
    backup = state_root / "backups" / "backup.sqlite3"
    backup.parent.mkdir(parents=True)
    backup.write_bytes(b"backup")
    manifest_path = tmp_path / "release-manifest.json"
    manifest_path.write_text("{}", encoding="utf-8")
    initial = _evidence()
    passes = iter([_evidence(page_count=4), _evidence(page_count=5)])

    monkeypatch.setattr(migration, "STATE_ROOT", state_root)
    monkeypatch.setattr(migration, "load_release_manifest", lambda _path: _valid_manifest())
    monkeypatch.setattr(
        migration,
        "require_release_interpreter",
        lambda _path: {
            "python_version": migration.REQUIRED_PYTHON,
            "environment": "/release/.venv",
        },
    )
    monkeypatch.setattr(
        migration,
        "require_writers_stopped",
        lambda: {label: "stopped" for label in migration.WRITER_LABELS},
    )
    monkeypatch.setattr(migration, "metadata", lambda _path: dict(initial))
    monkeypatch.setattr(
        migration,
        "create_consistent_backup",
        lambda *_args, **_kwargs: (backup, dict(initial)),
    )
    monkeypatch.setattr(migration, "_apply_and_verify", lambda _path: next(passes))
    monkeypatch.setenv(
        "TRADINGAGENT_ALLOW_PRODUCTION_DB_MIGRATION", "YES_I_AM_DEPLOYING"
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "migrate_runtime_db.py",
            "--database",
            str(database),
            "--release-manifest",
            str(manifest_path),
            "--allow-production-migration",
        ],
    )

    with pytest.raises(SystemExit, match="second migration pass changed") as exc:
        migration.main()

    assert str(backup) in str(exc.value)
    assert backup.exists()
    assert backup.read_bytes() == b"backup"
