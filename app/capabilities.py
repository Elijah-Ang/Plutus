"""Compile-time capability gates for paper execution.

Paper autonomy is supported only through the durable authority and final
revalidation path. Live trading remains permanently disabled in this build.
These constants are intentionally not configurable through YAML, environment
variables, Telegram, or runtime state.
"""

LIVE_TRADING_SUPPORTED = False
AUTO_EXECUTION_SUPPORTED = True
AUTONOMOUS_ENTRIES_SUPPORTED = True
AUTONOMOUS_EXITS_SUPPORTED = True
PROTECTIVE_PAPER_EXITS_SUPPORTED = True


def require_live_trading_support() -> None:
    if not LIVE_TRADING_SUPPORTED:
        raise PermissionError("Live trading is not supported by this build")


def require_auto_execution_support() -> None:
    if not AUTO_EXECUTION_SUPPORTED:
        raise PermissionError("Auto-execution is not supported by this build")


def require_autonomous_entry_support() -> None:
    if not AUTONOMOUS_ENTRIES_SUPPORTED:
        raise PermissionError("Autonomous ordinary entries are not supported by this build")


def require_autonomous_exit_support() -> None:
    if not AUTONOMOUS_EXITS_SUPPORTED:
        raise PermissionError("Autonomous ordinary exits are not supported by this build")


def require_protective_paper_exit_support() -> None:
    if not PROTECTIVE_PAPER_EXITS_SUPPORTED:
        raise PermissionError("Protective paper exits are not supported by this build")
