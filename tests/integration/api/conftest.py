"""Live uvicorn fixtures for outside-in REST API tests.

These tests sit one layer above the FastAPI ``TestClient`` suites in
``tests/unit/api/``. ``TestClient`` runs the app in-process, skipping
the lifespan, the uvicorn process bridge, real httpx connection
pooling, and the configured logging chain. This module spawns
``uvicorn`` as a real subprocess against the production-shape cloud
config (Neo4j knowledge plane + Postgres operational plane) and
exercises every route as a black-box HTTP client.

The ``live_api_server`` fixture lives in
``tests/integration/_live_server.py`` so the SDK live-round-trip suite
can reuse it. Re-importing the symbol here makes pytest register it as
a fixture in this conftest's namespace.

Skipped cleanly when ``TRELLIS_TEST_NEO4J_URI`` *or*
``TRELLIS_TEST_PG_DSN`` is unset — the cloud-default deployment shape
needs both. Mirrors the gating pattern in
``tests/integration/test_neo4j_e2e.py``.
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest

from tests.integration._live_server import live_api_server  # noqa: F401


@pytest.fixture
def client(live_api_server: str) -> Iterator[httpx.Client]:  # noqa: F811 — pytest fixture name shadows the imported fixture symbol on purpose
    """An httpx.Client bound to the live uvicorn base URL.

    Pure black-box wrapper — tests should not import any
    ``trellis_api`` symbols. Anything they want to assert needs to come
    out of an HTTP response body.
    """
    with httpx.Client(base_url=live_api_server, timeout=15.0) as session:
        yield session
