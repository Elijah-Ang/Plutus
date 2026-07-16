#!/usr/bin/env python3
"""Recompute release identity, configuration, versions and installed inventory."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import subprocess
import sys
import urllib.parse
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.build_isolated_wheel import (
    REQUIRED_PIP,
    REQUIRED_SETUPTOOLS,
    verify_wheel_evidence,
)

REQUIRED_PYTHON = "3.13.9"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def installed_app_package_digests(distribution: Any) -> dict[str, str]:
    """Hash the installed app tree while rejecting non-source install payloads."""
    installed_package_root = Path(distribution.locate_file("app"))
    if installed_package_root.is_symlink():
        raise ValueError("installed release app package is missing or unsafe")
    package_root = installed_package_root.resolve()
    if not package_root.is_dir():
        raise ValueError("installed release app package is missing or unsafe")
    current: dict[str, str] = {}
    for directory, names, files in os.walk(package_root, followlinks=False):
        retained_names = []
        for name in names:
            if name == "__pycache__":
                continue
            if (Path(directory) / name).is_symlink():
                raise ValueError("installed release app package contains an unsafe directory")
            retained_names.append(name)
        names[:] = retained_names
        for name in files:
            path = Path(directory) / name
            if path.suffix == ".pyc":
                continue
            if path.is_symlink() or not path.is_file():
                raise ValueError("installed release app package contains an unsafe file")
            relative = "app/" + path.relative_to(package_root).as_posix()
            current[relative] = digest(path)
    return current


def verify(
    root: Path,
    *,
    actual_python: str | None = None,
    frozen_lines: Iterable[str] | None = None,
    effective_config_hash: str | None = None,
    schema_version: str | None = None,
    required_schema_versions: Iterable[str] | None = None,
    formula_versions: dict[str, str] | None = None,
    installed_distribution_version: str | None = None,
    installed_wheel_sha256: str | None = None,
    installed_package_digests: dict[str, str] | None = None,
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
    required_gates = {
        "compileall", "installed_wheel_import", "targeted_safety_suites", "full_pytest"
    }
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
    source_path = root / "tracked-source-inventory.json"
    if not source_path.is_file():
        raise ValueError("tracked source inventory is missing")
    source_inventory = json.loads(source_path.read_text(encoding="utf-8"))
    if digest(source_path) != manifest.get("tracked_source_inventory_sha256"):
        raise ValueError("tracked source inventory file digest mismatch")
    if str(source_inventory.get("git_tree_sha") or "") != str(manifest.get("git_tree_sha") or ""):
        raise ValueError("artifact Git tree SHA changed")
    if str(source_inventory.get("inventory_digest") or "") != str(manifest.get("tracked_source_inventory_digest") or ""):
        raise ValueError("tracked source inventory authority digest changed")
    authority = manifest.get("release_authority") if isinstance(manifest.get("release_authority"), dict) else {}
    if (
        str(authority.get("source_tree_sha") or "") != str(manifest.get("git_tree_sha") or "")
        or str(authority.get("tracked_source_inventory_digest") or "")
        != str(manifest.get("tracked_source_inventory_digest") or "")
    ):
        raise ValueError("release authority is not bound to the tracked source tree")

    wheel_evidence_path = root / "wheel-build-evidence.json"
    wheel_root = root / "release-wheel"
    if not wheel_evidence_path.is_file() or not wheel_root.is_dir():
        raise ValueError("isolated release wheel evidence is missing")
    if digest(wheel_evidence_path) != manifest.get("wheel_build_evidence_sha256"):
        raise ValueError("release wheel evidence digest mismatch")
    wheel_evidence = json.loads(wheel_evidence_path.read_text(encoding="utf-8"))
    retained_wheel_entries = sorted(wheel_root.iterdir())
    wheels = [path for path in retained_wheel_entries if path.is_file() and path.suffix == ".whl"]
    if len(wheels) != 1 or len(retained_wheel_entries) != 1:
        raise ValueError("release artifact must retain exactly one wheel")
    verified_wheel = verify_wheel_evidence(
        wheels[0],
        wheel_evidence,
        source_inventory=source_inventory,
        release_commit=str(manifest.get("release_commit") or ""),
        git_tree_sha=str(manifest.get("git_tree_sha") or ""),
        source_inventory_digest=str(manifest.get("tracked_source_inventory_digest") or ""),
        expected_name=str(manifest.get("distribution_name") or ""),
        expected_version=str(manifest.get("distribution_version") or ""),
    )
    if (
        verified_wheel["wheel_sha256"] != manifest.get("release_wheel_sha256")
        or verified_wheel["wheel_filename"] != manifest.get("release_wheel_filename")
        or wheel_evidence.get("python_version") != REQUIRED_PYTHON
        or wheel_evidence.get("pip_version") != REQUIRED_PIP
        or wheel_evidence.get("setuptools_version") != REQUIRED_SETUPTOOLS
        or wheel_evidence.get("build_isolation") is not False
        or wheel_evidence.get("dependency_resolution") is not False
    ):
        raise ValueError("release wheel build identity is invalid")
    installed_version = installed_distribution_version
    installed_archive_sha256 = installed_wheel_sha256
    distribution = None
    if (
        installed_version is None
        or installed_archive_sha256 is None
        or installed_package_digests is None
    ):
        try:
            distribution = importlib.metadata.distribution(str(manifest.get("distribution_name") or ""))
        except importlib.metadata.PackageNotFoundError as exc:
            raise ValueError("release wheel distribution is not installed") from exc
        installed_version = distribution.version
        try:
            direct_url = json.loads(distribution.read_text("direct_url.json") or "")
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("installed release wheel provenance is missing") from exc
        archive_info = direct_url.get("archive_info") if isinstance(direct_url, dict) else None
        if isinstance(archive_info, dict):
            hashes = archive_info.get("hashes")
            installed_archive_sha256 = str(
                (hashes.get("sha256") if isinstance(hashes, dict) else "")
                or str(archive_info.get("hash") or "").removeprefix("sha256=")
            )
        else:
            installed_archive_sha256 = ""
        wheel_url = str(direct_url.get("url") or "") if isinstance(direct_url, dict) else ""
        parsed_wheel_url = urllib.parse.urlparse(wheel_url)
        installed_wheel_name = Path(urllib.parse.unquote(parsed_wheel_url.path)).name
        if parsed_wheel_url.scheme != "file" or installed_wheel_name != wheels[0].name:
            raise ValueError("installed release wheel provenance does not match retained evidence")
    if installed_version != manifest.get("distribution_version"):
        raise ValueError("installed release distribution version changed")
    if installed_archive_sha256 != verified_wheel["wheel_sha256"]:
        raise ValueError("installed release wheel digest does not match retained evidence")
    expected_installed_package = {
        str(item.get("path") or ""): str(item.get("content_sha256") or "")
        for item in (source_inventory.get("files") or [])
        if isinstance(item, dict) and str(item.get("path") or "").startswith("app/")
    }
    current_installed_package = installed_package_digests
    if current_installed_package is None:
        assert distribution is not None
        current_installed_package = installed_app_package_digests(distribution)
    if current_installed_package != expected_installed_package:
        raise ValueError("installed release app package changed from authoritative source")

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
