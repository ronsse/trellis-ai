"""Spin up an in-process API + SDK client pair for fast tests.

Implementation detail of :mod:`trellis.testing`.  See that module's
docstring for user-facing docs.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
from fastapi import FastAPI
from starlette.testclient import TestClient as StarletteTestClient

import trellis_api.app as api_app_module
from trellis_api.routes import (
    admin,
    curate,
    ingest,
    mutations,
    policies,
    retrieve,
    version,
)
from trellis_sdk.async_client import AsyncTrellisClient
from trellis_sdk.client import TrellisClient

if TYPE_CHECKING:
    from trellis.stores.registry import StoreRegistry


def _build_app(registry: StoreRegistry) -> FastAPI:
    """Assemble a FastAPI app that reuses the provided registry.

    Bypasses the normal ``create_app`` lifespan (which would call
    ``StoreRegistry.from_config_dir`` and blow past our tmp_path) by
    installing the registry directly on the module and using a
    no-op lifespan.
    """

    @asynccontextmanager
    async def _noop_lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield

    app = FastAPI(lifespan=_noop_lifespan)
    app.include_router(version.router)
    app.include_router(admin.router, prefix="/api/v1", tags=["admin"])
    app.include_router(ingest.router, prefix="/api/v1", tags=["ingest"])
    app.include_router(retrieve.router, prefix="/api/v1", tags=["retrieve"])
    app.include_router(curate.router, prefix="/api/v1", tags=["curate"])
    app.include_router(mutations.router, prefix="/api/v1", tags=["mutations"])
    app.include_router(policies.router, prefix="/api/v1", tags=["policies"])

    # Wire the registry the routes pull from via get_registry().
    api_app_module._registry = registry
    return app


def _make_registry(stores_dir: Path) -> StoreRegistry:
    """Create a StoreRegistry rooted at ``stores_dir``.

    Imported here (not at module top) to keep ``trellis_sdk`` importable
    without core being loaded; only callers that actually use the
    testing shim pay the import cost.
    """
    from trellis.stores.registry import StoreRegistry  # noqa: PLC0415

    stores_dir.mkdir(parents=True, exist_ok=True)
    return StoreRegistry(stores_dir=stores_dir)


@contextmanager
def in_memory_client(
    stores_dir: Path | None = None,
) -> Iterator[TrellisClient]:
    """Yield a :class:`TrellisClient` bound to an in-process FastAPI app.

    ``stores_dir`` defaults to a no-op (the caller's responsibility to
    clean up); pass a ``tmp_path`` from pytest for isolated tests.
    When omitted, store ops that need a path will raise — retrieval
    and version calls still work.

    Sync implementation uses Starlette's :class:`TestClient` (which
    *is* an :class:`httpx.Client` subclass) because bare
    :class:`httpx.ASGITransport` is async-only.  The async variant
    below uses the async transport directly.
    """
    registry = _make_registry(stores_dir) if stores_dir else _empty_registry()
    app = _build_app(registry)
    http = StarletteTestClient(app, base_url="http://testserver")
    # Enter the starlette test client context so lifespan startup runs
    # and async portal is initialized.
    http.__enter__()
    client = TrellisClient(http=http, verify_version=False)
    try:
        yield client
    finally:
        client.close()
        http.__exit__(None, None, None)
        registry.close()
        api_app_module._registry = None


@asynccontextmanager
async def in_memory_async_client(
    stores_dir: Path | None = None,
    *,
    max_concurrency: int = 16,
) -> AsyncIterator[AsyncTrellisClient]:
    """Async counterpart to :func:`in_memory_client`."""
    registry = _make_registry(stores_dir) if stores_dir else _empty_registry()
    app = _build_app(registry)
    transport = httpx.ASGITransport(app=app)
    http = httpx.AsyncClient(transport=transport, base_url="http://testserver")
    client = AsyncTrellisClient(
        http=http,
        verify_version=False,
        max_concurrency=max_concurrency,
    )
    try:
        yield client
    finally:
        await client.close()
        await http.aclose()
        registry.close()
        api_app_module._registry = None


def _empty_registry() -> StoreRegistry:
    """Registry with no stores_dir.  Only useful for version-handshake
    style tests where no store is accessed."""
    from trellis.stores.registry import StoreRegistry  # noqa: PLC0415

    return StoreRegistry()


__all__ = ["in_memory_async_client", "in_memory_client"]
