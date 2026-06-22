from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.formatting.rule import CellIsRule
from openpyxl.styles import Font, PatternFill

from .storage import Storage
from .utils import PROJECT_ROOT

SHEETS: list[tuple[str, str | None]] = [
    ("Summary Dashboard", None), ("Proposals", "trade_proposals"), ("Daily PnL", "daily_summaries"), ("Trades", "orders"),
    ("Orders", "orders"), ("Fills", "fills"), ("Positions", "positions"), ("Signals", "signals"),
    ("Risk Checks", "risk_checks"), ("AI Reviews", "ai_reviews"), ("Approvals", "approvals"),
    ("Cash Management", "cashout_suggestions"), ("ML Shadow Metrics", "model_versions"),
    ("Market Memory", "market_memory"),
    ("Telegram Digests", "telegram_digests"),
    ("Errors", "errors"), ("Audit Events", "audit_events"), ("Config Snapshot", "config_snapshots"),
]


def _write_rows(sheet: Any, rows: list[dict[str, Any]]) -> None:
    if not rows:
        sheet.append(["No records"])
        return
    headers = list(rows[0])
    sheet.append(headers)
    for cell in sheet[1]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="D9EAF7")
    for row in rows:
        sheet.append([row.get(header) for header in headers])
    sheet.freeze_panes = "A2"
    for column in sheet.columns:
        width = min(50, max(len(str(cell.value or "")) for cell in column) + 2)
        sheet.column_dimensions[column[0].column_letter].width = width


def export_excel(storage: Storage, config: dict[str, Any], output_path: str | Path | None = None) -> Path:
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
    for name, table in SHEETS[1:]:
        sheet = workbook.create_sheet(name)
        _write_rows(sheet, storage.fetch_all(f'SELECT * FROM "{table}"'))
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
