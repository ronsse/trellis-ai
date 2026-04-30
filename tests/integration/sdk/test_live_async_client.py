"""Black-box live HTTP round-trip for ``AsyncTrellisClient``.

Mirror of ``test_live_client.py`` against the async client. Validates
the same surface (handshake, create/get entity, link, search, pack
assembly, list_traces) plus the ``max_concurrency`` introspection
contract that's unique to the async client.

Skipped when ``TRELLIS_TEST_NEO4J_URI`` or ``TRELLIS_TEST_PG_DSN``
isn't set — same gating as the underlying ``live_api_server`` fixture.
"""

from __future__ import annotations

import httpx
import pytest

from trellis_sdk import AsyncTrellisClient


@pytest.mark.asyncio
async def test_async_version_handshake(live_api_server: str) -> None:
    """First async request fires the handshake under the asyncio lock."""
    async with AsyncTrellisClient(base_url=live_api_server) as client:
        results = await client.search("anything")
    assert isinstance(results, list)


@pytest.mark.asyncio
async def test_async_create_and_get_entity_round_trip(live_api_server: str) -> None:
    """``create_entity`` + ``get_entity`` round-trips against Neo4j over async HTTP."""
    async with AsyncTrellisClient(base_url=live_api_server) as client:
        node_id = await client.create_entity(
            name="async-sdk-roundtrip",
            entity_type="service",
            properties={"team": "async-sdk-test"},
        )
        assert isinstance(node_id, str)
        assert node_id

        entity = await client.get_entity(node_id)
        assert entity is not None
        # ``name`` is stored as a property on the graph node, not a
        # top-level column — see the parallel sync test for context.
        assert entity["properties"]["name"] == "async-sdk-roundtrip"


@pytest.mark.asyncio
async def test_async_get_entity_returns_none_for_missing(
    live_api_server: str,
) -> None:
    """``get_entity`` async path maps 404 to ``None``."""
    async with AsyncTrellisClient(base_url=live_api_server) as client:
        result = await client.get_entity("async-sdk:does-not-exist")
    assert result is None


@pytest.mark.asyncio
async def test_async_create_link(live_api_server: str) -> None:
    """``create_link`` async path returns the edge id."""
    async with AsyncTrellisClient(base_url=live_api_server) as client:
        src = await client.create_entity(name="async-link-src", entity_type="service")
        dst = await client.create_entity(name="async-link-dst", entity_type="service")
        edge_id = await client.create_link(
            source_id=src,
            target_id=dst,
            edge_kind="depends_on",
        )
    assert isinstance(edge_id, str)
    assert edge_id


@pytest.mark.asyncio
async def test_async_assemble_pack(live_api_server: str) -> None:
    """``assemble_pack`` async path round-trips through the live registry."""
    async with AsyncTrellisClient(base_url=live_api_server) as client:
        pack = await client.assemble_pack(
            intent="async-sdk-pack",
            max_items=5,
            max_tokens=1000,
        )
    assert pack["pack_id"]
    assert pack["intent"] == "async-sdk-pack"


@pytest.mark.asyncio
async def test_async_max_concurrency_property(live_api_server: str) -> None:
    """``max_concurrency`` reflects the constructor argument.

    Documents the contract that callers can introspect their bound
    semaphore depth. Specific to the async client — the sync client
    has no such notion. The value is read post-handshake to confirm
    the property is available throughout the client's lifecycle.
    """
    async with AsyncTrellisClient(
        base_url=live_api_server,
        max_concurrency=4,
    ) as client:
        await client.search("warmup")
        assert client.max_concurrency == 4


@pytest.mark.asyncio
async def test_async_client_constructed_with_injected_http(
    live_api_server: str,
) -> None:
    """``http=httpx.AsyncClient`` injection: SDK doesn't close transport it doesn't own."""
    async with httpx.AsyncClient(
        base_url=live_api_server,
        timeout=15.0,
    ) as transport:
        client = AsyncTrellisClient(http=transport)
        node_id = await client.create_entity(
            name="async-sdk-injected-http",
            entity_type="service",
        )
        await client.close()
        resp = await transport.get("/healthz")
        assert resp.status_code == 200
    assert isinstance(node_id, str)
    assert node_id
