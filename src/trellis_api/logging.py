"""Structured logging configuration for the Trellis API.

Controlled by two env vars:

- ``TRELLIS_LOG_FORMAT``  — ``json`` (default in containers) or ``console``.
- ``TRELLIS_LOG_LEVEL``   — standard log-level name; defaults to ``INFO``.

JSON mode emits one structlog-rendered JSON object per line, suitable for
CloudWatch / container log drivers. Console mode uses structlog's colorized
dev renderer for local work.
"""

from __future__ import annotations

import logging
import os

import structlog


def configure_logging() -> None:
    fmt = os.environ.get("TRELLIS_LOG_FORMAT", "json").strip().lower()
    level_name = os.environ.get("TRELLIS_LOG_LEVEL", "INFO").strip().upper()
    level = getattr(logging, level_name, logging.INFO)

    logging.basicConfig(format="%(message)s", level=level)

    processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    processors.append(
        structlog.processors.JSONRenderer()
        if fmt == "json"
        else structlog.dev.ConsoleRenderer()
    )

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
