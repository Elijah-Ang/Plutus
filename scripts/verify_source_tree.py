#!/usr/bin/env python3
"""Create and verify a release's tracked source against GitHub's Git tree."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.check_release_eligibility import _github_json


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _git_blob_sha(content: bytes) -> str:
    header = f"blob {len(content)}\0".encode("ascii")
    return hashlib.sha1(header + content).hexdigest()  # noqa: S324 - Git's object identity is SHA-1.


def _source_bytes(root: Path, path: str, mode: str) -> bytes:
    target = root / path
    if not target.exists() and not target.is_symlink():
        raise ValueError(f"tracked source file is missing: {path}")
    if mode == "120000":
        if not target.is_symlink():
            raise ValueError(f"tracked source type changed: {path}")
        return os.readlink(target).encode("utf-8")
    if target.is_symlink() or not target.is_file():
        raise ValueError(f"tracked source type changed: {path}")
    return target.read_bytes()


def _verify_no_unapproved_source_files(root: Path, tracked_paths: set[str]) -> None:
    generated_files = {
        "tracked-source-inventory.json", "artifact-test-results.json",
        "dependency-inventory.txt", "release-manifest.json",
        "release-file-inventory.sha256", ".coverage",
    }
    pruned_roots = {".venv", ".git", ".pytest_cache", "data", "logs", "scratch"}
    for directory, names, files in os.walk(root, followlinks=False):
        relative_directory = Path(directory).relative_to(root)
        names[:] = [
            name for name in names
            if not (
                (relative_directory == Path(".") and name in pruned_roots)
                or name == "__pycache__"
                or name.endswith(".egg-info")
            )
        ]
        for name in files:
            relative = (relative_directory / name).as_posix()
            if relative.startswith("./"):
                relative = relative[2:]
            if relative in tracked_paths or relative in generated_files:
                continue
            raise ValueError(f"unapproved file is outside the authoritative source tree: {relative}")


def github_tree(repository: str, commit: str) -> dict[str, Any]:
    commit_data, error, _ = _github_json(
        f"https://api.github.com/repos/{repository}/git/commits/{commit}"
    )
    if error or not isinstance(commit_data, Mapping):
        raise ValueError("authoritative GitHub commit identity is unavailable")
    if str(commit_data.get("sha") or "") != commit:
        raise ValueError("authoritative GitHub commit identity does not match")
    tree_sha = str((commit_data.get("tree") or {}).get("sha") or "")
    if not tree_sha:
        raise ValueError("authoritative Git tree SHA is missing")
    tree_data, error, _ = _github_json(
        f"https://api.github.com/repos/{repository}/git/trees/{tree_sha}?recursive=1"
    )
    if error or not isinstance(tree_data, Mapping) or tree_data.get("truncated") is True:
        raise ValueError("authoritative Git tree is unavailable or truncated")
    if str(tree_data.get("sha") or "") != tree_sha:
        raise ValueError("authoritative Git tree SHA does not match")
    files: list[dict[str, str]] = []
    for entry in tree_data.get("tree") or []:
        if not isinstance(entry, Mapping):
            continue
        kind = str(entry.get("type") or "")
        if kind == "tree":
            continue
        if kind != "blob":
            raise ValueError(f"unsupported tracked Git object: {entry.get('path')}")
        files.append({
            "path": str(entry.get("path") or ""),
            "mode": str(entry.get("mode") or ""),
            "blob_sha": str(entry.get("sha") or ""),
        })
    files.sort(key=lambda item: item["path"])
    if not files or any(not item["path"] or not item["blob_sha"] for item in files):
        raise ValueError("authoritative Git tree file inventory is invalid")
    return {"commit": commit, "tree_sha": tree_sha, "files": files}


def create_inventory(
    root: Path,
    *,
    commit: str,
    repository: str = "Elijah-Ang/Plutus",
    authority: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    root = root.resolve()
    remote = dict(authority or github_tree(repository, commit))
    if str(remote.get("commit") or "") != commit or not str(remote.get("tree_sha") or ""):
        raise ValueError("wrong Git commit or tree authority")
    files: list[dict[str, str]] = []
    for expected in remote.get("files") or []:
        path = str(expected.get("path") or "")
        mode = str(expected.get("mode") or "")
        content = _source_bytes(root, path, mode)
        blob_sha = _git_blob_sha(content)
        if blob_sha != str(expected.get("blob_sha") or ""):
            raise ValueError(f"tracked source does not match Git blob: {path}")
        files.append({
            "path": path,
            "mode": mode,
            "blob_sha": blob_sha,
            "content_sha256": hashlib.sha256(content).hexdigest(),
        })
    body = {
        "version": "tracked_source_inventory_v1",
        "repository": repository,
        "release_commit": commit,
        "git_tree_sha": str(remote["tree_sha"]),
        "files": files,
    }
    return {**body, "inventory_digest": hashlib.sha256(_canonical(body)).hexdigest()}


def verify_inventory(
    root: Path,
    inventory: Mapping[str, Any],
    *,
    repository: str | None = None,
    authority: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    root = root.resolve()
    body = {key: inventory.get(key) for key in (
        "version", "repository", "release_commit", "git_tree_sha", "files"
    )}
    if body["version"] != "tracked_source_inventory_v1":
        raise ValueError("tracked source inventory version is invalid")
    digest = hashlib.sha256(_canonical(body)).hexdigest()
    if digest != str(inventory.get("inventory_digest") or ""):
        raise ValueError("tracked source inventory digest is invalid")
    commit = str(body["release_commit"] or "")
    repo = repository or str(body["repository"] or "")
    remote = dict(authority or github_tree(repo, commit))
    if str(remote.get("commit") or "") != commit:
        raise ValueError("authoritative release commit changed")
    if str(remote.get("tree_sha") or "") != str(body["git_tree_sha"] or ""):
        raise ValueError("authoritative Git tree SHA changed")
    recorded = body["files"] if isinstance(body["files"], list) else []
    remote_files = [
        {key: str(item.get(key) or "") for key in ("path", "mode", "blob_sha")}
        for item in (remote.get("files") or [])
    ]
    recorded_git = [
        {key: str(item.get(key) or "") for key in ("path", "mode", "blob_sha")}
        for item in recorded if isinstance(item, Mapping)
    ]
    if recorded_git != remote_files:
        raise ValueError("tracked source inventory is not the authoritative Git tree")
    for item in recorded:
        path = str(item.get("path") or "")
        content = _source_bytes(root, path, str(item.get("mode") or ""))
        if _git_blob_sha(content) != str(item.get("blob_sha") or ""):
            raise ValueError(f"tracked source Git blob changed: {path}")
        if hashlib.sha256(content).hexdigest() != str(item.get("content_sha256") or ""):
            raise ValueError(f"tracked source content changed: {path}")
    _verify_no_unapproved_source_files(
        root,
        {str(item.get("path") or "") for item in recorded if isinstance(item, Mapping)},
    )
    return {
        "verified": True,
        "release_commit": commit,
        "git_tree_sha": body["git_tree_sha"],
        "inventory_digest": digest,
        "tracked_file_count": len(recorded),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--repository", default="Elijah-Ang/Plutus")
    parser.add_argument("--commit")
    parser.add_argument("--create", action="store_true")
    args = parser.parse_args()
    if args.create:
        if not args.commit:
            raise SystemExit("--create requires --commit")
        result = create_inventory(
            args.root, commit=args.commit, repository=args.repository
        )
        args.inventory.write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    else:
        result = verify_inventory(
            args.root,
            json.loads(args.inventory.read_text(encoding="utf-8")),
            repository=args.repository,
        )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
