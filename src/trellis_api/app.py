"""FastAPI application for Trellis."""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from fastapi import Depends, FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from trellis.auth import SCOPE_ADMIN, SCOPE_INGEST, SCOPE_MUTATE, SCOPE_READ
from trellis.errors import ConfigError
from trellis.stores.registry import StoreRegistry
from trellis_api.auth import require_scope, warn_if_unauthenticated
from trellis_api.middleware import (
    request_id_middleware,
    unhandled_exception_handler,
)
from trellis_api.observability import install_observability

logger = structlog.get_logger(__name__)

_registry: StoreRegistry | None = None


def get_registry() -> StoreRegistry:
    """Get the global StoreRegistry."""
    if _registry is None:
        msg = "StoreRegistry not initialized. Start the app with create_app()."
        raise RuntimeError(msg)
    return _registry


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:  # noqa: ARG001
    """Initialize, validate, and tear down the StoreRegistry.

    Validation runs eagerly so misconfigurations (missing DSN, unset
    S3 bucket, plugin import failure) crash uvicorn before it accepts
    its first request, rather than 500ing the first call to a broken
    store. See ``StoreRegistry.validate`` for the contract.
    """
    global _registry  # noqa: PLW0603
    _registry = StoreRegistry.from_config_dir()
    _registry.validate()
    warn_if_unauthenticated()
    logger.info("api_stores_initialized")
    yield
    _registry.close()
    _registry = None
    logger.info("api_stores_closed")


_STATIC_DIR = Path(__file__).parent / "static"

#: ``true`` (default) — mount the static UI at /ui and redirect / to it.
#: ``false`` — don't mount /ui; / redirects to /api/version instead.
#: Anything else refuses to start.
UI_ENABLED_ENV = "TRELLIS_UI_ENABLED"


def resolve_ui_enabled() -> bool:
    """Return whether the static UI should be mounted, loud on a bad value.

    Unset / empty env var → ``True`` (back-compat). Only ``true`` /
    ``false`` (case-insensitive) are accepted — anything else raises
    :class:`~trellis.errors.ConfigError` so a typo crashes
    ``create_app`` at startup instead of silently picking a posture.
    """
    raw = os.environ.get(UI_ENABLED_ENV)
    if raw is None or not raw.strip():
        return True
    value = raw.strip().lower()
    if value == "true":
        return True
    if value == "false":
        return False
    msg = (
        f"Invalid {UI_ENABLED_ENV}={raw!r}; expected 'true' or 'false'."
        " Refusing to guess whether to expose the UI."
    )
    raise ConfigError(msg, setting=UI_ENABLED_ENV)


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    from trellis_api.routes import (  # noqa: PLC0415
        admin,
        curate,
        extract,
        health,
        ingest,
        mutations,
        observations,
        policies,
        retrieve,
        version,
    )

    # Exposure toggles — resolved/validated here so a bad value crashes
    # startup loudly. TRELLIS_OPS_DETAIL is re-read per request inside
    # /readyz; this call is purely the startup validation chokepoint.
    ui_enabled = resolve_ui_enabled()
    health.resolve_ops_detail()

    app = FastAPI(
        title="Trellis API",
        description="Structured memory and learning for AI agents",
        version="0.2.0",
        lifespan=lifespan,
    )

    # Request-ID correlation — runs before everything so health,
    # version, and /api/v1 routes all log with the same request_id.
    app.add_middleware(BaseHTTPMiddleware, dispatch=request_id_middleware)

    # Translate uncaught exceptions into a structured 500 envelope so
    # responses don't leak internal types or stack frames.
    app.add_exception_handler(Exception, unhandled_exception_handler)

    # OpenTelemetry + Prometheus — no-op when the ``observability``
    # extra isn't installed or ``TRELLIS_DISABLE_OBSERVABILITY`` is set.
    # The /metrics endpoint mounted by the Prometheus instrumentator is
    # unauthenticated by default for orchestrator scrape jobs; set
    # TRELLIS_METRICS_PUBLIC=false to require a credential.
    install_observability(app)

    # Root redirect targets the UI when it's mounted, the version
    # handshake otherwise — never a dangling /ui/ that 404s.
    root_target = "/ui/" if ui_enabled else "/api/version"

    @app.get("/", include_in_schema=False)
    async def root_redirect() -> RedirectResponse:
        return RedirectResponse(url=root_target, status_code=307)

    # Version handshake — unversioned, mounted at /api/version (no prefix).
    # Deliberately outside /api/v1 because it describes which major is running.
    # Stays unauthenticated so clients can probe compatibility before
    # they have a key.
    app.include_router(version.router, tags=["version"])

    # Liveness/readiness probes — unversioned, deployment plumbing. Must
    # stay unauthenticated so orchestrator probes (k8s, ALB, etc.) work
    # without holding the API secret.
    app.include_router(health.router, tags=["health"])

    # Every ``/api/v1`` router requires one scope, enforced by
    # ``require_scope`` per the effective ``TRELLIS_AUTH_MODE`` (off /
    # optional / required — see ``trellis_api.auth``). The map:
    #
    #   retrieve                     -> read
    #   ingest                       -> ingest
    #   mutations / curate / extract -> mutate
    #   admin                        -> admin
    #   observations                 -> read, plus mutate on its two
    #                                   POST endpoints (route-level deps
    #                                   in routes/observations.py)
    #   policies                     -> read, plus admin on POST/DELETE
    #                                   (route-level deps in
    #                                   routes/policies.py)
    #
    # ``admin`` implies every other scope; the legacy ``TRELLIS_API_KEY``
    # shared secret is granted all scopes for backwards compatibility.
    read_auth = [Depends(require_scope(SCOPE_READ))]
    ingest_auth = [Depends(require_scope(SCOPE_INGEST))]
    mutate_auth = [Depends(require_scope(SCOPE_MUTATE))]
    admin_auth = [Depends(require_scope(SCOPE_ADMIN))]
    app.include_router(
        admin.router, prefix="/api/v1", tags=["admin"], dependencies=admin_auth
    )
    app.include_router(
        ingest.router, prefix="/api/v1", tags=["ingest"], dependencies=ingest_auth
    )
    app.include_router(
        retrieve.router, prefix="/api/v1", tags=["retrieve"], dependencies=read_auth
    )
    app.include_router(
        curate.router, prefix="/api/v1", tags=["curate"], dependencies=mutate_auth
    )
    app.include_router(
        mutations.router,
        prefix="/api/v1",
        tags=["mutations"],
        dependencies=mutate_auth,
    )
    app.include_router(
        policies.router, prefix="/api/v1", tags=["policies"], dependencies=read_auth
    )
    app.include_router(
        extract.router, prefix="/api/v1", tags=["extract"], dependencies=mutate_auth
    )
    app.include_router(
        observations.router,
        prefix="/api/v1",
        tags=["observations"],
        dependencies=read_auth,
    )

    # Static UI at /ui — the page itself is unauthenticated; the UI
    # calls /api/v1 routes which ARE gated, and the page stores its key
    # in localStorage and sends it as X-API-Key on every fetch.
    # Deployments that don't want the page served at all set
    # TRELLIS_UI_ENABLED=false (no mount, / redirects to /api/version).
    if ui_enabled and _STATIC_DIR.is_dir():
        app.mount("/ui", StaticFiles(directory=str(_STATIC_DIR), html=True), name="ui")

    return app


#: Default bind address. Loopback-only by default so a fresh install
#: doesn't expose the unauthenticated API on the network. Container
#: deployments that need to listen on the pod IP set
#: ``TRELLIS_API_HOST=0.0.0.0`` (or pass ``--host``) explicitly.
DEFAULT_HOST = os.environ.get("TRELLIS_API_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("TRELLIS_API_PORT", "8420"))


def main(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    """Run the API server.

    Host/port default to a container-friendly bind. The ``trellis serve``
    CLI subcommand is the preferred entrypoint — it exposes
    ``--host``/``--port``/``--config-dir`` flags and configures structured
    logging before the server starts.
    """
    import uvicorn  # noqa: PLC0415

    from trellis_api.logging import configure_logging  # noqa: PLC0415

    configure_logging()
    app = create_app()
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
