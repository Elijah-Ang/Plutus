#!/usr/bin/env python3
"""Run every release gate with the artifact interpreter and persist evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

REQUIRED_PYTHON = "3.13.9"
ROOT = Path(__file__).resolve().parents[1]
TARGETED_SUITES = (
    "tests/test_final_hardening.py",
    "tests/test_phase0_execution_integrity.py",
    "tests/test_phase0_approval_recovery.py",
    "tests/test_phase0_crash_boundaries.py",
    "tests/test_rotation_coordinator.py",
    "tests/test_execution_authority_final.py",
    "tests/test_release_eligibility.py",
    "tests/test_release_pipeline.py",
    "tests/test_profitability_validation_attribution.py",
    "tests/test_crypto_capabilities.py",
    "tests/test_crypto_market_data.py",
    "tests/test_crypto_research.py",
)


def schema_signature(path: Path) -> str:
    with sqlite3.connect(path) as conn:
        rows = conn.execute(
            "SELECT type,name,COALESCE(sql,'') FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
        ).fetchall()
    return hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()


def run(name: str, args: list[str], *, env: dict[str, str] | None = None) -> dict[str, object]:
    started = time.monotonic()
    result = subprocess.run(args, cwd=ROOT, text=True, capture_output=True, env=env, check=False)
    summary = "\n".join((result.stdout + "\n" + result.stderr).strip().splitlines()[-8:])
    evidence: dict[str, object] = {
        "name": name,
        "command": args,
        "exit_code": result.returncode,
        "duration_seconds": round(time.monotonic() - started, 3),
        "summary": summary,
    }
    match = re.search(r"(?:^|\s)(\d+) passed(?:,|\s|$)", summary)
    if match:
        evidence["passed_count"] = int(match.group(1))
    if result.returncode != 0:
        print(result.stdout, file=sys.stderr)
        print(result.stderr, file=sys.stderr)
        raise RuntimeError(f"artifact gate failed: {name}")
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if platform.python_version() != REQUIRED_PYTHON:
        raise SystemExit(f"release tests require Python {REQUIRED_PYTHON}; found {platform.python_version()}")

    import os
    from app.configuration import validate_config
    from app.formula_versions import REQUIRED_SCHEMA_VERSIONS
    from app.storage import Storage
    from app.utils import load_config

    config = load_config()
    errors = validate_config(config)
    if errors or not config.get("effective_config_hash"):
        raise SystemExit("artifact configuration validation failed: " + "; ".join(errors))

    test_env = dict(os.environ)
    test_env["TRADING_AGENT_TESTING"] = "1"
    results: list[dict[str, object]] = []
    results.append(run("compileall", [sys.executable, "-m", "compileall", "-q", "app", "scripts", "tests"]))
    results.append(run(
        "installed_wheel_import",
        [
            sys.executable,
            "-I",
            "-c",
            "import app,pathlib,sys; p=pathlib.Path(app.__file__).resolve(); "
            "root=pathlib.Path(sys.argv[1]).resolve(); "
            "source=(root/'app').resolve(); environment=pathlib.Path(sys.prefix).resolve(); "
            "assert source not in p.parents and environment in p.parents, "
            "f'package did not import from the release environment: {p}'; print(p)",
            str(ROOT),
        ],
    ))
    results.append(run(
        "alpaca_crypto_sdk_contract",
        [sys.executable, "scripts/verify_alpaca_crypto_sdk.py"],
    ))

    with tempfile.TemporaryDirectory(prefix="plutus-artifact-migration-") as directory:
        database = Path(directory) / "fresh.sqlite3"
        storage = Storage(database)
        storage.initialize()
        storage.apply_explicit_migrations(production_paper=False)
        first = schema_signature(database)
        storage.apply_explicit_migrations(production_paper=False)
        second = schema_signature(database)
        storage.require_runtime_schema()
        versions = sorted(storage.schema_versions())
        missing = sorted(set(REQUIRED_SCHEMA_VERSIONS) - set(versions))
        if first != second or missing:
            raise SystemExit("artifact migration proof is not idempotent or complete")
        migration = {
            "fresh_database_first_signature": first,
            "fresh_database_second_signature": second,
            "idempotent": True,
            "schema_versions": versions,
            "missing_schema_versions": missing,
        }

    results.append(run(
        "targeted_safety_suites",
        [sys.executable, "-m", "pytest", "-q", *TARGETED_SUITES],
        env=test_env,
    ))
    results.append(run("full_pytest", [sys.executable, "-m", "pytest", "-q"], env=test_env))
    report = {
        "tests_verified": True,
        "tested_at_utc": datetime.now(UTC).isoformat(),
        "python_version": platform.python_version(),
        "interpreter": sys.executable,
        "configuration_hash": config["effective_config_hash"],
        "migration_proof": migration,
        "results": results,
    }
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
