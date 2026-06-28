from __future__ import annotations

import argparse
import os
from pathlib import Path

from dotenv import load_dotenv

from .broker_alpaca import AlpacaBroker
from .logging_config import configure_logging
from .preflight import run_core_preflight, run_research_preflight, run_trading_preflight
from .storage import Storage
import sys
import traceback
from .service import TradingService
from .utils import PROJECT_ROOT, load_config, redact, redact_sensitive_url

def redacting_excepthook(type, value, tb):
    tb_lines = traceback.format_exception(type, value, tb)
    sanitized_tb = "".join(tb_lines)
    sys.stderr.write(redact_sensitive_url(sanitized_tb))

sys.excepthook = redacting_excepthook


def run_once(config_path: str | Path | None = None) -> int:
    load_dotenv(PROJECT_ROOT / ".env")
    logger = configure_logging()
    config = load_config(config_path)
    storage = Storage(PROJECT_ROOT / config["storage"]["sqlite_path"])
    storage.initialize()
    run_id = storage.start_run(config["mode"])
    if os.getenv("TRADING_AGENT_STALE_LOCK_RECOVERED") == "1":
        storage.audit(run_id, "stale_lock_recovered", {"lock": "logs/runtime/agent.lockdir"})
    storage.execute("INSERT INTO config_snapshots(run_id,config_json,created_at) VALUES(?,?,datetime('now'))", (run_id, __import__("json").dumps(redact(config), sort_keys=True)))
    try:
        core_result = run_core_preflight(
            config,
            storage,
            lock_held=os.getenv("TRADING_AGENT_LOCK_HELD") == "1",
            recorder=lambda c: storage.record_check(run_id, c.name, c.passed, c.reason, stage="preflight"),
        )
        if not core_result.passed:
            reasons = "; ".join(c.name for c in core_result.checks if not c.passed)
            storage.audit(run_id, "preflight_split_evaluated", {"core_preflight_passed": False, "research_ran": False, "trading_preflight_passed": False, "trading_skipped_reason": "core_preflight_failed"})
            storage.finish_run(run_id, "blocked", reasons)
            logger.warning("Core preflight blocked run: %s", reasons)
            return 2

        try:
            broker = AlpacaBroker(config)
        except Exception as exc:
            broker = None
            logger.warning("Broker initialization unavailable: %s", type(exc).__name__)
        service = TradingService(config, storage, broker, run_id)
        research_result = run_research_preflight(
            config,
            storage,
            recorder=lambda c: storage.record_check(run_id, c.name, c.passed, c.reason, stage="research_preflight"),
        )
        research_results = []
        if research_result.passed:
            research_results = service.run_dynamic_universe_research_only()
        else:
            skipped_reasons = [c.name for c in research_result.checks if not c.passed]
            storage.audit(run_id, "research_only_preflight_blocked", {"reasons": skipped_reasons})
        research_ran = any(r.get("status") == "completed" for r in research_results)
        research_status = "not_due"
        if research_results:
            statuses = sorted({str(r.get("status") or "unknown") for r in research_results})
            research_status = ",".join(statuses)

        trading_result = run_trading_preflight(
            config,
            storage,
            broker,
            recorder=lambda c: storage.record_check(run_id, c.name, c.passed, c.reason, stage="preflight"),
        )
        failed_trading = [c.name for c in trading_result.checks if not c.passed]
        market_open_failed = "market_open" in failed_trading
        catchup_required = False
        try:
            catchup_required = bool(storage.fetch_all("SELECT 1 FROM dynamic_universe_schedule_state WHERE catchup_required=1 LIMIT 1"))
        except Exception:
            catchup_required = False
        split_detail = {
            "core_preflight_passed": core_result.passed,
            "research_preflight_passed": research_result.passed,
            "trading_preflight_passed": trading_result.passed,
            "research_ran": research_ran,
            "research_status": research_status,
            "trading_skipped_reason": "; ".join(failed_trading) if failed_trading else None,
            "market_open_required_for_trading": bool(config.get("require_market_open", True)),
            "market_open_required_for_research": False,
            "dynamic_universe_due": bool(research_results),
            "daily_deep_research_due": any(r.get("run_type") == "daily_deep_research" for r in research_results),
            "catchup_required": catchup_required,
        }
        storage.audit(run_id, "preflight_split_evaluated", split_detail)
        if not trading_result.passed:
            reasons = "; ".join(failed_trading)
            if market_open_failed and set(failed_trading) == {"market_open"} and research_results:
                event_type = "research_completed_trading_blocked_market_closed" if research_ran else "research_checked_trading_blocked_market_closed"
                storage.audit(run_id, event_type, split_detail)
                service.notify_premarket_dynamic_universe_status(research_results, "market_closed")
                storage.finish_run(run_id, event_type, "market_open")
                logger.info("%s", event_type)
                return 0
            storage.finish_run(run_id, "blocked", reasons)
            logger.warning("Trading preflight blocked run: %s", reasons)
            return 2

        service.run_cycle(run_dynamic_universe=False)
        storage.finish_run(run_id, "completed", "bounded paper cycle complete")
        return 0
    except Exception as exc:
        storage.execute("INSERT INTO errors(run_id,category,message,detail,created_at) VALUES(?,?,?,?,datetime('now'))", (run_id, "runtime", type(exc).__name__, "See local error log"))
        storage.finish_run(run_id, "error", type(exc).__name__)
        logger.exception("Run failed")
        return 1


def run_listener(config_path: str | Path | None = None) -> int:
    load_dotenv(PROJECT_ROOT / ".env")
    logger = configure_logging()
    config = load_config(config_path)
    storage = Storage(PROJECT_ROOT / config["storage"]["sqlite_path"])
    storage.initialize()
    
    telegram_cfg = config.get("telegram", {})
    if not telegram_cfg.get("telegram_approval_listener_enabled", True):
        logger.error("Telegram approval listener is disabled in config")
        return 1
        
    poll_interval = telegram_cfg.get("telegram_approval_poll_interval_seconds", 30)
    logger.info("Starting Telegram approval listener. Mode: %s, Poll interval: %ds",
                telegram_cfg.get("telegram_approval_listener_mode", "approval_only"),
                poll_interval)
                
    run_id = storage.start_run("listener")
    storage.audit(run_id, "listener_started", {"poll_interval": poll_interval})
    
    try:
        broker = AlpacaBroker(config)
    except Exception as exc:
        broker = None
        logger.warning("Broker initialization unavailable: %s", type(exc).__name__)
        
    service = TradingService(config, storage, broker, run_id)
    
    import time
    try:
        while True:
            try:
                service.process_telegram()
            except Exception as e:
                logger.exception("Error processing Telegram updates: %s", e)
            time.sleep(poll_interval)
    except KeyboardInterrupt:
        logger.info("Listener stopped by user")
        storage.finish_run(run_id, "stopped", "listener stopped by KeyboardInterrupt")
        return 0
    except Exception as exc:
        storage.execute("INSERT INTO errors(run_id,category,message,detail,created_at) VALUES(?,?,?,?,datetime('now'))", (run_id, "listener", type(exc).__name__, "See local error log"))
        storage.finish_run(run_id, "error", f"Listener crashed: {type(exc).__name__}")
        logger.exception("Listener failed")
        return 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Run TradingAgent once (paper-only default)")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--mode", type=str, choices=["once", "listener"], default="once")
    args = parser.parse_args()
    if args.mode == "listener":
        raise SystemExit(run_listener(args.config))
    else:
        raise SystemExit(run_once(args.config))


if __name__ == "__main__":
    main()
