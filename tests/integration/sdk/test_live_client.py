"""Black-box live HTTP round-trip for the synchronous ``TrellisClient``.

The ``TestClient``-based suite under ``tests/unit/sdk/`` covers
parameter shaping and response decoding in isolation; this file
proves the SDK works end-to-end against a real ``uvicorn`` process,
exercising real httpx connection pooling, the version handshake
round-trip, and JSON decode paths the in-process Starlette client
short-circuits.

Skipped when ``TRELLIS_TEST_NEO4J_URI`` or ``TRELLIS_TEST_PG_DSN``
isn't set — the underlying ``live_api_server`` fixture spawns uvicorn
against the cloud-default deployment shape.
"""

from __future__ import annotations

import httpx

from trellis_sdk import TrellisClient


def test_version_handshake_against_live_server(live_api_server: str) -> None:
    """First request fires the version handshake against ``/api/version``.

    With ``verify_version=True`` (the default), the SDK rejects an
    incompatible server. A successful first call proves the handshake
    round-tripped real HTTP and parsed the version response.
    """
    with TrellisClient(base_url=live_api_server) as client:
        # search() is the cheapest call that goes through ``_request``,
        # which forces ``_ensure_handshake`` first.
        results = client.search("anything")
    assert isinstance(results, list)


def test_create_and_get_entity_round_trip(live_api_server: str) -> None:
    """``create_entity`` then ``get_entity`` round-trips through Neo4j."""
    with TrellisClient(base_url=live_api_server) as client:
        node_id = client.create_entity(
            name="sdk-live-roundtrip",
            entity_type="service",
            properties={"team": "sdk-test"},
        )
        assert isinstance(node_id, str)
        assert node_id

        entity = client.get_entity(node_id)
        assert entity is not None
        # The route returns ``{"entity": {...}}``; the SDK unwraps to the
        # inner dict. ``name`` is stored as a property on the node, not
        # a top-level column — the GraphStore contract is properties-first.
        assert entity["properties"]["name"] == "sdk-live-roundtrip"


def test_get_entity_returns_none_for_missing(live_api_server: str) -> None:
    """``get_entity`` maps 404 to ``None`` rather than raising."""
    with TrellisClient(base_url=live_api_server) as client:
        result = client.get_entity("sdk:does-not-exist")
    assert result is None


def test_create_link_returns_edge_id(live_api_server: str) -> None:
    """``create_link`` ties two existing entities together; SDK returns the edge id."""
    with TrellisClient(base_url=live_api_server) as client:
        src = client.create_entity(name="sdk-link-src", entity_type="service")
        dst = client.create_entity(name="sdk-link-dst", entity_type="service")
        edge_id = client.create_link(
            source_id=src,
            target_id=dst,
            edge_kind="depends_on",
        )
    assert isinstance(edge_id, str)
    assert edge_id


def test_assemble_pack_returns_pack_id(live_api_server: str) -> None:
    """``assemble_pack`` round-trips through PackBuilder + the live EventLog."""
    with TrellisClient(base_url=live_api_server) as client:
        pack = client.assemble_pack(
            intent="sdk-live-pack",
            max_items=5,
            max_tokens=1000,
        )
    assert pack["pack_id"]
    assert pack["intent"] == "sdk-live-pack"
    assert isinstance(pack["count"], int)


def test_search_against_live_server(live_api_server: str) -> None:
    """``search`` returns a list (empty pre-seed is fine)."""
    with TrellisClient(base_url=live_api_server) as client:
        results = client.search("test", limit=5)
    assert isinstance(results, list)


def test_list_traces_returns_empty_list_after_wipe(live_api_server: str) -> None:
    """``list_traces`` against a wiped registry returns an empty list, not 500."""
    with TrellisClient(base_url=live_api_server) as client:
        traces = client.list_traces(limit=10)
    # The fixture wipes the trace table before yielding, so a freshly
    # spawned uvicorn against this registry has no traces. An empty
    # list (rather than a server error) proves the read path is alive.
    assert isinstance(traces, list)
    assert traces == []


def test_client_constructed_with_injected_http(live_api_server: str) -> None:
    """Passing ``http=httpx.Client`` bypasses the SDK's owned client.

    Validates the documented dual-construction contract: pass
    ``base_url`` for production, or pass ``http=`` for tests / custom
    transports. The SDK shouldn't try to close an http client it
    doesn't own — the ``with`` block here closes ours, the SDK leaves
    it alone.
    """
    with httpx.Client(base_url=live_api_server, timeout=15.0) as transport:
        client = TrellisClient(http=transport)
        node_id = client.create_entity(
            name="sdk-injected-http",
            entity_type="service",
        )
        client.close()
        # transport must still be usable since we own it
        resp = transport.get("/healthz")
        assert resp.status_code == 200
    assert isinstance(node_id, str)
    assert node_id
