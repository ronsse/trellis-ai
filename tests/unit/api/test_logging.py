"""Tests for trellis_api.logging — uvicorn ↔ structlog unification."""

from __future__ import annotations

import contextlib
import io
import json
import logging
from collections.abc import Iterator

import pytest
import structlog

from trellis_api.logging import _UVICORN_LOGGERS, configure_logging


@pytest.fixture(autouse=True)
def _reset_logging() -> Iterator[None]:
    """Restore logging + structlog to a clean slate after each test.

    Both subsystems carry process-global state. Without this fixture a
    prior test's ``configure_logging`` call would leak into the next
    one — deterministic-failure-mode for assertions on handler lists.
    """
    saved_root_handlers = list(logging.getLogger().handlers)
    saved_root_level = logging.getLogger().level
    saved_uvicorn = {
        name: (
            list(logging.getLogger(name).handlers),
            logging.getLogger(name).propagate,
            logging.getLogger(name).level,
        )
        for name in _UVICORN_LOGGERS
    }
    try:
        yield
    finally:
        root = logging.getLogger()
        root.handlers = saved_root_handlers
        root.setLevel(saved_root_level)
        for name, (handlers, propagate, level) in saved_uvicorn.items():
            lg = logging.getLogger(name)
            lg.handlers = handlers
            lg.propagate = propagate
            lg.setLevel(level)
        structlog.reset_defaults()


def _capture(monkeypatch: pytest.MonkeyPatch, fmt: str = "json") -> io.StringIO:
    """Configure logging with ``fmt``, then redirect the bridge handler at a buffer."""
    monkeypatch.setenv("TRELLIS_LOG_FORMAT", fmt)
    monkeypatch.setenv("TRELLIS_LOG_LEVEL", "DEBUG")
    configure_logging()
    buffer = io.StringIO()
    # The configure_logging() call installs exactly one handler on the
    # root; redirect its stream so we can read what gets emitted.
    [handler] = logging.getLogger().handlers
    assert isinstance(handler, logging.StreamHandler)
    handler.setStream(buffer)
    return buffer


class TestUvicornLoggerWiring:
    """Uvicorn loggers must propagate to the bridged root handler."""

    @pytest.mark.parametrize("name", _UVICORN_LOGGERS)
    def test_uvicorn_logger_has_no_own_handlers(
        self,
        monkeypatch: pytest.MonkeyPatch,
        name: str,
    ) -> None:
        configure_logging()
        # Uvicorn installs handlers only after its own boot; we clear
        # them and rely on propagation. If a future Uvicorn release
        # auto-installs at import time, this assertion catches the
        # divergence before it ships to prod.
        assert logging.getLogger(name).handlers == []

    @pytest.mark.parametrize("name", _UVICORN_LOGGERS)
    def test_uvicorn_logger_propagates(
        self,
        monkeypatch: pytest.MonkeyPatch,
        name: str,
    ) -> None:
        configure_logging()
        assert logging.getLogger(name).propagate is True

    @pytest.mark.parametrize("name", _UVICORN_LOGGERS)
    def test_uvicorn_logger_inherits_root_level(
        self,
        monkeypatch: pytest.MonkeyPatch,
        name: str,
    ) -> None:
        # NOTSET (0) defers to the root, so a single TRELLIS_LOG_LEVEL
        # controls every uvicorn surface.
        configure_logging()
        assert logging.getLogger(name).level == logging.NOTSET


class TestJsonRendering:
    """JSON mode must emit one parseable JSON object per line for every source."""

    def test_uvicorn_message_renders_as_json(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        buffer = _capture(monkeypatch, fmt="json")
        logging.getLogger("uvicorn.error").warning(
            "shutting down", extra={"reason": "sigterm"}
        )
        line = buffer.getvalue().strip().splitlines()[-1]
        record = json.loads(line)
        assert record["event"] == "shutting down"
        assert record["level"] == "warning"
        # Stdlib `extra={...}` keys must propagate through the bridge —
        # otherwise uvicorn.access lines lose their request metadata.
        assert record["reason"] == "sigterm"
        assert "timestamp" in record  # injected by shared_processors

    def test_structlog_message_renders_as_json(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The native structlog half renders through the same JSON renderer.

        This used to be a no-op that ``del``'d its own buffer, with a comment
        saying structlog's output was unreachable "on stdout". Since #430 both
        halves resolve ``sys.stderr`` at write time, so ``capsys`` sees it.
        """
        _capture(monkeypatch, fmt="json")
        structlog.get_logger("trellis.test").info(
            "ingest_complete", trace_id="t-1", count=3
        )
        record = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
        assert record["event"] == "ingest_complete"
        assert record["trace_id"] == "t-1"
        assert record["count"] == 3

    def test_renders_same_shape_for_both_sources(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Bridged uvicorn line lands in our buffered stream handler;
        # native structlog line lands on stderr via the lazy proxy.
        # Both should be valid JSON with the same key set so a single
        # log-shipping config can parse the combined stream.
        buffer = _capture(monkeypatch, fmt="json")

        logging.getLogger("uvicorn.access").info("GET / 200")
        bridged_line = buffer.getvalue().strip().splitlines()[-1]
        bridged = json.loads(bridged_line)

        structlog.get_logger("api").info("request_received", method="GET")
        native_line = capsys.readouterr().err.strip().splitlines()[-1]
        native = json.loads(native_line)

        # Both records must carry the enrichment keys added by the
        # shared processor chain. Comparing key sets — not values —
        # asserts the *shape* contract without coupling to timestamps.
        common_keys = {"event", "level", "timestamp"}
        assert common_keys.issubset(bridged.keys())
        assert common_keys.issubset(native.keys())


class TestConsoleMode:
    def test_console_renderer_used_when_format_console(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        buffer = _capture(monkeypatch, fmt="console")
        logging.getLogger("uvicorn").info("starting up")
        rendered = buffer.getvalue()
        # Console renderer is human-readable; the message lands as text
        # rather than JSON. Reject any line that *parses* as JSON to
        # confirm we did not silently fall back to the JSON renderer.
        assert rendered.strip()
        with pytest.raises(json.JSONDecodeError):
            json.loads(rendered.strip().splitlines()[-1])
        assert "starting up" in rendered


class TestLogLevel:
    def test_below_level_messages_dropped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_LOG_FORMAT", "json")
        monkeypatch.setenv("TRELLIS_LOG_LEVEL", "WARNING")
        configure_logging()
        buffer = io.StringIO()
        [handler] = logging.getLogger().handlers
        handler.setStream(buffer)

        logging.getLogger("uvicorn").debug("verbose detail")
        logging.getLogger("uvicorn").warning("attention please")

        lines = [line for line in buffer.getvalue().splitlines() if line.strip()]
        events = [json.loads(line)["event"] for line in lines]
        assert "verbose detail" not in events
        assert "attention please" in events

    def test_unknown_level_falls_back_to_info(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_LOG_FORMAT", "json")
        monkeypatch.setenv("TRELLIS_LOG_LEVEL", "BOGUS")
        configure_logging()
        # Root level must default to INFO; an unparseable env var must
        # not blow up startup or silently default to DEBUG.
        assert logging.getLogger().level == logging.INFO


def _events(stream: io.StringIO) -> list[str]:
    """The ``event`` field of every JSON line written to *stream*."""
    return [
        json.loads(line)["event"] for line in stream.getvalue().splitlines() if line
    ]


def _emit_bridged(message: str) -> None:
    logging.getLogger("uvicorn.error").warning(message)


def _emit_native(message: str) -> None:
    structlog.get_logger("trellis.api.test").warning(message)


#: The two halves of ``configure_logging``: the stdlib ``ProcessorFormatter``
#: bridge that carries uvicorn, and structlog's own logger factory. #430 was a
#: baked stream on the first; the second had the same defect via
#: ``PrintLoggerFactory()``, whose ``PrintLogger`` resolves ``file or stdout``
#: against a ``from sys import stdout`` bound when structlog was imported. Both
#: are parametrized here because fixing one and leaving the other is exactly
#: the half-fix that made the split look deliberate.
_EMITTERS = {"bridged-uvicorn": _emit_bridged, "native-structlog": _emit_native}


@pytest.mark.parametrize("half", sorted(_EMITTERS))
class TestStreamIsResolvedAtWriteTime:
    """#430 — neither half may capture ``sys.stderr`` at configure time.

    Asserted on **the streams**, never on "it did not raise". Stdlib logging
    routes a write failure into ``Handler.handleError``, which prints
    ``--- Logging error ---`` to the real stderr and returns, so a handler
    pinned to a dead stream drops every line without a single exception
    reaching the caller. A test phrased as ``does not raise`` passes against
    the unfixed code.
    """

    @staticmethod
    def _configured(monkeypatch: pytest.MonkeyPatch, stream: io.StringIO) -> None:
        monkeypatch.setenv("TRELLIS_LOG_FORMAT", "json")
        monkeypatch.setenv("TRELLIS_LOG_LEVEL", "DEBUG")
        with contextlib.redirect_stderr(stream):
            configure_logging()

    def test_lines_follow_the_current_stream(
        self, monkeypatch: pytest.MonkeyPatch, half: str
    ) -> None:
        """A line goes wherever ``sys.stderr`` points *now*, not at setup."""
        emit = _EMITTERS[half]
        at_configure_time = io.StringIO()
        self._configured(monkeypatch, at_configure_time)

        later = io.StringIO()
        with contextlib.redirect_stderr(later):
            emit("after_the_redirect_moved")

        assert _events(later) == ["after_the_redirect_moved"]
        assert _events(at_configure_time) == [], (
            "the line went to the stream captured at configure time; the "
            "handler is holding a stream handle rather than resolving one"
        )

    def test_no_lines_are_lost_once_the_configure_time_stream_closes(
        self, monkeypatch: pytest.MonkeyPatch, half: str
    ) -> None:
        """The live failure mode: configure under a buffer that is then closed.

        ``CliRunner.invoke`` and ``redirect_stderr`` both do this. A baked
        handler keeps writing into the closed object, the ``ValueError`` is
        swallowed, and the operator sees a log that simply stops.
        """
        emit = _EMITTERS[half]
        ephemeral = io.StringIO()
        self._configured(monkeypatch, ephemeral)
        ephemeral.close()

        live = io.StringIO()
        with contextlib.redirect_stderr(live):
            emit("survived_the_close")

        assert _events(live) == ["survived_the_close"], (
            "log line lost after the configure-time stream was closed — the "
            "write failed silently inside Handler.handleError"
        )

    def test_both_halves_land_on_the_same_stream(
        self, monkeypatch: pytest.MonkeyPatch, half: str
    ) -> None:
        """The fd decision, pinned so a revert to stdout turns the suite red.

        Before #430 the bridge wrote to stderr while structlog wrote to
        stdout, which is not something anyone chose — see the module
        docstring of ``trellis_api.logging``. One collector, one fd, one
        interleaving.
        """
        emit = _EMITTERS[half]
        stream = io.StringIO()
        self._configured(monkeypatch, stream)

        with contextlib.redirect_stderr(stream):
            emit("on_stderr")

        assert _events(stream) == ["on_stderr"]
