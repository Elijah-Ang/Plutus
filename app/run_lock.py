from __future__ import annotations

import argparse
import os
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LockInspection:
    state: str
    pid: int | None
    age_seconds: float
    reason: str


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


def inspect_lock(path: str | Path, now: float | None = None, dead_pid_grace_seconds: float = 120.0, malformed_grace_seconds: float = 1200.0) -> LockInspection:
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
        return LockInspection("active", pid, age, "owner PID is running")
    grace = dead_pid_grace_seconds if pid is not None else malformed_grace_seconds
    if age >= grace:
        return LockInspection("stale", pid, age, "owner PID is absent and lock exceeded grace period")
    return LockInspection("recent_unknown", pid, age, "owner is absent but lock is inside grace period")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("lockdir", type=Path)
    args = parser.parse_args()
    result = inspect_lock(args.lockdir)
    print(result.state)
    raise SystemExit({"active": 0, "stale": 10, "recent_unknown": 11, "missing": 12}[result.state])


if __name__ == "__main__":
    main()
