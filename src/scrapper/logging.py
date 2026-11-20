from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Literal

import structlog

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
DEFAULT_LOG_LEVEL: LogLevel = "INFO"
DEFAULT_LOG_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_LOG_BACKUP_COUNT = 5


def configure_logging(
    log_file: Path | None,
    *,
    level: str = DEFAULT_LOG_LEVEL,
    console: bool = False,
    max_bytes: int = DEFAULT_LOG_MAX_BYTES,
    backup_count: int = DEFAULT_LOG_BACKUP_COUNT,
) -> Path | None:
    """Configure structured JSON logging for CLI runs."""
    log_level = _coerce_level(level)
    handlers: list[logging.Handler] = []
    configured_log_file = log_file.expanduser() if log_file else None
    if configured_log_file:
        configured_log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            RotatingFileHandler(
                configured_log_file,
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
        )
    if console:
        handlers.append(logging.StreamHandler(sys.stderr))

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
    ]
    formatter = structlog.stdlib.ProcessorFormatter(
        processors=(
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ),
        foreign_pre_chain=shared_processors,
    )
    root_logger = logging.getLogger()
    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)
        if getattr(handler, "_scrapper_structured_handler", False):
            handler.close()
    root_logger.setLevel(log_level)
    for handler in handlers:
        handler._scrapper_structured_handler = True  # type: ignore[attr-defined]
        handler.setLevel(log_level)
        handler.setFormatter(formatter)
        root_logger.addHandler(handler)

    logging.captureWarnings(True)
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    return configured_log_file


def _coerce_level(level: str) -> int:
    value = logging.getLevelNamesMapping().get(level.upper())
    if value is None:
        allowed = "DEBUG, INFO, WARNING, ERROR, CRITICAL"
        raise ValueError(f"Unsupported log level {level!r}. Expected one of: {allowed}.")
    return value
