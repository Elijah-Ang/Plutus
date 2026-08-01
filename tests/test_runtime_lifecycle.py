from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from app.preflight import run_core_preflight
from app import runtime_guard
from app.telegram_bot import TelegramBot
from app.utils import (
    PROJECT_ROOT,
    kill_switch_active,
    kill_switch_path,
    set_kill_switch,
)


ROOT = Path(__file__).resolve().parents[1]
ZSH = shutil.which("zsh")


def _production_state(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    state_root = tmp_path / "state"
    runtime = state_root / "runtime"
    runtime.mkdir(parents=True)
    runtime.chmod(0o700)
    monkeypatch.setenv("TRADING_AGENT_RUNTIME", "production-paper")
    monkeypatch.setenv("TRADING_AGENT_STATE_ROOT", str(state_root))
    return state_root, runtime


def _production_runtime_authority(
    tmp_path: Path, monkeypatch, **manifest_overrides
) -> tuple[Path, Path, dict]:
    release_root = tmp_path / "releases"
    release = release_root / "release-1"
    release.mkdir(parents=True)
    runtime_link = tmp_path / "TradingAgentRuntime"
    runtime_link.symlink_to(release, target_is_directory=True)
    state_root = tmp_path / "state"
    runtime_state = state_root / "runtime"
    runtime_state.mkdir(parents=True)
    runtime_state.chmod(0o700)
    manifest = {
        "release_id": "release-1",
        "release_commit": "a" * 40,
        "mode": "paper",
        "manual_approval_only": True,
        "live_capability": False,
        "tests_verified": True,
        "python_version": "3.13.9",
        "schema_version": runtime_guard.REQUIRED_SCHEMA_VERSION,
        "requirements_lock_sha256": "e" * 64,
        "requirements_hash_lock_sha256": "f" * 64,
        "dependency_inventory_sha256": "1" * 64,
        "artifact_test_results_sha256": "2" * 64,
        "tracked_source_inventory_sha256": "3" * 64,
        "wheel_build_evidence_sha256": "4" * 64,
        "release_wheel_sha256": "5" * 64,
        "release_file_inventory_sha256": "6" * 64,
        "release_wheel_filename": "trading-agent-0.1.0-py3-none-any.whl",
        "distribution_name": "trading-agent",
        "distribution_version": "0.1.0",
        **manifest_overrides,
    }
    (release / "release-manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    monkeypatch.setattr(runtime_guard, "RUNTIME_LINK", runtime_link)
    monkeypatch.setattr(runtime_guard, "RELEASE_ROOT", release_root)
    monkeypatch.delenv("TRADING_AGENT_TESTING", raising=False)
    monkeypatch.setenv("TRADING_AGENT_RUNTIME", "production-paper")
    monkeypatch.setenv("TRADING_AGENT_RELEASE_ID", "release-1")
    monkeypatch.setenv("TRADING_AGENT_STATE_ROOT", str(state_root))
    monkeypatch.chdir(release)
    return release, runtime_state, manifest


def test_production_kill_switch_is_external_owner_only_and_durable(
    tmp_path: Path, monkeypatch
) -> None:
    _state_root, runtime = _production_state(tmp_path, monkeypatch)

    path = kill_switch_path()
    assert path == runtime / "KILL_SWITCH"
    assert PROJECT_ROOT not in path.parents
    assert kill_switch_active() is False

    assert set_kill_switch(True) == path
    assert path.is_file()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert kill_switch_active() is True

    assert set_kill_switch(False) == path
    assert not path.exists()
    assert kill_switch_active() is False


def test_production_kill_switch_fails_closed_when_runtime_state_is_unsafe(
    tmp_path: Path, monkeypatch
) -> None:
    _state_root, runtime = _production_state(tmp_path, monkeypatch)
    runtime.chmod(0o755)

    assert kill_switch_active() is True
    with pytest.raises(RuntimeError, match="owner-only"):
        set_kill_switch(True)


def test_production_kill_switch_refuses_symlink(
    tmp_path: Path, monkeypatch
) -> None:
    _state_root, runtime = _production_state(tmp_path, monkeypatch)
    target = tmp_path / "target"
    target.write_text("", encoding="utf-8")
    (runtime / "KILL_SWITCH").symlink_to(target)

    assert kill_switch_active() is True
    with pytest.raises(RuntimeError, match="symlink"):
        set_kill_switch(True)
    with pytest.raises(RuntimeError, match="symlink"):
        set_kill_switch(False)


def test_production_kill_switch_refuses_non_regular_path(
    tmp_path: Path, monkeypatch
) -> None:
    _state_root, runtime = _production_state(tmp_path, monkeypatch)
    (runtime / "KILL_SWITCH").mkdir()

    assert kill_switch_active() is True
    with pytest.raises(RuntimeError, match="regular file"):
        set_kill_switch(True)


def test_development_kill_switch_remains_repository_local(monkeypatch) -> None:
    monkeypatch.delenv("TRADING_AGENT_RUNTIME", raising=False)
    monkeypatch.delenv("TRADING_AGENT_STATE_ROOT", raising=False)
    assert kill_switch_path() == PROJECT_ROOT / "config" / "KILL_SWITCH"


def test_telegram_pause_and_exact_resume_use_external_switch(
    tmp_path: Path, monkeypatch
) -> None:
    _state_root, runtime = _production_state(tmp_path, monkeypatch)
    bot = object.__new__(TelegramBot)
    bot.allowed_user_id = "123"

    assert "enabled" in bot.handle_command("/pause", sender_id="123")
    assert (runtime / "KILL_SWITCH").is_file()
    assert not (PROJECT_ROOT / "config" / "KILL_SWITCH").exists()

    assert "Exact phrase required" in bot.handle_command("/resume", sender_id="123")
    assert (runtime / "KILL_SWITCH").is_file()
    assert "cleared" in bot.handle_command(
        "/resume CONFIRM PAPER RESUME", sender_id="123"
    )
    assert not (runtime / "KILL_SWITCH").exists()


def test_unauthorized_telegram_user_cannot_change_external_switch(
    tmp_path: Path, monkeypatch
) -> None:
    _production_state(tmp_path, monkeypatch)
    bot = object.__new__(TelegramBot)
    bot.allowed_user_id = "123"

    assert "Unauthorized" in bot.handle_command("/pause", sender_id="999")
    assert kill_switch_active() is False


def test_core_preflight_reads_external_switch(tmp_path: Path, monkeypatch) -> None:
    _production_state(tmp_path, monkeypatch)

    class StorageStub:
        @staticmethod
        def writable() -> bool:
            return True

        @staticmethod
        def expire_proposals() -> int:
            return 0

    config = {"mode": "paper", "live_enabled": False}
    assert run_core_preflight(config, StorageStub()).passed is True
    set_kill_switch(True)
    result = run_core_preflight(config, StorageStub())
    assert result.passed is False
    assert next(check for check in result.checks if check.name == "core_kill_switch").passed is False


def test_production_startup_accepts_exact_manual_only_release_authority(
    tmp_path: Path, monkeypatch
) -> None:
    _release, _runtime_state, manifest = _production_runtime_authority(
        tmp_path, monkeypatch
    )
    assert runtime_guard.validate_production_runtime() == manifest


@pytest.mark.parametrize(
    ("override", "message"),
    (
        ({"manual_approval_only": False}, "manual-only"),
        ({"live_capability": True}, "manual-only"),
        ({"tests_verified": False}, "tests are not verified"),
        ({"python_version": "3.13.8"}, "Python identity"),
        ({"release_file_inventory_sha256": None}, "artifact evidence hash"),
        ({"release_commit": "wrong"}, "commit identity"),
    ),
)
def test_production_startup_rejects_weakened_release_authority(
    tmp_path: Path, monkeypatch, override: dict, message: str
) -> None:
    _production_runtime_authority(tmp_path, monkeypatch, **override)
    with pytest.raises(runtime_guard.RuntimeGuardError, match=message):
        runtime_guard.validate_production_runtime()


def test_production_startup_rejects_permissive_runtime_state(
    tmp_path: Path, monkeypatch
) -> None:
    _release, runtime_state, _manifest = _production_runtime_authority(
        tmp_path, monkeypatch
    )
    runtime_state.chmod(0o755)
    with pytest.raises(runtime_guard.RuntimeGuardError, match="owner-only"):
        runtime_guard.validate_production_runtime()


def test_application_has_no_release_local_kill_switch_authority() -> None:
    for relative in (
        "app/preflight.py",
        "app/service.py",
        "app/telegram_bot.py",
        "scripts/test_paper_order_proposal.py",
        "scripts/test_paper_sell_proposal.py",
    ):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert 'PROJECT_ROOT / "config" / "KILL_SWITCH"' not in text


def test_listener_restart_is_immutable_authority_only() -> None:
    text = (ROOT / "scripts" / "restart_telegram_listener.sh").read_text(
        encoding="utf-8"
    )
    for required in (
        "active immutable runtime",
        "verify_source_tree.py",
        "verify_release_artifact.py",
        "verify_deployment_authority.py",
        "release-file-inventory.sha256",
        "cmp -s",
        "launchctl bootout",
        "launchctl bootstrap",
        "check_runtime_freshness.sh",
    ):
        assert required in text
    for forbidden in (
        "kill -9",
        "kill -15",
        "xargs kill",
        "ps -ef",
        "git rev-parse",
        'ROOT/logs/runtime',
        "rmdir \"$LOCKDIR\"",
    ):
        assert forbidden not in text


def test_source_push_never_restarts_production() -> None:
    text = (ROOT / "scripts" / "safe_commit_push.sh").read_text(encoding="utf-8")
    assert "Source-tree pushes cannot restart" in text
    assert "launchctl" not in text
    assert '"$ROOT/scripts/restart_telegram_listener.sh"' not in text


def test_pause_resume_wrappers_only_manage_external_switch() -> None:
    stop = (ROOT / "scripts" / "stop_agent.sh").read_text(encoding="utf-8")
    start = (ROOT / "scripts" / "start_agent.sh").read_text(encoding="utf-8")
    manager = (ROOT / "scripts" / "manage_kill_switch.sh").read_text(
        encoding="utf-8"
    )
    assert "manage_kill_switch.sh\" enable" in stop
    assert "CONFIRM PAPER RESUME" in start
    assert "manage_kill_switch.sh\" disable" in start
    assert 'if [[ "${1:-}" == "disable" ]]' in manager
    assert "verify_source_tree.py" in manager
    assert "verify_release_artifact.py" in manager
    assert "verify_deployment_authority.py" in manager
    for text in (stop, start):
        assert "app.main" not in text
        assert "launchctl" not in text
        assert "config/KILL_SWITCH" not in text


def test_launchd_install_and_deploy_reject_symlink_targets() -> None:
    installer = (ROOT / "scripts" / "install_launchd.sh").read_text(
        encoding="utf-8"
    )
    deploy = (ROOT / "scripts" / "deploy_release.sh").read_text(encoding="utf-8")
    assert "active immutable release" in installer
    assert "verify_release_artifact.py" in installer
    assert "[[ ! -L \"$target\" ]]" in installer
    assert "scanner plist target is a symlink" in deploy
    assert "listener plist target is a symlink" in deploy
    assert "legacy active kill switch must be preserved" in deploy
    assert "external runtime state directory must be owner-only" in deploy
    assert deploy.index("/usr/bin/install -m 600") < deploy.index('ln -sfn "$RELEASE" "$RUNTIME"')


@pytest.mark.skipif(ZSH is None, reason="macOS runtime lifecycle requires zsh")
def test_emergency_pause_works_without_release_authority(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    runtime = home / "Library" / "Application Support" / "TradingAgent" / "runtime"
    runtime.mkdir(parents=True)
    runtime.chmod(0o700)
    environment = {**os.environ, "HOME": str(home)}

    result = subprocess.run(
        [str(ZSH), str(ROOT / "scripts" / "stop_agent.sh")],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    switch = runtime / "KILL_SWITCH"
    assert switch.is_file()
    assert stat.S_IMODE(switch.stat().st_mode) == 0o600


@pytest.mark.skipif(ZSH is None, reason="macOS runtime lifecycle requires zsh")
def test_resume_without_active_release_authority_fails_closed(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    runtime = home / "Library" / "Application Support" / "TradingAgent" / "runtime"
    runtime.mkdir(parents=True)
    runtime.chmod(0o700)
    switch = runtime / "KILL_SWITCH"
    switch.write_text("", encoding="utf-8")
    switch.chmod(0o600)
    environment = {**os.environ, "HOME": str(home)}

    result = subprocess.run(
        [
            str(ZSH),
            str(ROOT / "scripts" / "start_agent.sh"),
            "CONFIRM PAPER RESUME",
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "active runtime pointer is not a symlink" in result.stderr
    assert switch.is_file()
