from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .ai_review import AIReviewer, deterministic_review

logger = logging.getLogger("trading_agent")

from .approval_parser import parse_approval
from .execution import Executor
from .market_data import normalize_bars
from .power import get_power_status
from .risk_engine import RiskCheck, RiskEngine
from .strategy_rule_based import evaluate_symbol
from .telegram_bot import TelegramBot
from .utils import PROJECT_ROOT, iso_now, json_dumps, format_proposal_message, translate_reason, format_sgt


def _value(obj: Any, name: str, default: Any = None) -> Any:
    return getattr(obj, name, default) if not isinstance(obj, dict) else obj.get(name, default)


class TradingService:
    """One bounded launchd cycle. AI never receives a broker or execution object."""

    def __init__(self, config: dict[str, Any], storage: Any, broker: Any, run_id: str) -> None:
        self.config, self.storage, self.broker, self.run_id = config, storage, broker, run_id
        telegram = TelegramBot()
        self.telegram = telegram
        self.ai = AIReviewer(config.get("ai", {}))

    def _risk_engine(self, proposal_id: str, stage: str) -> RiskEngine:
        return RiskEngine(self.config, lambda c: self.storage.record_check(self.run_id, c.name, c.passed, c.reason, proposal_id, stage))

    def _portfolio_context(self, proposal: dict[str, Any], approval_valid: bool = False) -> dict[str, Any]:
        positions = self.broker.get_positions()
        orders = self.broker.get_open_orders()
        account = self.broker.get_account()
        symbol = proposal["symbol"]
        today_orders = self.storage.fetch_all("SELECT id FROM orders WHERE substr(created_at,1,10)=?", (datetime.now(UTC).date().isoformat(),))
        return {
            "power_connected": get_power_status().connected is True,
            "internet_available": True, "database_writable": self.storage.writable(), "broker_available": True,
            "telegram_available": True, "market_open": self.broker.is_market_open(),
            "kill_switch": (PROJECT_ROOT / "config" / "KILL_SWITCH").exists(),
            "open_positions": len(positions), "trades_today": len(today_orders),
            "duplicate_order": any(str(_value(o, "symbol", "")).upper() == symbol for o in orders),
            "same_symbol_position": any(str(_value(p, "symbol", "")).upper() == symbol for p in positions),
            "uses_margin": False, "daily_loss": 0, "weekly_loss": 0,
            "buying_power": float(_value(account, "buying_power", 0) or 0), "approval_valid": approval_valid,
        }

    def process_telegram(self) -> None:
        updates = self.telegram.get_updates(timeout=0)
        if not updates:
            return
        max_id = 0
        for update in updates:
            max_id = max(max_id, update.get("update_id", 0))
            message = update.get("message") or {}
            text = str(message.get("text", "")).strip()
            sender = str((message.get("from") or {}).get("id", ""))
            if not text:
                continue
            if text.startswith("/"):
                response = self.telegram.handle_command(text, sender)
                self.storage.audit(self.run_id, "telegram_command", {"command": text.split()[0], "authorized": self.telegram.is_authorized(sender)})
                self.telegram.send_message(response, str((message.get("chat") or {}).get("id", self.telegram.chat_id)))
                continue
            
            # Live safety check before processing approvals
            if self.config.get("mode") == "live" and not self.config.get("live_enabled"):
                self.telegram.send_message("Blocked for safety: live trading is disabled.")
                continue

            pending = self.storage.active_proposals()
            parsed = parse_approval(text, sender, self.telegram.allowed_user_id or "", pending)
            approval_id = str(uuid.uuid4())
            self.storage.execute(
                "INSERT INTO approvals(id,run_id,proposal_id,sender_id,raw_message,parsed_action,authorized,status,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (approval_id, self.run_id, parsed.proposal_id, sender, text, parsed.action, int(self.telegram.is_authorized(sender)), "accepted" if parsed.accepted else "rejected", iso_now()),
            )
            if not parsed.accepted or not parsed.proposal_id:
                msg = translate_reason(parsed.reason)
                self.telegram.send_message(msg)
                continue
            if parsed.action == "reject":
                self.storage.execute("UPDATE trade_proposals SET status='rejected' WHERE id=? AND status='pending'", (parsed.proposal_id,))
                self.telegram.send_message(f"Rejected. No order was placed for proposal {parsed.proposal_id[:8]}.")
                continue
            row = self.storage.fetch_all("SELECT * FROM trade_proposals WHERE id=?", (parsed.proposal_id,))[0]
            if not self.storage.consume_approval(parsed.proposal_id, approval_id):
                self.telegram.send_message("I did not take any action because this proposal was already handled earlier.")
                continue
            proposal = {**json.loads(row.get("payload") or "{}"), **row, "status": "approved"}
            context = self._portfolio_context(proposal, approval_valid=True)
            result = Executor(self.broker, self._risk_engine(parsed.proposal_id, "final")).execute(proposal, context)
            self.storage.execute(
                "INSERT INTO orders(id,run_id,proposal_id,broker_order_id,client_order_id,symbol,side,notional,qty,status,payload,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), self.run_id, parsed.proposal_id, str(_value(result.broker_response, "id", "")) or None, result.client_order_id, proposal["symbol"], proposal["side"], proposal.get("notional"), proposal.get("qty"), result.status, json_dumps({"submitted": result.submitted, "reason": result.reason}), iso_now(), iso_now()),
            )
            
            # User-friendly order status response
            if result.status.lower() in {"filled", "filled_fully", "orderstatus.filled"}:
                self.telegram.send_message(f"Filled. The paper order for {proposal['symbol']} was completed successfully.")
            elif result.submitted:
                self.telegram.send_message(f"Approved. A paper order was submitted for {proposal['symbol']}. Current status: pending.")
            else:
                self.telegram.send_message(f"Approved, but submission failed or was blocked: {result.reason}")
        if max_id > 0:
            self.telegram.get_updates(offset=max_id + 1, timeout=0)

    def _should_auto_execute(self, proposal: dict[str, Any]) -> bool:
        auto_enabled = self.config.get("auto_execution_enabled", False)
        auto_mode = self.config.get("auto_execution_mode", "manual_only")
        
        if not auto_enabled or auto_mode != "paper_high_confidence_only":
            return False
            
        # Hard safety: Mode MUST be paper, live auto-execution is strictly forbidden
        if self.config.get("mode") == "live" or self.config.get("live_enabled", False):
            return False
            
        score = proposal.get("score", 0)
        min_asset = self.config.get("paper_auto_min_asset_score", 90)
        min_trade = self.config.get("paper_auto_min_trade_score", 90)
        if score < min_asset or score < min_trade:
            return False
            
        notional = proposal.get("notional", 0)
        max_notional = self.config.get("paper_auto_max_notional", 1)
        if notional > max_notional:
            return False
            
        context = self._portfolio_context(proposal)
        trades_today = context.get("trades_today", 0)
        max_trades = self.config.get("paper_auto_max_trades_per_day", 1)
        if trades_today >= max_trades:
            return False
            
        if self.config.get("paper_auto_require_no_open_orders", True):
            if self.broker.get_open_orders():
                return False
                
        return True

    def notify_expired_proposals(self) -> None:
        # Find all expired proposals that haven't been notified yet
        expired_rows = self.storage.fetch_all(
            "SELECT * FROM trade_proposals WHERE status='expired' AND (expiry_notified=0 OR expiry_notified IS NULL)"
        )
        for row in expired_rows:
            proposal_id = row["id"]
            symbol = row["symbol"]
            expires_at = row["expires_at"]
            expires_fmt = format_sgt(expires_at)
            
            msg = (
                f"⏳ Proposal expired\n\n"
                f"The {symbol} paper trade proposal expired at {expires_fmt}.\n"
                f"No order was placed.\n"
                f"Reason: no yes/no reply before expiry."
            )
            self.telegram.send_message(msg)
            
            self.storage.execute(
                "UPDATE trade_proposals SET expiry_notified=1 WHERE id=?",
                (proposal_id,)
            )
            self.storage.audit(
                self.run_id,
                "proposal_expiry_notified",
                {"proposal_id": proposal_id, "symbol": symbol, "expires_at": expires_at}
            )

    def _calculate_asset_selection_score(self, symbol: str, bars: Any, price_at: Any, signal: Any, now: Any, spy_ret_20d: float | None = None) -> float:
        import pandas as pd
        # 1. Liquidity/spread quality (max 20)
        score_liq = 10.0
        if isinstance(bars, pd.DataFrame) and not bars.empty and "volume" in bars.columns:
            avg_vol = float(bars["volume"].tail(20).mean())
            if avg_vol >= 1000000:
                score_liq = 20.0
            elif avg_vol >= 500000:
                score_liq = 15.0
            elif avg_vol >= 100000:
                score_liq = 10.0
            else:
                score_liq = 5.0
                
        # 2. Trend strength (max 20)
        score_trend = 10.0
        if isinstance(bars, pd.DataFrame) and not bars.empty and len(bars) >= 50 and "close" in bars.columns:
            close = float(bars["close"].iloc[-1])
            ma_50 = float(bars["close"].tail(50).mean())
            ma_200 = float(bars["close"].tail(200).mean()) if len(bars) >= 200 else None
            if ma_200 is not None:
                if close > ma_50 and ma_50 > ma_200:
                    score_trend = 20.0
                elif close > ma_50:
                    score_trend = 15.0
                elif close > ma_200:
                    score_trend = 10.0
                else:
                    score_trend = 5.0
            else:
                score_trend = 15.0 if close > ma_50 else 5.0
                
        # 3. Volatility sanity (max 20)
        score_vol = 10.0
        vol_20 = signal.indicators.get("volatility_20")
        if vol_20 is not None and isinstance(vol_20, (int, float)) and vol_20 > 0:
            if 0.05 <= vol_20 <= 0.35:
                score_vol = 20.0
            elif 0.02 <= vol_20 <= 0.50:
                score_vol = 12.0
            else:
                score_vol = 5.0
                
        # 4. Relative strength vs SPY (max 15)
        score_rel = 10.0
        if isinstance(bars, pd.DataFrame) and not bars.empty and len(bars) >= 20 and "close" in bars.columns:
            ret_20d = float(bars["close"].iloc[-1] / bars["close"].iloc[-20]) - 1.0
            if spy_ret_20d is not None:
                if ret_20d > spy_ret_20d:
                    score_rel = 15.0
                elif ret_20d == spy_ret_20d:
                    score_rel = 10.0
                else:
                    score_rel = 5.0
                    
        # 5. Signal confirmation (max 15)
        score_sig = 5.0
        if signal.action in {"ENTRY", "EXIT"}:
            score_sig = 15.0
        elif signal.side in {"buy", "sell"}:
            score_sig = 10.0
            
        # 6. Data quality/confidence (max 10)
        age = (now - price_at).total_seconds() if price_at else float("inf")
        fresh_price = -5 <= age <= 120
        enough_bars = isinstance(bars, pd.DataFrame) and len(bars) >= 50
        if fresh_price and enough_bars:
            score_data = 10.0
        elif fresh_price:
            score_data = 5.0
        else:
            score_data = 2.0
            
        total = score_liq + score_trend + score_vol + score_rel + score_sig + score_data
        return float(round(total, 2))

    def _classify_asset_score(self, score: float) -> str:
        if score >= 80:
            return "Strong approved-universe candidate"
        if score >= 65:
            return "Moderate approved-universe candidate"
        if score >= 50:
            return "Watch only"
        return "Do not prioritize"

    def _classify_trade_score(self, score: float) -> str:
        if score >= 90:
            return "Very strong paper setup"
        if score >= 80:
            return "Strong paper setup"
        if score >= 65:
            return "Moderate paper setup"
        if score >= 50:
            return "Weak setup, watch only"
        return "No action suggested"

    def _calculate_expiry_minutes(self, symbol: str, signal: Any, vol_20: float | None, score: float, price_at: datetime, now: datetime) -> int:
        default_exp = self.config.get("proposal_expiry_default_minutes", 15)
        high_vol_thresh = self.config.get("proposal_expiry_high_volatility_threshold", 0.20)
        low_vol_thresh = self.config.get("proposal_expiry_low_volatility_threshold", 0.12)
        
        is_exit = signal.action == "EXIT" or signal.side == "sell"
        
        # Base expiry depending on Volatility and Action type
        if vol_20 is None or not isinstance(vol_20, (int, float)) or vol_20 <= 0:
            expiry_minutes = 10 if is_exit else default_exp
        elif vol_20 >= high_vol_thresh:
            expiry_minutes = 5
        elif vol_20 <= low_vol_thresh:
            expiry_minutes = 10 if is_exit else 20
        else:
            expiry_minutes = 10 if is_exit else default_exp

        # Dependency on setup confidence
        if score < 65:  # Weak setup
            expiry_minutes -= 2
        elif score >= 90:  # Very strong setup
            expiry_minutes += 2

        # Dependency on data freshness
        age = (now - price_at).total_seconds() if price_at else float("inf")
        if age > 60:
            expiry_minutes -= 3

        # Dependency on market session state (if close is within 20 mins)
        try:
            clock = self.broker.get_clock()
            if clock and clock.is_open:
                time_until_close = (clock.next_close - clock.timestamp).total_seconds() / 60
                if time_until_close < expiry_minutes:
                    expiry_minutes = max(5, int(time_until_close))
        except Exception:
            pass

        # Hard boundaries
        return max(5, min(20, expiry_minutes))


    def scan(self) -> None:
        if self.config.get("mode") == "live" and not self.config.get("live_enabled"):
            self.telegram.send_message("Blocked for safety: live trading is disabled.")
            return

        positions = self.broker.get_positions()
        orders = self.broker.get_open_orders()
        market_open = self.broker.is_market_open()
        strategy_config = __import__("yaml").safe_load((PROJECT_ROOT / "config" / "strategies.yaml").read_text())["rule_based_v1"]
        
        now = datetime.now(UTC)
        today_start = now.date().isoformat() + "T00:00:00"
        
        profiles = self.config.get("market_profiles", {})
        if not profiles:
            # Fallback if config has no profiles
            profiles = {
                "default": {
                    "status": "active",
                    "broker": "alpaca",
                    "watchlist": self.config.get("watchlist", []),
                    "observation_watchlist": [],
                    "proposals_enabled": True,
                    "execution_enabled": True
                }
            }

        for profile_key, profile in profiles.items():
            status = profile.get("status", "disabled")
            if status == "disabled":
                continue
                
            broker_name = profile.get("broker")
            if broker_name != "alpaca":
                # Report data_source_missing safely and skip
                self.storage.audit(
                    self.run_id,
                    "data_source_missing",
                    {"profile": profile_key, "broker": broker_name}
                )
                continue
                
            active_watchlist = profile.get("watchlist", [])
            obs_watchlist = profile.get("observation_watchlist", [])
            proposals_enabled = profile.get("proposals_enabled", False)
            
            all_symbols = list(dict.fromkeys(active_watchlist + obs_watchlist))
            
            spy_ret_20d = None
            if "SPY" in all_symbols or any(p.get("watchlist") and "SPY" in p.get("watchlist") for p in profiles.values()):
                try:
                    spy_bars = normalize_bars(self.broker.get_historical_bars("SPY", "1Day", 50), "SPY")
                    if not spy_bars.empty and len(spy_bars) >= 20:
                        spy_ret_20d = float(spy_bars["close"].iloc[-1] / spy_bars["close"].iloc[-20]) - 1.0
                except Exception:
                    pass
            
            profile_results = []
            
            for symbol in all_symbols:
                try:
                    trade = self.broker.get_latest_price(symbol)
                    price = float(_value(trade, "price", 0) or 0)
                    price_at = _value(trade, "timestamp", now)
                except Exception:
                    price = 0.0
                    price_at = now
                    
                bars = normalize_bars(self.broker.get_historical_bars(symbol, "1Day", 250), symbol)
                volume = float(bars.iloc[-1]["volume"]) if not bars.empty else 0.0
                
                self.storage.execute(
                    "INSERT INTO market_snapshots(run_id,symbol,price,price_at,volume,payload,created_at) VALUES(?,?,?,?,?,?,?)",
                    (self.run_id, symbol, price, price_at.isoformat() if hasattr(price_at, "isoformat") else str(price_at), volume, json_dumps({"price": price, "volume": volume}), now.isoformat())
                )
                
                has_position = any(str(_value(p, "symbol", "")).upper() == symbol for p in positions)
                has_order = any(str(_value(o, "symbol", "")).upper() == symbol for o in orders)
                signal = evaluate_symbol(symbol, bars, has_position, has_order, market_open, strategy_config["maximum_volatility_20d"], strategy_config["stop_drawdown_pct"])
                signal_id = str(uuid.uuid4())
                
                vol_20 = signal.indicators.get("volatility_20")
                asset_score = self._calculate_asset_selection_score(symbol, bars, price_at, signal, now, spy_ret_20d)
                asset_classification = self._classify_asset_score(asset_score)
                
                prev_row = self.storage.fetch_all("SELECT price, score, signal FROM market_memory WHERE symbol=? ORDER BY created_at DESC LIMIT 1", (symbol,))
                prev_price = float(prev_row[0]["price"]) if prev_row else price
                session_row = self.storage.fetch_all("SELECT price FROM market_memory WHERE symbol=? AND created_at>=? ORDER BY created_at ASC LIMIT 1", (symbol, today_start))
                session_start_price = float(session_row[0]["price"]) if session_row else price
                price_change = price - prev_price
                price_change_pct = (price / prev_price - 1) * 100 if prev_price > 0 else 0.0
                session_change = price - session_start_price
                session_change_pct = (price / session_start_price - 1) * 100 if session_start_price > 0 else 0.0
                
                # Part B scoring weighting:
                # 1. Strategy signal strength (max 25)
                score_rule = 25.0 if signal.action in {"ENTRY", "EXIT"} else 0.0
                
                # 2. Asset selection/rank (max 15)
                score_asset = 15.0 if asset_score >= 80 else (12.0 if asset_score >= 65 else (8.0 if asset_score >= 50 else 3.0))
                
                # 3. Recent 10-minute movement (max 10)
                score_5m = 5.0
                if prev_row:
                    if signal.side == "buy":
                        score_5m = 10.0 if price > prev_price else (5.0 if price == prev_price else 0.0)
                    elif signal.side == "sell":
                        score_5m = 10.0 if price < prev_price else (5.0 if price == prev_price else 0.0)
                    else:
                        score_5m = 5.0 if price == prev_price else (10.0 if price > prev_price else 0.0)
                
                # 4. Session trend (max 10)
                score_session = 5.0
                if session_row:
                    if signal.side == "buy":
                        score_session = 10.0 if price > session_start_price else (5.0 if price == session_start_price else 0.0)
                    elif signal.side == "sell":
                        score_session = 10.0 if price < session_start_price else (5.0 if price == session_start_price else 0.0)
                    else:
                        score_session = 5.0 if price == session_start_price else (10.0 if price > session_start_price else 0.0)
                
                # 5. Volatility sanity (max 15)
                max_vol = strategy_config.get("maximum_volatility_20d", 0.05)
                score_vol = 15.0 if (vol_20 is not None and vol_20 <= max_vol) else 0.0
                
                # 6. Risk safety (max 15)
                port_context = self._portfolio_context({"symbol": symbol, "side": signal.side or "buy", "action": "entry"})
                safety_ok = True
                if port_context.get("duplicate_order") or port_context.get("trades_today", 0) >= self.config["risk"].get("max_trades_per_day", 1):
                    safety_ok = False
                if signal.action == "ENTRY" and port_context.get("open_positions", 0) >= self.config["risk"].get("max_open_positions", 1):
                    safety_ok = False
                score_safety = 15.0 if safety_ok else 0.0
                
                # 7. Data quality/freshness (max 10)
                age = (now - price_at).total_seconds() if price_at else float("inf")
                fresh_price = -5 <= age <= self.config["risk"].get("max_price_age_seconds", 120)
                enough_bars = len(bars) >= self.config["risk"].get("min_historical_bars", 50)
                score_data = 10.0 if (fresh_price and enough_bars) else (5.0 if fresh_price else 0.0)
                
                # Calculate final trade decision score
                score = float(round(score_rule + score_asset + score_5m + score_session + score_vol + score_safety + score_data, 2))
                classification = self._classify_trade_score(score)
                
                # System confidence
                system_confidence = "No action suggested"
                if score >= 90:
                    system_confidence = "Very strong"
                elif score >= 80:
                    system_confidence = "Strong"
                elif score >= 65:
                    system_confidence = "Moderate"
                elif score >= 50:
                    system_confidence = "Weak"
                
                # Dynamic expiry
                expiry_minutes = self._calculate_expiry_minutes(symbol, signal, vol_20, score, price_at, now)
                
                high_vol_thresh = self.config.get("proposal_expiry_high_volatility_threshold", 0.20)
                low_vol_thresh = self.config.get("proposal_expiry_low_volatility_threshold", 0.12)
                if vol_20 is None or not isinstance(vol_20, (int, float)) or vol_20 <= 0:
                    volatility_class = "normal"
                elif vol_20 >= high_vol_thresh:
                    volatility_class = "high"
                elif vol_20 <= low_vol_thresh:
                    volatility_class = "low"
                else:
                    volatility_class = "normal"
                
                expiry = now + timedelta(minutes=expiry_minutes)
                
                self.storage.execute("INSERT INTO signals(id,run_id,symbol,side,action,strategy_version,reason,confidence,created_at,expires_at,payload) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (signal_id, self.run_id, symbol, signal.side, signal.action, signal.strategy_version, signal.reason, signal.confidence, now.isoformat(), expiry.isoformat(), json_dumps(signal.indicators)))
                self.storage.execute("INSERT INTO indicators(run_id,symbol,values_json,created_at) VALUES(?,?,?,?)", (self.run_id, symbol, json_dumps(signal.indicators), now.isoformat()))
                
                profile_results.append({
                    "symbol": symbol,
                    "price": price,
                    "price_at": price_at,
                    "bars": bars,
                    "volume": volume,
                    "has_position": has_position,
                    "has_order": has_order,
                    "signal": signal,
                    "signal_id": signal_id,
                    "vol_20": vol_20,
                    "expiry_minutes": expiry_minutes,
                    "volatility_class": volatility_class,
                    "expiry": expiry,
                    "prev_price": prev_price,
                    "price_change": price_change,
                    "price_change_pct": price_change_pct,
                    "session_start_price": session_start_price,
                    "session_change": session_change,
                    "session_change_pct": session_change_pct,
                    "score": score,
                    "classification": classification,
                    "system_confidence": system_confidence,
                    "asset_score": asset_score,
                    "asset_classification": asset_classification,
                })

            profile_results.sort(key=lambda x: x["asset_score"], reverse=True)
            any_generated = False
            
            for idx, res in enumerate(profile_results):
                rank = idx + 1
                symbol = res["symbol"]
                price = res["price"]
                price_at = res["price_at"]
                bars = res["bars"]
                volume = res["volume"]
                signal = res["signal"]
                signal_id = res["signal_id"]
                vol_20 = res["vol_20"]
                expiry_minutes = res["expiry_minutes"]
                volatility_class = res["volatility_class"]
                expiry = res["expiry"]
                prev_price = res["prev_price"]
                price_change = res["price_change"]
                price_change_pct = res["price_change_pct"]
                session_start_price = res["session_start_price"]
                session_change = res["session_change"]
                session_change_pct = res["session_change_pct"]
                score = res["score"]
                classification = res["classification"]
                system_confidence = res["system_confidence"]
                asset_score = res["asset_score"]
                asset_classification = res["asset_classification"]
                
                ai_config = self.config.get("ai", {})
                proposal_allowed = (symbol in active_watchlist and proposals_enabled and signal.action in {"ENTRY", "EXIT"} and score >= ai_config.get("ai_review_min_score", 65))
                gpt_called = False
                proposal_generated = False
                no_action_reason = ""
                proposal_id = None
                decision = None
                proposal = None
                review = None
                
                if not proposal_allowed:
                    if symbol not in active_watchlist:
                        no_action_reason = "symbol not in active watchlist"
                    elif not proposals_enabled:
                        no_action_reason = "proposals disabled for profile"
                    elif signal.action not in {"ENTRY", "EXIT"}:
                        no_action_reason = f"no entry/exit signal ({signal.reason})"
                    else:
                        no_action_reason = f"trade score below threshold ({score} < {ai_config.get('ai_review_min_score', 65)})"
                else:
                    calls_today = len(self.storage.fetch_all("SELECT id FROM ai_reviews WHERE created_at >= ?", (today_start,)))
                    last_call = self.storage.fetch_all("SELECT created_at FROM ai_reviews WHERE proposal_id IN (SELECT id FROM trade_proposals WHERE symbol=?) ORDER BY created_at DESC LIMIT 1", (symbol,))
                    time_since = (now - datetime.fromisoformat(last_call[0]["created_at"].replace("Z", "+00:00")).replace(tzinfo=UTC)).total_seconds() / 60 if last_call else float("inf")
                    if (ai_config.get("ai_review_on_every_run", False) or (calls_today < ai_config.get("ai_daily_call_limit", 10) and self.ai.calls_made < ai_config.get("ai_max_calls_per_run", 2) and time_since >= ai_config.get("ai_review_min_interval_minutes", 30))):
                        gpt_called = True
                    
                    proposal_id = str(uuid.uuid4())
                    proposal = {"id": proposal_id, "run_id": self.run_id, "signal_id": signal_id, "symbol": symbol, "side": signal.side, "action": "entry" if signal.action == "ENTRY" else "exit", "notional": float(self.config["risk"].get("max_trade_notional_paper" if self.config.get("mode") == "paper" else "max_trade_notional_live", 5)), "latest_price": price, "price_at": str(price_at), "historical_bars": len(bars), "volume": volume, "price_gap_pct": float((price / float(bars.iloc[-1]["close"]) - 1) * 100) if not bars.empty and float(bars.iloc[-1]["close"]) > 0 else 0.0, "created_at": now.isoformat(), "expires_at": expiry.isoformat(), "strategy_version": signal.strategy_version, "reason": signal.reason, "order_type": "market", "asset_class": "equity", "indicators": signal.indicators, "score": score, "classification": classification, "system_confidence": system_confidence, "expiry_minutes": expiry_minutes, "volatility_class": volatility_class, "asset_score": asset_score, "asset_classification": asset_classification, "symbol_rank": rank, "total_active_symbols": len(active_watchlist), "price_change_pct": price_change_pct, "session_change_pct": session_change_pct, "gpt_called": gpt_called}
                    
                    if self._should_auto_execute(proposal):
                        proposal_generated = True
                        no_action_reason = "auto-executed"
                        any_generated = True
                    else:
                        decision = self._risk_engine(proposal_id, "proposal").evaluate(proposal, self._portfolio_context(proposal))
                        if not decision.passed:
                            no_action_reason = f"blocked by risk checks: {'; '.join(decision.reasons)}"
                        else:
                            proposal_generated = True
                            no_action_reason = "proposal generated"
                            any_generated = True

                if proposal_allowed:
                    review = self.ai.review(proposal) if gpt_called else deterministic_review(proposal, warning="AI review throttled to avoid spam")
                    proposal["review"] = review

                g_conf = review.get("gpt_confidence", "Not called") if (gpt_called and review) else "Not called"
                g_caut = review.get("gpt_caution", "Low") if (gpt_called and review) else "N/A"
                m_risk = review.get("main_risk", "No AI risk evaluation was performed.") if (gpt_called and review) else "N/A"
                exp_sgt = format_sgt(expiry)

                self.storage.execute(
                    "INSERT INTO market_memory(run_id,market_profile,symbol,price,prev_price,price_change,price_change_pct,session_start_price,session_change,volatility,signal,score,classification,reason,proposal_allowed,gpt_called,created_at,asset_score,asset_classification,symbol_rank,proposal_generated,no_action_reason,asset_selection_score,trade_decision_score,system_confidence,gpt_confidence,gpt_caution,expiry_minutes,expires_at_sgt,main_risk) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (self.run_id, profile_key, symbol, price, prev_price, price_change, price_change_pct, session_start_price, session_change, vol_20 or 0.0, signal.action, score, classification, signal.reason, int(proposal_allowed), int(gpt_called), now.isoformat(), asset_score, asset_classification, rank, int(proposal_generated), no_action_reason, asset_score, score, system_confidence, g_conf, g_caut, expiry_minutes, exp_sgt, m_risk)
                )
                
                logger.info(
                    "Symbol: %s | Profile: %s | Asset Score: %.2f (%s) | Trade Score: %.2f (%s) | Rank: #%d | Prev Change: %.2f%% | Session Change: %.2f | Proposal Allowed: %s | GPT Called: %s | Proposal Generated: %s | No-Action Reason: %s",
                    symbol, profile_key, asset_score, asset_classification, score, classification, rank, price_change_pct, session_change, proposal_allowed, gpt_called, proposal_generated, no_action_reason or "N/A"
                )
                
                if not proposal_generated:
                    if proposal_allowed and decision and not decision.passed:
                        self.storage.audit(self.run_id, "proposal_blocked", {"symbol": symbol, "reasons": decision.reasons})
                    continue
                
                if no_action_reason == "auto-executed":
                    approval_id = str(uuid.uuid4())
                    self.storage.execute("INSERT INTO approvals(id,run_id,proposal_id,sender_id,raw_message,parsed_action,authorized,status,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (approval_id, self.run_id, proposal_id, "system_auto", "AUTO_EXECUTE", "approve", 1, "accepted", iso_now()))
                    self.storage.execute("INSERT INTO trade_proposals(id,run_id,signal_id,symbol,side,notional,status,created_at,expires_at,strategy_version,payload) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (proposal_id, self.run_id, signal_id, symbol, signal.side, proposal["notional"], "approved", now.isoformat(), expiry.isoformat(), signal.strategy_version, json_dumps(proposal)))
                    self.storage.consume_approval(proposal_id, approval_id)
                    context = self._portfolio_context(proposal, approval_valid=True)
                    if self.config.get("paper_auto_require_final_revalidation", True): context["final_revalidation"] = True
                    result = Executor(self.broker, self._risk_engine(proposal_id, "final")).execute(proposal, context)
                    self.storage.execute("INSERT INTO orders(id,run_id,proposal_id,broker_order_id,client_order_id,symbol,side,notional,qty,status,payload,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (str(uuid.uuid4()), self.run_id, proposal_id, str(_value(result.broker_response, "id", "")) or None, result.client_order_id, symbol, signal.side, proposal.get("notional"), proposal.get("qty"), result.status, json_dumps({"submitted": result.submitted, "reason": result.reason}), iso_now(), iso_now()))
                    self.telegram.send_message(f"⚡ [AUTO-EXECUTED] A high-confidence paper order was automatically submitted for {symbol}." if result.submitted else f"⚡ [AUTO-EXECUTION BLOCKED] Auto-execution attempted for {symbol} but failed/blocked: {result.reason}")
                else:
                    self.storage.execute("INSERT INTO trade_proposals(id,run_id,signal_id,symbol,side,notional,status,created_at,expires_at,strategy_version,payload) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (proposal_id, self.run_id, signal_id, symbol, signal.side, proposal["notional"], "pending", now.isoformat(), expiry.isoformat(), signal.strategy_version, json_dumps(proposal)))
                    self.storage.execute("INSERT INTO ai_reviews(run_id,proposal_id,summary,risks,caution_level,payload,created_at) VALUES(?,?,?,?,?,?,?)", (self.run_id, proposal_id, review["summary"], json_dumps(review["risks"]), review["caution_level"], json_dumps(review), iso_now()))
                    self.telegram.send_message(f"Proposal {proposal_id}\n\n" + format_proposal_message(proposal, self.config))

            if profile_results:
                best_watch_res = profile_results[0]
                active_results = [r for r in profile_results if r["symbol"] in active_watchlist]
                best_trade_res = max(active_results, key=lambda x: x["score"]) if active_results else (max(profile_results, key=lambda x: x["score"]) if profile_results else None)
                
                logger.info("=== Profile '%s' Scan Summary ===", profile_key)
                logger.info("Best symbol to watch: %s (Asset Score: %.2f)", best_watch_res["symbol"], best_watch_res["asset_score"])
                if best_trade_res:
                    logger.info("Best symbol for trade consideration: %s (Trade Score: %.2f)", best_trade_res["symbol"], best_trade_res["score"])
                else:
                    logger.info("Best symbol for trade consideration: None")
                
                if any_generated:
                    logger.info("Why no proposal was generated: N/A (Proposal was generated)")
                else:
                    reasons_summary = ", ".join(f"{r['symbol']}: {r.get('no_action_reason') or 'N/A'}" for r in profile_results)
                    logger.info("Why no proposal was generated: %s", reasons_summary)

    def check_and_send_digest(self) -> None:
        digest_config = self.config.get("digest", {})
        if not digest_config.get("telegram_digest_enabled", True):
            return

        now = datetime.now(UTC)
        interval_minutes = digest_config.get("telegram_digest_interval_minutes", 30)

        try:
            market_open = self.broker.is_market_open()
        except Exception:
            market_open = False

        if not market_open and not digest_config.get("telegram_digest_send_when_market_closed", False):
            return

        # 1. Throttling
        last_sent = self.storage.fetch_all(
            "SELECT sent_at FROM telegram_digests WHERE status='sent' ORDER BY sent_at DESC LIMIT 1"
        )
        if last_sent:
            last_sent_dt = datetime.fromisoformat(last_sent[0]["sent_at"].replace("Z", "+00:00")).replace(tzinfo=UTC)
            elapsed_mins = (now - last_sent_dt).total_seconds() / 60
            if elapsed_mins < (interval_minutes - 2):
                return

        # 2. Minimum cycles
        window_start = now - timedelta(minutes=interval_minutes)
        window_start_iso = window_start.isoformat()
        
        cycles = self.storage.fetch_all(
            "SELECT COUNT(DISTINCT run_id) as cnt FROM market_memory WHERE created_at >= ?",
            (window_start_iso,)
        )
        cycle_count = cycles[0]["cnt"] if cycles else 0
        min_cycles = digest_config.get("telegram_digest_min_cycles_required", 2)
        if cycle_count < min_cycles:
            return

        # 3. Retrieve rows
        include_obs = digest_config.get("telegram_digest_include_observation_symbols", True)
        profiles = self.config.get("market_profiles", {})
        active_watchlist = []
        obs_watchlist = []
        for p in profiles.values():
            if p.get("status") == "active":
                active_watchlist.extend(p.get("watchlist", []))
                obs_watchlist.extend(p.get("observation_watchlist", []))
                
        allowed_symbols = set(active_watchlist)
        if include_obs:
            allowed_symbols.update(obs_watchlist)

        rows = self.storage.fetch_all(
            "SELECT * FROM market_memory WHERE created_at >= ? ORDER BY created_at ASC",
            (window_start_iso,)
        )
        if not rows:
            return

        import collections
        symbol_rows = collections.defaultdict(list)
        for row in rows:
            sym = row["symbol"]
            if allowed_symbols and sym not in allowed_symbols:
                continue
            symbol_rows[sym].append(row)

        if not symbol_rows:
            return

        symbols_list = []
        for sym, s_rows in symbol_rows.items():
            first_row = s_rows[0]
            latest_row = s_rows[-1]
            p_first = first_row["price"]
            p_latest = latest_row["price"]
            change_30m = ((p_latest / p_first) - 1.0) * 100.0 if p_first > 0 else 0.0
            
            p_session_start = latest_row.get("session_start_price") or p_latest
            session_change = ((p_latest / p_session_start) - 1.0) * 100.0 if p_session_start > 0 else 0.0
            
            has_prop = any(bool(r.get("proposal_generated")) for r in s_rows)
            was_auto = any(r.get("no_action_reason") == "auto-executed" for r in s_rows)
            
            status_str = "Watch"
            if was_auto:
                status_str = "Auto-executed"
            elif has_prop:
                status_str = "Proposal generated, pending approval"
            elif latest_row.get("signal") in {"ENTRY", "EXIT"}:
                status_str = "Watch, no proposal"
                
            symbols_list.append({
                "symbol": sym,
                "trade_score": latest_row["score"],
                "trade_classification": latest_row["classification"],
                "asset_score": latest_row.get("asset_score"),
                "price_change_30m": change_30m,
                "session_change": session_change,
                "status": status_str
            })

        symbols_list.sort(key=lambda x: x["trade_score"] if x["trade_score"] is not None else -1, reverse=True)

        strongest = symbols_list[0]
        weakest = min(symbols_list, key=lambda x: x["trade_score"] if x["trade_score"] is not None else 1000)

        max_syms = digest_config.get("telegram_digest_max_symbols", 6)
        top_watched = symbols_list[:max_syms]

        proposals = self.storage.fetch_all(
            "SELECT COUNT(*) as cnt FROM trade_proposals WHERE created_at >= ?",
            (window_start_iso,)
        )
        prop_cnt = proposals[0]["cnt"] if proposals else 0
        
        orders = self.storage.fetch_all(
            "SELECT COUNT(*) as cnt FROM orders WHERE created_at >= ?",
            (window_start_iso,)
        )
        order_cnt = orders[0]["cnt"] if orders else 0
        
        gpt_calls = sum(bool(r.get("gpt_called")) for r in rows)
        
        expired = self.storage.fetch_all(
            "SELECT COUNT(*) as cnt FROM trade_proposals WHERE status='expired' AND expires_at >= ? AND expires_at <= ?",
            (window_start_iso, now.isoformat())
        )
        expired_cnt = expired[0]["cnt"] if expired else 0

        if prop_cnt > 0:
            pending_rows = self.storage.fetch_all(
                "SELECT symbol, side FROM trade_proposals WHERE created_at >= ? AND status='pending'",
                (window_start_iso,)
            )
            if pending_rows:
                syms_p = ", ".join(f"{r['side'].upper()} {r['symbol']}" for r in pending_rows)
                summary_str = f"{strongest['symbol']} is strongest, pending proposal for {syms_p}."
            else:
                summary_str = f"Setup triggered action for {strongest['symbol']} during the window."
        else:
            summary_str = f"{strongest['symbol']} is strongest, but no setup crossed the proposal threshold."

        digest_data = {
            "market_open_status": "Open" if market_open else "Closed",
            "window_start": window_start,
            "window_end": now,
            "symbols_list": top_watched,
            "weakest_symbol": weakest["symbol"],
            "weakest_score": weakest["trade_score"],
            "weakest_classification": weakest["trade_classification"],
            "actions": {
                "proposals": prop_cnt,
                "orders": order_cnt,
                "gpt_calls": gpt_calls,
                "expired": expired_cnt
            },
            "summary": summary_str
        }

        from .utils import format_digest_message
        message_text = format_digest_message(digest_data, self.config)
        
        try:
            self.telegram.send_message(message_text)
            status = "sent"
        except Exception as e:
            status = "error"
            self.storage.record_check(self.run_id, "digest_send", False, str(e), stage="digest")
            
        symbols_str = ", ".join(x["symbol"] for x in top_watched)
        self.storage.execute(
            "INSERT INTO telegram_digests(run_id,window_start,window_end,sent_at,symbols,summary_text,status) VALUES(?,?,?,?,?,?,?)",
            (self.run_id, window_start_iso, now.isoformat(), now.isoformat(), symbols_str, summary_str, status)
        )
        self.storage.audit(self.run_id, "digest_processed", {"status": status, "window_start": window_start_iso, "window_end": now.isoformat()})

    def run_cycle(self) -> None:
        self.notify_expired_proposals()
        self.process_telegram()
        if not (PROJECT_ROOT / "config" / "KILL_SWITCH").exists():
            self.scan()
        self.check_and_send_digest()



