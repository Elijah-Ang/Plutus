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


def _github_json(url: str) -> tuple[dict[str, Any] | list[Any] | None, str | None, int | None]:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "plutus-release-eligibility"}
    token = os.getenv("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=15) as response:
            getcode = getattr(response, "getcode", None)
            status = getcode() if callable(getcode) else 200
            payload = json.load(response)
        if status and status >= 400:
            return None, f"GitHub returned HTTP {status}", status
        return payload, None, status
    except urllib.error.HTTPError as exc:
        return None, f"GitHub returned HTTP {exc.code}", exc.code
    except (OSError, urllib.error.URLError, ValueError) as exc:
        return None, f"GitHub lookup failed: {type(exc).__name__}", None


def _repository_from_remote(remote: str) -> str | None:
    if "github.com" not in remote:
        return None
    tail = remote.removesuffix(".git").split("github.com", 1)[-1].lstrip(":/")
    return tail if tail.count("/") == 1 else None


def _release_reachability(
    sha: str,
    repository: str | None = None,
    *,
    config_hash: str | None = None,
    skip: bool = False,
) -> dict[str, Any]:
    if skip:
        return {"status": "unverified", "passed": False, "reason": "remote release reachability lookup skipped"}

    fetched = _run("git", "fetch", "--prune", "origin")
    if fetched.returncode != 0:
        return {
            "status": "unverified", "passed": False,
            "reason": "remote fetch/prune failed; release ancestry is unverified",
        }
    if repository is None:
        repository = _repository_from_remote(_run("git", "remote", "get-url", "origin").stdout.strip())
    if not repository:
        return {"status": "unverified", "passed": False, "reason": "GitHub repository cannot be resolved from origin"}

    main_payload, main_error, main_status = _github_json(
        f"https://api.github.com/repos/{repository}/git/ref/heads/main"
    )
    main_ref = main_payload.get("object") if isinstance(main_payload, dict) else None
    remote_main_sha = str(main_ref.get("sha") or "") if isinstance(main_ref, dict) else ""
    if main_error or not remote_main_sha:
        return {
            "status": "unverified", "passed": False,
            "repository": repository, "github_main_status": main_status,
            "reason": main_error or "GitHub main ref did not contain an exact SHA",
        }

    ancestry = _run("git", "merge-base", "--is-ancestor", sha, remote_main_sha)
    if ancestry.returncode == 0:
        on_main = True
    elif ancestry.returncode == 1:
        on_main = False
    else:
        return {
            "status": "unverified", "passed": False, "repository": repository,
            "remote_main_sha": remote_main_sha,
            "reason": "local ancestry check against the fetched remote SHA failed",
        }

    approved_tags: list[str] = []
    tag_manifest_verified: list[str] = []
    tags_payload, tags_error, tags_status = _github_json(
        f"https://api.github.com/repos/{repository}/git/matching-refs/tags/immutable-release-"
    )
    if tags_error and tags_status != 404:
        if not on_main:
            return {
                "status": "unverified", "passed": False, "repository": repository,
                "remote_main_sha": remote_main_sha, "reachable_from_github_main": on_main,
                "reason": tags_error,
            }
    elif isinstance(tags_payload, list):
        for ref in tags_payload:
            if not isinstance(ref, dict):
                continue
            ref_name = str(ref.get("ref") or "")
            tag_name = ref_name.removeprefix("refs/tags/")
            if not tag_name.startswith("immutable-release-"):
                continue
            target = ref.get("object") or {}
            target_sha = str(target.get("sha") or "") if isinstance(target, dict) else ""
            target_type = str(target.get("type") or "") if isinstance(target, dict) else ""
            exact = target_type == "commit" and target_sha == sha
            annotated = False
            if target_type == "tag" and target_sha:
                tag_payload, tag_error, _tag_status = _github_json(
                    f"https://api.github.com/repos/{repository}/git/tags/{target_sha}"
                )
                tag_object = tag_payload.get("object") if isinstance(tag_payload, dict) else None
                exact = (
                    not tag_error
                    and isinstance(tag_object, dict)
                    and str(tag_object.get("type") or "") == "commit"
                    and str(tag_object.get("sha") or "") == sha
                )
                annotated = exact
            if exact:
                approved_tags.append(tag_name)
                if annotated:
                    tag_manifest_verified.append(tag_name)

    approved_tags = sorted(set(approved_tags))
    return {
        "passed": on_main or bool(approved_tags),
        "status": "passed" if on_main or approved_tags else "failed",
        "repository": repository,
        "remote_main_sha": remote_main_sha,
        "reachable_from_github_main": on_main,
        "approved_immutable_release_tags": approved_tags,
        "annotated_immutable_release_tags": sorted(set(tag_manifest_verified)),
        "configuration_hash_checked": bool(config_hash),
        "reason": None if on_main or approved_tags else "commit is not reachable from GitHub main or an exact remote immutable release tag",
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
        repository = _repository_from_remote(remote)
    remote_ci = _remote_ci(sha, repository, skip=not check_remote)
    release_reachability = _release_reachability(
        sha, repository, config_hash=config_hash, skip=not check_remote
    )
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
