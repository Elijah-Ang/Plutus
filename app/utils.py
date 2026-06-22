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
        mode_notice = "Live trading"
    else:
        mode_notice = "Paper only"

    if symbol == "TEST" or is_fake_test:
        return (
            f"🧪 Fake paper test proposal\n\n"
            f"This is only testing the Telegram approval flow.\n"
            f"No Alpaca order will be placed for this fake TEST symbol.\n\n"
            f"Reply yes to approve the test, or no to reject it.\n"
            f"Time to decide: {expiry_minutes} minutes\n"
            f"Expires: {expiry_fmt}"
        )

    # Header / Action & Amount
    notional_adjustment_note = proposal.get("notional_adjustment_note", "")
    if side.lower() == "buy":
        header = "📄 Paper trade proposal\n\n"
        action_line = f"Buy {symbol} — {mode_notice}\n"
        amount_line = f"Amount: ${notional:.0f}{notional_adjustment_note}\n\n"
    else:
        header = "📄 Paper sell proposal\n\n"
        action_line = f"Sell {symbol} — {mode_notice}\n"
        if qty is not None:
            amount_line = f"Quantity: {qty} shares\n\n"
        elif notional is not None:
            amount_line = f"Amount: ${notional:.0f}{notional_adjustment_note}\n\n"
        else:
            amount_line = "Amount: N/A\n\n"

    # Confidence and Scores
    score_val = proposal.get("score")
    
    system_confidence = "No action suggested"
    if score_val is not None:
        if score_val >= 90:
            system_confidence = "Very strong"
        elif score_val >= 80:
            system_confidence = "Strong"
        elif score_val >= 65:
            system_confidence = "Moderate"
        elif score_val >= 50:
            system_confidence = "Weak"
            
    confidence_line = f"Confidence: {system_confidence}\n"
    score_line = f"Trade score: {score_val:.0f}/100\n" if score_val is not None else ""
    
    rank = proposal.get("symbol_rank")
    total_active = proposal.get("total_active_symbols")
    rank_line = ""
    if rank is not None and total_active is not None:
        rank_line = f"Rank: #{rank} of {total_active} active ETFs\n"
        
    scores_section = f"{confidence_line}{score_line}{rank_line}\n"

    # Why this appeared
    raw_reason = proposal.get("reason", "")
    why_text = ""
    if side.lower() == "buy":
        if "volatility normal" in raw_reason or "volatility_normal" in raw_reason or "normal" in raw_reason.lower():
            why_text = "The longer-term trend passed the bot’s filters, and volatility is normal for this ETF."
        elif "volatility elevated" in raw_reason or "volatility_elevated" in raw_reason or "elevated" in raw_reason.lower():
            why_text = "The longer-term trend passed the bot’s filters, and volatility is elevated for this ETF."
        else:
            why_text = f"The longer-term trend passed the bot’s filters, and volatility is normal for this ETF."
    else:
        if "close below 50-day MA" in raw_reason or "below 50-day MA" in raw_reason:
            why_text = "The price closed below the 50-day moving average, signaling an exit to protect capital."
        elif "stop drawdown" in raw_reason:
            why_text = "The position hit the trailing stop drawdown threshold."
        else:
            why_text = f"Exit condition met: {raw_reason}."
            
    why_section = f"Why this appeared:\n{why_text}\n\n"

    # Current movement
    price_change_pct = proposal.get("price_change_pct", 0.0)
    session_change_pct = proposal.get("session_change_pct", 0.0)
    
    vol_20 = proposal.get("volatility")
    if vol_20 is None:
        vol_20 = proposal.get("indicators", {}).get("volatility_20")
        
    vol_regime_str = ""
    if vol_20 is not None and isinstance(vol_20, (int, float)):
        vol_pct = vol_20 * 100
        volatility_class = proposal.get("volatility_class", "normal")
        if volatility_class == "extreme" or vol_20 > 0.45:
            regime = "extreme ETF regime"
        elif volatility_class == "high" or vol_20 > 0.35:
            regime = "high ETF regime"
        elif volatility_class == "elevated" or vol_20 >= 0.25:
            regime = "elevated ETF regime"
        elif volatility_class == "low" or vol_20 < 0.08:
            regime = "quiet ETF regime"
        else:
            regime = "normal ETF regime"
        vol_regime_str = f"Volatility: {vol_pct:.1f}% annualized — {regime}\n"

    movement_section = (
        f"Current movement:\n"
        f"Since last check: {price_change_pct:+.2f}%\n"
        f"Since market open: {session_change_pct:+.2f}%\n"
        f"{vol_regime_str}\n"
    )

    # Main risk / GPT review / caution
    review = proposal.get("review")
    gpt_called = proposal.get("gpt_called", True)
    
    main_risk_str = ""
    if review and review.get("main_risk"):
        main_risk_str = f"Main risk:\n{review.get('main_risk')}\n\n"
    elif not gpt_called and side.lower() == "buy":
        main_risk_str = (
            f"Main risk:\nRule-based only. AI review was not available. Treat with extra caution.\n\n"
        )

    # Decision time
    decision_time_str = f"Decision time: {expiry_minutes} minutes\n"
    expires_str = f"Expires: {expiry_fmt}\n\n"
    
    # Instructions
    side_lowercase = side.lower()
    instructions = (
        f"Reply to this message with:\n"
        f"yes = approve this {symbol} paper {side_lowercase}\n"
        f"no = reject this {symbol} paper {side_lowercase}\n\n"
        f"No reply = expires and no order is placed."
    )

    return (
        f"{header}"
        f"{action_line}"
        f"{amount_line}"
        f"{scores_section}"
        f"{why_section}"
        f"{movement_section}"
        f"{main_risk_str}"
        f"{decision_time_str}"
        f"{expires_str}"
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


def format_digest_message(digest_data: dict[str, Any], config: dict[str, Any]) -> str:
    mode = config.get("mode", "paper")
    live_enabled = config.get("live_enabled", False)
    if mode == "live" and live_enabled:
        mode_str = "Live trading"
    else:
        mode_str = "Paper trading only"

    def parse_dt(val: Any) -> datetime:
        if isinstance(val, str):
            val = datetime.fromisoformat(val.replace("Z", "+00:00"))
        if val.tzinfo is None:
            val = val.replace(tzinfo=UTC)
        return val

    def format_time_only(dt: datetime) -> str:
        sgt_tz = timezone(timedelta(hours=8))
        dt_sgt = dt.astimezone(sgt_tz)
        hour = dt_sgt.hour % 12
        if hour == 0:
            hour = 12
        minute = dt_sgt.strftime("%M")
        ampm = dt_sgt.strftime("%p")
        return f"{hour}:{minute} {ampm}"
        
    w_start = format_time_only(parse_dt(digest_data["window_start"]))
    w_end = format_time_only(parse_dt(digest_data["window_end"]))
    
    msg_parts = [
        f"📊 30-min market digest\n",
        f"US market: {digest_data['market_open_status']}",
        f"Window: {w_start}–{w_end} SGT",
        f"Mode: {mode_str}\n",
        "Top watched:"
    ]
    
    for idx, sym_data in enumerate(digest_data["symbols_list"]):
        rank = idx + 1
        score_val = sym_data["trade_score"]
        score_str = f"{score_val:.1f}" if isinstance(score_val, (int, float)) else "N/A"
        class_str = sym_data["trade_classification"]
        
        change_30m = sym_data["price_change_30m"]
        change_30m_str = f"{change_30m:+.2f}%" if isinstance(change_30m, (int, float)) else "0.00%"
        
        session_change = sym_data["session_change"]
        session_change_str = f"{session_change:+.2f}%" if isinstance(session_change, (int, float)) else "0.00%"
        
        msg_parts.append(
            f"{rank}. {sym_data['symbol']} — Trade score {score_str}, {class_str}\n"
            f"   30-min: {change_30m_str} | Session: {session_change_str}\n"
            f"   Status: {sym_data['status']}"
        )
        
    weakest_score = digest_data.get("weakest_score")
    weakest_score_str = f"{weakest_score:.1f}" if isinstance(weakest_score, (int, float)) else "N/A"
    msg_parts.append(
        f"\nWeakest: {digest_data['weakest_symbol']} — {weakest_score_str}, {digest_data.get('weakest_classification', 'No action suggested')}\n"
    )
    
    actions = digest_data.get("actions", {})
    msg_parts.append(
        f"Past 30 min actions:\n"
        f"Proposals: {actions.get('proposals', 0)} | Orders: {actions.get('orders', 0)} | GPT calls: {actions.get('gpt_calls', 0)} | Expired: {actions.get('expired', 0)}\n"
    )
    
    msg_parts.append(f"Summary: {digest_data.get('summary', '')}\n")
    
    if actions.get('proposals', 0) > 0:
        msg_parts.append("No action needed unless approving the active proposal above.")
    else:
        msg_parts.append("No action needed.")
        
    return "\n".join(msg_parts)


