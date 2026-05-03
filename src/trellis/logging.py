"""Shared structlog configuration for stdio-style processes.

Both the Trellis CLI and the MCP server reserve stdout for their
output (``--format json`` payloads and JSON-RPC frames respectively).
structlog's default ``PrintLoggerFactory`` writes to stdout, which
corrupts that channel. Pinning the factory to ``sys.stderr`` keeps
logs visible to operators while leaving stdout exclusively for
protocol / payload traffic.

The Trellis API uses a richer config (``trellis_api.logging``) that
bridges uvicorn's stdlib loggers and supports JSON output for log
shippers — this module is intentionally narrower.
"""

from __future__ import annotations

import logging
import os
import sys

import structlog


def configure_stderr_logging() -> None:
    """Route structlog output to stderr; honour ``TRELLIS_LOG_LEVEL``."""
    level_name = os.environ.get("TRELLIS_LOG_LEVEL", "INFO").strip().upper()
    level = getattr(logging, level_name, logging.INFO)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )


__all__ = ["configure_stderr_logging"]
