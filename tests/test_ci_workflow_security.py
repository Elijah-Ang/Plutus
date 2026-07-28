from __future__ import annotations

import re
from pathlib import Path

import yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CI_WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml"
DEPENDABOT = REPOSITORY_ROOT / ".github" / "dependabot.yml"
ACTION_REFERENCE = re.compile(
    r"^\s*(?:-\s*)?uses:\s*(?P<action>[^@\s]+)@(?P<ref>[^\s#]+)"
    r"(?:\s+#\s*(?P<version>v\d+(?:\.\d+){0,2}))?\s*$"
)
MINIMUM_NODE24_MAJORS = {
    "actions/checkout": 5,
    "actions/setup-python": 6,
}


def _workflow() -> dict[str, object]:
    return yaml.load(CI_WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def test_external_actions_are_immutable_current_node24_releases() -> None:
    references = []
    for line in CI_WORKFLOW.read_text(encoding="utf-8").splitlines():
        if "uses:" not in line:
            continue
        match = ACTION_REFERENCE.match(line)
        assert match is not None, f"unparseable action reference: {line.strip()}"
        action = match.group("action")
        if action.startswith("./"):
            continue
        ref = match.group("ref")
        version = match.group("version")
        assert re.fullmatch(r"[0-9a-f]{40}", ref), (
            f"{action} must be pinned to a full commit SHA"
        )
        assert version is not None, (
            f"{action} needs an adjacent release comment for Dependabot"
        )
        minimum_major = MINIMUM_NODE24_MAJORS.get(action)
        if minimum_major is not None:
            major = int(version.removeprefix("v").split(".", maxsplit=1)[0])
            assert major >= minimum_major, f"{action} must use its Node 24 generation"
        references.append(action)

    assert references == ["actions/checkout", "actions/setup-python"]


def test_checkout_does_not_persist_ci_credentials() -> None:
    steps = _workflow()["jobs"]["offline-tests"]["steps"]
    checkout = next(
        step
        for step in steps
        if str(step.get("uses", "")).startswith("actions/checkout@")
    )
    assert checkout["with"]["persist-credentials"] == "false"


def test_ci_keeps_exact_release_python_and_minimal_permissions() -> None:
    workflow = _workflow()
    assert workflow["permissions"] == {"contents": "read", "actions": "read"}
    steps = workflow["jobs"]["offline-tests"]["steps"]
    setup_python = next(
        step
        for step in steps
        if str(step.get("uses", "")).startswith("actions/setup-python@")
    )
    assert setup_python["with"]["python-version"] == "3.13.9"


def test_dependabot_tracks_pinned_github_actions() -> None:
    config = yaml.safe_load(DEPENDABOT.read_text(encoding="utf-8"))
    assert config == {
        "version": 2,
        "updates": [
            {
                "package-ecosystem": "github-actions",
                "directory": "/",
                "schedule": {"interval": "weekly"},
                "open-pull-requests-limit": 5,
            }
        ],
    }
