from __future__ import annotations

import argparse
import os
from pathlib import Path

from dotenv import load_dotenv

from .broker_alpaca import AlpacaBroker
from .logging_config import configure_logging
from .preflight import run_preflight
from .storage import Storage
from .service import TradingService
from .utils import PROJECT_ROOT, load_config, redact


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
        try:
            broker = AlpacaBroker(config)
        except Exception as exc:
            broker = None
            logger.warning("Broker initialization unavailable: %s", type(exc).__name__)
        result = run_preflight(config, storage, broker, lock_held=os.getenv("TRADING_AGENT_LOCK_HELD") == "1", recorder=lambda c: storage.record_check(run_id, c.name, c.passed, c.reason, stage="preflight"))
        if not result.passed:
            reasons = "; ".join(c.name for c in result.checks if not c.passed)
            storage.finish_run(run_id, "blocked", reasons)
            logger.warning("Preflight blocked run: %s", reasons)
            return 2
        TradingService(config, storage, broker, run_id).run_cycle()
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
