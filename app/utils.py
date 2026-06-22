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
    expiry_fmt = format_sgt(expiry)

    # 1. Mode/Header
    mode = config.get("mode", "paper")
    live_enabled = config.get("live_enabled", False)
    if mode == "live" and live_enabled:
        mode_str = "Live trading"
        mode_notice = "WARNING: This is a LIVE trade using real money."
    else:
        mode_str = "Paper trading only"
        mode_notice = "This uses fake Alpaca Paper money, not real money."

    if symbol == "TEST" or is_fake_test:
        return (
            f"🧪 Fake paper test proposal\n\n"
            f"This is only testing the Telegram approval flow.\n"
            f"No Alpaca order will be placed for this fake TEST symbol.\n\n"
            f"Reply yes to approve the test, or no to reject it.\n"
            f"Time to decide: {expiry_minutes} minutes\n"
            f"Expires: {expiry_fmt}"
        )

    # 2. Action / Amount
    if side.lower() == "buy":
        action_header = f"📄 Paper trade proposal\n\n"
        action_detail = f"Action: Buy {symbol}\n"
        amount_detail = f"Amount: ${notional:.0f}" if notional is not None else "Amount: N/A"
    else:
        action_header = f"📄 Paper sell proposal\n\n"
        action_detail = f"Action: Sell {symbol}\n"
        amount_detail = f"Quantity: {qty} shares" if qty is not None else (f"Amount: ${notional:.0f}" if notional is not None else "Amount: N/A")
        amount_detail += "\nPurpose: Close or reduce the existing paper position."

    # 3. Scores & Confidence
    asset_score = proposal.get("asset_score")
    asset_classification = proposal.get("asset_classification", "Watch only")
    score_val = proposal.get("score")
    trade_classification = proposal.get("classification", "No action suggested")
    
    # Map score to system confidence
    system_confidence = "No action suggested"
    if score_val is not None:
        if score_val >= 90: system_confidence = "Very strong paper setup"
        elif score_val >= 80: system_confidence = "Strong paper setup"
        elif score_val >= 65: system_confidence = "Moderate paper setup"
        elif score_val >= 50: system_confidence = "Weak setup, watch only"
        
    scores_str = ""
    if score_val is not None and asset_score is not None:
        scores_str = (
            f"Asset score: {asset_score:.0f}/100 — {asset_classification}\n"
            f"Trade score: {score_val:.0f}/100 — {trade_classification}\n"
            f"System confidence: {system_confidence}\n\n"
        )
    elif score_val is not None:
        if score_val >= 80:
            urgency_note = "High priority/confidence signal."
        elif score_val >= 65:
            urgency_note = "Moderate confidence signal."
        else:
            urgency_note = "Lower urgency, watch-only signal."
        scores_str = (
            f"Recommendation score: {score_val:.0f}/100\n"
            f"Urgency guidance: {urgency_note}\n"
            f"Suggestion: {trade_classification}\n"
            f"Why: {proposal.get('reason', '')}\n\n"
        )

    # 4. Rank
    rank = proposal.get("symbol_rank")
    total_active = proposal.get("total_active_symbols")
    rank_str = ""
    if rank is not None and total_active is not None:
        rank_str = f"Rank: #{rank} of {total_active} active ETFs\n\n"

    # 5. Price changes
    price_change_pct = proposal.get("price_change_pct", 0.0)
    session_change_pct = proposal.get("session_change_pct", 0.0)
    changes_str = (
        f"Since last check: {price_change_pct:+.2f}%\n"
        f"Since market open: {session_change_pct:+.2f}%\n\n"
    )

    # 6. GPT Review
    gpt_str = ""
    review = proposal.get("review")
    gpt_called = proposal.get("gpt_called", True)
    
    if review:
        gpt_conf = review.get("gpt_confidence", "Not called")
        gpt_caution = review.get("gpt_caution", "Low")
        main_risk = review.get("main_risk", "")
        if gpt_called and gpt_conf != "Not called" and "Deterministic fallback" not in review.get("reasoning_notes", ""):
            gpt_str = (
                f"GPT review: {gpt_conf} confidence\n"
                f"Main caution: {main_risk or 'None'}\n\n"
            )
        else:
            if score_val is not None and score_val < 65:
                gpt_str = "GPT review: Not called. The setup was not strong enough to need AI review.\n\n"
            else:
                gpt_str = "GPT review: Not called. AI review was skipped due to throttling/safety limits.\n\n"
    else:
        if score_val is not None and score_val < 65:
            gpt_str = "GPT review: Not called. The setup was not strong enough to need AI review.\n\n"
        else:
            gpt_str = "GPT review: Not called. AI review was skipped due to throttling/safety limits.\n\n"

    # 7. Reason
    reason_str = ""
    if asset_score is not None:
        raw_reason = proposal.get("reason", "")
        if raw_reason.startswith("Test") or symbol == "TEST" or is_fake_test:
            reason_str = f"Why: {raw_reason}\n\n"
        else:
            reasons_why = []
            if rank is not None and total_active is not None:
                reasons_why.append(f"ETF is ranked #{rank} of {total_active} active candidates")
            if raw_reason:
                reasons_why.append(f"primary strategy signal indicates '{raw_reason}'")
            volatility_class = proposal.get("volatility_class", "normal")
            if volatility_class:
                reasons_why.append(f"volatility condition is {volatility_class}")
            if price_change_pct != 0.0:
                reasons_why.append(f"recent price change is {price_change_pct:+.2f}% since last check")
            if session_change_pct != 0.0:
                reasons_why.append(f"session trend is {session_change_pct:+.2f}% since market open")
            
            if reasons_why:
                why_text = "; ".join(reasons_why)
                why_text = why_text[0].upper() + why_text[1:] + "."
                reason_str = f"Why: {why_text}\n\n"
            else:
                reason_str = f"Why: {raw_reason}\n\n"

    # 8. Time to decide and expiry
    volatility_class = proposal.get("volatility_class", "normal")
    if volatility_class == "high":
        time_to_decide = f"Time to decide: {expiry_minutes} minutes because the market is moving quickly."
    elif volatility_class == "low":
        time_to_decide = f"Time to decide: {expiry_minutes} minutes because conditions are relatively stable."
    else:
        time_to_decide = f"Time to decide: {expiry_minutes} minutes"

    expiry_info = (
        f"{time_to_decide}\n"
        f"Expires: {expiry_fmt}\n\n"
    )

    # 9. Instructions
    instructions = (
        f"Reply yes to approve, or no to reject.\n"
        f"No reply = proposal expires and no order is placed."
    )

    return (
        f"{action_header}"
        f"Mode: {mode_str}\n"
        f"{action_detail}"
        f"{amount_detail}\n\n"
        f"{scores_str}"
        f"{rank_str}"
        f"{changes_str}"
        f"{gpt_str}"
        f"{reason_str}"
        f"{expiry_info}"
        f"{instructions}"
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

