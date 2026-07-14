from __future__ import annotations

import io
import json

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
