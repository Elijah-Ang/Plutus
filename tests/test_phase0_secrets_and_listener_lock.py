from __future__ import annotations

import os
import logging
import subprocess
import threading
import time
from dataclasses import dataclass

import pytest
import requests

import app.run_lock as run_lock
from app.secrets import SecretStore, SecretValue
from app.utils import redact, redact_exception, redact_sensitive_url


def test_secret_value_never_displays_plaintext():
    value = SecretValue("synthetic-super-secret", "test")
    assert "synthetic-super-secret" not in str(value)
    assert "synthetic-super-secret" not in repr(value)
    assert value.reveal() == "synthetic-super-secret"


def test_secret_store_is_environment_first_and_testing_never_calls_keychain():
    calls: list[str] = []
    store = SecretStore(
        {"TRADING_AGENT_TESTING": "1", "ALPACA_API_KEY": "synthetic-value"},
        lambda name: calls.append(name) or "must-not-be-read",
    )
    assert store.get_plaintext("ALPACA_API_KEY") == "synthetic-value"
    assert store.get_plaintext("OPENAI_API_KEY") is None
    assert calls == []


def test_secret_store_keychain_boundary_is_injectable_without_real_access():
    store = SecretStore({}, lambda name: "synthetic-keychain-value")
    assert store.get("OPENAI_API_KEY").source == "keychain"
    assert store.get_plaintext("OPENAI_API_KEY") == "synthetic-keychain-value"
    with pytest.raises(ValueError):
        store.get("not a safe name")


@pytest.mark.parametrize(
    "text, needle",
    [
        ("https://api.telegram.org/bot123:abc_DEF/sendMessage", "123:abc_DEF"),
        ("https://host/x?api_key=abc123&token=def456", "abc123"),
        ("Authorization: Bearer bearer-value", "bearer-value"),
        ("ALPACA_SECRET_KEY=secret-value", "secret-value"),
        ('{"password":"json-value"}', "json-value"),
    ],
)
def test_central_redactor_covers_supported_secret_shapes(text, needle):
    assert needle not in redact_sensitive_url(text)


def _write_lock(lock, pid: int, *, age: float = 300, command="listener-command", start="start-a", repo=None, commit="old"):
    lock.mkdir()
    (lock / "pid").write_text(f"{pid}\n")
    (lock / "started_at_epoch").write_text(f"{time.time() - age}\n")
    (lock / "command_identity").write_text(command + "\n")
    (lock / "process_start_token").write_text(start + "\n")
    (lock / "repository_path").write_text(str(repo or lock.parent) + "\n")
    (lock / "commit").write_text(commit + "\n")


def test_dead_listener_lock_becomes_reclaimable_after_grace(tmp_path, monkeypatch):
    lock = tmp_path / "listener.lockdir"
    _write_lock(lock, 987654)
    monkeypatch.setattr(run_lock, "_pid_exists", lambda pid: False)
    assert run_lock.inspect_lock(lock, dead_pid_grace_seconds=120).state == "stale"


def test_recent_dead_listener_lock_is_not_reclaimed(tmp_path, monkeypatch):
    lock = tmp_path / "listener.lockdir"
    _write_lock(lock, 987654, age=10)
    monkeypatch.setattr(run_lock, "_pid_exists", lambda pid: False)
    assert run_lock.inspect_lock(lock, dead_pid_grace_seconds=120).state == "recent_unknown"


def test_pid_reuse_is_not_accepted_as_listener_owner(tmp_path, monkeypatch):
    lock = tmp_path / "listener.lockdir"
    _write_lock(lock, 42, start="original-start")
    monkeypatch.setattr(run_lock, "_pid_exists", lambda pid: True)
    monkeypatch.setattr(run_lock, "_process_identity", lambda pid: run_lock.ProcessIdentity("other-command", "reused-start"))
    result = run_lock.inspect_lock(lock, expected_command="listener-command")
    assert result.state == "stale" and "reused" in result.reason


def test_live_listener_from_old_commit_is_reported_but_never_reclaimed(tmp_path, monkeypatch):
    lock = tmp_path / "listener.lockdir"
    _write_lock(lock, 42, command="run_telegram_listener.sh", repo=tmp_path, commit="old", start="same-start")
    monkeypatch.setattr(run_lock, "_pid_exists", lambda pid: True)
    monkeypatch.setattr(run_lock, "_process_identity", lambda pid: run_lock.ProcessIdentity("/bin/zsh run_telegram_listener.sh", "same-start"))
    result = run_lock.inspect_lock(
        lock,
        expected_command="run_telegram_listener.sh",
        expected_repository=tmp_path,
        expected_commit="new",
    )
    assert result.state == "active"
    assert "commit" in result.reason


def test_real_temp_process_identity_distinguishes_live_and_dead_lock(tmp_path):
    process = subprocess.Popen(["/bin/sleep", "5"])
    lock = tmp_path / "listener.lockdir"
    lock.mkdir()
    try:
        run_lock.write_owner_metadata(lock, process.pid, str(tmp_path), "test-commit")
        live = run_lock.inspect_lock(lock, expected_command="sleep", expected_repository=tmp_path, expected_commit="test-commit")
        assert live.state == "active"
    finally:
        process.terminate()
        process.wait(timeout=5)
    dead = run_lock.inspect_lock(lock, now=time.time() + 121, dead_pid_grace_seconds=120)
    assert dead.state == "stale"


def test_test_environment_contains_only_synthetic_credentials():
    assert os.environ["TRADING_AGENT_TESTING"] == "1"
    for name in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "TELEGRAM_BOT_TOKEN", "OPENAI_API_KEY", "EODHD_API_TOKEN"):
        assert os.environ[name].startswith(("synthetic-", "999999:synthetic-"))


def test_redaction_01_full_telegram_url_exception():
    error = RuntimeError("https://api.telegram.org/bot999999:synthetic-token/sendMessage")
    assert "synthetic-token" not in redact_exception(error)


def test_redaction_02_nested_exception_chain():
    try:
        try:
            raise ValueError("Authorization: Bearer nested-secret")
        except ValueError as inner:
            raise RuntimeError("outer retry failed") from inner
    except RuntimeError as outer:
        safe = redact_exception(outer)
    assert "nested-secret" not in safe and "RuntimeError" in safe and "ValueError" in safe


def test_redaction_03_requests_exception_request_url():
    request = requests.Request("GET", "https://example.test/path?api_key=request-secret").prepare()
    safe = redact_exception(requests.RequestException(f"failed {request.url}", request=request))
    assert "request-secret" not in safe


def test_redaction_04_logging_dictionary_headers():
    value = redact({"headers": {"Authorization": "Bearer header-secret", "X-Trace": "safe"}})
    assert value["headers"]["Authorization"] == "[REDACTED]"
    assert value["headers"]["X-Trace"] == "safe"


def test_redaction_05_dataclass_object_repr():
    @dataclass
    class Envelope:
        credential: SecretValue

    rendered = repr(Envelope(SecretValue("object-secret", "test")))
    assert "object-secret" not in rendered and "REDACTED" in rendered


def test_redaction_06_json_logging_payload():
    safe = redact_sensitive_url('{"authorization":"Bearer json-secret","message":"ok"}')
    assert "json-secret" not in safe and '"message":"ok"' in safe


def test_redaction_07_retry_diagnostics():
    safe = redact_exception(RuntimeError("retry=3 token=retry-secret"), ["retry-secret"])
    assert "retry-secret" not in safe and "retry=3" in safe


def test_redaction_08_configuration_validation_error():
    safe = redact_exception(ValueError("invalid OPENAI_API_KEY=config-secret"))
    assert "config-secret" not in safe


def test_redaction_09_broker_error_credentials():
    safe = redact_exception(RuntimeError("ALPACA_SECRET_KEY=broker-secret account request failed"))
    assert "broker-secret" not in safe


def test_redaction_10_token_query_string_and_database_url():
    safe = redact_sensitive_url("https://x.test?a=1&token=query-secret postgresql://user:db-secret@host/db")
    assert "query-secret" not in safe and "db-secret" not in safe


def test_redaction_11_multiple_registered_secrets():
    safe = redact_sensitive_url("first-value and second-value", ["first-value", "second-value"])
    assert "first-value" not in safe and "second-value" not in safe


def test_redaction_12_secret_like_but_nonsensitive_text_is_not_overmatched():
    text = "tokenization and secretariat are ordinary words"
    assert redact_sensitive_url(text) == text


def test_synthetic_secret_never_appears_in_captured_log(caplog):
    synthetic = "known-synthetic-log-secret"
    with caplog.at_level(logging.WARNING):
        logging.getLogger("phase0-security-test").warning("%s", redact_sensitive_url(synthetic, [synthetic]))
    assert synthetic not in caplog.text


def test_listener_live_expected_process_is_active(tmp_path, monkeypatch):
    lock = tmp_path / "listener.lockdir"
    _write_lock(lock, 42, command="run_telegram_listener.sh", start="same")
    monkeypatch.setattr(run_lock, "_pid_exists", lambda pid: True)
    monkeypatch.setattr(run_lock, "_process_identity", lambda pid: run_lock.ProcessIdentity("run_telegram_listener.sh", "same"))
    assert run_lock.inspect_lock(lock, expected_command="run_telegram_listener.sh").state == "active"


def test_listener_live_wrong_command_is_stale_after_grace(tmp_path, monkeypatch):
    lock = tmp_path / "listener.lockdir"
    _write_lock(lock, 42, command="unrelated", start="same")
    monkeypatch.setattr(run_lock, "_pid_exists", lambda pid: True)
    monkeypatch.setattr(run_lock, "_process_identity", lambda pid: run_lock.ProcessIdentity("unrelated", "same"))
    assert run_lock.inspect_lock(lock, expected_command="run_telegram_listener.sh").state == "stale"


def test_listener_malformed_metadata_and_absent_lock(tmp_path):
    malformed = tmp_path / "malformed.lockdir"; malformed.mkdir()
    (malformed / "pid").write_text("not-a-pid")
    (malformed / "started_at_epoch").write_text("1")
    assert run_lock.inspect_lock(malformed, now=5000, malformed_grace_seconds=10).state == "stale"
    assert run_lock.inspect_lock(tmp_path / "absent.lockdir").state == "missing"


def test_listener_repository_mismatch_does_not_displace_live_owner(tmp_path, monkeypatch):
    lock = tmp_path / "listener.lockdir"
    _write_lock(lock, 42, command="run_telegram_listener.sh", start="same", repo=tmp_path / "other")
    monkeypatch.setattr(run_lock, "_pid_exists", lambda pid: True)
    monkeypatch.setattr(run_lock, "_process_identity", lambda pid: run_lock.ProcessIdentity("run_telegram_listener.sh", "same"))
    result = run_lock.inspect_lock(lock, expected_command="run_telegram_listener.sh", expected_repository=tmp_path)
    assert result.state == "active" and "repository" in result.reason


def test_listener_rapid_restart_new_owner_is_accepted(tmp_path, monkeypatch):
    lock = tmp_path / "listener.lockdir"
    _write_lock(lock, 41, age=500, command="run_telegram_listener.sh", start="old")
    monkeypatch.setattr(run_lock, "_pid_exists", lambda pid: False)
    assert run_lock.inspect_lock(lock, dead_pid_grace_seconds=10).state == "stale"
    for child in lock.iterdir(): child.unlink()
    lock.rmdir()
    monkeypatch.setattr(run_lock, "_pid_exists", lambda pid: True)
    monkeypatch.setattr(run_lock, "_process_identity", lambda pid: run_lock.ProcessIdentity("run_telegram_listener.sh", "new"))
    _write_lock(lock, 42, age=0, command="run_telegram_listener.sh", start="new")
    assert run_lock.inspect_lock(lock, expected_command="run_telegram_listener.sh").state == "active"


def test_two_listener_starts_have_exactly_one_atomic_mkdir_winner(tmp_path):
    lock = tmp_path / "listener.lockdir"
    barrier = threading.Barrier(2)
    outcomes = []

    def start():
        barrier.wait()
        try:
            lock.mkdir()
            outcomes.append("winner")
        except FileExistsError:
            outcomes.append("blocked")

    workers = [threading.Thread(target=start) for _ in range(2)]
    for worker in workers: worker.start()
    for worker in workers: worker.join()
    assert sorted(outcomes) == ["blocked", "winner"]


def test_interrupted_stale_cleanup_remains_safely_reclaimable(tmp_path, monkeypatch):
    lock = tmp_path / "listener.lockdir"
    _write_lock(lock, 999999, age=500)
    (lock / "cleanup_started").write_text("interrupted")
    monkeypatch.setattr(run_lock, "_pid_exists", lambda pid: False)
    assert run_lock.inspect_lock(lock, dead_pid_grace_seconds=10).state == "stale"
