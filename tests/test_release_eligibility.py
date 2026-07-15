from __future__ import annotations

import io
import json
from types import SimpleNamespace

import scripts.check_release_eligibility as release_check
from scripts.check_release_eligibility import _remote_ci, build_report


def test_release_eligibility_never_treats_skipped_local_or_remote_checks_as_passed() -> None:
    report = build_report(run_tests=False, check_remote=False)
    assert report["configuration_valid"] is True
    assert report["migration_compatible"] is True
    assert report["paper_only_verified"] is True
    assert report["local_tests"] == {
        "status": "unverified", "passed": False, "reason": "local tests were skipped"
    }
    assert report["github_ci"]["passed"] is False
    assert report["release_eligible"] is False


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def test_remote_ci_uses_newest_exact_sha_run(monkeypatch) -> None:
    payload = {"workflow_runs": [
        {"id": 1, "name": "CI", "head_sha": "abc", "created_at": "2026-07-14T08:00:00Z",
         "status": "completed", "conclusion": "success"},
        {"id": 2, "name": "CI", "head_sha": "abc", "created_at": "2026-07-14T08:01:00Z",
         "status": "completed", "conclusion": "failure"},
    ]}
    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: _Response(json.dumps(payload).encode()))
    result = _remote_ci("abc", "owner/repo", skip=False)
    assert result["run_id"] == 2
    assert result["passed"] is False
    assert result["status"] == "failed"


def test_remote_ci_requires_named_jobs_to_pass(monkeypatch) -> None:
    run_payload = {"workflow_runs": [
        {"id": 7, "name": "CI", "head_sha": "abc", "created_at": "2026-07-14T08:00:00Z",
         "status": "completed", "conclusion": "success"},
    ]}
    jobs_payload = {"jobs": [{"name": "offline-tests", "status": "completed", "conclusion": "success"}]}
    payloads = iter((run_payload, jobs_payload))
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *_args, **_kwargs: _Response(json.dumps(next(payloads)).encode()),
    )
    result = _remote_ci("abc", "owner/repo", skip=False)
    assert result["passed"] is True
    assert result["required_jobs"] == ["offline-tests"]


def test_release_reachability_uses_exact_github_main_sha(monkeypatch) -> None:
    def fake_run(*args):
        if args[:3] == ("git", "fetch", "--prune"):
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args[:3] == ("git", "merge-base", "--is-ancestor"):
            assert args[3:] == ("candidate", "github-main")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(args)

    def fake_github(url):
        if url.endswith("/git/ref/heads/main"):
            return {"object": {"sha": "github-main"}}, None, 200
        if "/git/matching-refs/tags/" in url:
            return [], None, 200
        raise AssertionError(url)

    monkeypatch.setattr(release_check, "_run", fake_run)
    monkeypatch.setattr(release_check, "_github_json", fake_github)
    result = release_check._release_reachability("candidate", "owner/repo")
    assert result["passed"] is True
    assert result["remote_main_sha"] == "github-main"


def test_unpushed_local_tag_and_stale_local_main_do_not_pass_release_check(monkeypatch) -> None:
    def fake_run(*args):
        if args[:3] == ("git", "fetch", "--prune"):
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args[:3] == ("git", "merge-base", "--is-ancestor"):
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        # A local tag or local origin/main is intentionally not consulted.
        if args[:3] == ("git", "tag", "--points-at"):
            return SimpleNamespace(returncode=0, stdout="immutable-release-local\n", stderr="")
        raise AssertionError(args)

    def fake_github(url):
        if url.endswith("/git/ref/heads/main"):
            return {"object": {"sha": "new-github-main"}}, None, 200
        if "/git/matching-refs/tags/" in url:
            return [], None, 200
        raise AssertionError(url)

    monkeypatch.setattr(release_check, "_run", fake_run)
    monkeypatch.setattr(release_check, "_github_json", fake_github)
    result = release_check._release_reachability("candidate", "owner/repo")
    assert result["passed"] is False
    assert result["status"] == "failed"


def test_release_reachability_is_unverified_when_github_is_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(
        release_check, "_run",
        lambda *args: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr(
        release_check, "_github_json",
        lambda _url: (None, "network unavailable", None),
    )
    result = release_check._release_reachability("candidate", "owner/repo")
    assert result["passed"] is False
    assert result["status"] == "unverified"
