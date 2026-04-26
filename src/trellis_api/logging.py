"""Structured logging configuration for the Trellis API.

Controlled by two env vars:

- ``TRELLIS_LOG_FORMAT``  — ``json`` (default in containers) or ``console``.
- ``TRELLIS_LOG_LEVEL``   — standard log-level name; defaults to ``INFO``.

JSON mode emits one structlog-rendered JSON object per line, suitable for
CloudWatch / container log drivers. Console mode uses structlog's colorized
dev renderer for local work.

Both Trellis's own ``structlog`` loggers and Uvicorn's stdlib ``logging``
loggers (``uvicorn``, ``uvicorn.error``, ``uvicorn.access``) flow through
the same processor chain via :class:`structlog.stdlib.ProcessorFormatter`.
That guarantees container log drivers see one shape per line — no half
of the stream in JSON and the other half in Uvicorn's default text.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import structlog

#: Uvicorn loggers we route through structlog. ``uvicorn`` is the
#: lifecycle/startup logger, ``uvicorn.error`` is general output, and
#: ``uvicorn.access`` is the per-request line. Pinning the list keeps
#: behaviour predictable when uvicorn adds new sub-loggers in future
#: releases — the cost of missing one is a single text line in JSON
#: mode, not a crash.
_UVICORN_LOGGERS: tuple[str, ...] = (
    "uvicorn",
    "uvicorn.error",
    "uvicorn.access",
)


def configure_logging() -> None:
    fmt = os.environ.get("TRELLIS_LOG_FORMAT", "json").strip().lower()
    level_name = os.environ.get("TRELLIS_LOG_LEVEL", "INFO").strip().upper()
    level = getattr(logging, level_name, logging.INFO)

    # Shared chain: every event — structlog-native or stdlib-bridged —
    # passes through these processors before the renderer. Splitting
    # the shared section from the renderer lets the stdlib bridge
    # reuse the same enrichment without duplicating processor wiring.
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    renderer: Any = (
        structlog.processors.JSONRenderer()
        if fmt == "json"
        else structlog.dev.ConsoleRenderer()
    )

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Bridge stdlib ``logging`` (uvicorn et al) into the same renderer.
    # ``ProcessorFormatter`` consumes ``LogRecord``s and re-emits them
    # through the supplied processor chain. ``foreign_pre_chain``
    # replays ``shared_processors`` on records that did NOT originate
    # from a ``structlog`` BoundLogger, so a plain ``logging.info(...)``
    # gets the same level / timestamp keys as structlog calls.
    # ``ExtraAdder`` promotes stdlib ``extra={...}`` kwargs into the
    # event dict — without it, ``logger.info("x", extra={"k": "v"})``
    # would silently drop ``k`` from the JSON output.
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=[structlog.stdlib.ExtraAdder(), *shared_processors],
        processors=[
            # Drop the leftover ``_record`` / ``_from_structlog`` keys
            # that ProcessorFormatter injects for routing — they are
            # noise in the rendered output.
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler()
    handler.setFormatter(formatter)

    # Replace the root handler installed by the previous
    # ``logging.basicConfig`` call, then re-target uvicorn's
    # per-logger handlers at our formatter so its access lines and
    # startup messages render through the same chain.
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)

    for name in _UVICORN_LOGGERS:
        uvicorn_logger = logging.getLogger(name)
        # Uvicorn installs its own handlers per logger when it boots;
        # clearing them and propagating to root ensures every line
        # flows through our single bridged handler. Setting level to
        # NOTSET defers to the root so ``TRELLIS_LOG_LEVEL`` controls
        # the floor uniformly.
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = True
        uvicorn_logger.setLevel(logging.NOTSET)
