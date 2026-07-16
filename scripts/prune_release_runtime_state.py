#!/usr/bin/env python3
"""Remove generated runtime state without deleting tracked release source."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping

RUNTIME_STATE_ROOTS = ("data", "logs", "scratch")


def _tracked_paths(inventory: Mapping[str, Any]) -> set[str]:
    files = inventory.get("files")
    if not isinstance(files, list):
        raise ValueError("tracked source inventory files are unavailable")
    paths = {
        str(item.get("path") or "")
        for item in files
        if isinstance(item, Mapping)
    }
    if not paths or "" in paths:
        raise ValueError("tracked source inventory paths are invalid")
    return paths


def prune_runtime_state(root: Path, inventory: Mapping[str, Any]) -> dict[str, Any]:
    root = root.resolve()
    tracked = _tracked_paths(inventory)
    removed_files: list[str] = []
    removed_directories: list[str] = []
    preserved_tracked: list[str] = []

    for name in RUNTIME_STATE_ROOTS:
        state_root = root / name
        if not state_root.exists() and not state_root.is_symlink():
            continue
        if state_root.is_symlink() or not state_root.is_dir():
            raise ValueError(f"release runtime-state root is unsafe: {name}")
        for target in sorted(
            state_root.rglob("*"),
            key=lambda path: (len(path.relative_to(root).parts), path.as_posix()),
            reverse=True,
        ):
            relative = target.relative_to(root).as_posix()
            if relative in tracked:
                if target.is_symlink() or not target.is_file():
                    raise ValueError(f"tracked runtime-state source changed type: {relative}")
                preserved_tracked.append(relative)
                continue
            if target.is_symlink() or target.is_file():
                target.unlink()
                removed_files.append(relative)
                continue
            if not target.is_dir():
                raise ValueError(f"release runtime-state entry is unsafe: {relative}")
            try:
                target.rmdir()
            except OSError:
                continue
            removed_directories.append(relative)

    missing = sorted(
        path
        for path in tracked
        if path.split("/", 1)[0] in RUNTIME_STATE_ROOTS
        and not (root / path).is_file()
    )
    if missing:
        raise ValueError("tracked runtime-state source is missing: " + ", ".join(missing))
    return {
        "removed_file_count": len(removed_files),
        "removed_directory_count": len(removed_directories),
        "preserved_tracked_paths": sorted(preserved_tracked),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--inventory", type=Path, required=True)
    args = parser.parse_args()
    result = prune_runtime_state(
        args.root,
        json.loads(args.inventory.read_text(encoding="utf-8")),
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
