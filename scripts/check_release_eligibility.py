#!/usr/bin/env python3
"""Fail-closed local and commit-linked release eligibility report."""

from __future__ import annotations

import argparse
import base64
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
from app.formula_versions import (  # noqa: E402
    CONFIGURATION_SCHEMA_VERSION,
    PHASE4_ALLOCATION_VERSION,
    PHASE4_SCHEMA_VERSION,
    PROFITABILITY_RANKING_FORMULA_VERSION,
    PROFITABILITY_RANKING_SCHEMA_VERSION,
    REQUIRED_SCHEMA_VERSIONS,
    ROTATION_FORMULA_VERSION,
    ROTATION_SCHEMA_VERSION,
    STRATEGY_EXECUTION_REGISTRY_FORMULA_VERSION,
    STRATEGY_EXECUTION_REGISTRY_SCHEMA_VERSION,
    TRADE_ECONOMICS_FORMULA_VERSION,
    TRADE_ECONOMICS_SCHEMA_VERSION,
)
from app.storage import Storage  # noqa: E402
from app.utils import load_config  # noqa: E402

REQUIRED_CI_JOBS = frozenset({"offline-tests"})
RELEASE_FORMULA_VERSIONS = {
    "configuration_schema": CONFIGURATION_SCHEMA_VERSION,
    "phase4_allocation": PHASE4_ALLOCATION_VERSION,
    "phase4_schema": PHASE4_SCHEMA_VERSION,
    "rotation": ROTATION_FORMULA_VERSION,
    "rotation_schema": ROTATION_SCHEMA_VERSION,
    "strategy_registry": STRATEGY_EXECUTION_REGISTRY_FORMULA_VERSION,
    "strategy_registry_schema": STRATEGY_EXECUTION_REGISTRY_SCHEMA_VERSION,
    "trade_economics": TRADE_ECONOMICS_FORMULA_VERSION,
    "trade_economics_schema": TRADE_ECONOMICS_SCHEMA_VERSION,
    "profitability_ranking": PROFITABILITY_RANKING_FORMULA_VERSION,
    "profitability_ranking_schema": PROFITABILITY_RANKING_SCHEMA_VERSION,
}


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
        "workflow_name": newest.get("name"),
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


def _decode_release_manifest(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    content = payload.get("content")
    if not isinstance(content, str):
        return None
    try:
        decoded = base64.b64decode(content.replace("\n", ""), validate=True)
        manifest = json.loads(decoded.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return manifest if isinstance(manifest, dict) else None


def _release_attestation_asset(repository: str, tag_name: str) -> dict[str, Any] | None:
    release, error, _ = _github_json(
        f"https://api.github.com/repos/{repository}/releases/tags/{urllib.parse.quote(tag_name, safe='')}"
    )
    if (
        error
        or not isinstance(release, dict)
        or bool(release.get("draft"))
        or release.get("immutable") is not True
        or not release.get("id")
    ):
        return None
    assets = release.get("assets") if isinstance(release.get("assets"), list) else []
    matching = [asset for asset in assets if isinstance(asset, dict) and asset.get("name") == "release-attestation.json"]
    asset = matching[0] if len(matching) == 1 else None
    if (
        asset is None
        or not asset.get("url")
        or not asset.get("id")
        or not str(asset.get("digest") or "").startswith("sha256:")
    ):
        return None
    headers = {
        "Accept": "application/octet-stream",
        "User-Agent": "plutus-release-eligibility",
    }
    token = os.getenv("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with urllib.request.urlopen(
            urllib.request.Request(str(asset["url"]), headers=headers), timeout=15
        ) as response:
            raw = response.read()
        download_digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        if download_digest != str(asset["digest"]):
            return None
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, urllib.error.URLError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return {
        "manifest": payload,
        "release_id": int(release["id"]),
        "release_immutable": True,
        "asset_id": int(asset["id"]),
        "asset_digest": str(asset["digest"]),
        "asset_size": int(asset.get("size") or len(raw)),
        "download_digest": download_digest,
    }


def _manifest_ci_identity(manifest: dict[str, Any]) -> tuple[str, str, str]:
    ci = manifest.get("ci") if isinstance(manifest.get("ci"), dict) else {}
    workflow = str(ci.get("workflow_name") or manifest.get("ci_workflow_name") or "")
    run_id = str(ci.get("run_id") or manifest.get("ci_run_id") or "")
    head_sha = str(ci.get("head_sha") or manifest.get("ci_head_sha") or manifest.get("release_commit") or "")
    return workflow, run_id, head_sha


def _verify_release_manifest(
    repository: str,
    tag_name: str,
    tag_object_sha: str,
    sha: str,
    config_hash: str | None,
    remote_ci: dict[str, Any] | None,
) -> tuple[bool, str]:
    tag_payload, tag_error, _ = _github_json(
        f"https://api.github.com/repos/{repository}/git/tags/{tag_object_sha}"
    )
    tag_object = tag_payload.get("object") if isinstance(tag_payload, dict) else None
    if tag_error or not isinstance(tag_object, dict) or str(tag_object.get("type") or "") != "commit":
        return False, f"{tag_name}: annotated tag object is unavailable"
    if str(tag_object.get("sha") or "") != sha:
        return False, f"{tag_name}: annotated tag does not point to the candidate commit"

    attestation = _release_attestation_asset(repository, tag_name)
    if attestation is None:
        return False, f"{tag_name}: immutable GitHub release attestation asset is missing or invalid"
    manifest = attestation.get("manifest") if isinstance(attestation, dict) else None
    if not isinstance(manifest, dict):
        return False, f"{tag_name}: immutable attestation evidence is incomplete"
    if (
        attestation.get("release_immutable") is not True
        or not attestation.get("release_id")
        or not attestation.get("asset_id")
        or attestation.get("asset_digest") != attestation.get("download_digest")
    ):
        return False, f"{tag_name}: attestation asset identity or digest is not immutable and verified"
    if str(manifest.get("tag_name") or "") != tag_name:
        return False, f"{tag_name}: release attestation tag identity does not match"
    if str(manifest.get("release_commit") or manifest.get("commit_sha") or "") != sha:
        return False, f"{tag_name}: release manifest commit does not match the candidate"
    attested_tree = str(manifest.get("git_tree_sha") or manifest.get("source_tree_sha") or "")
    if not attested_tree or not str(manifest.get("tracked_source_inventory_digest") or ""):
        return False, f"{tag_name}: release attestation is not bound to a tracked source tree"
    remote_commit, remote_error, _ = _github_json(
        f"https://api.github.com/repos/{repository}/git/commits/{sha}"
    )
    remote_tree = str(((remote_commit or {}).get("tree") or {}).get("sha") or "") if isinstance(remote_commit, dict) else ""
    if remote_error or not remote_tree or remote_tree != attested_tree:
        return False, f"{tag_name}: attested source tree does not match the immutable GitHub commit"
    if not config_hash or str(manifest.get("configuration_hash") or manifest.get("config_hash") or "") != str(config_hash):
        return False, f"{tag_name}: release manifest configuration hash does not match"
    schema_versions = manifest.get("required_schema_versions") or manifest.get("schema_versions")
    if not isinstance(schema_versions, (list, tuple, set)) or set(map(str, schema_versions)) != set(REQUIRED_SCHEMA_VERSIONS):
        return False, f"{tag_name}: release manifest schema versions are incomplete or mismatched"
    formula_versions = manifest.get("formula_versions")
    if not isinstance(formula_versions, dict) or any(
        str(formula_versions.get(key) or "") != value
        for key, value in RELEASE_FORMULA_VERSIONS.items()
    ):
        return False, f"{tag_name}: release manifest formula versions are incomplete or mismatched"
    workflow, run_id, head_sha = _manifest_ci_identity(manifest)
    if workflow != "CI" or not run_id or head_sha != sha:
        return False, f"{tag_name}: release manifest CI identity is missing or mismatched"
    if not remote_ci or not remote_ci.get("passed"):
        return False, f"{tag_name}: exact-SHA CI is not verified"
    if str(remote_ci.get("run_id") or "") != run_id or str(remote_ci.get("head_sha") or "") != sha:
        return False, f"{tag_name}: release manifest CI run does not match the remote exact-SHA run"
    if str(remote_ci.get("workflow_name") or "CI") != workflow:
        return False, f"{tag_name}: release manifest workflow identity does not match remote CI"
    return True, f"{tag_name}: verified immutable GitHub release attestation"


def _release_reachability(
    sha: str,
    repository: str | None = None,
    *,
    config_hash: str | None = None,
    remote_ci: dict[str, Any] | None = None,
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

    # Main authority is exact identity, never ancestry. Otherwise every old
    # vulnerable ancestor of main remains deployable forever.
    on_main = sha == remote_main_sha

    approved_tags: list[str] = []
    tag_manifest_verified: list[str] = []
    tag_rejection_reasons: list[str] = []
    release_attestation_evidence: dict[str, Any] = {}
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
            if target_type != "tag" or not target_sha:
                if target_type == "commit" and target_sha == sha:
                    tag_rejection_reasons.append(f"{tag_name}: lightweight tags are not release authority")
                continue
            verified, reason = _verify_release_manifest(
                repository, tag_name, target_sha, sha, config_hash, remote_ci
            )
            if verified:
                approved_tags.append(tag_name)
                tag_manifest_verified.append(tag_name)
                evidence = _release_attestation_asset(repository, tag_name)
                if isinstance(evidence, dict):
                    release_attestation_evidence[tag_name] = {
                        key: evidence.get(key)
                        for key in (
                            "release_id", "release_immutable", "asset_id", "asset_digest",
                            "asset_size", "download_digest",
                        )
                    }
            else:
                tag_rejection_reasons.append(reason)

    approved_tags = sorted(set(approved_tags))
    return {
        "passed": on_main or bool(tag_manifest_verified),
        "status": "passed" if on_main or tag_manifest_verified else "failed",
        "repository": repository,
        "remote_main_sha": remote_main_sha,
        "exact_github_main_sha": on_main,
        "approved_immutable_release_tags": approved_tags,
        "annotated_immutable_release_tags": sorted(set(tag_manifest_verified)),
        "verified_release_manifest": sorted(set(tag_manifest_verified)),
        "release_attestation_evidence": release_attestation_evidence,
        "configuration_hash_checked": bool(on_main and config_hash) or bool(tag_manifest_verified),
        "tag_rejection_reasons": tag_rejection_reasons,
        "reason": None if on_main or tag_manifest_verified else "commit is not the exact GitHub main SHA or a verified immutable release attestation",
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
        sha, repository, config_hash=config_hash, remote_ci=remote_ci, skip=not check_remote
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
