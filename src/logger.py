from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import structlog

from src.config import LoggingConfig


def setup_logging(cfg: LoggingConfig) -> structlog.stdlib.BoundLogger:
    """Configure structured logging with JSON output to file and console."""
    log_level = getattr(logging, cfg.level.upper(), logging.INFO)

    # Ensure log directory exists
    log_path = Path(cfg.file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # Root logger setup
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    root_logger.handlers.clear()

    # Console handler (human-readable)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(log_level)
    root_logger.addHandler(console_handler)

    # File handler (JSON, rotating)
    file_handler = RotatingFileHandler(
        cfg.file,
        maxBytes=cfg.max_file_size_mb * 1024 * 1024,
        backupCount=cfg.rotate_count,
    )
    file_handler.setLevel(log_level)
    root_logger.addHandler(file_handler)

    # Structlog configuration
    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
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

    # IMPORTANT: Each handler MUST have its own formatter instance.
    # Sharing a single ProcessorFormatter between handlers causes recursive
    # rendering because format() mutates the log record in-place.
    console_formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.dev.ConsoleRenderer(),
        ],
        foreign_pre_chain=shared_processors,
    )
    console_handler.setFormatter(console_formatter)

    file_renderer = structlog.processors.JSONRenderer() if cfg.format == "json" else structlog.dev.ConsoleRenderer()
    file_formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            file_renderer,
        ],
        foreign_pre_chain=shared_processors,
    )
    file_handler.setFormatter(file_formatter)

    return structlog.get_logger("polymarket")
