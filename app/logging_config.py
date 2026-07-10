from __future__ import annotations

import logging
import os
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from .utils import PROJECT_ROOT, redact_sensitive_url


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        formatted = super().format(record)
        return redact_sensitive_url(formatted)


def configure_logging(level: int = logging.INFO) -> logging.Logger:
    runtime_root = Path(os.environ["TRADING_AGENT_LOG_ROOT"]) if os.getenv("TRADING_AGENT_LOG_ROOT") else PROJECT_ROOT / "logs"
    log_dir = runtime_root if os.getenv("TRADING_AGENT_LOG_ROOT") else runtime_root / "runtime"
    error_dir = runtime_root if os.getenv("TRADING_AGENT_LOG_ROOT") else runtime_root / "errors"
    log_dir.mkdir(parents=True, exist_ok=True)
    error_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(log_dir, 0o700)
        os.chmod(error_dir, 0o700)
    except OSError:
        pass
    logger = logging.getLogger("trading_agent")
    if logger.handlers:
        return logger
    logger.setLevel(level)
    formatter = RedactingFormatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    for path, handler_level, backups in [
        (log_dir / "agent.log", level, 30),
        (error_dir / "errors.log", logging.ERROR, 180),
    ]:
        handler = TimedRotatingFileHandler(path, when="midnight", backupCount=backups, encoding="utf-8")
        handler.setLevel(handler_level)
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)
    return logger
