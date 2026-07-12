from __future__ import annotations

import json
import os
from pathlib import Path

from .formula_versions import REQUIRED_SCHEMA_VERSIONS


STATE_ROOT = Path.home() / "Library" / "Application Support" / "TradingAgent"
RELEASE_ROOT = Path.home() / "TradingAgentReleases"
RUNTIME_LINK = Path.home() / "TradingAgentRuntime"
REQUIRED_SCHEMA_VERSION = "runtime_safety_accounting_v1"

# Runtime starts only after the migration ledger and the concrete columns used
# by safety gates agree. Checking only one marker row allowed a partial P1
# execution migration to look deployable.
REQUIRED_RUNTIME_TABLE_COLUMNS = {
    "schema_migrations": {"version", "applied_at"},
    "runs": {"id", "started_at", "status", "mode"},
    "config_snapshots": {"run_id", "config_json", "effective_config_json", "effective_config_hash"},
    "risk_checks": {"run_id", "proposal_id", "name", "passed", "formula_version", "evidence_version", "config_hash"},
    "orders": {"id", "client_order_id", "status", "quote_bid", "quote_ask", "limit_price"},
    "fills": {"order_id", "qty", "price", "implementation_shortfall_bps"},
    "order_intents": {"id", "logical_action_key", "client_order_id", "reserved_notional", "reserved_stop_risk", "state", "trading_mode"},
    "risk_reservations": {"intent_id", "active_notional", "active_stop_risk", "state"},
    "reconciliation_attempts": {"intent_id", "outcome", "created_at"},
    "telegram_updates": {"update_id", "message_id", "message_timestamp", "received_at", "processing_state"},
    "research_outcomes": {"opportunity_id", "horizon_sessions", "status", "exit_session", "calculation_version", "outcome_class"},
    "phase3_risk_decisions": {"run_id", "requested_notional", "binding_caps_json", "evidence_version", "formula_version"},
    "phase4_allocation_decisions": {"run_id", "strategy_weights_json", "allocation_class", "operational_kelly_used", "binding_caps_json", "evidence_versions_json"},
    "cash_snapshots": {"equity", "realized_fifo_pnl", "account_equity_change", "unrealized_change", "external_cash_flow", "accounting_version"},
}


class RuntimeGuardError(RuntimeError):
    pass


def is_production_path(path: str | Path) -> bool:
    try:
        return Path(path).resolve().is_relative_to(STATE_ROOT.resolve())
    except (OSError, ValueError):
        return False


def runtime_database_path(config: dict) -> Path:
    if os.getenv("TRADING_AGENT_RUNTIME") == "production-paper" and os.getenv("TRADING_AGENT_TESTING") == "1":
        raise RuntimeGuardError("production runtime rejects TRADING_AGENT_TESTING=1")
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
    if os.getenv("TRADING_AGENT_TESTING") == "1":
        raise RuntimeGuardError("production runtime rejects TRADING_AGENT_TESTING=1")
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
