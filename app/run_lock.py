from __future__ import annotations

import argparse
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LockInspection:
    state: str
    pid: int | None
    age_seconds: float
    reason: str


@dataclass(frozen=True)
class ProcessIdentity:
    command: str
    start_token: str


def _pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _process_identity(pid: int) -> ProcessIdentity | None:
    """Return non-sensitive OS process identity used to detect PID reuse."""
    try:
        result = subprocess.run(
            ["/bin/ps", "-p", str(pid), "-o", "command=", "-o", "lstart="],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    line = result.stdout.strip()
    # lstart is the final five whitespace-separated fields on macOS/Linux ps.
    parts = line.rsplit(maxsplit=5)
    if len(parts) < 6:
        return ProcessIdentity(line, "unknown")
    return ProcessIdentity(parts[0], " ".join(parts[1:]))


def write_owner_metadata(path: str | Path, pid: int, repository_path: str, commit: str) -> None:
    lock = Path(path)
    identity = _process_identity(pid)
    values = {
        "pid": str(pid),
        "started_at_epoch": str(int(time.time())),
        "repository_path": str(Path(repository_path).resolve()),
        "commit": commit or "unknown",
        "command_identity": identity.command if identity else "unknown",
        "process_start_token": identity.start_token if identity else "unknown",
    }
    for name, value in values.items():
        (lock / name).write_text(value + "\n", encoding="utf-8")


def inspect_lock(
    path: str | Path,
    now: float | None = None,
    dead_pid_grace_seconds: float = 120.0,
    malformed_grace_seconds: float = 1200.0,
    *,
    expected_command: str | None = None,
    expected_repository: str | Path | None = None,
    expected_commit: str | None = None,
) -> LockInspection:
    lock = Path(path)
    now = time.time() if now is None else now
    try:
        pid = int((lock / "pid").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        pid = None
    try:
        started = float((lock / "started_at_epoch").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        try:
            started = lock.stat().st_mtime
        except OSError:
            return LockInspection("missing", None, 0.0, "lock directory disappeared")
    age = max(0.0, now - started)
    if pid is not None and _pid_exists(pid):
        identity = _process_identity(pid)
        recorded_command = _read(lock / "command_identity")
        recorded_start = _read(lock / "process_start_token")
        if identity and recorded_start not in (None, "unknown") and identity.start_token != recorded_start:
            if age >= dead_pid_grace_seconds:
                return LockInspection("stale", pid, age, "PID was reused by a different process start")
            return LockInspection("recent_unknown", pid, age, "PID start identity changed inside grace period")
        command = identity.command if identity else recorded_command
        if expected_command and command not in (None, "unknown") and expected_command not in command:
            if age >= dead_pid_grace_seconds:
                return LockInspection("stale", pid, age, "PID command does not identify the listener")
            return LockInspection("recent_unknown", pid, age, "PID command mismatch inside grace period")
        mismatches: list[str] = []
        repository = _read(lock / "repository_path")
        commit = _read(lock / "commit")
        if expected_repository and repository and Path(repository).resolve() != Path(expected_repository).resolve():
            mismatches.append("repository")
        if expected_commit and commit not in (None, "unknown", expected_commit):
            mismatches.append("commit")
        suffix = f"; live owner metadata mismatch: {','.join(mismatches)}" if mismatches else ""
        return LockInspection("active", pid, age, "owner PID and process identity are running" + suffix)
    grace = dead_pid_grace_seconds if pid is not None else malformed_grace_seconds
    if age >= grace:
        return LockInspection("stale", pid, age, "owner PID is absent and lock exceeded grace period")
    return LockInspection("recent_unknown", pid, age, "owner is absent but lock is inside grace period")


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("lockdir", type=Path)
    parser.add_argument("--expected-command")
    parser.add_argument("--expected-repository")
    parser.add_argument("--expected-commit")
    parser.add_argument("--write-owner", action="store_true")
    parser.add_argument("--pid", type=int)
    args = parser.parse_args()
    if args.write_owner:
        if args.pid is None or not args.expected_repository:
            parser.error("--write-owner requires --pid and --expected-repository")
        write_owner_metadata(args.lockdir, args.pid, args.expected_repository, args.expected_commit or "unknown")
        return
    result = inspect_lock(
        args.lockdir,
        expected_command=args.expected_command,
        expected_repository=args.expected_repository,
        expected_commit=args.expected_commit,
    )
    print(result.state)
    raise SystemExit({"active": 0, "stale": 10, "recent_unknown": 11, "missing": 12}[result.state])


if __name__ == "__main__":
    main()
