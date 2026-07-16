from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

import scripts.run_artifact_tests as artifact_tests
from app.utils import resolve_project_root
import scripts.verify_deployment_authority as deployment_authority
from scripts.build_isolated_wheel import (
    REQUIRED_PIP,
    REQUIRED_SETUPTOOLS,
    build_isolated_wheel,
    verify_wheel_evidence,
)
from scripts.prune_release_runtime_state import prune_runtime_state
from scripts.verify_release_artifact import REQUIRED_PYTHON, installed_app_package_digests, verify
from scripts.verify_source_tree import create_inventory, verify_inventory


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_wheel(
    path: Path,
    *,
    name: str = "trading-agent",
    version: str = "0.1.0",
    package_content: bytes = b"VALUE = 1\n",
) -> None:
    dist_info = f"{name.replace('-', '_')}-{version}.dist-info"
    with zipfile.ZipFile(path, "w") as wheel:
        wheel.writestr("app/__init__.py", package_content)
        wheel.writestr(f"{dist_info}/METADATA", f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")
        wheel.writestr(f"{dist_info}/WHEEL", "Wheel-Version: 1.0\nTag: py3-none-any\n")
        wheel.writestr(f"{dist_info}/top_level.txt", "app\n")
        wheel.writestr(f"{dist_info}/RECORD", "")


def _artifact(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    results = [
        {"name": "compileall", "exit_code": 0},
        {"name": "installed_wheel_import", "exit_code": 0},
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
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    source_authority = _source_authority(tmp_path, commit="commit-a")
    source_inventory = create_inventory(
        tmp_path, commit="commit-a", authority=source_authority
    )
    (tmp_path / "tracked-source-inventory.json").write_text(
        json.dumps(source_inventory, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    wheel_root = tmp_path / "release-wheel"
    wheel_root.mkdir()
    wheel_path = wheel_root / "trading_agent-0.1.0-py3-none-any.whl"
    _write_wheel(wheel_path)
    wheel_evidence = {
        "version": "isolated_wheel_build_v1",
        "wheel_filename": wheel_path.name,
        "wheel_sha256": _digest(wheel_path),
        "distribution_name": "trading-agent",
        "distribution_version": "0.1.0",
        "source_commit": "commit-a",
        "git_tree_sha": source_inventory["git_tree_sha"],
        "source_inventory_digest": source_inventory["inventory_digest"],
        "tracked_package_file_count": 1,
        "tracked_package_payload_digest": hashlib.sha256(
            json.dumps(
                [{"path": "app/__init__.py", "content_sha256": _digest(tmp_path / "app" / "__init__.py")}],
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest(),
        "python_version": REQUIRED_PYTHON,
        "pip_version": REQUIRED_PIP,
        "setuptools_version": REQUIRED_SETUPTOOLS,
        "build_isolation": False,
        "dependency_resolution": False,
    }
    (tmp_path / "wheel-build-evidence.json").write_text(
        json.dumps(wheel_evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
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
        "release_commit": "commit-a",
        "git_tree_sha": source_inventory["git_tree_sha"],
        "tracked_source_inventory_sha256": _digest(tmp_path / "tracked-source-inventory.json"),
        "tracked_source_inventory_digest": source_inventory["inventory_digest"],
        "wheel_build_evidence_sha256": _digest(tmp_path / "wheel-build-evidence.json"),
        "release_wheel_sha256": _digest(wheel_path),
        "release_wheel_filename": wheel_path.name,
        "distribution_name": "trading-agent",
        "distribution_version": "0.1.0",
        "release_authority": {
            "mode": "forward",
            "source_tree_sha": source_inventory["git_tree_sha"],
            "tracked_source_inventory_digest": source_inventory["inventory_digest"],
        },
    }
    (tmp_path / "release-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return tmp_path, manifest


def _git_blob(content: bytes) -> str:
    return hashlib.sha1(f"blob {len(content)}\0".encode() + content).hexdigest()


def _source_authority(root: Path, *, commit: str = "commit-a", tree: str = "tree-a") -> dict:
    paths = [Path("app.py")]
    if (root / "app" / "__init__.py").is_file():
        paths.append(Path("app/__init__.py"))
    return {
        "commit": commit,
        "tree_sha": tree,
        "files": [
            {
                "path": path.as_posix(),
                "mode": "100644",
                "blob_sha": _git_blob((root / path).read_bytes()),
            }
            for path in sorted(paths)
        ],
    }


def _fixture_git_project(root: Path) -> tuple[str, dict[str, object]]:
    root.mkdir()
    (root / "app").mkdir()
    (root / "app" / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        """[build-system]
requires = ["setuptools==80.9.0"]
build-backend = "setuptools.build_meta"

[project]
name = "plutus-wheel-fixture"
version = "1.2.3"

[tool.setuptools.packages.find]
include = ["app*"]
""",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "release-test@example.invalid"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Release Test"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=root, check=True)
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, text=True, capture_output=True
    ).stdout.strip()
    tree = subprocess.run(
        ["git", "rev-parse", f"{commit}^{{tree}}"], cwd=root, check=True, text=True, capture_output=True
    ).stdout.strip()
    entries: list[dict[str, str]] = []
    raw = subprocess.run(
        ["git", "ls-tree", "-rz", commit], cwd=root, check=True, capture_output=True
    ).stdout
    for item in raw.split(b"\0"):
        if not item:
            continue
        metadata, path = item.split(b"\t", 1)
        mode, kind, blob = metadata.decode().split()
        assert kind == "blob"
        entries.append({"path": path.decode(), "mode": mode, "blob_sha": blob})
    return commit, {"commit": commit, "tree_sha": tree, "files": entries}


def _extract_git_commit(repository: Path, commit: str, destination: Path) -> None:
    archive = destination.parent / f"{destination.name}.tar"
    subprocess.run(
        ["git", "archive", "--format=tar", f"--output={archive}", commit],
        cwd=repository,
        check=True,
    )
    destination.mkdir()
    with tarfile.open(archive) as payload:
        payload.extractall(destination, filter="data")


def _verify(root: Path, **overrides):
    arguments = {
        "actual_python": REQUIRED_PYTHON,
        "frozen_lines": ["demo==1"],
        "effective_config_hash": "config-a",
        "schema_version": "schema-a",
        "required_schema_versions": ["migration-a"],
        "formula_versions": {"sizing": "v1"},
        "installed_distribution_version": "0.1.0",
        "installed_wheel_sha256": _digest(root / "release-wheel" / "trading_agent-0.1.0-py3-none-any.whl"),
        "installed_package_digests": {
            "app/__init__.py": _digest(root / "app" / "__init__.py")
        },
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


def test_installed_wheel_uses_explicit_immutable_release_root(tmp_path, monkeypatch) -> None:
    release = tmp_path / "immutable-release"
    (release / "config").mkdir(parents=True)
    (release / "config" / "config.yaml").write_text("mode: paper\n", encoding="utf-8")
    monkeypatch.setenv("TRADING_AGENT_PROJECT_ROOT", str(release))

    installed_module = tmp_path / ".venv" / "lib" / "python3.13" / "site-packages" / "app" / "utils.py"
    assert resolve_project_root(installed_module) == release.resolve()


def test_explicit_release_root_without_configuration_fails_closed(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TRADING_AGENT_PROJECT_ROOT", str(tmp_path / "incomplete-release"))
    with pytest.raises(RuntimeError, match="does not contain config/config.yaml"):
        resolve_project_root(tmp_path / "site-packages" / "app" / "utils.py")


def test_release_entrypoints_export_project_root_before_artifact_imports() -> None:
    root = Path(__file__).parents[1]
    build = (root / "scripts" / "build_release.sh").read_text()
    artifact_gate = '"$STAGING/.venv/bin/python" scripts/run_artifact_tests.py'
    assert 'export TRADING_AGENT_PROJECT_ROOT="$STAGING"' in build
    assert build.index('export TRADING_AGENT_PROJECT_ROOT="$STAGING"') < build.index(artifact_gate)

    for name in ("run_once.sh", "run_telegram_listener.sh"):
        script = (root / "scripts" / name).read_text()
        export = 'export TRADING_AGENT_PROJECT_ROOT="$ROOT"'
        assert export in script
        assert script.index(export) < script.index('"$ROOT/.venv/bin/python" -m app.')


def test_release_build_uses_external_verified_wheel_workspace_and_atomic_promotion() -> None:
    script = (Path(__file__).parents[1] / "scripts" / "build_release.sh").read_text()
    assert 'STAGING=$(mktemp -d "$RELEASE_ROOT/.' in script
    assert 'LOCK="$DEST.building"' in script
    assert "LOCK_HELD=0" in script and "LOCK_HELD=1" in script
    assert 'chmod -R u+w "$STAGING"' in script
    assert 'scripts/build_isolated_wheel.py' in script
    assert 'pip install --no-deps --no-index "$WHEEL_PATH"' in script
    assert 'pip install --no-deps --no-build-isolation "$STAGING"' not in script
    prune = "scripts/prune_release_runtime_state.py"
    source_verify = "scripts/verify_source_tree.py"
    artifact_verify = 'scripts/verify_release_artifact.py "$STAGING"'
    final_inventory = "release-file-inventory.sha256"
    assert prune in script
    assert script.index(prune) < script.index(source_verify, script.index(prune))
    assert script.rindex(source_verify) > script.index(artifact_verify)
    assert script.rindex(source_verify) < script.index(final_inventory)
    assert 'rm -rf "$STAGING/.git" "$STAGING/data" "$STAGING/logs" "$STAGING/scratch"' not in script
    assert script.index('mv "$STAGING" "$DEST"') > script.index('scripts/verify_release_artifact.py')


def test_release_state_pruning_preserves_tracked_placeholders_and_removes_generated_state(tmp_path) -> None:
    tracked = (
        "data/exports/.gitkeep",
        "data/market_cache/.gitkeep",
        "data/models/.gitkeep",
        "logs/audit/.gitkeep",
        "logs/errors/.gitkeep",
        "logs/runtime/.gitkeep",
    )
    for relative in tracked:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    generated = (
        "data/trading_agent.db",
        "data/exports/report.xlsx",
        "logs/runtime/scanner_identity.json",
        "logs/errors/error.log",
        "scratch/nested/transient.json",
    )
    for relative in generated:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("generated", encoding="utf-8")

    result = prune_runtime_state(
        tmp_path,
        {"files": [{"path": relative} for relative in tracked]},
    )

    assert result["removed_file_count"] == len(generated)
    assert result["preserved_tracked_paths"] == sorted(tracked)
    assert all((tmp_path / relative).is_file() for relative in tracked)
    assert all(not (tmp_path / relative).exists() for relative in generated)


def test_release_state_pruning_fails_closed_for_unsafe_root(tmp_path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "data").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="runtime-state root is unsafe"):
        prune_runtime_state(tmp_path, {"files": [{"path": "app.py"}]})


def test_ci_pins_release_pip_before_installing_dependencies() -> None:
    workflow = (Path(__file__).parents[1] / ".github" / "workflows" / "ci.yml").read_text()
    pin = "python -m pip install 'pip==25.3'"
    dependencies = "python -m pip install --requirement requirements.lock"
    assert pin in workflow
    assert workflow.index(pin) < workflow.index(dependencies)


def test_release_artifact_verifier_accepts_exact_evidence(tmp_path) -> None:
    root, _ = _artifact(tmp_path)
    assert _verify(root)["verified"] is True


def test_isolated_wheel_build_succeeds_without_contaminating_staging(tmp_path) -> None:
    repository = tmp_path / "repository"
    staging = tmp_path / "staging"
    workspaces = tmp_path / "workspaces"
    commit, authority = _fixture_git_project(repository)
    inventory = create_inventory(repository, commit=commit, authority=authority)
    _extract_git_commit(repository, commit, staging)

    evidence = build_isolated_wheel(
        repository_root=repository,
        staging_root=staging,
        inventory=inventory,
        commit=commit,
        python=Path(sys.executable),
        workspace_parent=workspaces,
        repository="fixture/repository",
        authority=authority,
    )

    wheels = list((staging / "release-wheel").glob("*.whl"))
    assert len(wheels) == 1
    assert evidence["wheel_sha256"] == _digest(wheels[0])
    assert evidence["distribution_name"] == "plutus-wheel-fixture"
    assert evidence["distribution_version"] == "1.2.3"
    assert not (staging / "build").exists()
    assert not (staging / "dist").exists()
    assert not list(staging.glob("*.egg-info"))
    assert not list(workspaces.iterdir())
    assert verify_inventory(staging, inventory, authority=authority)["verified"] is True


def test_isolated_wheel_failure_cleans_temporary_and_partial_output(tmp_path, monkeypatch) -> None:
    import scripts.build_isolated_wheel as isolated_wheel

    repository = tmp_path / "repository"
    staging = tmp_path / "staging"
    workspaces = tmp_path / "workspaces"
    staging.mkdir()
    commit, authority = _fixture_git_project(repository)
    inventory = create_inventory(repository, commit=commit, authority=authority)
    original_run = isolated_wheel._run

    def fail_wheel(command, *, cwd):
        if "wheel" in command:
            raise subprocess.CalledProcessError(1, command)
        return original_run(command, cwd=cwd)

    monkeypatch.setattr(isolated_wheel, "_run", fail_wheel)
    with pytest.raises(subprocess.CalledProcessError):
        build_isolated_wheel(
            repository_root=repository,
            staging_root=staging,
            inventory=inventory,
            commit=commit,
            python=Path(sys.executable),
            workspace_parent=workspaces,
            repository="fixture/repository",
            authority=authority,
        )
    assert not (staging / "release-wheel").exists()
    assert not (staging / "wheel-build-evidence.json").exists()
    assert not list(workspaces.iterdir())


def test_release_artifact_rejects_more_than_one_retained_wheel(tmp_path) -> None:
    root, _ = _artifact(tmp_path)
    _write_wheel(root / "release-wheel" / "other-9.9.9-py3-none-any.whl", name="other", version="9.9.9")
    with pytest.raises(ValueError, match="exactly one wheel"):
        _verify(root)


def test_tampered_wheel_payload_cannot_be_blessed_by_regenerated_local_evidence(tmp_path) -> None:
    root, manifest = _artifact(tmp_path)
    wheel = root / "release-wheel" / str(manifest["release_wheel_filename"])
    replacement = root / "tampered.whl"
    with zipfile.ZipFile(wheel) as original, zipfile.ZipFile(replacement, "w") as changed:
        for member in original.namelist():
            content = b"TAMPERED = True\n" if member == "app/__init__.py" else original.read(member)
            changed.writestr(member, content)
    replacement.replace(wheel)

    evidence_path = root / "wheel-build-evidence.json"
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence["wheel_sha256"] = _digest(wheel)
    evidence["tracked_package_payload_digest"] = hashlib.sha256(b"attacker-regenerated").hexdigest()
    evidence_path.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    manifest["release_wheel_sha256"] = _digest(wheel)
    manifest["wheel_build_evidence_sha256"] = _digest(evidence_path)
    (root / "release-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (root / "release-file-inventory.sha256").write_text(
        f"{_digest(wheel)}  ./release-wheel/{wheel.name}\n", encoding="utf-8"
    )

    with pytest.raises(ValueError, match="package payload changed"):
        _verify(root)


def test_wheel_with_unapproved_installable_payload_is_rejected(tmp_path) -> None:
    root, manifest = _artifact(tmp_path)
    wheel = root / "release-wheel" / str(manifest["release_wheel_filename"])
    replacement = root / "extra-payload.whl"
    with zipfile.ZipFile(wheel) as original, zipfile.ZipFile(replacement, "w") as changed:
        for member in original.namelist():
            changed.writestr(member, original.read(member))
        changed.writestr("release_startup.pth", "import os\n")
    replacement.replace(wheel)
    with pytest.raises(ValueError, match="unapproved installable payload"):
        _verify(root)


@pytest.mark.parametrize(
    ("expected_name", "expected_version", "message"),
    [
        ("different-project", "0.1.0", "distribution name"),
        ("trading-agent", "9.9.9", "distribution version"),
    ],
)
def test_release_wheel_wrong_name_or_version_fails(
    tmp_path, expected_name, expected_version, message
) -> None:
    root, manifest = _artifact(tmp_path)
    evidence = json.loads((root / "wheel-build-evidence.json").read_text(encoding="utf-8"))
    wheel = next((root / "release-wheel").glob("*.whl"))
    with pytest.raises(ValueError, match=message):
        verify_wheel_evidence(
            wheel,
            evidence,
            source_inventory=json.loads(
                (root / "tracked-source-inventory.json").read_text(encoding="utf-8")
            ),
            release_commit=str(manifest["release_commit"]),
            git_tree_sha=str(manifest["git_tree_sha"]),
            source_inventory_digest=str(manifest["tracked_source_inventory_digest"]),
            expected_name=expected_name,
            expected_version=expected_version,
        )


def test_unexpected_build_output_remains_rejected_by_source_verifier(tmp_path) -> None:
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    authority = _source_authority(tmp_path)
    inventory = create_inventory(tmp_path, commit="commit-a", authority=authority)
    contamination = tmp_path / "build" / "lib" / "app"
    contamination.mkdir(parents=True)
    (contamination / "telegram_bot.py").write_text("TAMPERED = True\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"build/lib/app/telegram_bot\.py"):
        verify_inventory(tmp_path, inventory, authority=authority)


def test_release_artifact_verifier_rejects_wrong_python(tmp_path) -> None:
    root, _ = _artifact(tmp_path)
    with pytest.raises(ValueError, match="Python version"):
        _verify(root, actual_python="3.13.8")


def test_release_artifact_verifier_rejects_changed_installed_inventory(tmp_path) -> None:
    root, _ = _artifact(tmp_path)
    with pytest.raises(ValueError, match="inventory changed"):
        _verify(root, frozen_lines=["demo==2"])


def test_release_artifact_verifier_rejects_changed_installed_package(tmp_path) -> None:
    root, _ = _artifact(tmp_path)
    with pytest.raises(ValueError, match="app package changed"):
        _verify(root, installed_package_digests={"app/__init__.py": hashlib.sha256(b"tampered").hexdigest()})


def test_installed_app_package_scan_hashes_source_and_ignores_only_bytecode(tmp_path) -> None:
    package = tmp_path / "site-packages" / "app"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    cache = package / "__pycache__"
    cache.mkdir()
    (cache / "__init__.cpython-313.pyc").write_bytes(b"generated")

    class Distribution:
        def locate_file(self, path):
            assert path == "app"
            return package

    assert installed_app_package_digests(Distribution()) == {
        "app/__init__.py": _digest(package / "__init__.py")
    }


def test_installed_app_package_scan_rejects_symlink_payload(tmp_path) -> None:
    package = tmp_path / "site-packages" / "app"
    package.mkdir(parents=True)
    outside = tmp_path / "outside.py"
    outside.write_text("TAMPERED = True\n", encoding="utf-8")
    (package / "linked.py").symlink_to(outside)

    class Distribution:
        def locate_file(self, _path):
            return package

    with pytest.raises(ValueError, match="unsafe file"):
        installed_app_package_digests(Distribution())


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


def test_exact_remote_git_tree_and_tracked_sources_pass(tmp_path) -> None:
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    authority = _source_authority(tmp_path)
    inventory = create_inventory(tmp_path, commit="commit-a", authority=authority)
    result = verify_inventory(tmp_path, inventory, authority=authority)
    assert result["verified"] is True and result["tracked_file_count"] == 1


def test_changed_tracked_python_file_fails_source_authority(tmp_path) -> None:
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    authority = _source_authority(tmp_path)
    inventory = create_inventory(tmp_path, commit="commit-a", authority=authority)
    (tmp_path / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="tracked source"):
        verify_inventory(tmp_path, inventory, authority=authority)


def test_regenerated_artifact_inventory_cannot_bless_tampered_source(tmp_path) -> None:
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    authority = _source_authority(tmp_path)
    inventory = create_inventory(tmp_path, commit="commit-a", authority=authority)
    (tmp_path / "app.py").write_text("TAMPERED = True\n", encoding="utf-8")
    (tmp_path / "release-file-inventory.sha256").write_text(
        f"{_digest(tmp_path / 'app.py')}  ./app.py\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="tracked source"):
        verify_inventory(tmp_path, inventory, authority=authority)


def test_wrong_remote_git_tree_sha_fails(tmp_path) -> None:
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    authority = _source_authority(tmp_path)
    inventory = create_inventory(tmp_path, commit="commit-a", authority=authority)
    wrong = {**authority, "tree_sha": "different-tree"}
    with pytest.raises(ValueError, match="tree SHA"):
        verify_inventory(tmp_path, inventory, authority=wrong)


def test_missing_tracked_file_fails(tmp_path) -> None:
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    authority = _source_authority(tmp_path)
    inventory = create_inventory(tmp_path, commit="commit-a", authority=authority)
    (tmp_path / "app.py").unlink()
    with pytest.raises(ValueError, match="missing"):
        verify_inventory(tmp_path, inventory, authority=authority)


def test_generated_artifact_evidence_is_outside_tracked_source_inventory(tmp_path) -> None:
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    authority = _source_authority(tmp_path)
    inventory = create_inventory(tmp_path, commit="commit-a", authority=authority)
    (tmp_path / "artifact-test-results.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "dependency-inventory.txt").write_text("demo==1\n", encoding="utf-8")
    assert verify_inventory(tmp_path, inventory, authority=authority)["verified"] is True


def test_forward_deployment_requires_exact_current_main(monkeypatch) -> None:
    monkeypatch.setattr(deployment_authority, "github_tree", lambda *_: {"commit": "commit-a", "tree_sha": "tree-a", "files": []})
    monkeypatch.setattr(
        deployment_authority,
        "_github_json",
        lambda _url: ({"object": {"sha": "commit-a"}}, None, 200),
    )
    result = deployment_authority.verify(
        {"release_commit": "commit-a", "git_tree_sha": "tree-a", "tracked_source_inventory_digest": "inventory-a",
         "release_authority": {"mode": "forward", "source_tree_sha": "tree-a", "tracked_source_inventory_digest": "inventory-a"}},
        mode="forward",
    )
    assert result["remote_main_sha"] == "commit-a"


def test_unauthorized_old_ancestor_cannot_deploy_forward(monkeypatch) -> None:
    monkeypatch.setattr(deployment_authority, "github_tree", lambda *_: {"commit": "old-ancestor", "tree_sha": "tree-old", "files": []})
    monkeypatch.setattr(
        deployment_authority,
        "_github_json",
        lambda _url: ({"object": {"sha": "new-main"}}, None, 200),
    )
    with pytest.raises(RuntimeError, match="exact current GitHub main"):
        deployment_authority.verify(
            {"release_commit": "old-ancestor", "git_tree_sha": "tree-old", "tracked_source_inventory_digest": "inventory-old",
             "release_authority": {"mode": "forward", "source_tree_sha": "tree-old", "tracked_source_inventory_digest": "inventory-old"}},
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
        "git_tree_sha": "rollback-tree",
        "tracked_source_inventory_digest": "rollback-inventory",
        "release_authority": {
            "mode": "rollback",
            "tag_name": "immutable-release-good",
            "attestation": evidence,
            "source_tree_sha": "rollback-tree",
            "tracked_source_inventory_digest": "rollback-inventory",
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
    monkeypatch.setattr(deployment_authority, "github_tree", lambda *_: {"commit": "rollback-commit", "tree_sha": "rollback-tree", "files": []})
    monkeypatch.setattr(deployment_authority, "_release_attestation_asset", lambda *_: {
        **evidence,
        "release_immutable": True,
        "manifest": {"release_commit": "rollback-commit", "git_tree_sha": "rollback-tree", "tracked_source_inventory_digest": "rollback-inventory"},
    })
    result = deployment_authority.verify(manifest, mode="rollback")
    assert result["asset_id"] == 8


def test_replaced_rollback_attestation_asset_is_rejected(monkeypatch) -> None:
    manifest, evidence = _rollback_manifest()
    monkeypatch.setattr(deployment_authority, "_github_json", _rollback_github)
    monkeypatch.setattr(deployment_authority, "github_tree", lambda *_: {"commit": "rollback-commit", "tree_sha": "rollback-tree", "files": []})
    monkeypatch.setattr(deployment_authority, "_release_attestation_asset", lambda *_: {
        **evidence,
        "asset_id": 999,
        "release_immutable": True,
        "manifest": {"release_commit": "rollback-commit", "git_tree_sha": "rollback-tree", "tracked_source_inventory_digest": "rollback-inventory"},
    })
    with pytest.raises(RuntimeError, match="asset_id was replaced"):
        deployment_authority.verify(manifest, mode="rollback")
