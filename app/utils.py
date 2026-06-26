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

        sizing_section = ""
        if stop_price is not None:
            sizing_section += f"Stop price: ${stop_price:.2f} ({stop_model})\n"
        if stop_dist_pct is not None:
            sizing_section += f"Stop distance: {stop_dist_pct:.2f}% (${stop_dist_dollars:.2f})\n"

        proposed_total_exposure = proposal.get("proposed_total_exposure_pct")
        proposed_cluster_exposure = proposal.get("proposed_cluster_exposure_pct")
        if proposed_total_exposure is not None:
            sizing_section += f"Proposed total exposure: {proposed_total_exposure:.2f}%\n"
        if proposed_cluster_exposure is not None:
            sizing_section += f"Proposed cluster exposure: {proposed_cluster_exposure:.2f}%\n"

        if sizing_section:
            sizing_section = f"Sizing & Risk:\n{sizing_section}\n"

        scores_section = f"{confidence_line}{score_line}{rank_line}\n{sizing_section}"

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
            f"Since market open: {session_change_pct:+.2f}%\n"
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
            f"yes means permission to attempt, not guaranteed order. A final safety check still runs after yes."
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
        return f"* {item['symbol']} — {tradable}{held}{score_part} | Proposal {proposal_label}: {reason}"

    if tier_snapshot_has_items:
        sections = []
        
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
            f"* Proposals {actions.get('proposals', 0)} | Orders {actions.get('orders', 0)} | "
            f"Fills {actions.get('fills', 0)} | GPT {actions.get('gpt_calls', 0)} | Expired {actions.get('expired', 0)}"
        )
        sections.append(actions_sec)
        
        # 3-6. Tiers
        section_specs = [
            ("Static paper-tradable", "static_paper_tradable", 6),
            ("Dynamic paper-tradable", "dynamic_paper_tradable", 4),
            ("Observation", "observation", 6),
            ("Research candidates", "research_candidate", 6),
        ]
        trunc_labels = {
            "static_paper_tradable": "Static paper-tradable",
            "dynamic_paper_tradable": "Dynamic paper-tradable",
            "observation": "Observation",
            "research_candidate": "Research candidates",
        }
        for title, key, limit in section_specs:
            items = tier_snapshot.get(key) or []
            lines = [f"{title}:"]
            if not items:
                lines.append("* None")
            else:
                for item in items[:limit]:
                    lines.append(line_for_symbol(item))
                if len(items) > limit:
                    lines.append(f"* {trunc_labels[key]} shown: top {limit} of {len(items)} by score")
            sections.append("\n".join(lines))
            
        # 7. Universe update
        uu = digest_data.get("universe_update") or {}
        obs_promo = uu.get("promoted_to_observation") or []
        obs_promo_str = ", ".join(obs_promo) if obs_promo else "none"
        trade_promo = uu.get("promoted_to_paper_tradable") or []
        trade_promo_str = ", ".join(trade_promo) if trade_promo else "none"
        demoted = uu.get("demoted_retired") or []
        demoted_str = ", ".join(demoted) if demoted else "none"
        actions_created_str = uu.get('actions_created', 'No dynamic proposals/orders created')
        
        uu_lines = [
            "Universe update:",
            f"* Promoted to observation: {obs_promo_str}",
            f"* Promoted to paper-tradable: {trade_promo_str}",
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
        
        # 9. Summary
        paper_items = []
        for k in ["static_paper_tradable", "dynamic_paper_tradable"]:
            paper_items.extend(tier_snapshot.get(k) or [])
        paper_items = [x for x in paper_items if x.get("score_val") is not None]
        if paper_items:
            paper_items.sort(key=lambda x: (x["score_val"], x["symbol"]), reverse=True)
            highest_cand = paper_items[0]
            highest_str = f"{highest_cand['symbol']} ({highest_cand.get('score_label') or 'Trade score'} {fmt_score(highest_cand['score_val'])})"
            blocker_str = highest_cand.get("proposal_block_reason") or "None"
        else:
            highest_str = "None"
            blocker_str = "None"
            
        action_note = "No action needed unless approving the active proposal above." if actions.get('proposals', 0) > 0 else "No action needed."
        summary_lines = [
            "Summary:",
            f"* Highest tradable candidate: {highest_str}",
            f"* Main blocker: {blocker_str}",
            f"* {action_note}"
        ]
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
            f"Proposals: {actions.get('proposals', 0)} | Orders: {actions.get('orders', 0)} | Fills: {actions.get('fills', 0)} | GPT calls: {actions.get('gpt_calls', 0)} | Expired: {actions.get('expired', 0)}\n"
        )

        exit_first_blocker = digest_data.get("exit_first_blocker")
        if exit_first_blocker:
            msg_parts.append(f"Exit-first blocker: {exit_first_blocker}\n")

        msg_parts.append(f"Summary: {digest_data.get('summary', '')}\n")

        if actions.get('proposals', 0) > 0:
            msg_parts.append("No action needed unless approving the active proposal above.")
        else:
            msg_parts.append("No action needed.")

    return "\n".join(msg_parts)


def redact_sensitive_url(text: str) -> str:
    import re
    if not isinstance(text, str):
        text = str(text)
    # Redact Telegram bot token, e.g. bot12345:abc-XYZ
    text = re.sub(r"bot[0-9]+:[A-Za-z0-9_-]+", "bot<redacted_token>", text)
    # Also handle full api.telegram.org/bot... URLs
    text = re.sub(r"api\.telegram\.org/bot[^/ \'\"]+", "api.telegram.org/bot<redacted_token>", text)
    # Redact credentials/tokens in query parameters if any
    text = re.sub(r"token=[A-Za-z0-9_-]+", "token=<redacted>", text)
    return text
