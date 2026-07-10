#!/usr/bin/env python3
"""Offline old/new compatibility matrix using temporary worktrees and cloned DBs."""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = "cddfe4c6a656a78dcdba5015a752bfab59c8dc7e"

def run(cwd: Path, code: str, db: Path) -> dict:
    env = {**os.environ, "TRADING_AGENT_TESTING": "1", "TRADING_AGENT_DATABASE_PATH": str(db), "PYTHONPATH": str(cwd)}
    result = subprocess.run([sys.executable, "-c", code], cwd=cwd, env=env, text=True, capture_output=True, timeout=30)
    return {"returncode": result.returncode, "stdout": result.stdout.strip()[-500:], "stderr": result.stderr.strip()[-500:]}

def backup(source: Path, target: Path) -> None:
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as src, sqlite3.connect(target) as dst: src.backup(dst)

def main() -> int:
    with tempfile.TemporaryDirectory(prefix="phase0-compat-") as temp:
        temp = Path(temp); old = Path("/tmp/tradingagent-old"); new = Path("/tmp/tradingagent-new")
        for worktree in (old, new):
            if worktree.exists(): subprocess.run(["git", "-C", str(ROOT), "worktree", "remove", "--force", str(worktree)], check=False)
        subprocess.run(["git", "-C", str(ROOT), "worktree", "add", "--detach", str(old), BASELINE], check=True)
        subprocess.run(["git", "-C", str(ROOT), "worktree", "add", "--detach", str(new), "HEAD"], check=True)
        try:
            original = temp / "db_original_clone.sqlite"; migrated = temp / "db_migrated_clone.sqlite"; restored = temp / "db_restored_clone.sqlite"
            # A minimal original-schema clone is created by the baseline application.
            baseline_db = temp / "baseline.sqlite"
            run(old, "from app.storage import Storage; Storage(r'%s').initialize()" % baseline_db, baseline_db)
            backup(baseline_db, original); backup(baseline_db, migrated); backup(baseline_db, restored)
            matrix = {
                "old_original": run(old, "from app.storage import Storage; s=Storage(r'%s'); s.initialize(); print(s.fetch_all('SELECT name FROM sqlite_master LIMIT 1'))" % original, original),
                "new_original_blocks": run(new, "from app.storage import Storage; s=Storage(r'%s');\ntry: s.require_runtime_schema()\nexcept RuntimeError as e: print(str(e))" % original, original),
            }
            matrix["new_migrated"] = run(new, "from app.storage import Storage; s=Storage(r'%s'); s.apply_explicit_migrations(); s.require_runtime_schema(); print(s.fetch_all('SELECT COUNT(*) n FROM trade_proposals')[0]['n'])" % migrated, migrated)
            matrix["old_migrated"] = run(old, "from app.storage import Storage; s=Storage(r'%s'); print(s.fetch_all('SELECT COUNT(*) n FROM trade_proposals')[0]['n'])" % migrated, migrated)
            matrix["old_restored"] = run(old, "from app.storage import Storage; s=Storage(r'%s'); s.initialize(); print(s.fetch_all('SELECT COUNT(*) n FROM trade_proposals')[0]['n'])" % restored, restored)
            print(matrix)
            return 0 if all(value["returncode"] == 0 for value in matrix.values()) else 1
        finally:
            for worktree in (old, new): subprocess.run(["git", "-C", str(ROOT), "worktree", "remove", "--force", str(worktree)], check=False)

if __name__ == "__main__": raise SystemExit(main())
