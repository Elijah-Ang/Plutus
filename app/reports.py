from __future__ import annotations

from datetime import date
import json
import re
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.formatting.rule import CellIsRule
from openpyxl.styles import Font, PatternFill

from .storage import Storage
from .utils import PROJECT_ROOT, json_dumps, redact

SHEETS: list[tuple[str, str | None]] = [
    ("Summary Dashboard", None), ("Proposals", "trade_proposals"), ("Daily PnL", "daily_summaries"), ("Trades", "orders"),
    ("Orders", "orders"), ("Fills", "fills"), ("Positions", "positions"), ("Signals", "signals"),
    ("Risk Checks", "risk_checks"), ("AI Reviews", "ai_reviews"), ("Approvals", "approvals"),
    ("Cash Management", "cashout_suggestions"), ("ML Shadow Metrics", "model_versions"),
    ("Market Memory", "market_memory"),
    ("Telegram Digests", "telegram_digests"),
    ("Errors", "errors"), ("Audit Events", "audit_events"), ("Config Snapshot", "config_snapshots"),
    ("Control State", "control_state"),
    ("Sleep Mode Events", "SELECT * FROM audit_events WHERE event_type IN ('sleep_mode_enabled', 'sleep_mode_disabled', 'sleep_mode_command_ignored_unauthorized', 'sleep_mode_command_ignored_old')"),
    ("Emergency Exit Events", "SELECT * FROM audit_events WHERE event_type IN ('emergency_exit_auto_timeout_reached', 'emergency_exit_submitted', 'emergency_exit_blocked', 'emergency_exit_cancelled_by_user', 'emergency_exit_approved_by_user')"),
    ("Emergency Exit Score Breakdown", "SELECT symbol, position_drawdown_pct, average_entry_price, latest_position_price, atr_value, adverse_move_atr, minutes_to_close, emergency_exit_score, emergency_exit_triggered, emergency_exit_trigger_reason, created_at FROM market_memory WHERE emergency_exit_score IS NOT NULL"),
    ("Exit Blocker State", "SELECT symbol, side, status, CASE WHEN status IN ('pending','approved') AND expires_at > strftime('%Y-%m-%dT%H:%M:%f+00:00','now') THEN 'active' ELSE 'stale' END AS blocker_state, CASE WHEN status IN ('pending','approved') AND expires_at > strftime('%Y-%m-%dT%H:%M:%f+00:00','now') AND emergency_exit_triggered=1 THEN symbol || ' emergency exit review active' WHEN status IN ('pending','approved') AND expires_at > strftime('%Y-%m-%dT%H:%M:%f+00:00','now') THEN symbol || ' EXIT proposal pending' ELSE 'stale ' || symbol || ' exit flag ignored' END AS blocker_reason, created_at, expires_at, exit_trigger_reason, emergency_exit_score, emergency_exit_triggered, emergency_exit_trigger_reason FROM trade_proposals WHERE side='sell' AND status IN ('pending','approved','submitted','filled','blocked','expired','rejected','superseded','stale_resolved') ORDER BY created_at DESC"),
    ("Exit Blocked BUY Candidates", "SELECT symbol, price, signal, score, no_action_reason, candidate_suppression_reason, exit_priority_applied, CASE WHEN EXISTS (SELECT 1 FROM trade_proposals p WHERE p.side='sell' AND p.status IN ('pending','approved') AND p.expires_at > strftime('%Y-%m-%dT%H:%M:%f+00:00','now')) THEN 'active_exit_blocker_currently_exists' ELSE 'stale_or_historical_exit_blocker_reason' END AS current_exit_blocker_state, created_at FROM market_memory WHERE signal='ENTRY' AND lower(no_action_reason) LIKE '%exit%'"),
    ("Suppressed Sleep BUY Candidates", "SELECT symbol, price, signal, score, no_action_reason, candidate_suppression_reason, created_at FROM market_memory WHERE candidate_suppression_reason = 'suppressed_by_sleep_mode'"),
    ("Wake Summary Events", "SELECT * FROM audit_events WHERE event_type = 'wake_summary_sent'"),
    ("Performance Lab Summary", "performance_lab_summaries"),
    ("Shadow Trades", "shadow_trades"),
    ("Actual vs Shadow", "SELECT * FROM trade_outcomes"),
    ("Forward Returns", "SELECT symbol, actual_or_shadow, entry_time, entry_price, forward_return_1d, forward_return_5d, forward_return_20d, outcome_status FROM trade_outcomes"),
    ("MAE MFE", "SELECT symbol, actual_or_shadow, entry_time, entry_price, max_favorable_excursion, max_adverse_excursion, stop_hit, target_reached FROM trade_outcomes"),
    ("Score Band Performance", "SELECT t.symbol, t.actual_or_shadow, COALESCE(s.score, json_extract(p.payload, '$.score')) as score, t.forward_return_1d, t.forward_return_5d, t.forward_return_20d, t.outcome_status FROM trade_outcomes t LEFT JOIN shadow_trades s ON t.trade_id = s.id LEFT JOIN orders o ON t.trade_id = o.id LEFT JOIN trade_proposals p ON o.proposal_id = p.id"),
    ("Symbol Performance", "SELECT symbol, actual_or_shadow, COUNT(*) as count, AVG(forward_return_20d) as avg_return_20d FROM trade_outcomes GROUP BY symbol, actual_or_shadow"),
    ("Add-on Opportunities", "add_on_opportunities"),
    ("Portfolio Exposure", "portfolio_exposure_snapshots"),
    ("Position Sizing Decisions", "position_sizing_decisions"),
    ("Candidate Ranking Decisions", "candidate_rankings"),
    ("Ranked Opportunity Sets", "ranked_opportunity_sets"),
    ("Proposal Batches", "proposal_batches"),
    ("Batch Candidates", "proposal_batch_candidates"),
    ("Risk Budget Decisions", "candidate_risk_budget_decisions"),
    ("Batch Approval Actions", "approval_batch_actions"),
    ("Candidate Allocation Decisions", "candidate_batch_allocations"),
    ("Risk Budget Snapshots", "risk_budget_snapshots"),
]


SECRET_VALUE_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"[0-9]{6,}:[A-Za-z0-9_-]{20,}"),
    re.compile(r"PK[A-Z0-9]{15,}"),
)

TELEGRAM_TEXT_KEYS = {"raw_message", "raw_command", "raw_command_redacted", "text"}
TELEGRAM_ID_KEYS = {"sender_id", "updated_by", "telegram_user_id", "chat_id", "from_id"}
SENSITIVE_KEY_PARTS = ("key", "secret", "token", "password", "account_id")


def redact_report_payload(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_lower = str(key).lower()
            if key_lower in TELEGRAM_TEXT_KEYS:
                redacted[key] = "[REDACTED TELEGRAM TEXT]"
            elif key_lower in TELEGRAM_ID_KEYS or key_lower.endswith("_sender_id"):
                redacted[key] = "[REDACTED ID]"
            elif any(part in key_lower for part in SENSITIVE_KEY_PARTS):
                redacted[key] = "[REDACTED]"
            else:
                redacted[key] = redact_report_payload(item)
        return redacted
    if isinstance(value, list):
        return [redact_report_payload(item) for item in value]
    return redact(value)


def redact_report_value(table: str, header: str, value: Any, include_raw_telegram: bool = False) -> Any:
    if header in TELEGRAM_TEXT_KEYS and not include_raw_telegram:
        return "[REDACTED TELEGRAM TEXT]"
    if header in TELEGRAM_ID_KEYS:
        return "[REDACTED ID]"
    if value is None:
        return None
    if header in {"payload", "values_json", "config_json", "detail", "risks", "portfolio_state_json", "single_symbol_exposure_json", "cluster_exposure_json"} and isinstance(value, str):
        try:
            value = json_dumps(redact_report_payload(json.loads(value)))
        except (ValueError, TypeError):
            value = redact(value)
    if isinstance(value, str):
        for pattern in SECRET_VALUE_PATTERNS:
            value = pattern.sub("[REDACTED SECRET]", value)
    return value


def _write_rows(sheet: Any, rows: list[dict[str, Any]], table: str, include_raw_telegram: bool = False) -> None:
    if not rows:
        sheet.append(["No records"])
        return
    headers = list(rows[0])
    sheet.append(headers)
    for cell in sheet[1]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="D9EAF7")
    for row in rows:
        sheet.append([redact_report_value(table, header, row.get(header), include_raw_telegram) for header in headers])
    sheet.freeze_panes = "A2"
    for column in sheet.columns:
        width = min(50, max(len(str(cell.value or "")) for cell in column) + 2)
        sheet.column_dimensions[column[0].column_letter].width = width


def export_excel(storage: Storage, config: dict[str, Any], output_path: str | Path | None = None, include_raw_telegram: bool = False) -> Path:
    output = Path(output_path or PROJECT_ROOT / "data" / "exports" / f"trading_agent_report_{date.today().isoformat()}.xlsx")
    output.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    workbook.remove(workbook.active)
    summary = workbook.create_sheet("Summary Dashboard")
    orders = storage.fetch_all("SELECT * FROM orders")
    fills = storage.fetch_all("SELECT * FROM fills")
    proposals = storage.fetch_all("SELECT * FROM trade_proposals")
    positions = storage.fetch_all("SELECT * FROM positions ORDER BY created_at DESC")
    runs = storage.fetch_all("SELECT * FROM runs ORDER BY started_at DESC LIMIT 1")
    errors = storage.fetch_all("SELECT * FROM errors ORDER BY created_at DESC LIMIT 1")
    profiles = config.get("market_profiles", {})
    active_profile_key = "default"
    active_profile = {}
    for k, v in profiles.items():
        if v.get("status") == "active":
            active_profile_key = k
            active_profile = v
            break

    metrics = [
        ("Mode", config.get("mode", "paper")), ("Report date", date.today().isoformat()),
        ("Starting capital", "See broker statement"), ("Current equity", "See latest cash snapshot"),
        ("Realized PnL", "See Daily PnL"), ("Unrealized PnL", "See Positions"), ("Total PnL", "See Daily PnL"),
        ("Win rate", "Requires completed fills"), ("Number of proposals", len(proposals)),
        ("Approved trades", sum(p["status"] == "approved" for p in proposals)),
        ("Rejected trades", sum(p["status"] == "rejected" for p in proposals)),
        ("Expired proposals", sum(p["status"] == "expired" for p in proposals)), ("Orders placed", len(orders)),
        ("Fills", len(fills)), ("Max drawdown", "See Daily PnL"), ("Average gain", "Requires completed fills"),
        ("Average loss", "Requires completed fills"), ("Profit factor", "Requires completed fills"),
        ("Current open positions", len(positions)), ("Daily loss limit status", "See Risk Checks"),
        ("Weekly loss limit status", "See Risk Checks"), ("Last bot run", runs[0]["started_at"] if runs else "Never"),
        ("Last error", errors[0]["message"] if errors else "None"), ("System health status", "Review latest preflight checks"),
        ("Suggested cash-out amount", "Recommendation only; see Cash Management"),
        ("Active Market Profile", active_profile_key),
        ("Execution Enabled", "yes" if active_profile.get("execution_enabled") else "no"),
        ("Default Proposal Expiry (minutes)", config.get("proposal_expiry_default_minutes", 15)),
        ("Auto-execution Mode Status", config.get("auto_execution_mode", "manual_only")),
        ("Auto-execution Enabled", str(config.get("auto_execution_enabled", False))),
        ("Asset Universe Status (Tradeable)", ", ".join(active_profile.get("watchlist", [])) if active_profile else ""),
        ("Asset Universe Status (Observation)", ", ".join(active_profile.get("observation_watchlist", [])) if active_profile else ""),
    ]
    summary.append(["TradingAgent Summary Dashboard"])
    summary["A1"].font = Font(bold=True, size=16)
    summary.append(["Metric", "Value"])
    for cell in summary[2]: cell.font = Font(bold=True)
    for metric in metrics: summary.append(metric)
    summary.freeze_panes = "A3"
    summary.column_dimensions["A"].width = 32
    summary.column_dimensions["B"].width = 48
    for name, query_or_table in SHEETS[1:]:
        sheet = workbook.create_sheet(name)
        if query_or_table.startswith("SELECT"):
            _write_rows(sheet, storage.fetch_all(query_or_table), name.lower().replace(" ", "_"), include_raw_telegram)
        else:
            _write_rows(sheet, storage.fetch_all(f'SELECT * FROM "{query_or_table}"'), str(query_or_table), include_raw_telegram)
        sheet.conditional_formatting.add("A2:Z10000", CellIsRule(operator="lessThan", formula=["0"], fill=PatternFill("solid", fgColor="F4CCCC")))
    workbook.save(output)
    return output


def main() -> None:
    from .utils import load_config
    config = load_config()
    storage = Storage(PROJECT_ROOT / config["storage"]["sqlite_path"])
    storage.initialize()
    print(export_excel(storage, config))


if __name__ == "__main__":
    main()
