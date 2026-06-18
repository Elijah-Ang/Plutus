from __future__ import annotations

import platform
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class PowerStatus:
    connected: bool | None
    source: str
    detail: str


def get_power_status() -> PowerStatus:
    if platform.system() != "Darwin":
        return PowerStatus(None, "unsupported", "Power detection is only supported on macOS")
    try:
        result = subprocess.run(["/usr/bin/pmset", "-g", "batt"], capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return PowerStatus(None, "pmset", f"pmset failed: {type(exc).__name__}")
    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        return PowerStatus(None, "pmset", "pmset returned an error")
    first_line = output.splitlines()[0].lower() if output else ""
    if "ac power" in first_line:
        return PowerStatus(True, "pmset", "AC power connected")
    if "battery power" in first_line:
        return PowerStatus(False, "pmset", "Running on battery")
    return PowerStatus(None, "pmset", "Power status ambiguous")


def is_ac_power_connected() -> bool:
    return get_power_status().connected is True
