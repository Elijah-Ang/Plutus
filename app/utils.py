from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso_now() -> str:
    return utc_now().isoformat()


def load_yaml(path: str | Path) -> dict[str, Any]:
    with Path(path).open(encoding="utf-8") as handle:
        value = yaml.safe_load(handle) or {}
    if not isinstance(value, dict):
        raise ValueError(f"Expected mapping in {path}")
    return value


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    config = load_yaml(path or PROJECT_ROOT / "config" / "config.yaml")
    if config.get("mode") not in {"paper", "live"}:
        raise ValueError("mode must be paper or live")
    return config


def secret_present(name: str) -> bool:
    value = get_secret(name)
    return bool(value and not value.startswith("replace_with_"))


def get_secret(name: str) -> str | None:
    """Read environment first, then macOS Keychain without logging the value."""
    value = os.getenv(name)
    if value and not value.startswith("replace_with_"):
        return value
    try:
        result = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-a", os.getenv("USER", ""), "-s", f"TradingAgent.{name}", "-w"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None


def json_dumps(value: Any) -> str:
    return json.dumps(value, default=str, sort_keys=True, separators=(",", ":"))


def redact(value: Any) -> Any:
    sensitive = ("key", "secret", "token", "password", "account_id")
    if isinstance(value, dict):
        return {k: "[REDACTED]" if any(s in k.lower() for s in sensitive) else redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value
