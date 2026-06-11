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
need them. The endpoint is unauthenticated by default for the same
reason ``/healthz`` and ``/readyz`` are — orchestrator scrape jobs
(Prometheus server, k8s ServiceMonitor) need it open. Deployments
whose scraper can carry a credential set
``TRELLIS_METRICS_PUBLIC=false`` to require one (401 otherwise).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import structlog
from fastapi import Depends

from trellis.errors import ConfigError
from trellis_api.auth import AuthContext, _raise_401, authenticate_optional

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = structlog.get_logger(__name__)

#: Env var used to opt out — useful when the observability extras are
#: installed (e.g. via the ``all`` extra) but a particular deploy wants
#: them disabled. Any non-empty value disables both OTel + Prometheus.
DISABLE_ENV = "TRELLIS_DISABLE_OBSERVABILITY"

#: ``true`` (default) — /metrics is open for credential-less scrape
#: jobs (current behavior). ``false`` — /metrics requires a valid
#: credential (any scope). Anything else refuses to start.
METRICS_PUBLIC_ENV = "TRELLIS_METRICS_PUBLIC"


def _enabled() -> bool:
    return not os.environ.get(DISABLE_ENV)


def resolve_metrics_public() -> bool:
    """Return whether /metrics is open to credential-less callers.

    Unset / empty env var → ``True`` (preserves existing scrape-job
    behavior). Only ``true`` / ``false`` (case-insensitive) are
    accepted — anything else raises so a typo crashes uvicorn at
    startup instead of silently picking an exposure posture.
    """
    raw = os.environ.get(METRICS_PUBLIC_ENV)
    if raw is None or not raw.strip():
        return True
    value = raw.strip().lower()
    if value == "true":
        return True
    if value == "false":
        return False
    msg = (
        f"Invalid {METRICS_PUBLIC_ENV}={raw!r}; expected 'true' or 'false'."
        " Refusing to guess an exposure posture."
    )
    raise ConfigError(msg, setting=METRICS_PUBLIC_ENV)


def _require_metrics_access(
    ctx: AuthContext | None = Depends(authenticate_optional),  # noqa: B008 — FastAPI DI idiom
) -> None:
    """Gate /metrics per ``TRELLIS_METRICS_PUBLIC`` (read per request).

    Public (default) → always pass. Gated → require an authenticated
    caller; :func:`authenticate_optional` resolves ``None`` only in
    auth mode ``required`` with no credential presented, so in modes
    ``off`` / ``optional`` the endpoint stays reachable (matching the
    rest of the API's posture in those modes). A presented-but-invalid
    credential 401s inside ``authenticate_optional`` itself.
    """
    if resolve_metrics_public():
        return
    if ctx is None:
        _raise_401("metrics_requires_credential")


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
    # GRACEFUL-DEGRADATION: optional [observability] extra not
    # installed — app boot proceeds. ImportError is narrow and the absence is
    # an expected steady-state, not a runtime failure.
    # TODO(c2-phase5): add metrics.telemetry_failures counter (structlog-only).
    except ImportError:
        logger.info(
            "otel_skipped_not_installed",
            hint="install with `pip install trellis-ai[observability]`",
        )
        return False

    # Psycopg auto-instrument is best-effort — installable as a separate
    # package, may not be present even if the FastAPI bits are.
    try:
        from opentelemetry.instrumentation.psycopg import (  # noqa: PLC0415
            PsycopgInstrumentor,
        )

        PsycopgInstrumentor().instrument()
    # GRACEFUL-DEGRADATION: psycopg OTel sub-extra optional;
    # absence expected on non-Postgres deploys.
    # TODO(c2-phase5): add metrics.telemetry_failures counter (structlog-only).
    except ImportError:
        logger.info("otel_psycopg_not_installed")

    return True


def _install_prometheus(app: FastAPI) -> bool:
    """Mount ``/metrics``. Returns True on success."""
    try:
        from prometheus_fastapi_instrumentator import (  # noqa: PLC0415
            Instrumentator,
        )
    # GRACEFUL-DEGRADATION: optional [observability] extra not
    # installed — /metrics not mounted, app boot proceeds.
    # TODO(c2-phase5): add metrics.telemetry_failures counter (structlog-only).
    except ImportError:
        logger.info(
            "prometheus_skipped_not_installed",
            hint="install with `pip install trellis-ai[observability]`",
        )
        return False

    # ``expose`` forwards extra kwargs to ``app.get`` — the least
    # invasive hook for attaching the TRELLIS_METRICS_PUBLIC gate as a
    # plain FastAPI dependency on the instrumentator's own endpoint.
    Instrumentator().instrument(app).expose(
        app,
        endpoint="/metrics",
        include_in_schema=False,
        tags=["observability"],
        dependencies=[Depends(_require_metrics_access)],
    )
    return True


def install_observability(app: FastAPI) -> dict[str, bool]:
    """Install OTel + Prometheus on the given FastAPI app.

    Returns a small status dict so the caller can log what was wired
    up. Safe to call when extras aren't installed — each piece
    silently no-ops on ImportError.
    """
    # Validate the exposure env up front (even when observability is
    # disabled or the extra is missing) — a typo'd value is operator
    # misuse and must crash startup, not lurk until the extra lands.
    resolve_metrics_public()

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
        # GRACEFUL-DEGRADATION: telemetry hookup must not break app
        # boot; failure surfaces via the returned status dict.
        # TODO(c2-phase5): add metrics.telemetry_failures counter (structlog-only).
        except Exception:
            logger.exception("otel_fastapi_instrument_failed")

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
