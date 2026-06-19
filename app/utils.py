from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime, timezone, timedelta
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


def format_sgt(dt_val: datetime | str) -> str:
    if isinstance(dt_val, str):
        dt_val = datetime.fromisoformat(dt_val.replace("Z", "+00:00"))
    if dt_val.tzinfo is None:
        dt_val = dt_val.replace(tzinfo=UTC)
    sgt_tz = timezone(timedelta(hours=8))
    dt_sgt = dt_val.astimezone(sgt_tz)
    date_part = dt_sgt.strftime("%b %d, %Y")
    hour = dt_sgt.hour % 12
    if hour == 0:
        hour = 12
    minute = dt_sgt.strftime("%M")
    ampm = dt_sgt.strftime("%p")
    return f"{date_part}, {hour}:{minute} {ampm} SGT"


def format_expiry(expiry_dt: datetime | str, now: datetime | None = None) -> str:
    if isinstance(expiry_dt, str):
        expiry_dt = datetime.fromisoformat(expiry_dt.replace("Z", "+00:00"))
    if expiry_dt.tzinfo is None:
        expiry_dt = expiry_dt.replace(tzinfo=UTC)
    
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
        
    diff = expiry_dt - now
    diff_minutes = round(diff.total_seconds() / 60)
    
    sgt_str = format_sgt(expiry_dt)
    if 0 < diff_minutes <= 120:
        return f"{sgt_str} (about {diff_minutes} minutes)"
    return sgt_str


def format_proposal_message(proposal: dict[str, Any], config: dict[str, Any], is_fake_test: bool = False) -> str:
    symbol = proposal.get("symbol", "").upper()
    side = proposal.get("side", "").capitalize()
    notional = proposal.get("notional")
    qty = proposal.get("qty")
    expiry = proposal.get("expires_at", "")
    
    expiry_minutes = proposal.get("expiry_minutes", 15)
    volatility_class = proposal.get("volatility_class", "normal")
    expiry_fmt = format_sgt(expiry)

    if volatility_class == "high":
        time_to_decide = f"Time to decide: {expiry_minutes} minutes because the market is moving quickly."
    elif volatility_class == "low":
        time_to_decide = f"Time to decide: {expiry_minutes} minutes because conditions are relatively stable."
    else:
        time_to_decide = f"Time to decide: {expiry_minutes} minutes"

    score_val = proposal.get("score", 50)
    if score_val >= 80:
        urgency_note = "High priority/confidence signal."
    elif score_val >= 65:
        urgency_note = "Moderate confidence signal."
    else:
        urgency_note = "Lower urgency, watch-only signal."

    if symbol == "TEST" or is_fake_test:
        return (
            f"🧪 Fake paper test proposal\n\n"
            f"This is only testing the Telegram approval flow.\n"
            f"No Alpaca order will be placed for this fake TEST symbol.\n\n"
            f"Reply yes to approve the test, or no to reject it.\n"
            f"Time to decide: {expiry_minutes} minutes\n"
            f"Expires: {expiry_fmt}"
        )
        
    mode = config.get("mode", "paper")
    live_enabled = config.get("live_enabled", False)
    
    if mode == "live" and live_enabled:
        mode_str = "Live trading"
        mode_notice = "WARNING: This is a LIVE trade using real money."
    else:
        mode_str = "Paper trading only"
        mode_notice = "This uses fake Alpaca Paper money, not real money."
        
    if side.lower() == "buy":
        amount_str = f"${notional:.0f}" if notional is not None else "N/A"
        scoring_str = ""
        if score_val is not None:
            scoring_str = (
                f"Recommendation score: {score_val:.0f}/100\n"
                f"Urgency guidance: {urgency_note}\n"
                f"Suggestion: {proposal.get('classification', 'Watch only')}\n"
                f"Why: {proposal.get('reason', '')}\n\n"
            )
        return (
            f"📄 Paper trade proposal\n\n"
            f"Mode: {mode_str}\n"
            f"Action: Buy {symbol}\n"
            f"Amount: {amount_str}\n"
            f"{mode_notice}\n\n"
            f"{scoring_str}"
            f"Reply yes to approve, or no to reject.\n"
            f"{time_to_decide}\n"
            f"Expires: {expiry_fmt}"
        )
    else:
        qty_str = f"{qty} shares" if qty is not None else (f"${notional:.0f}" if notional is not None else "N/A")
        scoring_str = ""
        if score_val is not None:
            scoring_str = (
                f"Recommendation score: {score_val:.0f}/100\n"
                f"Urgency guidance: {urgency_note}\n"
                f"Suggestion: {proposal.get('classification', 'Watch only')}\n"
                f"Why: {proposal.get('reason', '')}\n\n"
            )
        return (
            f"📄 Paper sell proposal\n\n"
            f"Mode: {mode_str}\n"
            f"Action: Sell {symbol}\n"
            f"Quantity: {qty_str}\n"
            f"Purpose: Close or reduce the existing paper position.\n\n"
            f"{scoring_str}"
            f"Reply yes to approve, or no to reject.\n"
            f"{time_to_decide}\n"
            f"Expires: {expiry_fmt}"
        )


def translate_reason(reason: str) -> str:
    if "message is not an unambiguous approval or rejection" in reason:
        return "I did not take any action because I could not tell whether you meant yes or no. Please reply yes to approve or no to reject."
    if "identify proposal when pending count is not one" in reason:
        return "I did not take any action because there is more than one pending proposal. Please reply with the proposal ID and yes/no, for example: yes 5e165d49."
    if "exactly one matching pending proposal is required" in reason:
        return "I did not take any action because I could not match your reply to a single pending proposal. Please specify the proposal ID or symbol."
    if "proposal expired" in reason:
        return "I did not take any action because this proposal has already expired."
    if "unauthorized sender" in reason:
        return "I ignored this message because it was not sent by the authorized Telegram user."
    if "unauthorized" in reason:
        return "I ignored this message because it was not sent by the authorized Telegram user."
    return f"No action taken: {reason}."

