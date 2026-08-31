"""Shared structlog configuration for stdio-style processes.

Both the Trellis CLI and the MCP server reserve stdout for their
output (``--format json`` payloads and JSON-RPC frames respectively).
structlog's default ``PrintLoggerFactory`` writes to stdout, which
corrupts that channel. Routing logs to ``sys.stderr`` keeps them
visible to operators while leaving stdout exclusively for protocol /
payload traffic.

The stream is resolved **per write** rather than at configure time —
see :class:`_LazyStderr` for why that distinction is load-bearing.

The Trellis API uses a richer config (``trellis_api.logging``) that
bridges uvicorn's stdlib loggers and supports JSON output for log
shippers — this module is intentionally narrower.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import TextIO, cast

import structlog


def _current_stderr() -> TextIO | None:
    """The process's stderr *right now*, or ``None`` if it has none.

    ``sys.stderr`` is ``None`` in a detached GUI process (``pythonw.exe``,
    some frozen bundles), so ``sys.__stderr__`` is consulted second. When
    both are ``None`` there is genuinely nowhere to write.

    ``sys.stdout`` is deliberately **not** a fallback. It carries JSON-RPC
    frames under the MCP server and ``--format json`` payloads under the
    CLI; a log line on either corrupts the exact channel this module
    exists to protect. Dropping a log line is recoverable, corrupting a
    protocol stream is not.

    Nor is the drop announced, and that is not an oversight: the channel one
    would announce on is the channel that is missing. CPython makes the same
    call — ``warnings._showwarnmsg_impl`` reads ``sys.stderr`` and returns
    silently when it is ``None``, commenting that warnings are simply lost
    under ``pythonw``. Counting the drops was considered and rejected for
    the same reason: a counter needs a reader, the only reader would have to
    surface it on stdout, and read-side logging state has no home in
    ``write_config`` (which is write-behaviour only, by design). An explicit
    ``TRELLIS_LOG_FILE`` sink is the real answer if this host ever matters —
    that is a feature, not part of closing #377.
    """
    stream = sys.stderr
    if stream is None:
        stream = sys.__stderr__
    return stream


class _LazyStderr:
    """Write-only file proxy that resolves ``sys.stderr`` at write time.

    ``structlog.configure`` stores its ``logger_factory`` in *process-global*
    state, so ``PrintLoggerFactory(file=sys.stderr)`` captures whatever object
    ``sys.stderr`` named at configure time and keeps it alive far beyond the
    caller that configured it.

    That is a live defect, not a hypothetical (#377). Click's ``CliRunner``
    redirects ``sys.stderr`` to a temporary buffer for the duration of
    ``invoke()``; a Trellis CLI command calls :func:`configure_stderr_logging`
    on the way in, capturing that buffer into the global config; ``invoke()``
    returns and click **closes** it. Every later ``logger.*`` call anywhere in
    the process then raises ``ValueError: I/O operation on closed file`` — 109
    tests across 23 directories on one CI run, each failing at whatever logged
    next rather than at the ``CliRunner`` call responsible.

    Deferring the lookup means the global config holds a *rule* ("whatever
    stderr is now") instead of a *handle*, so a redirection that ends takes
    its stream with it. No caller needs a special ``CliRunner``, and the
    containment shipped in #370 becomes belt-and-braces rather than
    load-bearing.

    Only ``write`` and ``flush`` are implemented, which is the whole surface
    structlog's ``WriteLogger`` uses. One consequence, latent today: its
    ``__getstate__`` / ``__deepcopy__`` special-case ``sys.stdout`` and
    ``sys.stderr`` and raise for anything else, so a bound logger built from
    this factory can no longer be pickled across a ``multiprocessing``
    boundary. Nothing in ``src/`` does that; ``trellis_workers`` is where it
    would surface, and the error would not point here.
    A closed *real* stderr still raises, exactly as it did before — that is a
    genuine error, and swallowing it would trade a loud failure for a silent
    one.
    """

    def write(self, data: str) -> int:
        stream = _current_stderr()
        if stream is None:
            return 0
        return stream.write(data)

    def flush(self) -> None:
        stream = _current_stderr()
        if stream is not None:
            stream.flush()


#: Module-level singleton. structlog keys a per-file write lock off the file
#: object in a ``WeakKeyDictionary``, so a stable instance means one lock for
#: the process rather than a fresh one per ``configure_stderr_logging`` call.
_STDERR_PROXY = _LazyStderr()


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
        # ``WriteLogger`` rather than ``PrintLogger``: its ``msg`` is one
        # ``write(message + "\n")`` under the lock, where ``PrintLogger``
        # goes through ``print()`` and issues ``write(message)`` and
        # ``write("\n")`` separately. With a lazily-resolved stream those are
        # two lookups, so a ``sys.stderr`` swap landing between them (another
        # thread's ``redirect_stderr``) would tear one line across two
        # streams — structlog's per-file lock cannot help, being keyed on the
        # proxy while the swap is external. Same bytes out, one resolution
        # fewer, and structlog documents it as the faster of the two.
        #
        # ``cast`` because structlog types this as ``TextIO`` while only
        # exercising ``write`` / ``flush``; a full ``io.TextIOBase`` subclass
        # would add a dozen unreachable methods to satisfy a nominal type.
        logger_factory=structlog.WriteLoggerFactory(file=cast("TextIO", _STDERR_PROXY)),
        # Kept, and now free of the hazard it used to compound: the cached
        # logger holds the proxy, not a stream, so caching can no longer pin
        # a dead handle. It still caches the *level* filter, which is what
        # ``tests.structlog_isolation.clear_cached_logger_proxies`` is for.
        cache_logger_on_first_use=True,
    )


__all__ = ["configure_stderr_logging"]
