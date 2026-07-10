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
from .utils import PROJECT_ROOT, iso_now, load_config, redact, redact_sensitive_url

def redacting_excepthook(type, value, tb):
    tb_lines = traceback.format_exception(type, value, tb)
    sanitized_tb = "".join(tb_lines)
    sys.stderr.write(redact_sensitive_url(sanitized_tb))

sys.excepthook = redacting_excepthook


def _load_runtime_environment() -> None:
    # Tests establish synthetic credentials before importing application code.
    # Never consult a developer/production .env inside the offline suite.
    if os.getenv("TRADING_AGENT_TESTING") != "1":
        load_dotenv(PROJECT_ROOT / ".env")


def run_once(config_path: str | Path | None = None) -> int:
    _load_runtime_environment()
    logger = configure_logging()
    config = load_config(config_path)
    storage = Storage(PROJECT_ROOT / config["storage"]["sqlite_path"])
    storage.initialize()
    run_id = storage.start_run(config["mode"])
    from .utils import record_process_identity
    identity = record_process_identity("scanner", run_id)
    storage.audit(run_id, "process_started", identity)
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
        service.cleanup_stale_research_runs()
        research_result = run_research_preflight(
            config,
            storage,
            recorder=lambda c: storage.record_check(run_id, c.name, c.passed, c.reason, stage="research_preflight"),
        )
        crypto_results = []
        if research_result.passed:
            crypto_results = service.run_crypto_research_due()
        else:
            skipped_reasons = [c.name for c in research_result.checks if not c.passed]
            storage.audit(run_id, "research_only_preflight_blocked", {"reasons": skipped_reasons})

        trading_result = run_trading_preflight(
            config,
            storage,
            broker,
            recorder=lambda c: storage.record_check(run_id, c.name, c.passed, c.reason, stage="preflight"),
        )
        failed_trading = [c.name for c in trading_result.checks if not c.passed]
        market_open_failed = "market_open" in failed_trading

        research_results = []
        if trading_result.passed:
            service.run_cycle(run_dynamic_universe=False)
            if research_result.passed:
                runtime_cfg = config.get("runtime_orchestration") or config.get("dynamic_universe", {}).get("runtime_orchestration", {})
                research_results = service.run_dynamic_universe_research_only(
                    timeout_seconds=int(runtime_cfg.get("market_open_research_timeout_seconds", 60)),
                    run_types=["intraday_light_refresh", "event_triggered_refresh"],
                    skip_run_types=["daily_deep_research", "post_market_review", "weekly_cleanup"],
                    label="market_open_dynamic_universe_research",
                )
            research_status = "not_due"
            if research_results:
                research_status = ",".join(sorted({str(r.get("status") or "unknown") for r in research_results}))
            storage.audit(
                run_id,
                "preflight_split_evaluated",
                {
                    "core_preflight_passed": core_result.passed,
                    "research_preflight_passed": research_result.passed,
                    "trading_preflight_passed": True,
                    "research_ran": any(r.get("status") == "completed" for r in research_results),
                    "research_status": research_status,
                    "crypto_research_ran": bool(crypto_results),
                    "trading_skipped_reason": None,
                    "market_open_required_for_trading": bool(config.get("require_market_open", True)),
                    "market_open_required_for_research": False,
                    "dynamic_universe_due": bool(research_results),
                    "daily_deep_research_due": False,
                    "market_open_research_policy": "scan_digest_first_defer_deep",
                },
            )
            storage.finish_run(run_id, "completed", "bounded paper cycle complete")
            return 0

        if research_result.passed:
            research_results = service.run_dynamic_universe_research_only(label="market_closed_dynamic_universe_research")
        research_ran = any(r.get("status") == "completed" for r in research_results)
        research_status = "not_due"
        if research_results:
            statuses = sorted({str(r.get("status") or "unknown") for r in research_results})
            research_status = ",".join(statuses)

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
            "crypto_research_ran": bool(crypto_results),
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
            if research_ran:
                market_open_only = market_open_failed and set(failed_trading) == {"market_open"}
                event_type = "research_completed_trading_blocked_market_closed"
                notification_result = service.notify_premarket_dynamic_universe_status(research_results, "market_closed" if market_open_failed else reasons)
                detail = {
                    **split_detail,
                    "research_completed": True,
                    "research_skipped_reason": None,
                    "research_status_notification_evaluated": True,
                    "research_status_notification_result": notification_result,
                }
                storage.audit(run_id, event_type, detail)
                storage.finish_run(run_id, event_type if market_open_only else "blocked", reasons)
                logger.info("%s", event_type)
                return 0 if market_open_only else 2
            storage.finish_run(run_id, "blocked", reasons)
            logger.warning("Trading preflight blocked run: %s", reasons)
            return 2

        storage.finish_run(run_id, "completed", "bounded paper cycle complete")
        return 0
    except Exception as exc:
        storage.execute("INSERT INTO errors(run_id,category,message,detail,created_at) VALUES(?,?,?,?,datetime('now'))", (run_id, "runtime", type(exc).__name__, "See local error log"))
        storage.finish_run(run_id, "error", type(exc).__name__)
        logger.exception("Run failed")
        return 1


def run_listener(config_path: str | Path | None = None) -> int:
    _load_runtime_environment()
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
    from .utils import record_process_identity
    identity = record_process_identity("telegram_listener", run_id)
    storage.audit(run_id, "process_started", identity)
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
                from .health import record_heartbeat
                record_heartbeat(storage, "listener_poll", "failed", attempted_at=iso_now(), detail={"error_type": type(e).__name__})
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
