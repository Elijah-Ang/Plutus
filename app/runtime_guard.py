from __future__ import annotations

import json
import os
from pathlib import Path


STATE_ROOT = Path.home() / "Library" / "Application Support" / "TradingAgent"
RELEASE_ROOT = Path.home() / "TradingAgentReleases"
RUNTIME_LINK = Path.home() / "TradingAgentRuntime"
REQUIRED_SCHEMA_VERSION = "phase0_execution_integrity_v3_runtime_isolation"


class RuntimeGuardError(RuntimeError):
    pass


def is_production_path(path: str | Path) -> bool:
    try:
        return Path(path).resolve().is_relative_to(STATE_ROOT.resolve())
    except (OSError, ValueError):
        return False


def runtime_database_path(config: dict) -> Path:
    if os.getenv("TRADING_AGENT_TESTING") == "1":
        return Path(config["storage"]["sqlite_path"])
    raw = os.getenv("TRADING_AGENT_DATABASE_PATH")
    if not raw:
        raise RuntimeGuardError("explicit database path required; development defaults are forbidden")
    path = Path(raw).resolve()
    if os.getenv("TRADING_AGENT_RUNTIME") == "production-paper":
        if not is_production_path(path):
            raise RuntimeGuardError("production runtime database must be under Application Support")
    elif is_production_path(path):
        raise RuntimeGuardError("development invocation cannot open the production-paper database")
    return path


def validate_production_runtime() -> dict:
    if os.getenv("TRADING_AGENT_RUNTIME") != "production-paper":
        raise RuntimeGuardError("production runtime marker is required")
    runtime = RUNTIME_LINK.resolve()
    if not runtime.is_relative_to(RELEASE_ROOT.resolve()):
        raise RuntimeGuardError("runtime path must resolve inside TradingAgentReleases")
    cwd = Path.cwd().resolve()
    if cwd != runtime:
        raise RuntimeGuardError("runtime working directory does not match selected immutable release")
    manifest_path = runtime / "release-manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeGuardError("release manifest is unavailable or invalid") from exc
    if manifest.get("mode") != "paper":
        raise RuntimeGuardError("release manifest is not paper-only")
    if manifest.get("schema_version") != REQUIRED_SCHEMA_VERSION:
        raise RuntimeGuardError("release schema requirement is not explicit")
    if os.getenv("TRADING_AGENT_RELEASE_ID") != manifest.get("release_id"):
        raise RuntimeGuardError("runtime release ID does not match manifest")
    return manifest
