from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def get_git_commit() -> str:
    try:
        res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
            check=False
        )
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
    except Exception:
        pass
    manifest_path = PROJECT_ROOT / "release-manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        commit = str(manifest.get("release_commit") or "").strip()
        if commit:
            return commit
    except Exception:
        pass
    return "unknown"


def is_git_clean() -> bool:
    try:
        res = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
            check=False
        )
        if res.returncode == 0:
            return len(res.stdout.strip()) == 0
    except Exception:
        pass
    # Immutable releases intentionally contain no .git directory. Their
    # manifest is created from a clean committed archive before write bits are
    # removed, so presence of a valid pinned release commit is the clean-state
    # proof used by production identity reporting.
    try:
        manifest = json.loads((PROJECT_ROOT / "release-manifest.json").read_text(encoding="utf-8"))
        return bool(manifest.get("release_commit") and manifest.get("release_id"))
    except Exception:
        pass
    return False


BOOT_COMMIT = get_git_commit()


def record_process_identity(role: str, run_id: str) -> dict[str, Any]:
    now = datetime.now(UTC)
    identity = {
        "role": role,
        "run_id": run_id,
        "pid": os.getpid(),
        "start_time": now.isoformat(),
        "project_root": str(PROJECT_ROOT),
        "commit": BOOT_COMMIT,
        "git_clean": is_git_clean(),
    }
    runtime_dir = Path(os.environ["TRADING_AGENT_STATE_ROOT"]) / "runtime" if os.getenv("TRADING_AGENT_STATE_ROOT") else PROJECT_ROOT / "logs" / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    json_path = runtime_dir / f"{role}_identity.json"
    try:
        with json_path.open("w", encoding="utf-8") as handle:
            json.dump(identity, handle, indent=2)
        os.chmod(json_path, 0o600)
    except Exception:
        pass
    return identity


def check_listener_freshness() -> dict[str, Any]:
    current_head = get_git_commit()
    runtime_dir = Path(os.environ["TRADING_AGENT_STATE_ROOT"]) / "runtime" if os.getenv("TRADING_AGENT_STATE_ROOT") else PROJECT_ROOT / "logs" / "runtime"
    identity_path = runtime_dir / "telegram_listener_identity.json"
    status = {
        "running": False,
        "pid": None,
        "start_time": None,
        "startup_commit": None,
        "current_head": current_head,
        "fresh": False,
        "mismatch": False,
        "message": "Telegram listener is not running."
    }
    if identity_path.exists():
        try:
            with identity_path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            pid = data.get("pid")
            if pid:
                try:
                    os.kill(pid, 0)
                    status["running"] = True
                except OSError:
                    status["running"] = False
            if status["running"]:
                status["pid"] = pid
                status["start_time"] = data.get("start_time")
                status["startup_commit"] = data.get("commit")
                if status["startup_commit"] == current_head:
                    status["fresh"] = True
                    status["message"] = "Telegram listener is running and fresh."
                else:
                    status["mismatch"] = True
                    status["message"] = "Telegram listener is stale and must be restarted"
            else:
                status["message"] = "Telegram listener is not running (stale pid)."
        except Exception as e:
            status["message"] = f"Error reading listener status: {e}"
    return status



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
    from .configuration import effective_config_hash, validate_config

    validate_config(config)
    config["effective_config_hash"] = effective_config_hash(config)
    return config


def secret_present(name: str) -> bool:
    value = get_secret(name)
    return bool(value and not value.startswith("replace_with_"))


def get_secret(name: str) -> str | None:
    """Compatibility boundary for callers that need plaintext client credentials."""
    from .secrets import default_secret_store

    return default_secret_store().get_plaintext(name)


def json_dumps(value: Any) -> str:
    return json.dumps(value, default=str, sort_keys=True, separators=(",", ":"))


def redact(value: Any) -> Any:
    sensitive = ("key", "secret", "token", "password", "account_id", "authorization", "cookie")
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

    if is_fake_test and os.getenv("TRADING_AGENT_TESTING") != "1":
        raise RuntimeError("fake TEST proposals are restricted to isolated tests")
    if is_fake_test:
        return (
            f"🧪 Fake paper test proposal\n\n"
            f"This is only testing the Telegram approval flow.\n"
            f"No Alpaca order will be placed for this fake TEST symbol.\n\n"
            f"Reply yes to approve the test, or no to reject it.\n"
            f"Time to decide: {expiry_minutes} minutes\n"
            f"Expires: {expiry_fmt}"
        )

    side_lower = side.lower()

    if side_lower == "buy":
        is_add = proposal.get("action") == "add" or bool(proposal.get("is_add", False))
        if is_add:
            pm_type = proposal.get("position_management_decision_type")
            header = "📈 Paper add-to-winner proposal\n\n" if pm_type == "HEALTHY_PULLBACK_ADD" else "📄 Paper trade ADD proposal\n\n"
            action_line = f"Add to {symbol} — {mode_notice}\n"

            avg_entry = proposal.get("average_entry_price")
            current_drawdown = proposal.get("position_drawdown_pct")
            pm = proposal.get("position_management_decision") or {}

            avg_entry_str = f"Current avg entry: ${avg_entry:.2f}\n" if avg_entry else ""
            drawdown_str = f"Current drawdown: {current_drawdown * 100:.2f}%\n" if current_drawdown is not None else ""
            gain_str = f"Current gain: {float(pm['unrealized_profit_pct']):+.2f}%\n" if pm.get("unrealized_profit_pct") is not None else ""
            peak_str = f"Peak gain: {float(pm['max_unrealized_profit_pct']):+.2f}%\n" if pm.get("max_unrealized_profit_pct") is not None else ""
            pullback_str = f"Pullback from peak: {float(pm['pullback_from_peak_pct']):.2f}%\n" if pm.get("pullback_from_peak_pct") is not None else ""
            pos_stats = f"{avg_entry_str}{drawdown_str}{gain_str}{peak_str}{pullback_str}"
            if pos_stats:
                pos_stats = f"Current position:\n{pos_stats}\n"
            else:
                pos_stats = ""

            notional_adjustment_note = proposal.get("notional_adjustment_note", "")
            health_note = "This is a healthy pullback inside a winning position, not averaging down.\n" if pm_type == "HEALTHY_PULLBACK_ADD" else ""
            amount_line = f"{pos_stats}{health_note}Add amount: ${notional:.0f}{notional_adjustment_note}\n\n"
        else:
            header = "📄 Paper trade proposal\n\n"
            action_line = f"Buy {symbol} — {mode_notice}\n"
            notional_adjustment_note = proposal.get("notional_adjustment_note", "")
            amount_line = f"Amount: ${notional:.0f}{notional_adjustment_note}\n\n"

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
        strategy_state = proposal.get("strategy_state")
        policy_line = (
            f"Strategy authorization: {strategy_state} — authorised for operational paper entry risk\n"
            f"Strategy policy: {strategy_state}\n"
            if strategy_state in {"PROBE", "EXPLORATION", "THROTTLED", "ACTIVE"}
            else (
                f"Strategy authorization: {strategy_state}\nStrategy policy: {strategy_state}\n"
                if strategy_state
                else ""
            )
        )
        if strategy_state == "PROBE":
            policy_line += "PROBE controls: new entry only; no adds; manual Telegram approval; 0.03% stop risk; 0.10% heat; 2.5% gross; one active/reserved.\n"

        watchlist_order = proposal.get("watchlist_order")
        total_active = proposal.get("total_active_symbols")
        true_score_rank = proposal.get("true_score_rank")
        eligible_rank = proposal.get("proposal_eligible_rank")
        selection_reason = proposal.get("selection_reason")

        rank_line = ""
        if watchlist_order is not None and total_active is not None:
            rank_line += f"Watchlist order: #{watchlist_order} of {total_active}\n"
        if true_score_rank is not None and total_active is not None:
            rank_line += f"Score rank: #{true_score_rank} of {total_active} active ETFs\n"
        if eligible_rank is not None:
            rank_line += f"Eligible proposal rank: #{eligible_rank} currently eligible candidate\n"
        if selection_reason:
            rank_line += f"Selection reason: {selection_reason}\n"

        # Sizing and Stops section
        stop_price = proposal.get("stop_price")
        stop_dist_pct = proposal.get("stop_distance_pct")
        stop_dist_dollars = proposal.get("stop_distance_dollars")
        stop_model = proposal.get("stop_model_used", "default")

        risk_budget = proposal.get("risk_budget")
        score_multiplier = proposal.get("score_multiplier")
        volatility_multiplier = proposal.get("volatility_multiplier")
        caps_applied = proposal.get("caps_applied")

        # Account context
        equity = proposal.get("portfolio_equity") or proposal.get("indicators", {}).get("portfolio_equity")
        cash = proposal.get("cash") or proposal.get("indicators", {}).get("cash")
        buying_power = proposal.get("buying_power") or proposal.get("indicators", {}).get("buying_power")

        account_section = ""
        if equity is not None:
            account_section += f"Account Equity: ${float(equity):,.2f}\n"
        if cash is not None:
            account_section += f"Available Cash: ${float(cash):,.2f}\n"
        if buying_power is not None:
            account_section += f"Buying Power: ${float(buying_power):,.2f} (authoritative broker capacity; margin use is forbidden)\n"

        if account_section:
            account_section = f"Account Context:\n{account_section}\n"

        sizing_section = ""
        if stop_price is not None:
            sizing_section += f"Stop price: ${stop_price:.2f} ({stop_model})\n"
        if stop_dist_pct is not None:
            sizing_section += f"Stop distance: {stop_dist_pct:.2f}% (${stop_dist_dollars:.2f})\n"
        if risk_budget is not None:
            sizing_section += f"Risk budget: ${risk_budget:.2f}\n"

        sizing_basis_parts = []
        if risk_budget is not None:
            sizing_basis_parts.append(f"risk budget (${risk_budget:.2f})")
        if stop_dist_pct is not None:
            sizing_basis_parts.append(f"stop distance ({stop_dist_pct:.1f}%)")
        if score_multiplier is not None:
            sizing_basis_parts.append(f"score multiplier ({score_multiplier:.2f}x)")
        if volatility_multiplier is not None:
            sizing_basis_parts.append(f"volatility multiplier ({volatility_multiplier:.2f}x)")

        if sizing_basis_parts:
            sizing_section += f"Sizing basis: {', '.join(sizing_basis_parts)}\n"

        if caps_applied and caps_applied != "none":
            sizing_section += f"Main cap applied: {caps_applied}\n"

        proposed_total_exposure = proposal.get("proposed_total_exposure_pct")
        proposed_cluster_exposure = proposal.get("proposed_cluster_exposure_pct")
        if proposed_total_exposure is not None:
            sizing_section += f"Proposed total exposure: {proposed_total_exposure:.2f}%\n"
        if proposed_cluster_exposure is not None:
            sizing_section += f"Proposed cluster exposure: {proposed_cluster_exposure:.2f}%\n"

        if sizing_section:
            sizing_section = f"Sizing & Risk:\n{sizing_section}\n"

        adaptive_section = ""
        adaptive = proposal.get("adaptive_conviction") or {}
        if adaptive:
            adaptive_section = (
                "Adaptive Conviction (operational paper): "
                f"{adaptive.get('deployment_mode')}/{adaptive.get('opportunity_class')}; "
                f"permitted stop risk {float(adaptive.get('recommended_stop_risk_pct') or 0.0):.4f}%; "
                f"heat target {float(adaptive.get('portfolio_heat_target_pct') or 0.0):.2f}%, "
                f"gross target {float(adaptive.get('gross_exposure_target_pct') or 0.0):.1f}%; "
                f"binding {adaptive.get('binding_cap')}.\n\n"
            )
        adaptive_sizing = proposal.get("adaptive_sizing") or {}
        if adaptive_sizing:
            stop_risk_dollars = float(adaptive_sizing.get("stop_risk_dollars") or proposal.get("stop_risk_dollars") or 0.0)
            stop_risk_pct = float(adaptive.get("recommended_stop_risk_pct") or proposal.get("permitted_stop_risk_pct") or 0.0)
            sizing_reason = adaptive_sizing.get("reason") or adaptive.get("reason") or "bounded by conviction and authoritative ceilings"
            adaptive_section += (
                "Adaptive Sizing (operational paper): "
                f"actual proposed ${float(adaptive_sizing.get('operational_notional') or 0.0):,.2f} "
                f"({float(adaptive_sizing.get('operational_quantity') or 0.0):.6f} shares); "
                f"stop risk {stop_risk_pct:.4f}% (${stop_risk_dollars:,.2f}); "
                f"canonical baseline ${float(proposal.get('baseline_operational_notional') or 0.0):,.2f}; "
                f"{adaptive_sizing.get('comparison_direction')}, binding {adaptive_sizing.get('binding_adaptive_cap')}. "
                f"Reason: {sizing_reason}. This displayed adaptive size is the maximum that approval can submit.\n\n"
            )

        scores_section = f"{confidence_line}{score_line}{policy_line}{rank_line}\n{account_section}{sizing_section}{adaptive_section}"

        raw_reason = proposal.get("reason", "")
        if "volatility normal" in raw_reason or "volatility_normal" in raw_reason or "normal" in raw_reason.lower():
            why_text = "The longer-term trend passed the bot’s filters, and volatility is normal for this ETF."
        elif "volatility elevated" in raw_reason or "volatility_elevated" in raw_reason or "elevated" in raw_reason.lower():
            why_text = "The longer-term trend passed the bot’s filters, and volatility is elevated for this ETF."
        else:
            why_text = "The longer-term trend passed the bot’s filters, and volatility is normal for this ETF."

        revival_reason = proposal.get("revival_reason")
        if revival_reason:
            if "score improved" in revival_reason:
                revival_msg = "This setup was re-proposed because the score improved by +10 since the last proposal."
            elif "volatility" in revival_reason:
                match = re.search(r"improved from (\w+) to (\w+)", revival_reason)
                if match:
                    revival_msg = f"This setup was re-proposed because volatility improved from {match.group(1)} to {match.group(2)}."
                else:
                    revival_msg = "This setup was re-proposed because volatility improved."
            else:
                revival_msg = f"This setup was re-proposed: {revival_reason}."
            why_text += f"\n{revival_msg}"

        why_section = f"Why this appeared:\n{why_text}\n\n"

        price_change_pct = proposal.get("price_change_pct", 0.0)
        session_change_pct = proposal.get("session_change_pct", 0.0)
        vol_20 = proposal.get("volatility") or proposal.get("indicators", {}).get("volatility_20")

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
            f"Since first stored observation today (UTC): {session_change_pct:+.2f}%\n"
            f"{vol_regime_str}\n"
        )

        review = proposal.get("review")
        gpt_called = proposal.get("gpt_called", True)
        if gpt_called and review:
            ai_confidence = review.get("gpt_confidence", "Not called")
            ai_caution = review.get("gpt_caution", "Low")
            main_risk = review.get("main_risk", "No AI risk evaluation was performed.")
            ai_review_section = (
                f"AI review: Completed\n"
                f"AI confidence: {ai_confidence}\n"
                f"AI caution: {ai_caution}\n"
                f"Main risk:\n{main_risk}\n\n"
            )
        else:
            ai_review_section = (
                f"AI review: Not available\n"
                f"Rule-based only. AI review was not available. Treat with extra caution.\n\n"
            )

        decision_time_str = f"Decision time: {expiry_minutes} minutes\n"
        expires_str = f"Expires: {expiry_fmt}\n\n"

        side_lower_instr = "add" if is_add else "buy"
        instructions = (
            f"Reply to this message with:\n"
            f"yes = approve this {symbol} paper {side_lower_instr}\n"
            f"no = reject this {symbol} paper {side_lower_instr}\n\n"
            f"No reply = expires and no order is placed.\n"
            f"yes means permission to attempt, not guaranteed order. A final safety check still runs after yes.\n"
            f"Final order size will be revalidated before placement."
        )

        return (
            f"{header}"
            f"{action_line}"
            f"{amount_line}"
            f"{scores_section}"
            f"{why_section}"
            f"{movement_section}"
            f"{ai_review_section}"
            f"{decision_time_str}"
            f"{expires_str}"
            f"{instructions}"
        )

    else:
        # Sell / Exit proposal
        pm_type = proposal.get("position_management_decision_type")
        if pm_type:
            pm = proposal.get("position_management_decision") or {}
            title_map = {
                "TAKE_PROFIT_PARTIAL": "💰 Paper profit-taking proposal",
                "PROFIT_PROTECT_EXIT": "🛡️ Paper profit-protection proposal",
                "TRAILING_STOP_EXIT": "📉 Paper trailing-stop exit proposal",
                "TIME_STOP_EXIT": "⏱️ Paper time-stop exit proposal",
            }
            header = f"{title_map.get(pm_type, '📄 Paper position-management proposal')}: {symbol}\n"
            current_gain = pm.get("unrealized_profit_pct")
            peak_gain = pm.get("max_unrealized_profit_pct")
            giveback = pm.get("profit_giveback_ratio")
            r_mult = pm.get("current_r_multiple")
            trailing_stop = pm.get("trailing_stop_price")
            sell_fraction = proposal.get("position_management_sell_fraction") or pm.get("suggested_sell_fraction")
            sell_pct = float(sell_fraction or 0.0) * 100.0
            details = [
                f"Current gain: {float(current_gain):+.2f}%" if current_gain is not None else None,
                f"Peak gain: {float(peak_gain):+.2f}%" if peak_gain is not None else None,
                f"R-multiple: {float(r_mult):+.2f}R" if r_mult is not None else None,
                f"Profit giveback: {float(giveback) * 100:.1f}%" if giveback is not None else None,
                f"Trailing stop: ${float(trailing_stop):.2f}" if trailing_stop is not None else None,
                f"Suggested action: Sell {sell_pct:.0f}%" if sell_pct else "Suggested action: Sell partial position",
                f"Estimated shares: {float(proposal.get('qty') or 0.0):.6f}",
                f"Estimated notional: ${float(proposal.get('notional') or 0.0):.2f}",
                f"Why:\n{proposal.get('reason') or pm.get('reason') or 'Position management rule triggered.'}",
                "Reply:\nyes = approve\nno = reject\n\nNo reply = expires and no order is placed.\nyes means permission to attempt after final safety check.",
            ]
            return header + "\n".join(item for item in details if item)

        header = "📄 Paper sell proposal\n\n"
        action_line = f"Sell {symbol} — {mode_notice}\n"
        if qty is not None:
            amount_line = f"Quantity: {qty:.4f} shares\n" if isinstance(qty, float) and qty % 1 != 0 else f"Quantity: {int(qty)} shares\n"
        else:
            amount_line = ""

        exit_trigger = proposal.get("exit_trigger_reason", "exit condition met")
        trigger_line = f"Reason for exit: {exit_trigger}\n"

        drawdown_pct = proposal.get("position_drawdown_pct", 0.0)
        drawdown_str = f"Current drawdown: {drawdown_pct * 100:.2f}%\n" if drawdown_pct is not None else ""

        latest_price_val = proposal.get("latest_price")
        price_str = f"Latest price: ${latest_price_val:.2f}\n" if latest_price_val is not None else ""

        avg_entry = proposal.get("average_entry_price")
        avg_entry_str = f"Average entry price: ${avg_entry:.2f}\n" if avg_entry is not None else ""

        details_section = f"{amount_line}{trigger_line}{drawdown_str}{price_str}{avg_entry_str}\n"

        # AI Explanation
        gpt_exit_explanation_status = proposal.get("gpt_exit_explanation_status") or "Not available; using rule-based exit reason"
        review = proposal.get("review")
        gpt_called = proposal.get("gpt_called", False)

        if gpt_called and review:
            ai_confidence = review.get("gpt_confidence", "Not called")
            ai_caution = review.get("gpt_caution", "Low")
            main_risk = review.get("main_risk", "No AI risk evaluation was performed.")
            ai_explanation_section = (
                f"AI explanation: Completed\n"
                f"AI confidence: {ai_confidence}\n"
                f"AI caution: {ai_caution}\n"
                f"Main risk:\n{main_risk}\n\n"
            )
        else:
            ai_explanation_section = (
                f"AI explanation: {gpt_exit_explanation_status}\n"
            )
            if review and review.get("main_risk"):
                ai_explanation_section += f"Main risk:\n{review.get('main_risk')}\n\n"
            else:
                ai_explanation_section += "\n"

        decision_time_str = f"Decision time: {expiry_minutes} minutes\n"
        expires_str = f"Expires: {expiry_fmt}\n\n"

        instructions = (
            f"Reply to this message with:\n"
            f"yes = approve this {symbol} paper {side_lower}\n"
            f"no = reject this {symbol} paper {side_lower}\n\n"
            f"No reply = expires and no order is placed.\n"
            f"yes means permission to attempt exit after final safety check."
        )

        return (
            f"{header}"
            f"{action_line}"
            f"{details_section}"
            f"{ai_explanation_section}"
            f"{decision_time_str}"
            f"{expires_str}"
            f"{instructions}"
        )

    return (
        f"{header}"
        f"{action_line}"
        f"{amount_line}"
        f"{scores_section}"
        f"{why_section}"
        f"{movement_section}"
        f"{ai_review_section}"
        f"{decision_time_str}"
        f"{expires_str}"
        f"{instructions}"
    )



def translate_reason(reason: str) -> str:
    if "message is not an unambiguous approval or rejection" in reason:
        return "I did not take any action because I could not tell whether you meant yes or no. Please reply yes to approve or no to reject."
    if "identify proposal when pending count is not one" in reason:
        return "I found multiple pending proposals. Please reply directly to the proposal message, or include the symbol/proposal ID."
    if "ambiguous plain action with multiple pending proposals" in reason:
        return "I found multiple pending proposals. Please reply directly to the proposal message, or include the symbol/proposal ID."
    if "exactly one matching pending proposal is required" in reason:
        return "I did not take any action because I could not match your reply to a single pending proposal. Please specify the proposal ID or symbol."
    if "proposal expired" in reason:
        return "⏳ This proposal has already expired. No order was placed."
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

    tier_snapshot = digest_data.get("tier_snapshot") or {}
    tier_snapshot_has_items = any(tier_snapshot.get(key) for key in tier_snapshot)

    def fmt_score(value: Any) -> str:
        if isinstance(value, (int, float)):
            if float(value).is_integer():
                return f"{int(value)}"
            return f"{value:.1f}"
        return "N/A"

    def line_for_symbol(item: dict[str, Any]) -> str:
        held = " | Held" if item.get("held") else ""
        source_label = item.get("source_label")
        source_prefix = f"{source_label} | " if source_label else ""
        tradable = "Tradable" if item.get("tradable") else "Not tradable"
        proposal = item.get("proposal_allowed") or "blocked"
        reason = item.get("proposal_block_reason") or "not eligible"
        proposal_label = "allowed" if proposal == "allowed" else "blocked"
        score_val = item.get("score_val")
        score_label = item.get("score_label")
        if score_val is None:
            score_val = item.get("score")
            score_label = score_label or "Score"
        else:
            score_label = score_label or "Trade score"
        score_part = f" | {score_label} {fmt_score(score_val)}" if score_val is not None else ""
        status = item.get("status")
        status_part = f" | Status: {status}" if status else ""
        return f"* {item['symbol']} — {source_prefix}{tradable}{held}{score_part} | Proposal {proposal_label}: {reason}{status_part}"

    if tier_snapshot_has_items:
        sections = []
        paper_tradable_items = tier_snapshot.get("paper_tradable")
        if paper_tradable_items is None:
            paper_tradable_items = list(tier_snapshot.get("static_paper_tradable") or []) + list(tier_snapshot.get("dynamic_paper_tradable") or [])
            paper_tradable_items.sort(key=lambda x: (-(x.get("score_val") if x.get("score_val") is not None else x.get("score") if x.get("score") is not None else -1.0), x.get("symbol") or ""))
        
        # 1. Market
        market_sec = (
            "📊 30-min market digest\n\n"
            "Market:\n"
            f"* US market: {digest_data['market_open_status']}\n"
            f"* Window: {w_start}–{w_end} SGT\n"
            f"* Mode: {mode_str}"
        )
        sections.append(market_sec)
        
        # 2. Actions
        actions = digest_data.get("actions", {})
        actions_sec = (
            "Actions:\n"
            f"* Proposals: {actions.get('proposals', 0)} | Orders: {actions.get('orders', 0)} | "
            f"Fills: {actions.get('fills', 0)} | GPT: {actions.get('gpt_calls', 0)} | Expired: {actions.get('expired', 0)}"
        )
        perf = digest_data.get("performance_lab") or {}
        if perf:
            actions_sec += (
                f"\n* Performance Lab: tracked {perf.get('tracked', 0)} setups, "
                f"proposed {perf.get('proposed', 0)}, suppressed {perf.get('suppressed', 0)}, "
                f"{perf.get('outcome_status', 'outcomes pending')}."
            )
        exit_watch = digest_data.get("exit_watch")
        if exit_watch:
            actions_sec += f"\n* {exit_watch}"
        proposal_capacity = digest_data.get("proposal_capacity")
        if proposal_capacity:
            actions_sec += f"\n* {proposal_capacity}"
        crypto_research = digest_data.get("crypto_research")
        if crypto_research:
            actions_sec += f"\n* {crypto_research}"
        strategy_policy = digest_data.get("strategy_policy")
        if strategy_policy:
            actions_sec += f"\n* Strategy policy: {strategy_policy}"
        adaptive_conviction = digest_data.get("adaptive_conviction")
        if adaptive_conviction:
            actions_sec += f"\n* {adaptive_conviction}"
        sections.append(actions_sec)
        
        # 3-6. Tiers
        section_specs = [
            ("Paper-tradable", "paper_tradable", 8),
            ("Observation", "observation", 6),
            ("Research candidates", "research_candidate", 6),
        ]
        trunc_labels = {
            "paper_tradable": "Paper-tradable",
            "observation": "Observation",
            "research_candidate": "Research candidates",
        }
        for title, key, limit in section_specs:
            items = paper_tradable_items if key == "paper_tradable" else tier_snapshot.get(key) or []
            lines = [f"{title}:"]
            if not items:
                lines.append("* None")
            else:
                for item in items[:limit]:
                    lines.append(line_for_symbol(item))
                if len(items) > limit:
                    lines.append(f"* {trunc_labels[key]} shown: top {limit} of {len(items)} by score")
            sections.append("\n".join(lines))

        dynamic_items = tier_snapshot.get("dynamic_paper_tradable") or []
        if not dynamic_items and paper_tradable_items:
            sections.append("Dynamic paper-tradable:\n* None")
            
        # 7. Universe update
        uu = digest_data.get("universe_update") or {}
        obs_promo = uu.get("promoted_to_observation") or []
        obs_promo_str = ", ".join(obs_promo) if obs_promo else "none"
        global_updates = uu.get("global_research_only_updated") or []
        global_updates_str = ", ".join(global_updates) if global_updates else "none"
        static_reconciled = uu.get("static_paper_tradable_reconciled") or []
        static_reconciled_str = ", ".join(static_reconciled) if static_reconciled else "none"
        trade_promo = uu.get("promoted_to_paper_tradable") or []
        trade_promo_str = ", ".join(trade_promo) if trade_promo else "none"
        demoted = uu.get("demoted_retired") or []
        demoted_str = ", ".join(demoted) if demoted else "none"
        actions_created_str = uu.get('actions_created', 'No dynamic proposals/orders created')
        
        uu_lines = [
            "Universe update:",
            f"* Static paper-tradable reconciled: {static_reconciled_str}",
            f"* Dynamic paper-tradable promotions: {trade_promo_str}",
            f"* Global research-only tracked: {global_updates_str}",
            f"* Observation promoted: {obs_promo_str}",
            f"* Demoted/retired: {demoted_str}",
            f"* {actions_created_str}"
        ]
        sections.append("\n".join(uu_lines))
        
        # 8. Provider status
        prov_sec = (
            "Provider status:\n"
            f"* {digest_data.get('provider_status', 'EODHD: ok for current research subtasks')}"
        )
        sections.append(prov_sec)

        exit_first_blocker = digest_data.get("exit_first_blocker")
        if exit_first_blocker:
            sections.append(f"Exit-first blocker: {exit_first_blocker}")
        
        # 9. Summary
        paper_items = list(paper_tradable_items or [])
        paper_items = [x for x in paper_items if x.get("score_val") is not None]
        if paper_items:
            paper_items.sort(key=lambda x: (x["score_val"], x["symbol"]), reverse=True)
            highest_cand = paper_items[0]
            highest_str = f"{highest_cand['symbol']} ({highest_cand.get('score_label') or 'Trade score'} {fmt_score(highest_cand['score_val'])})"
            blocker_str = highest_cand.get("proposal_block_reason") or "None"
        else:
            highest_str = "None"
            blocker_str = "None"
            
        if actions.get("active_proposals", 0) > 0:
            action_note = "No action needed unless approving the active proposal above."
        elif actions.get("expired", 0) > 0 and actions.get("orders", 0) == 0:
            action_note = "No active proposal remains; previous proposal expired with no order."
        else:
            action_note = "No action needed."
        summary_lines = [
            "Summary:",
            f"* Highest tradable candidate: {highest_str}",
            f"* Main blocker: {blocker_str}",
        ]
        if digest_data.get("summary"):
            summary_lines.append(f"Summary: {digest_data.get('summary')}")
        summary_lines.append(f"* {action_note}")
        sections.append("\n".join(summary_lines))
        
        return "\n\n".join(sections)

    else:
        msg_parts = [
            f"📊 30-min market digest\n",
            f"US market: {digest_data['market_open_status']}",
            f"Window: {w_start}–{w_end} SGT",
            f"Mode: {mode_str}\n",
        ]
        msg_parts.append("Top watched:")
        for idx, sym_data in enumerate(digest_data.get("symbols_list") or []):
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
                f"   30-min: {change_30m_str} | Since first UTC-day observation: {session_change_str}\n"
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
            f"Proposals: {actions.get('proposals', 0)} | Orders: {actions.get('orders', 0)} | Fills: {actions.get('fills', 0)} | GPT calls: {actions.get('gpt_calls', 0)} | Expired: {actions.get('expired', 0)}\n"
        )
        perf = digest_data.get("performance_lab") or {}
        if perf:
            msg_parts.append(
                f"Performance Lab: tracked {perf.get('tracked', 0)} setups, proposed {perf.get('proposed', 0)}, "
                f"suppressed {perf.get('suppressed', 0)}, {perf.get('outcome_status', 'outcomes pending')}.\n"
            )
        if digest_data.get("exit_watch"):
            msg_parts.append(f"{digest_data['exit_watch']}\n")
        if digest_data.get("proposal_capacity"):
            msg_parts.append(f"{digest_data['proposal_capacity']}\n")
        if digest_data.get("crypto_research"):
            msg_parts.append(f"{digest_data['crypto_research']}\n")
        if digest_data.get("adaptive_conviction"):
            msg_parts.append(f"{digest_data['adaptive_conviction']}\n")

        exit_first_blocker = digest_data.get("exit_first_blocker")
        if exit_first_blocker:
            msg_parts.append(f"Exit-first blocker: {exit_first_blocker}\n")

        msg_parts.append(f"Summary: {digest_data.get('summary', '')}\n")

        if actions.get("active_proposals", 0) > 0:
            msg_parts.append("No action needed unless approving the active proposal above.")
        elif actions.get("expired", 0) > 0 and actions.get("orders", 0) == 0:
            msg_parts.append("No active proposal remains; previous proposal expired with no order.")
        else:
            msg_parts.append("No action needed.")

    return "\n".join(msg_parts)


def redact_sensitive_url(text: str, registered_secrets: tuple[str, ...] | list[str] = ()) -> str:
    import re
    if not isinstance(text, str):
        text = str(text)
    # Redact Telegram bot token, e.g. bot12345:abc-XYZ
    text = re.sub(r"bot[0-9]+:[A-Za-z0-9_-]+", "bot<redacted_token>", text)
    # Also handle full api.telegram.org/bot... URLs
    text = re.sub(r"api\.telegram\.org/bot[^/ \'\"]+", "api.telegram.org/bot<redacted_token>", text)
    # Redact common URL query credentials, preserving the parameter name.
    text = re.sub(
        r"(?i)([?&](?:token|api[_-]?key|access[_-]?token|secret|password)=)[^&#\s'\"]+",
        r"\1<redacted>",
        text,
    )
    # Redact headers, environment-style assignments and JSON/log key-value pairs.
    text = re.sub(r"(?i)(authorization\s*:\s*(?:bearer|basic)\s+)[^\s,;]+", r"\1<redacted>", text)
    text = re.sub(
        r"(?i)(\b(?:ALPACA_(?:API|SECRET)_KEY|TELEGRAM_BOT_TOKEN|OPENAI_API_KEY|EODHD_API_TOKEN)\s*=\s*)[^\s,;]+",
        r"\1<redacted>",
        text,
    )
    text = re.sub(
        r'(?i)(["\'](?:api[_-]?key|secret(?:[_-]?key)?|token|password|authorization)["\']\s*:\s*["\'])[^"\']+(["\'])',
        r"\1<redacted>\2",
        text,
    )
    # Credential-bearing database URLs and webhook-style URL userinfo.
    text = re.sub(
        r"(?i)(\b[a-z][a-z0-9+.-]*://[^\s:/@]+:)[^\s@/]+(@)",
        r"\1<redacted>\2",
        text,
    )
    for value in registered_secrets:
        if value:
            text = text.replace(str(value), "<redacted>")
    return text


def redact_exception(exc: BaseException, registered_secrets: tuple[str, ...] | list[str] = ()) -> str:
    """Return a bounded, redacted exception chain without request/credential reprs."""
    parts: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen and len(parts) < 8:
        seen.add(id(current))
        parts.append(f"{type(current).__name__}: {redact_sensitive_url(str(current), registered_secrets)}")
        current = current.__cause__ or current.__context__
    return " <- ".join(parts)
