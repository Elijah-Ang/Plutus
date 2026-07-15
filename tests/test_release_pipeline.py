from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

import scripts.run_artifact_tests as artifact_tests
import scripts.verify_deployment_authority as deployment_authority
from scripts.verify_release_artifact import REQUIRED_PYTHON, verify


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _artifact(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    results = [
        {"name": "compileall", "exit_code": 0},
        {"name": "targeted_safety_suites", "exit_code": 0},
        {"name": "full_pytest", "exit_code": 0},
    ]
    tests = {
        "tests_verified": True,
        "python_version": REQUIRED_PYTHON,
        "configuration_hash": "config-a",
        "migration_proof": {"idempotent": True, "missing_schema_versions": []},
        "results": results,
    }
    (tmp_path / "artifact-test-results.json").write_text(json.dumps(tests), encoding="utf-8")
    (tmp_path / "requirements.lock").write_text("demo==1\n", encoding="utf-8")
    (tmp_path / "requirements-hashes.lock").write_text("demo==1 --hash=sha256:abc\n", encoding="utf-8")
    inventory = "demo==1\n"
    (tmp_path / "dependency-inventory.txt").write_text(inventory, encoding="utf-8")
    manifest: dict[str, object] = {
        "python_version": REQUIRED_PYTHON,
        "mode": "paper",
        "manual_approval_only": True,
        "live_capability": False,
        "tests_verified": True,
        "artifact_test_results": results,
        "configuration_hash": "config-a",
        "schema_version": "schema-a",
        "required_schema_versions": ["migration-a"],
        "formula_versions": {"sizing": "v1"},
        "artifact_test_results_sha256": _digest(tmp_path / "artifact-test-results.json"),
        "requirements_lock_sha256": _digest(tmp_path / "requirements.lock"),
        "requirements_hash_lock_sha256": _digest(tmp_path / "requirements-hashes.lock"),
        "dependency_inventory_sha256": hashlib.sha256(inventory.encode()).hexdigest(),
    }
    (tmp_path / "release-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return tmp_path, manifest


def _verify(root: Path, **overrides):
    arguments = {
        "actual_python": REQUIRED_PYTHON,
        "frozen_lines": ["demo==1"],
        "effective_config_hash": "config-a",
        "schema_version": "schema-a",
        "required_schema_versions": ["migration-a"],
        "formula_versions": {"sizing": "v1"},
    }
    arguments.update(overrides)
    return verify(root, **arguments)


def test_fresh_artifact_test_runner_executes_with_supplied_interpreter(tmp_path, monkeypatch) -> None:
    marker = tmp_path / "ran.txt"
    script = tmp_path / "gate.py"
    script.write_text(f"from pathlib import Path; Path({str(marker)!r}).write_text('tested')\n")
    monkeypatch.setattr(artifact_tests, "ROOT", tmp_path)
    evidence = artifact_tests.run("fresh_artifact_gate", [sys.executable, str(script)])
    assert evidence["exit_code"] == 0
    assert evidence["command"][0] == sys.executable
    assert marker.read_text() == "tested"


def test_build_records_tests_verified_only_after_fresh_environment_tests() -> None:
    script = (Path(__file__).parents[1] / "scripts" / "build_release.sh").read_text()
    test_call = '"$STAGING/.venv/bin/python" scripts/run_artifact_tests.py'
    assert test_call in script
    assert script.index(test_call) < script.index('"tests_verified":True')


def test_release_artifact_verifier_accepts_exact_evidence(tmp_path) -> None:
    root, _ = _artifact(tmp_path)
    assert _verify(root)["verified"] is True


def test_release_artifact_verifier_rejects_wrong_python(tmp_path) -> None:
    root, _ = _artifact(tmp_path)
    with pytest.raises(ValueError, match="Python version"):
        _verify(root, actual_python="3.13.8")


def test_release_artifact_verifier_rejects_changed_installed_inventory(tmp_path) -> None:
    root, _ = _artifact(tmp_path)
    with pytest.raises(ValueError, match="inventory changed"):
        _verify(root, frozen_lines=["demo==2"])


def test_release_artifact_verifier_rejects_changed_config(tmp_path) -> None:
    root, _ = _artifact(tmp_path)
    with pytest.raises(ValueError, match="configuration hash"):
        _verify(root, effective_config_hash="config-b")


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"schema_version": "schema-b"}, "runtime schema"),
        ({"required_schema_versions": ["migration-b"]}, "required schema"),
        ({"formula_versions": {"sizing": "v2"}}, "formula versions"),
    ],
)
def test_release_artifact_verifier_rejects_changed_schema_or_formula(tmp_path, override, message) -> None:
    root, _ = _artifact(tmp_path)
    with pytest.raises(ValueError, match=message):
        _verify(root, **override)


def test_forward_deployment_requires_exact_current_main(monkeypatch) -> None:
    monkeypatch.setattr(
        deployment_authority,
        "_github_json",
        lambda _url: ({"object": {"sha": "commit-a"}}, None, 200),
    )
    result = deployment_authority.verify(
        {"release_commit": "commit-a", "release_authority": {"mode": "forward"}},
        mode="forward",
    )
    assert result["remote_main_sha"] == "commit-a"


def test_unauthorized_old_ancestor_cannot_deploy_forward(monkeypatch) -> None:
    monkeypatch.setattr(
        deployment_authority,
        "_github_json",
        lambda _url: ({"object": {"sha": "new-main"}}, None, 200),
    )
    with pytest.raises(RuntimeError, match="exact current GitHub main"):
        deployment_authority.verify(
            {"release_commit": "old-ancestor", "release_authority": {"mode": "forward"}},
            mode="forward",
        )


def _rollback_manifest() -> tuple[dict[str, object], dict[str, object]]:
    evidence = {
        "release_id": 7,
        "asset_id": 8,
        "asset_digest": "sha256:abc",
        "asset_size": 99,
        "download_digest": "sha256:abc",
    }
    manifest = {
        "release_commit": "rollback-commit",
        "release_authority": {
            "mode": "rollback",
            "tag_name": "immutable-release-good",
            "attestation": evidence,
        },
    }
    return manifest, evidence


def _rollback_github(url: str):
    if "/git/ref/tags/" in url:
        return {"object": {"sha": "tag-object", "type": "tag"}}, None, 200
    if "/git/tags/tag-object" in url:
        return {"object": {"sha": "rollback-commit", "type": "commit"}}, None, 200
    raise AssertionError(url)


def test_authorized_immutable_rollback_deployment(monkeypatch) -> None:
    manifest, evidence = _rollback_manifest()
    monkeypatch.setattr(deployment_authority, "_github_json", _rollback_github)
    monkeypatch.setattr(deployment_authority, "_release_attestation_asset", lambda *_: {
        **evidence,
        "release_immutable": True,
        "manifest": {"release_commit": "rollback-commit"},
    })
    result = deployment_authority.verify(manifest, mode="rollback")
    assert result["asset_id"] == 8


def test_replaced_rollback_attestation_asset_is_rejected(monkeypatch) -> None:
    manifest, evidence = _rollback_manifest()
    monkeypatch.setattr(deployment_authority, "_github_json", _rollback_github)
    monkeypatch.setattr(deployment_authority, "_release_attestation_asset", lambda *_: {
        **evidence,
        "asset_id": 999,
        "release_immutable": True,
        "manifest": {"release_commit": "rollback-commit"},
    })
    with pytest.raises(RuntimeError, match="asset_id was replaced"):
        deployment_authority.verify(manifest, mode="rollback")
