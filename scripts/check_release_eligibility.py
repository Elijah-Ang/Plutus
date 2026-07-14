#!/usr/bin/env python3
"""Fail-closed local and commit-linked release eligibility report."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.configuration import validate_config  # noqa: E402
from app.formula_versions import REQUIRED_SCHEMA_VERSIONS  # noqa: E402
from app.storage import Storage  # noqa: E402
from app.utils import load_config  # noqa: E402

REQUIRED_CI_JOBS = frozenset({"offline-tests"})


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=ROOT, text=True, capture_output=True, check=False)


def _schema_signature(path: Path) -> str:
    with sqlite3.connect(path) as conn:
        rows = conn.execute(
            "SELECT type,name,COALESCE(sql,'') FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
        ).fetchall()
    return hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()


def _remote_ci(sha: str, repository: str | None, *, skip: bool) -> dict[str, Any]:
    if skip or not repository:
        return {"status": "unverified", "passed": False, "reason": "remote CI lookup skipped or repository unavailable"}
    url = (
        f"https://api.github.com/repos/{repository}/actions/runs?"
        + urllib.parse.urlencode({"head_sha": sha, "per_page": 100})
    )
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "plutus-release-eligibility"}
    token = os.getenv("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=15) as response:
            payload = json.load(response)
    except (OSError, urllib.error.URLError, ValueError) as exc:
        return {"status": "unverified", "passed": False, "reason": f"GitHub lookup failed: {type(exc).__name__}"}
    runs = [
        run for run in payload.get("workflow_runs", [])
        if run.get("head_sha") == sha and run.get("name") == "CI"
    ]
    if not runs:
        return {"status": "unverified", "passed": False, "head_sha": sha, "reason": "no exact-SHA CI run"}
    newest = max(runs, key=lambda run: (str(run.get("created_at") or ""), int(run.get("id") or 0)))
    result = {
        "status": "pending" if newest.get("status") != "completed" else "failed",
        "passed": False, "run_id": newest.get("id"), "url": newest.get("html_url"),
        "conclusion": newest.get("conclusion"), "head_sha": sha,
    }
    if newest.get("status") != "completed" or newest.get("conclusion") != "success":
        result["reason"] = "newest exact-SHA CI run is not completed successfully"
        return result
    jobs_url = f"https://api.github.com/repos/{repository}/actions/runs/{newest.get('id')}/jobs?per_page=100"
    try:
        with urllib.request.urlopen(urllib.request.Request(jobs_url, headers=headers), timeout=15) as response:
            jobs_payload = json.load(response)
    except (OSError, urllib.error.URLError, ValueError) as exc:
        return {**result, "status": "unverified", "reason": f"GitHub jobs lookup failed: {type(exc).__name__}"}
    jobs = {str(job.get("name") or ""): job for job in jobs_payload.get("jobs", [])}
    missing = sorted(REQUIRED_CI_JOBS - jobs.keys())
    unsuccessful = sorted(
        name for name in REQUIRED_CI_JOBS
        if name in jobs and (jobs[name].get("status") != "completed" or jobs[name].get("conclusion") != "success")
    )
    if missing or unsuccessful:
        return {**result, "required_jobs_missing": missing, "required_jobs_unsuccessful": unsuccessful,
                "reason": "required CI jobs are missing or unsuccessful"}
    return {**result, "status": "passed", "passed": True, "required_jobs": sorted(REQUIRED_CI_JOBS)}


def _release_reachability(sha: str) -> dict[str, Any]:
    on_main = _run("git", "merge-base", "--is-ancestor", sha, "origin/main").returncode == 0
    tags = _run("git", "tag", "--points-at", sha).stdout.splitlines()
    approved_tags = sorted(tag for tag in tags if tag.startswith("immutable-release-"))
    return {
        "passed": on_main or bool(approved_tags),
        "reachable_from_origin_main": on_main,
        "approved_immutable_release_tags": approved_tags,
        "reason": None if on_main or approved_tags else "commit is not on origin/main or an approved immutable release tag",
    }


def build_report(*, run_tests: bool, check_remote: bool, repository: str | None = None) -> dict[str, Any]:
    sha_result = _run("git", "rev-parse", "HEAD")
    sha = sha_result.stdout.strip() if sha_result.returncode == 0 else "unknown"
    status = _run("git", "status", "--porcelain")
    worktree_lines = [line for line in status.stdout.splitlines() if line]
    config = load_config()
    config_errors = validate_config(config)
    config_hash = str(config.get("effective_config_hash") or "")
    paper_identity = {
        "mode": config.get("mode"),
        "live_enabled": config.get("live_enabled"),
        "auto_execution_enabled": config.get("auto_execution_enabled"),
        "auto_execution_mode": config.get("auto_execution_mode"),
        "live_capability": (config.get("execution_capabilities") or {}).get("live_execution_enabled"),
    }
    paper_only = (
        paper_identity["mode"] == "paper"
        and paper_identity["live_enabled"] is False
        and paper_identity["auto_execution_enabled"] is False
        and paper_identity["auto_execution_mode"] == "manual_only"
        and paper_identity["live_capability"] is False
    )
    with tempfile.TemporaryDirectory(prefix="plutus-release-schema-") as temporary:
        database = Path(temporary) / "eligibility.sqlite3"
        storage = Storage(database)
        storage.initialize()
        storage.apply_explicit_migrations(production_paper=False)
        first_signature = _schema_signature(database)
        storage.apply_explicit_migrations(production_paper=False)
        second_signature = _schema_signature(database)
        schema_versions = sorted(storage.schema_versions())
        missing_versions = sorted(REQUIRED_SCHEMA_VERSIONS - set(schema_versions))
        try:
            storage.require_runtime_schema()
            runtime_schema_compatible = True
            runtime_schema_reason = None
        except RuntimeError as exc:
            runtime_schema_compatible = False
            runtime_schema_reason = str(exc)
    migration_compatible = first_signature == second_signature and not missing_versions and runtime_schema_compatible
    if run_tests:
        compile_result = _run("python", "-m", "compileall", "app", "tests", "scripts")
        pytest_result = _run("python", "-m", "pytest", "-q")
        local_tests = {
            "status": "passed" if compile_result.returncode == 0 and pytest_result.returncode == 0 else "failed",
            "passed": compile_result.returncode == 0 and pytest_result.returncode == 0,
            "compileall_exit_code": compile_result.returncode,
            "pytest_exit_code": pytest_result.returncode,
            "pytest_summary": "\n".join(pytest_result.stdout.splitlines()[-3:]),
        }
    else:
        local_tests = {"status": "unverified", "passed": False, "reason": "local tests were skipped"}
    remote = _run("git", "remote", "get-url", "origin").stdout.strip()
    if repository is None and remote:
        tail = remote.removesuffix(".git").split("github.com")[-1].lstrip(":/")
        repository = tail if "/" in tail else None
    remote_ci = _remote_ci(sha, repository, skip=not check_remote)
    release_reachability = _release_reachability(sha)
    report = {
        "commit_sha": sha,
        "branch": _run("git", "branch", "--show-current").stdout.strip(),
        "worktree_clean": status.returncode == 0 and not worktree_lines,
        "worktree_status": worktree_lines,
        "configuration_hash": config_hash,
        "configuration_valid": not config_errors and bool(config_hash),
        "configuration_errors": config_errors,
        "schema_versions": schema_versions,
        "missing_schema_versions": missing_versions,
        "migration_compatible": migration_compatible,
        "migration_idempotent": first_signature == second_signature,
        "runtime_schema_compatible": runtime_schema_compatible,
        "runtime_schema_reason": runtime_schema_reason,
        "paper_only_identity": paper_identity,
        "paper_only_verified": paper_only,
        "local_tests": local_tests,
        "github_ci": remote_ci,
        "release_reachability": release_reachability,
    }
    report["release_eligible"] = all((
        report["worktree_clean"], report["configuration_valid"], migration_compatible,
        paper_only, local_tests["passed"], remote_ci["passed"], release_reachability["passed"],
    ))
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-tests", action="store_true", help="report local tests as unverified")
    parser.add_argument("--skip-remote", action="store_true", help="report GitHub CI as unverified")
    parser.add_argument("--repository", help="GitHub owner/repository; otherwise derived from origin")
    args = parser.parse_args()
    report = build_report(
        run_tests=not args.skip_tests,
        check_remote=not args.skip_remote,
        repository=args.repository,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["release_eligible"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
