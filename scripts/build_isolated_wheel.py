#!/usr/bin/env python3
"""Build one verified release wheel outside immutable release staging."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.verify_source_tree import verify_inventory

REQUIRED_PYTHON = "3.13.9"
REQUIRED_PIP = "25.3"
REQUIRED_SETUPTOOLS = "80.9.0"
EVIDENCE_VERSION = "isolated_wheel_build_v1"


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _normalise_distribution(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def wheel_identity(wheel: Path) -> dict[str, str]:
    """Return and structurally validate the wheel's core distribution identity."""
    with zipfile.ZipFile(wheel) as archive:
        corrupt_member = archive.testzip()
        if corrupt_member is not None:
            raise ValueError(f"release wheel member is corrupt: {corrupt_member}")
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError("release wheel contains duplicate archive members")
        for member in names:
            path = PurePosixPath(member)
            if path.is_absolute() or ".." in path.parts or "\\" in member:
                raise ValueError("release wheel contains an unsafe archive path")
        metadata_files = [
            name for name in names
            if name.endswith(".dist-info/METADATA") and name.count("/") == 1
        ]
        if len(metadata_files) != 1:
            raise ValueError("release build must contain exactly one wheel METADATA record")
        dist_info = metadata_files[0].removesuffix("/METADATA")
        expected_metadata = {
            f"{dist_info}/METADATA",
            f"{dist_info}/WHEEL",
            f"{dist_info}/top_level.txt",
            f"{dist_info}/RECORD",
        }
        archive_files = {name for name in names if not name.endswith("/")}
        if not expected_metadata.issubset(archive_files):
            raise ValueError("release wheel metadata records are incomplete")
        unexpected = {
            name for name in archive_files
            if not name.startswith("app/") and name not in expected_metadata
        }
        if unexpected:
            raise ValueError("release wheel contains an unapproved installable payload")
        metadata = BytesParser().parsebytes(archive.read(metadata_files[0]))
    name = str(metadata.get("Name") or "").strip()
    version = str(metadata.get("Version") or "").strip()
    if not name or not version:
        raise ValueError("release wheel distribution name or version is missing")
    return {"distribution_name": name, "distribution_version": version}


def wheel_package_payload(
    wheel: Path, source_inventory: Mapping[str, Any]
) -> dict[str, Any]:
    """Bind every executable app package byte to its tracked Git source byte."""
    expected = {
        str(item.get("path") or ""): str(item.get("content_sha256") or "")
        for item in (source_inventory.get("files") or [])
        if isinstance(item, Mapping) and str(item.get("path") or "").startswith("app/")
    }
    if not expected or any(not path or not digest for path, digest in expected.items()):
        raise ValueError("tracked app package source inventory is incomplete")
    with zipfile.ZipFile(wheel) as archive:
        package_members = [
            name for name in archive.namelist()
            if name.startswith("app/") and not name.endswith("/")
        ]
        if len(package_members) != len(set(package_members)):
            raise ValueError("release wheel contains duplicate app package members")
        if set(package_members) != set(expected):
            raise ValueError("release wheel app package does not match tracked source paths")
        for path, expected_digest in expected.items():
            info = archive.getinfo(path)
            file_type = (info.external_attr >> 16) & 0o170000
            if file_type not in (0, 0o100000):
                raise ValueError(f"release wheel app package member is not a regular file: {path}")
            actual_digest = hashlib.sha256(archive.read(path)).hexdigest()
            if actual_digest != expected_digest:
                raise ValueError(f"release wheel app package payload changed: {path}")
    payload = [
        {"path": path, "content_sha256": expected[path]} for path in sorted(expected)
    ]
    return {
        "tracked_package_file_count": len(payload),
        "tracked_package_payload_digest": hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
    }


def verify_wheel_evidence(
    wheel: Path,
    evidence: Mapping[str, Any],
    *,
    source_inventory: Mapping[str, Any],
    release_commit: str,
    git_tree_sha: str,
    source_inventory_digest: str,
    expected_name: str,
    expected_version: str,
) -> dict[str, Any]:
    """Verify a retained wheel and its binding to the authoritative source tree."""
    if evidence.get("version") != EVIDENCE_VERSION:
        raise ValueError("release wheel evidence version is invalid")
    identity = wheel_identity(wheel)
    if _normalise_distribution(identity["distribution_name"]) != _normalise_distribution(expected_name):
        raise ValueError("release wheel distribution name is invalid")
    if identity["distribution_version"] != expected_version:
        raise ValueError("release wheel distribution version is invalid")
    package_payload = wheel_package_payload(wheel, source_inventory)
    expected = {
        "wheel_filename": wheel.name,
        "wheel_sha256": _digest(wheel),
        "distribution_name": identity["distribution_name"],
        "distribution_version": identity["distribution_version"],
        "source_commit": release_commit,
        "git_tree_sha": git_tree_sha,
        "source_inventory_digest": source_inventory_digest,
        **package_payload,
    }
    for key, value in expected.items():
        if evidence.get(key) != value:
            raise ValueError(f"release wheel evidence {key} does not match")
    return expected


def _run(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        check=True,
        text=True,
        capture_output=True,
    )


def build_isolated_wheel(
    *,
    repository_root: Path,
    staging_root: Path,
    inventory: Mapping[str, Any],
    commit: str,
    python: Path,
    workspace_parent: Path,
    repository: str = "Elijah-Ang/Plutus",
    authority: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Archive, verify, build, retain, and attest exactly one wheel."""
    repository_root = repository_root.resolve()
    staging_root = staging_root.resolve()
    workspace_parent = workspace_parent.resolve()
    python = python.resolve()
    workspace_parent.mkdir(parents=True, exist_ok=True)
    if staging_root == workspace_parent or staging_root in workspace_parent.parents:
        raise ValueError("wheel build workspace must be outside immutable staging")
    if str(inventory.get("release_commit") or "") != commit:
        raise ValueError("wheel build commit does not match the tracked-source inventory")
    git_tree_sha = str(inventory.get("git_tree_sha") or "")
    source_inventory_digest = str(inventory.get("inventory_digest") or "")
    if not git_tree_sha or not source_inventory_digest:
        raise ValueError("wheel build source-tree authority is incomplete")

    versions = {
        "python_version": _run(
            [str(python), "-c", "import platform; print(platform.python_version())"],
            cwd=repository_root,
        ).stdout.strip(),
        "pip_version": _run(
            [str(python), "-c", "import importlib.metadata as m; print(m.version('pip'))"],
            cwd=repository_root,
        ).stdout.strip(),
        "setuptools_version": _run(
            [str(python), "-c", "import importlib.metadata as m; print(m.version('setuptools'))"],
            cwd=repository_root,
        ).stdout.strip(),
    }
    required = {
        "python_version": REQUIRED_PYTHON,
        "pip_version": REQUIRED_PIP,
        "setuptools_version": REQUIRED_SETUPTOOLS,
    }
    for key, value in required.items():
        if versions[key] != value:
            raise ValueError(f"wheel build requires {key} {value}")

    wheel_root = staging_root / "release-wheel"
    evidence_path = staging_root / "wheel-build-evidence.json"
    if wheel_root.exists() or evidence_path.exists():
        raise ValueError("release wheel output already exists")

    try:
        with tempfile.TemporaryDirectory(
            prefix=f"plutus-wheel-{commit[:12]}-", dir=workspace_parent
        ) as temporary:
            temporary_root = Path(temporary)
            source = temporary_root / "source"
            distribution = temporary_root / "distribution"
            archive = temporary_root / "source.tar"
            source.mkdir()
            distribution.mkdir()
            _run(
                ["git", "archive", "--format=tar", f"--output={archive}", commit],
                cwd=repository_root,
            )
            with tarfile.open(archive) as payload:
                payload.extractall(source, filter="data")
            verified = verify_inventory(
                source,
                inventory,
                repository=repository,
                authority=authority,
            )
            if verified.get("git_tree_sha") != git_tree_sha:
                raise ValueError("temporary wheel source Git tree does not match staging")

            project = tomllib.loads((source / "pyproject.toml").read_text(encoding="utf-8"))
            project_identity = project.get("project") if isinstance(project.get("project"), dict) else {}
            expected_name = str(project_identity.get("name") or "")
            expected_version = str(project_identity.get("version") or "")
            if not expected_name or not expected_version:
                raise ValueError("release project name or version is missing")

            _run(
                [
                    str(python), "-m", "pip", "wheel", "--no-deps",
                    "--no-build-isolation", "--wheel-dir", str(distribution), str(source),
                ],
                cwd=temporary_root,
            )
            wheels = sorted(distribution.glob("*.whl"))
            if len(wheels) != 1:
                raise ValueError("release build must produce exactly one wheel")
            identity = wheel_identity(wheels[0])
            if _normalise_distribution(identity["distribution_name"]) != _normalise_distribution(expected_name):
                raise ValueError("release wheel distribution name is invalid")
            if identity["distribution_version"] != expected_version:
                raise ValueError("release wheel distribution version is invalid")
            package_payload = wheel_package_payload(wheels[0], inventory)

            wheel_root.mkdir()
            retained_wheel = wheel_root / wheels[0].name
            shutil.copyfile(wheels[0], retained_wheel)
            evidence: dict[str, Any] = {
                "version": EVIDENCE_VERSION,
                "wheel_filename": retained_wheel.name,
                "wheel_sha256": _digest(retained_wheel),
                **identity,
                "source_commit": commit,
                "git_tree_sha": git_tree_sha,
                "source_inventory_digest": source_inventory_digest,
                **package_payload,
                **versions,
                "build_isolation": False,
                "dependency_resolution": False,
            }
            verify_wheel_evidence(
                retained_wheel,
                evidence,
                source_inventory=inventory,
                release_commit=commit,
                git_tree_sha=git_tree_sha,
                source_inventory_digest=source_inventory_digest,
                expected_name=expected_name,
                expected_version=expected_version,
            )
            evidence_path.write_text(
                json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            return evidence
    except BaseException:
        shutil.rmtree(wheel_root, ignore_errors=True)
        evidence_path.unlink(missing_ok=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--staging-root", type=Path, required=True)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--workspace-parent", type=Path, required=True)
    parser.add_argument("--repository", default="Elijah-Ang/Plutus")
    args = parser.parse_args()
    evidence = build_isolated_wheel(
        repository_root=args.repository_root,
        staging_root=args.staging_root,
        inventory=json.loads(args.inventory.read_text(encoding="utf-8")),
        commit=args.commit,
        python=args.python,
        workspace_parent=args.workspace_parent,
        repository=args.repository,
    )
    print(json.dumps(evidence, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
