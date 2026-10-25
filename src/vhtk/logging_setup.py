"""structlog setup: JSON lines to logs/ plus human-readable console output.

Call :func:`setup_logging` once at startup, then get loggers anywhere with
``structlog.get_logger(__name__)``.
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

import structlog

LOG_FILE_NAME = "vhtk.jsonl"


def setup_logging(log_dir: Path = Path("logs"), level: str = "INFO") -> None:
    """Configure structlog + stdlib logging.

    - File handler: JSON lines, one event per line, written to
      ``<log_dir>/vhtk.jsonl`` (rotated at 10 MB, 5 backups).
    - Console handler: colored, human-readable rendering of the same events.

    Args:
        log_dir: Directory for log files; created if missing.
        level: Minimum log level (``DEBUG``/``INFO``/``WARNING``/``ERROR``).
    """
    log_dir.mkdir(parents=True, exist_ok=True)

    shared_processors: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=False),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    json_formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
    )
    console_formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.dev.ConsoleRenderer(),
        ],
    )

    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / LOG_FILE_NAME,
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(json_formatter)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(console_formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(file_handler)
    root.addHandler(console_handler)
    root.setLevel(level.upper())
