from __future__ import annotations

from scripts.check_release_eligibility import build_report


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
