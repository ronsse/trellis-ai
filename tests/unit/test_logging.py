"""Regression tests for #377 — the configured log stream must stay live.

:func:`trellis.logging.configure_stderr_logging` writes into structlog's
*process-global* config. Passing ``sys.stderr`` by value bound whatever
stream happened to be current at configure time into that global, so a
caller that reconfigured under a redirection left the entire process
logging into a stream it did not own — and, when click closed it,
into a stream that no longer existed.

Every test in ``TestStreamResolution`` fails against the pre-fix
implementation on **both** dependency sets. ``TestBareCliRunner`` covers
the incident shape itself; ``TestNoStdoutFallback`` covers the failure
mode a careless *fix* would introduce, which is the reason the issue asked
for its own review — see each class docstring.
"""

from __future__ import annotations

import io
import sys
from typing import TYPE_CHECKING

import pytest
import structlog
from typer.testing import CliRunner as BareCliRunner

from tests.structlog_isolation import reset_structlog_global_state
from trellis.logging import configure_stderr_logging

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture(autouse=True)
def _isolate_structlog(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep this module's global-config edits out of the rest of the session.

    Resets on the way in as well as out: an earlier test may have left a
    cached lazy-proxy bind pinned to some other level, which would filter
    out the events asserted on here.
    """
    monkeypatch.setenv("TRELLIS_LOG_LEVEL", "INFO")
    reset_structlog_global_state()
    yield
    reset_structlog_global_state()


class _RecordingStream:
    """A minimal write-only stream that remembers how it was called.

    ``StringIO`` cannot answer either question below: it concatenates
    writes, and it has no observable flush. Both were unpinned — deleting
    ``_LazyStderr.flush``'s body left the whole suite green.
    """

    def __init__(self) -> None:
        self.writes: list[str] = []
        self.flushes = 0

    def write(self, data: str) -> int:
        self.writes.append(data)
        return len(data)

    def flush(self) -> None:
        self.flushes += 1


def _log(event: str) -> None:
    """Emit *event* through a freshly bound logger.

    ``structlog.get_logger`` returns a new lazy proxy per call, so the
    logger factory runs at *this* point rather than reusing some earlier
    test's memoised bind. That matters: the whole question under test is
    which stream the factory resolves, and when.
    """
    structlog.get_logger("tests.trellis.logging").warning(event)


class TestStreamResolution:
    """The stream is a rule evaluated per write, not a handle held forever."""

    def test_configure_time_stream_is_not_pinned(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Logs follow ``sys.stderr``, not the stderr present at configure time."""
        at_configure = io.StringIO()
        monkeypatch.setattr(sys, "stderr", at_configure)
        configure_stderr_logging()

        at_write = io.StringIO()
        monkeypatch.setattr(sys, "stderr", at_write)
        _log("resolved_at_write_time")

        assert "resolved_at_write_time" in at_write.getvalue()
        assert at_configure.getvalue() == "", (
            "log went to the stream captured at configure time; the "
            "logger_factory is still baking a handle"
        )

    def test_logging_survives_the_configure_time_stream_being_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The incident mechanism, reduced to its three lines.

        Click's ``CliRunner`` does exactly this: redirect stderr, let the
        CLI callback reconfigure logging, then close the buffer on return.
        Pre-fix this raised ``ValueError: I/O operation on closed file`` on
        the *next* log call anywhere in the process.
        """
        ephemeral = io.StringIO()
        monkeypatch.setattr(sys, "stderr", ephemeral)
        configure_stderr_logging()
        ephemeral.close()

        restored = io.StringIO()
        monkeypatch.setattr(sys, "stderr", restored)
        _log("after_close")

        assert "after_close" in restored.getvalue()

    def test_reconfiguring_under_a_redirection_leaves_no_residue(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Repeated configure/redirect cycles do not accumulate dead streams.

        Guards the shape a caching bug would take: each cycle configures
        under its own doomed buffer, and only the last (live) stream may
        receive anything.
        """
        closed_buffers = []
        for _ in range(3):
            buf = io.StringIO()
            monkeypatch.setattr(sys, "stderr", buf)
            configure_stderr_logging()
            _log("inside_cycle")
            buf.close()
            closed_buffers.append(buf)

        live = io.StringIO()
        monkeypatch.setattr(sys, "stderr", live)
        _log("after_all_cycles")

        assert "after_all_cycles" in live.getvalue()
        assert len(closed_buffers) == 3

    def test_a_log_line_is_one_write_and_a_flush(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One resolution per line, and the flush actually reaches the stream.

        ``PrintLogger`` renders through ``print()``, which issues
        ``write(message)`` and ``write("\n")`` as separate calls. Against a
        *lazily* resolved stream those are two independent lookups, so a
        ``sys.stderr`` swap landing between them tears one line across two
        streams — a hazard the pinned handle did not have, introduced by the
        fix itself. ``WriteLogger`` emits the line in a single write.

        The flush half is asserted because nothing else can see it: with
        ``StringIO`` or ``capsys``, removing the flush changes no observable
        behaviour at all.
        """
        stream = _RecordingStream()
        monkeypatch.setattr(sys, "stderr", stream)
        configure_stderr_logging()

        _log("atomic_line")

        assert len(stream.writes) == 1, (
            f"expected the whole line in one write; got {stream.writes!r}"
        )
        assert stream.writes[0].endswith("\n")
        assert "atomic_line" in stream.writes[0]
        assert stream.flushes >= 1, "flush never reached the stream"


class TestNoStdoutFallback:
    """Lazy resolution must never degrade into "resolve to stdout".

    stdout carries JSON-RPC frames under the MCP server and ``--format
    json`` payloads under the CLI. A log line on either corrupts the
    channel ``configure_stderr_logging`` exists to protect, so these
    assertions guard the fix's own failure mode: they fail if the factory
    is ever handed ``file=None`` (structlog's ``PrintLogger`` defaults that
    to stdout) or given stdout as a fallback.
    """

    def test_log_output_never_reaches_stdout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        out, err = io.StringIO(), io.StringIO()
        monkeypatch.setattr(sys, "stdout", out)
        monkeypatch.setattr(sys, "stderr", err)

        configure_stderr_logging()
        _log("stderr_only")

        assert "stderr_only" in err.getvalue()
        assert out.getvalue() == ""

    def test_absent_stderr_drops_the_line_rather_than_using_stdout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A process with no stderr at all must still leave stdout alone.

        ``sys.stderr`` is ``None`` under ``pythonw.exe`` and some frozen
        bundles. Pre-fix this was a *live* stdout-corruption path and not
        merely a theoretical one: ``PrintLoggerFactory(file=None)`` makes
        ``PrintLogger`` fall back to ``sys.stdout``, so on such a host
        ``configure_stderr_logging()`` routed every log line onto the MCP
        server's JSON-RPC channel. Dropping the line is the only safe
        answer once there is genuinely nowhere to write.
        """
        out = io.StringIO()
        monkeypatch.setattr(sys, "stdout", out)
        monkeypatch.setattr(sys, "stderr", None)
        monkeypatch.setattr(sys, "__stderr__", None)

        configure_stderr_logging()
        _log("nowhere_to_go")

        assert out.getvalue() == ""

    def test_falls_back_to_the_original_stderr_when_sys_stderr_is_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``sys.__stderr__`` is preferred over silence, and over stdout."""
        out, original = io.StringIO(), io.StringIO()
        monkeypatch.setattr(sys, "stdout", out)
        monkeypatch.setattr(sys, "stderr", None)
        monkeypatch.setattr(sys, "__stderr__", original)

        configure_stderr_logging()
        _log("via_dunder_stderr")

        assert "via_dunder_stderr" in original.getvalue()
        assert out.getvalue() == ""


class TestBareCliRunner:
    """The acceptance criterion: no test needs a special runner any more."""

    def test_a_bare_cli_runner_does_not_poison_later_logging(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Deliberately uses the *bare* runner, not the ``cli_runner`` fixture.

        The containment shipped in #370 (``IsolatedCliRunner``) is what
        makes a bare runner survivable today; this asserts it is no longer
        needed, so the containment is belt-and-braces rather than
        load-bearing. Pre-fix, the assertion on ``after_bare_invoke``
        failed on *both* dependency sets — under click 8.5.0 because the
        buffer was closed and the log call raised, and under 8.3.2 because
        the line went to click's still-open buffer instead of the stream
        the process actually has. The visible symptom differed by version;
        the defect did not.
        """
        from trellis_cli.main import app

        monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
        monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "data"))
        (tmp_path / "data" / "stores").mkdir(parents=True)

        BareCliRunner().invoke(app, ["policy", "list", "--format", "json"])

        after = io.StringIO()
        monkeypatch.setattr(sys, "stderr", after)
        _log("after_bare_invoke")

        assert "after_bare_invoke" in after.getvalue()
