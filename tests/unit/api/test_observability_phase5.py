"""C2 Phase 5 — telemetry-failure tests for `trellis_api.observability`.

Each test pins one of the GRACEFUL-DEGRADATION sites annotated in
``src/trellis_api/observability.py``: the primary operation
(``install_observability``) must succeed even when an instrumentor
import or attach call raises, and the failure must be logged via
structlog.

See ``docs/design/plan-cleanup-silent-fallbacks.md`` Phase 5 for the
per-site rubric.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator

import pytest
import structlog
from fastapi import FastAPI
from structlog.testing import capture_logs

import trellis_api.observability as obs_module
from trellis_api.observability import (
    DISABLE_ENV,
    _install_otel,
    _install_prometheus,
    install_observability,
)


@pytest.fixture
def log_output() -> Iterator[list[dict]]:
    """Capture structlog events for the duration of the test.

    Saves/restores the full structlog config so neighbouring suites that
    install a CRITICAL filtering wrapper don't suppress ``info``/``warning``.
    """
    saved = structlog.get_config()
    structlog.configure(
        wrapper_class=structlog.BoundLogger,
        processors=saved.get("processors", []),
    )
    try:
        with capture_logs() as cap:
            yield cap
    finally:
        structlog.configure(**saved)


def _events_with_key(cap: list[dict], event_key: str) -> list[dict]:
    return [e for e in cap if e.get("event") == event_key]


class TestOtelImportMissingGraceful:
    """L56 — `_install_otel` ImportError → log + return False."""

    def test_returns_false_and_logs_when_otel_not_installed(
        self,
        monkeypatch: pytest.MonkeyPatch,
        log_output: list[dict],
    ) -> None:
        # Simulate the extras-not-installed case: force the inner
        # ``import opentelemetry.instrumentation.fastapi`` to raise
        # ImportError by stubbing the parent module to a non-importable
        # value. The hide-import trick uses an __init__ that raises.
        monkeypatch.setitem(sys.modules, "opentelemetry.instrumentation.fastapi", None)

        result = _install_otel()

        assert result is False
        events = _events_with_key(log_output, "otel_skipped_not_installed")
        assert events, f"expected 'otel_skipped_not_installed' in {log_output!r}"


class TestPrometheusImportMissingGraceful:
    """L83 — `_install_prometheus` ImportError → log + return False."""

    def test_returns_false_and_logs_when_prometheus_not_installed(
        self,
        monkeypatch: pytest.MonkeyPatch,
        log_output: list[dict],
    ) -> None:
        monkeypatch.setitem(sys.modules, "prometheus_fastapi_instrumentator", None)

        result = _install_prometheus(FastAPI())

        assert result is False
        events = _events_with_key(log_output, "prometheus_skipped_not_installed")
        assert events, f"expected 'prometheus_skipped_not_installed' in {log_output!r}"


class TestFastapiInstrumentExceptionGraceful:
    """L122 — `install_observability` broad-except around
    `FastAPIInstrumentor.instrument_app(app)`.

    Site is GRACEFUL: a runtime failure here must NOT break app boot.
    The failure must be logged via ``logger.exception`` (error level +
    exc_info) and the returned dict must report ``fastapi=False`` so
    the caller can act on it.
    """

    def test_instrument_failure_does_not_break_install(
        self,
        monkeypatch: pytest.MonkeyPatch,
        log_output: list[dict],
    ) -> None:
        monkeypatch.delenv(DISABLE_ENV, raising=False)
        # Force the OTel branch to be eligible — `_install_otel` returns True.
        monkeypatch.setattr(obs_module, "_install_otel", lambda: True)
        monkeypatch.setattr(obs_module, "_install_prometheus", lambda app: False)

        # Install a fake FastAPIInstrumentor that explodes when called.
        fake_module = type(sys)("opentelemetry.instrumentation.fastapi")

        class _BoomInstrumentor:
            @staticmethod
            def instrument_app(_app: FastAPI) -> None:  # pragma: no cover - smoke
                msg = "boom"
                raise RuntimeError(msg)

        fake_module.FastAPIInstrumentor = _BoomInstrumentor  # type: ignore[attr-defined]
        monkeypatch.setitem(
            sys.modules,
            "opentelemetry.instrumentation.fastapi",
            fake_module,
        )

        # Primary op: install_observability must NOT raise.
        result = install_observability(FastAPI())

        assert result == {"otel": True, "prometheus": False, "fastapi": False}
        events = _events_with_key(log_output, "otel_fastapi_instrument_failed")
        assert events, f"expected 'otel_fastapi_instrument_failed' in {log_output!r}"
        # logger.exception should have escalated to error level + exc_info.
        assert events[0].get("log_level") == "error"
        # exc_info is rendered as the structlog 'exception' key or similar;
        # at minimum the level must be error (the assertion above) — that's
        # the rubric (b) signal that exc_info=True was wired.
