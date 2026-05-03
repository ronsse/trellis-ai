"""FastAPI application for Trellis."""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from trellis.stores.registry import StoreRegistry

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
    logger.info("api_stores_initialized")
    yield
    _registry.close()
    _registry = None
    logger.info("api_stores_closed")


_STATIC_DIR = Path(__file__).parent / "static"


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    from trellis_api.routes import (  # noqa: PLC0415
        admin,
        curate,
        extract,
        health,
        ingest,
        mutations,
        policies,
        retrieve,
        version,
    )

    app = FastAPI(
        title="Trellis API",
        description="Structured memory and learning for AI agents",
        version="0.2.0",
        lifespan=lifespan,
    )

    @app.get("/", include_in_schema=False)
    async def root_redirect() -> RedirectResponse:
        return RedirectResponse(url="/ui/", status_code=307)

    # Version handshake — unversioned, mounted at /api/version (no prefix).
    # Deliberately outside /api/v1 because it describes which major is running.
    app.include_router(version.router, tags=["version"])

    # Liveness/readiness probes — unversioned, deployment plumbing.
    app.include_router(health.router, tags=["health"])

    app.include_router(admin.router, prefix="/api/v1", tags=["admin"])
    app.include_router(ingest.router, prefix="/api/v1", tags=["ingest"])
    app.include_router(retrieve.router, prefix="/api/v1", tags=["retrieve"])
    app.include_router(curate.router, prefix="/api/v1", tags=["curate"])
    app.include_router(mutations.router, prefix="/api/v1", tags=["mutations"])
    app.include_router(policies.router, prefix="/api/v1", tags=["policies"])
    app.include_router(extract.router, prefix="/api/v1", tags=["extract"])

    # Serve the UI at /ui (static files bundled in the package)
    if _STATIC_DIR.is_dir():
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
