#!/usr/bin/env python3
"""Inspect or change the external production-paper kill switch."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.utils import (  # noqa: E402
    kill_switch_active,
    kill_switch_path,
    load_config,
    set_kill_switch,
)
from app.runtime_guard import paper_authority_mode  # noqa: E402


RESUME_CONFIRMATION = "CONFIRM PAPER RESUME"


def _release_manifest() -> dict:
    manifest_path = ROOT / "release-manifest.json"
    if not manifest_path.is_file():
        raise RuntimeError("kill-switch management requires an immutable release manifest")
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def _require_paper_manual_runtime(manifest: dict) -> None:
    config = load_config()
    if (
        manifest.get("mode") != "paper"
        or paper_authority_mode(manifest) is None
        or manifest.get("live_capability") is not False
        or config.get("mode") != "paper"
        or config.get("live_enabled") is not False
    ):
        raise RuntimeError("kill-switch management requires bounded paper authority")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "enable", "disable"))
    parser.add_argument("--confirm", default="")
    args = parser.parse_args(argv)

    manifest = _release_manifest()
    if args.action == "enable":
        path = set_kill_switch(True)
    elif args.action == "disable":
        _require_paper_manual_runtime(manifest)
        if args.confirm != RESUME_CONFIRMATION:
            raise RuntimeError(
                f"resume requires --confirm {RESUME_CONFIRMATION!r}"
            )
        path = set_kill_switch(False)
    else:
        path = kill_switch_path()

    result = {
        "action": args.action,
        "active": kill_switch_active(),
        "kill_switch_path": str(path),
        "release_id": str(manifest.get("release_id") or ""),
        "release_commit": str(manifest.get("release_commit") or ""),
        "paper_only": True,
        "paper_authority_mode": paper_authority_mode(manifest),
        "manual_approval_only": manifest.get("manual_approval_only") is True,
    }
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
