#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.formula_versions import REQUIRED_SCHEMA_VERSIONS  # noqa: E402
from app.runtime_guard import (  # noqa: E402
    RELEASE_ROOT,
    REQUIRED_SCHEMA_VERSION,
    STATE_ROOT,
)
from app.storage import Storage  # noqa: E402

WRITER_LABELS = (
    "com.elijah.tradingagent",
    "com.elijah.tradingagent.telegram",
)
REQUIRED_PYTHON = "3.13.9"
IDEMPOTENCE_KEYS = (
    "tables",
    "schema_sha256",
    "versions",
    "row_counts",
    "page_count",
)


def _readonly_uri(path: Path) -> str:
    return f"{path.resolve(strict=True).as_uri()}?mode=ro"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _quoted_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def metadata(path: Path) -> dict[str, Any]:
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise RuntimeError(f"database is not a regular file: {resolved}")
    with sqlite3.connect(_readonly_uri(resolved), uri=True) as conn:
        objects = conn.execute(
            "SELECT type,name,COALESCE(sql,'') "
            "FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' "
            "ORDER BY type,name"
        ).fetchall()
        table_names = [str(row[1]) for row in objects if row[0] == "table"]
        row_counts = {
            table: int(
                conn.execute(
                    f"SELECT COUNT(*) FROM {_quoted_identifier(table)}"
                ).fetchone()[0]
            )
            for table in table_names
        }
        versions = (
            [
                str(row[0])
                for row in conn.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                )
            ]
            if "schema_migrations" in table_names
            else []
        )
        quick_check = [str(row[0]) for row in conn.execute("PRAGMA quick_check")]
        integrity_check = [
            str(row[0]) for row in conn.execute("PRAGMA integrity_check")
        ]
        foreign_key_violations = len(
            conn.execute("PRAGMA foreign_key_check").fetchall()
        )
        page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
        page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
        freelist_count = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
    schema_payload = "\n".join("|".join(map(str, row)) for row in objects)
    return {
        "bytes": resolved.stat().st_size,
        "file_sha256": _sha256(resolved),
        "quick_check": quick_check,
        "integrity_check": integrity_check,
        "foreign_key_violations": foreign_key_violations,
        "tables": len(table_names),
        "row_counts": row_counts,
        "schema_sha256": hashlib.sha256(schema_payload.encode()).hexdigest(),
        "versions": versions,
        "page_count": page_count,
        "page_size": page_size,
        "logical_bytes": page_count * page_size,
        "freelist_count": freelist_count,
    }


def _require_healthy_database(evidence: dict[str, Any], *, label: str) -> None:
    if evidence["quick_check"] != ["ok"]:
        raise RuntimeError(f"{label} quick_check failed")
    if evidence["integrity_check"] != ["ok"]:
        raise RuntimeError(f"{label} integrity_check failed")
    if evidence["foreign_key_violations"] != 0:
        raise RuntimeError(f"{label} has foreign-key violations")


def _logical_identity(evidence: dict[str, Any]) -> dict[str, Any]:
    return {key: evidence[key] for key in IDEMPOTENCE_KEYS}


def require_writers_stopped() -> dict[str, str]:
    result: dict[str, str] = {}
    for label in WRITER_LABELS:
        try:
            probe = subprocess.run(
                ["/bin/launchctl", "print", f"gui/{os.getuid()}/{label}"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(
                f"cannot verify that launchd writer {label} is stopped"
            ) from exc
        output = f"{probe.stdout}\n{probe.stderr}".lower()
        if probe.returncode == 0:
            raise RuntimeError(f"launchd writer is still loaded: {label}")
        if "could not find service" not in output:
            raise RuntimeError(
                f"launchd writer stop state is unverifiable: {label}"
            )
        result[label] = "stopped"
    return result


def validate_release_manifest(manifest: dict[str, Any]) -> None:
    commit = str(manifest.get("release_commit") or "")
    ci = manifest.get("ci") if isinstance(manifest.get("ci"), dict) else {}
    authority = (
        manifest.get("release_authority")
        if isinstance(manifest.get("release_authority"), dict)
        else {}
    )
    required_versions = {
        str(value) for value in manifest.get("required_schema_versions") or []
    }
    if manifest.get("mode") != "paper":
        raise RuntimeError("release manifest is not paper-only")
    if manifest.get("manual_approval_only") is not True:
        raise RuntimeError("release manifest does not require manual approval")
    if manifest.get("live_capability") is not False:
        raise RuntimeError("release manifest permits live capability")
    if manifest.get("tests_verified") is not True:
        raise RuntimeError("release artifact tests are not verified")
    if manifest.get("python_version") != REQUIRED_PYTHON:
        raise RuntimeError("release Python identity is incompatible")
    if manifest.get("schema_version") != REQUIRED_SCHEMA_VERSION:
        raise RuntimeError("release schema requirement is incompatible")
    if required_versions != set(REQUIRED_SCHEMA_VERSIONS):
        raise RuntimeError("release required schema versions are incompatible")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise RuntimeError("release commit identity is missing or malformed")
    if not str(manifest.get("release_id") or ""):
        raise RuntimeError("release ID is missing")
    if (
        ci.get("workflow_name") != "CI"
        or ci.get("head_sha") != commit
        or not isinstance(ci.get("run_id"), int)
        or ci["run_id"] <= 0
    ):
        raise RuntimeError("release CI identity is missing or mismatched")
    tree_sha = str(manifest.get("git_tree_sha") or "")
    source_digest = str(manifest.get("tracked_source_inventory_digest") or "")
    if (
        not re.fullmatch(r"[0-9a-f]{40}", tree_sha)
        or authority.get("source_tree_sha") != tree_sha
        or not re.fullmatch(r"[0-9a-f]{64}", source_digest)
        or authority.get("tracked_source_inventory_digest") != source_digest
        or authority.get("mode") not in {"forward", "rollback"}
    ):
        raise RuntimeError("release source-tree authority is missing or mismatched")


def load_release_manifest(path: Path) -> dict[str, Any]:
    resolved = path.resolve(strict=True)
    if resolved.name != "release-manifest.json":
        raise RuntimeError("release manifest filename is invalid")
    if not resolved.is_relative_to(RELEASE_ROOT.resolve()):
        raise RuntimeError("release manifest must be inside an immutable release")
    try:
        manifest = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError("release manifest is unavailable or invalid") from exc
    if not isinstance(manifest, dict):
        raise RuntimeError("release manifest must be a JSON object")
    validate_release_manifest(manifest)
    return manifest


def require_release_interpreter(manifest_path: Path) -> dict[str, str]:
    release = manifest_path.resolve(strict=True).parent
    expected_prefix = (release / ".venv").resolve()
    actual_prefix = Path(sys.prefix).resolve()
    actual_python = platform.python_version()
    if actual_prefix != expected_prefix or actual_python != REQUIRED_PYTHON:
        raise RuntimeError(
            "production migration must use the exact immutable release interpreter"
        )
    return {
        "python_version": actual_python,
        "environment": str(actual_prefix),
    }


def create_consistent_backup(
    database: Path,
    backup_root: Path,
    *,
    source_evidence: dict[str, Any] | None = None,
) -> tuple[Path, dict[str, Any]]:
    source = database.resolve(strict=True)
    evidence = source_evidence or metadata(source)
    _require_healthy_database(evidence, label="source database")

    backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(backup_root, 0o700)
    required_free = max(int(evidence["logical_bytes"]), int(evidence["bytes"])) * 2
    if shutil.disk_usage(backup_root).free < required_free:
        raise RuntimeError("insufficient free disk for verified database backup")

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    descriptor, raw_path = tempfile.mkstemp(
        prefix=f"explicit-pre-migration-{timestamp}-",
        suffix=".sqlite3",
        dir=backup_root,
    )
    backup = Path(raw_path)
    os.fchmod(descriptor, 0o600)
    os.close(descriptor)
    try:
        with (
            sqlite3.connect(_readonly_uri(source), uri=True) as source_conn,
            sqlite3.connect(backup) as backup_conn,
        ):
            source_conn.execute("PRAGMA query_only=ON")
            source_conn.backup(backup_conn)
        os.chmod(backup, 0o600)
        with backup.open("rb") as handle:
            os.fsync(handle.fileno())
        directory_fd = os.open(backup_root, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

        backup_evidence = metadata(backup)
        _require_healthy_database(backup_evidence, label="backup database")
        if _logical_identity(backup_evidence) != _logical_identity(evidence):
            raise RuntimeError(
                "backup logical identity does not match the stopped source database"
            )
        return backup, backup_evidence
    except Exception:
        for candidate in (
            backup,
            Path(f"{backup}-journal"),
            Path(f"{backup}-wal"),
            Path(f"{backup}-shm"),
        ):
            candidate.unlink(missing_ok=True)
        raise


def _apply_and_verify(database: Path) -> dict[str, Any]:
    storage = Storage(database)
    storage.apply_explicit_migrations(production_paper=True)
    storage.require_runtime_schema(production=True)
    evidence = metadata(database)
    _require_healthy_database(evidence, label="migrated production database")
    if REQUIRED_SCHEMA_VERSION not in evidence["versions"]:
        raise RuntimeError("required runtime schema version was not recorded")
    if not REQUIRED_SCHEMA_VERSIONS.issubset(set(evidence["versions"])):
        raise RuntimeError("one or more required schema versions were not recorded")
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--release-manifest", type=Path, required=True)
    parser.add_argument("--allow-production-migration", action="store_true")
    args = parser.parse_args()
    if (
        os.getenv("TRADINGAGENT_ALLOW_PRODUCTION_DB_MIGRATION")
        != "YES_I_AM_DEPLOYING"
        or not args.allow_production_migration
    ):
        raise SystemExit("explicit production migration authorization is required")

    backup: Path | None = None
    try:
        database = args.database.resolve(strict=True)
        if not database.is_file():
            raise RuntimeError("production database must be an existing regular file")
        if not database.is_relative_to((STATE_ROOT / "database").resolve()):
            raise RuntimeError(
                "database must be under the production state database directory"
            )
        manifest = load_release_manifest(args.release_manifest)
        release_interpreter = require_release_interpreter(args.release_manifest)
        writers = require_writers_stopped()
        source_before = metadata(database)
        _require_healthy_database(source_before, label="source database")
        backup, backup_evidence = create_consistent_backup(
            database,
            STATE_ROOT / "backups",
            source_evidence=source_before,
        )
        print(
            json.dumps(
                {
                    "backup_created": str(backup),
                    "backup_sha256": backup_evidence["file_sha256"],
                },
                sort_keys=True,
            ),
            file=sys.stderr,
            flush=True,
        )

        require_writers_stopped()
        first_pass = _apply_and_verify(database)
        second_pass = _apply_and_verify(database)
        idempotent = _logical_identity(first_pass) == _logical_identity(second_pass)
        if not idempotent:
            raise RuntimeError(
                "second migration pass changed schema, rows, versions, or page count"
            )
        retained_backup = metadata(backup)
        if retained_backup["file_sha256"] != backup_evidence["file_sha256"]:
            raise RuntimeError("verified pre-migration backup changed after migration")
    except Exception as exc:
        suffix = (
            f"; verified pre-migration backup retained at {backup}"
            if backup is not None and backup.is_file()
            else ""
        )
        raise SystemExit(f"{exc}{suffix}") from exc

    print(
        json.dumps(
            {
                "release_id": manifest["release_id"],
                "release_commit": manifest["release_commit"],
                "release_interpreter": release_interpreter,
                "backup": str(backup),
                "writers": writers,
                "source_before": source_before,
                "backup_evidence": backup_evidence,
                "first_pass": first_pass,
                "second_pass": second_pass,
                "idempotent": idempotent,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
