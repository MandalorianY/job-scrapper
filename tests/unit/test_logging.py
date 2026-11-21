from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
import structlog

from scrapper.logging import configure_logging


@pytest.fixture
def restore_logging() -> Iterator[None]:
    root_logger = logging.getLogger()
    original_handlers = list(root_logger.handlers)
    original_level = root_logger.level
    try:
        yield
    finally:
        for handler in list(root_logger.handlers):
            root_logger.removeHandler(handler)
            if getattr(handler, "_scrapper_structured_handler", False):
                handler.close()
        for handler in original_handlers:
            root_logger.addHandler(handler)
        root_logger.setLevel(original_level)
        structlog.contextvars.clear_contextvars()
        structlog.reset_defaults()


def test_configure_logging_writes_structured_json(tmp_path: Path, restore_logging: None) -> None:
    log_file = tmp_path / "logs" / "job-scrapper.jsonl"

    configured_log_file = configure_logging(log_file, level="INFO")
    structlog.contextvars.bind_contextvars(run_id="test-run")
    structlog.get_logger("scrapper.test").info("test_event", answer=42)
    for handler in logging.getLogger().handlers:
        handler.flush()

    assert configured_log_file == log_file
    record = json.loads(log_file.read_text(encoding="utf-8").strip())
    assert record["event"] == "test_event"
    assert record["answer"] == 42
    assert record["run_id"] == "test-run"
    assert record["logger"] == "scrapper.test"
    assert record["level"] == "info"
    assert record["timestamp"]
    assert "_record" not in record
    assert "_from_structlog" not in record


def test_configure_logging_rejects_unknown_level(tmp_path: Path, restore_logging: None) -> None:
    with pytest.raises(ValueError, match="Unsupported log level"):
        configure_logging(tmp_path / "job-scrapper.jsonl", level="TRACE")
