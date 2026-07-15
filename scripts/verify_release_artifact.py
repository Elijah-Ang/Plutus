#!/usr/bin/env python3
"""Recompute release identity, configuration, versions and installed inventory."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
import sys
from pathlib import Path
from typing import Iterable

REQUIRED_PYTHON = "3.13.9"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify(
    root: Path,
    *,
    actual_python: str | None = None,
    frozen_lines: Iterable[str] | None = None,
    effective_config_hash: str | None = None,
    schema_version: str | None = None,
    required_schema_versions: Iterable[str] | None = None,
    formula_versions: dict[str, str] | None = None,
) -> dict[str, object]:
    """Verify a built artifact; injectable evidence keeps negative tests hermetic."""
    root = root.resolve()
    manifest = json.loads((root / "release-manifest.json").read_text(encoding="utf-8"))
    tests = json.loads((root / "artifact-test-results.json").read_text(encoding="utf-8"))
    current_python = actual_python or platform.python_version()
    if current_python != REQUIRED_PYTHON or manifest.get("python_version") != REQUIRED_PYTHON:
        raise ValueError("release Python version mismatch")
    if (
        manifest.get("mode") != "paper"
        or manifest.get("manual_approval_only") is not True
        or manifest.get("live_capability") is not False
    ):
        raise ValueError("release manifest is not paper-only and manual-approval-only")
    if (
        manifest.get("tests_verified") is not True
        or tests.get("tests_verified") is not True
        or tests.get("python_version") != REQUIRED_PYTHON
    ):
        raise ValueError("artifact tests are missing or were run with the wrong Python")
    results = tests.get("results") if isinstance(tests.get("results"), list) else []
    result_by_name = {
        str(item.get("name")): item for item in results if isinstance(item, dict)
    }
    required_gates = {"compileall", "targeted_safety_suites", "full_pytest"}
    if (
        not required_gates.issubset(result_by_name)
        or any(result_by_name[name].get("exit_code") != 0 for name in required_gates)
        or manifest.get("artifact_test_results") != results
    ):
        raise ValueError("artifact gate results are incomplete or were not successful")
    migration = tests.get("migration_proof") if isinstance(tests.get("migration_proof"), dict) else {}
    if migration.get("idempotent") is not True or migration.get("missing_schema_versions") not in ([], ()):
        raise ValueError("artifact migration proof is incomplete or not idempotent")
    if digest(root / "artifact-test-results.json") != manifest.get("artifact_test_results_sha256"):
        raise ValueError("artifact test result digest mismatch")
    if digest(root / "requirements.lock") != manifest.get("requirements_lock_sha256"):
        raise ValueError("requirements lock digest mismatch")
    if digest(root / "requirements-hashes.lock") != manifest.get("requirements_hash_lock_sha256"):
        raise ValueError("hashed requirements lock digest mismatch")

    frozen = list(frozen_lines) if frozen_lines is not None else subprocess.run(
        [str(root / ".venv" / "bin" / "python"), "-m", "pip", "freeze", "--all"],
        cwd=root, text=True, capture_output=True, check=True,
    ).stdout.splitlines()
    current_inventory = "\n".join(sorted(line for line in frozen if line.strip())) + "\n"
    recorded_inventory = (root / "dependency-inventory.txt").read_text(encoding="utf-8")
    if current_inventory != recorded_inventory:
        raise ValueError("installed dependency inventory changed after artifact testing")
    if hashlib.sha256(current_inventory.encode()).hexdigest() != manifest.get("dependency_inventory_sha256"):
        raise ValueError("installed dependency inventory digest mismatch")

    if any(value is None for value in (
        effective_config_hash, schema_version, required_schema_versions, formula_versions,
    )):
        sys.path.insert(0, str(root))
        from app.formula_versions import REQUIRED_SCHEMA_VERSIONS
        from app.runtime_guard import REQUIRED_SCHEMA_VERSION
        from app.utils import load_config
        from scripts.check_release_eligibility import RELEASE_FORMULA_VERSIONS

        effective_config_hash = effective_config_hash or load_config().get("effective_config_hash")
        schema_version = schema_version or REQUIRED_SCHEMA_VERSION
        required_schema_versions = required_schema_versions or REQUIRED_SCHEMA_VERSIONS
        formula_versions = formula_versions or RELEASE_FORMULA_VERSIONS
    if effective_config_hash != manifest.get("configuration_hash"):
        raise ValueError("effective artifact configuration hash changed")
    if tests.get("configuration_hash") != effective_config_hash:
        raise ValueError("artifact test configuration hash does not match the release")
    if manifest.get("schema_version") != schema_version:
        raise ValueError("artifact runtime schema version changed")
    if set(manifest.get("required_schema_versions") or []) != set(required_schema_versions or []):
        raise ValueError("artifact required schema versions changed")
    if manifest.get("formula_versions") != formula_versions:
        raise ValueError("artifact formula versions changed")
    return {
        "verified": True,
        "python_version": current_python,
        "configuration_hash": effective_config_hash,
        "schema_version_count": len(set(required_schema_versions or [])),
        "formula_version_count": len(formula_versions or {}),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("release", type=Path, nargs="?", default=Path.cwd())
    args = parser.parse_args()
    try:
        result = verify(args.release)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
