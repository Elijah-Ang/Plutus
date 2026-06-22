"""Compile-time capability gates for unsupported high-risk features.

These constants are intentionally not configurable through YAML, environment
variables, Telegram, or runtime state. Enabling either capability requires a
future reviewed code change and new safety tests.
"""

LIVE_TRADING_SUPPORTED = False
AUTO_EXECUTION_SUPPORTED = False


def require_live_trading_support() -> None:
    if not LIVE_TRADING_SUPPORTED:
        raise PermissionError("Live trading is not supported by this build")


def require_auto_execution_support() -> None:
    if not AUTO_EXECUTION_SUPPORTED:
        raise PermissionError("Auto-execution is not supported by this build")
