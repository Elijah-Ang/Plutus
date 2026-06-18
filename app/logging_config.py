from __future__ import annotations

import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from .utils import PROJECT_ROOT


def configure_logging(level: int = logging.INFO) -> logging.Logger:
    log_dir = PROJECT_ROOT / "logs" / "runtime"
    error_dir = PROJECT_ROOT / "logs" / "errors"
    log_dir.mkdir(parents=True, exist_ok=True)
    error_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("trading_agent")
    if logger.handlers:
        return logger
    logger.setLevel(level)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
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
