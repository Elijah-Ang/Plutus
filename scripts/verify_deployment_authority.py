#!/usr/bin/env python3
"""Verify exact-current-main forward authority or one immutable rollback tag."""

from __future__ import annotations

import argparse
import json
import urllib.parse
from pathlib import Path

from scripts.check_release_eligibility import _github_json, _release_attestation_asset


def verify(manifest: dict, *, mode: str, repository: str = "Elijah-Ang/Plutus") -> dict:
    commit = str(manifest.get("release_commit") or "")
    authority = manifest.get("release_authority") if isinstance(manifest.get("release_authority"), dict) else {}
    if mode == "forward":
        main, error, _ = _github_json(f"https://api.github.com/repos/{repository}/git/ref/heads/main")
        sha = str(((main or {}).get("object") or {}).get("sha") or "") if isinstance(main, dict) else ""
        if error or not sha or sha != commit or authority.get("mode") != "forward":
            raise RuntimeError("forward deployment requires the exact current GitHub main commit")
        return {"mode": mode, "commit": commit, "remote_main_sha": sha}

    if mode != "rollback" or authority.get("mode") != "rollback":
        raise RuntimeError("deployment mode and manifest authority disagree")
    tag = str(authority.get("tag_name") or "")
    expected = authority.get("attestation") if isinstance(authority.get("attestation"), dict) else {}
    if not tag.startswith("immutable-release-") or not expected:
        raise RuntimeError("rollback requires a verified immutable release tag and attestation evidence")
    ref, error, _ = _github_json(
        f"https://api.github.com/repos/{repository}/git/ref/tags/{urllib.parse.quote(tag, safe='')}"
    )
    target = (ref or {}).get("object") if isinstance(ref, dict) else None
    if error or not isinstance(target, dict) or target.get("type") != "tag":
        raise RuntimeError("rollback tag is absent, replaced, or not annotated")
    tag_object, error, _ = _github_json(
        f"https://api.github.com/repos/{repository}/git/tags/{target.get('sha')}"
    )
    tagged = (tag_object or {}).get("object") if isinstance(tag_object, dict) else None
    if error or not isinstance(tagged, dict) or tagged.get("type") != "commit" or tagged.get("sha") != commit:
        raise RuntimeError("rollback annotated tag does not identify the release commit")
    current = _release_attestation_asset(repository, tag)
    if not isinstance(current, dict):
        raise RuntimeError("rollback release is not immutable or its attestation is invalid")
    for key in ("release_id", "asset_id", "asset_digest", "asset_size", "download_digest"):
        if current.get(key) != expected.get(key):
            raise RuntimeError(f"rollback attestation {key} was replaced or does not match the approved artifact")
    if current.get("release_immutable") is not True:
        raise RuntimeError("rollback GitHub release is not immutable")
    attested = current.get("manifest") if isinstance(current.get("manifest"), dict) else {}
    if str(attested.get("release_commit") or attested.get("commit_sha") or "") != commit:
        raise RuntimeError("rollback attestation commit does not match the artifact")
    return {"mode": mode, "commit": commit, "tag_name": tag, **expected}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--mode", choices=("forward", "rollback"), required=True)
    parser.add_argument("--repository", default="Elijah-Ang/Plutus")
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    print(json.dumps(verify(manifest, mode=args.mode, repository=args.repository), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
