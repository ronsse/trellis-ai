"""OpenTelemetry + Prometheus instrumentation for the Trellis REST API.

Wired into the FastAPI app from :func:`trellis_api.app.create_app` via
:func:`install_observability`. Gracefully no-ops when the optional
``observability`` extra isn't installed — operators in dev / CI don't
have to think about telemetry until they want it.

**OpenTelemetry.** Auto-instruments FastAPI (per-request spans),
psycopg (per-statement spans + connection metrics), and uses the
default OTLP exporter so anything in the OTEL ecosystem can pick up
traces. The exporter is no-op unless ``OTEL_EXPORTER_OTLP_ENDPOINT``
or another OTel env config is set, so install-but-don't-configure is
the same as not-installed for telemetry cost.

**Prometheus.** Mounts ``/metrics`` via
``prometheus-fastapi-instrumentator``. Per-route latency histograms
and counters out of the box; operators add custom counters if they
need them. The endpoint stays unauthenticated for the same reason
``/healthz`` and ``/readyz`` do — orchestrator scrape jobs (Prometheus
server, k8s ServiceMonitor) need it open.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = structlog.get_logger(__name__)

#: Env var used to opt out — useful when the observability extras are
#: installed (e.g. via the ``all`` extra) but a particular deploy wants
#: them disabled. Any non-empty value disables both OTel + Prometheus.
DISABLE_ENV = "TRELLIS_DISABLE_OBSERVABILITY"


def _enabled() -> bool:
    return not os.environ.get(DISABLE_ENV)


def _install_otel() -> bool:
    """Wire OpenTelemetry auto-instrumentation. Returns True on success.

    The actual ``FastAPIInstrumentor.instrument_app`` call lives in
    :func:`install_observability` because it needs the app instance.
    This helper just confirms the FastAPI instrumentation package is
    importable, then attaches the psycopg instrumentor (process-wide,
    no app needed).
    """
    try:
        import opentelemetry.instrumentation.fastapi  # noqa: F401, PLC0415
    except ImportError:
        logger.debug(
            "otel_skipped_not_installed",
            extra="install with `pip install trellis-ai[observability]`",
        )
        return False

    # Psycopg auto-instrument is best-effort — installable as a separate
    # package, may not be present even if the FastAPI bits are.
    try:
        from opentelemetry.instrumentation.psycopg import (  # noqa: PLC0415
            PsycopgInstrumentor,
        )

        PsycopgInstrumentor().instrument()
    except ImportError:
        logger.debug("otel_psycopg_not_installed")

    return True


def _install_prometheus(app: FastAPI) -> bool:
    """Mount ``/metrics``. Returns True on success."""
    try:
        from prometheus_fastapi_instrumentator import (  # noqa: PLC0415
            Instrumentator,
        )
    except ImportError:
        logger.debug(
            "prometheus_skipped_not_installed",
            extra="install with `pip install trellis-ai[observability]`",
        )
        return False

    Instrumentator().instrument(app).expose(
        app,
        endpoint="/metrics",
        include_in_schema=False,
        tags=["observability"],
    )
    return True


def install_observability(app: FastAPI) -> dict[str, bool]:
    """Install OTel + Prometheus on the given FastAPI app.

    Returns a small status dict so the caller can log what was wired
    up. Safe to call when extras aren't installed — each piece
    silently no-ops on ImportError.
    """
    if not _enabled():
        logger.info("observability_disabled_via_env", env=DISABLE_ENV)
        return {"otel": False, "prometheus": False, "fastapi": False}

    otel_loaded = _install_otel()
    prom_loaded = _install_prometheus(app)

    fastapi_instrumented = False
    if otel_loaded:
        try:
            from opentelemetry.instrumentation.fastapi import (  # noqa: PLC0415
                FastAPIInstrumentor,
            )

            FastAPIInstrumentor.instrument_app(app)
            fastapi_instrumented = True
        except Exception as exc:
            logger.warning("otel_fastapi_instrument_failed", error=str(exc))

    logger.info(
        "observability_installed",
        otel=otel_loaded,
        prometheus=prom_loaded,
        fastapi=fastapi_instrumented,
    )
    return {
        "otel": otel_loaded,
        "prometheus": prom_loaded,
        "fastapi": fastapi_instrumented,
    }
