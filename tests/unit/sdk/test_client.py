"""Tests for TrellisClient (sync, HTTP-only)."""

from __future__ import annotations

from pathlib import Path

import pytest

from trellis.testing import in_memory_client
from trellis_sdk.client import TrellisClient


@pytest.fixture
def client(tmp_path: Path):
    """An in-memory client backed by a tmp_path StoreRegistry."""
    with in_memory_client(tmp_path / "stores") as c:
        yield c


class TestConstruction:
    def test_requires_base_url_or_http(self):
        with pytest.raises(ValueError, match="base_url"):
            TrellisClient()

    def test_rejects_both_base_url_and_http(self, tmp_path: Path):
        with (
            in_memory_client(tmp_path / "stores") as c,
            pytest.raises(ValueError, match="base_url OR http"),
        ):
            TrellisClient(base_url="http://x", http=c._http)


class TestIngestAndRetrieve:
    def test_ingest_and_get_trace(self, client):
        trace = {
            "source": "agent",
            "intent": "test task",
            "steps": [],
            "context": {"agent_id": "test-agent", "domain": "test"},
        }
        trace_id = client.ingest_trace(trace)
        assert trace_id is not None

        result = client.get_trace(trace_id)
        assert result is not None
        assert result["intent"] == "test task"

    def test_get_nonexistent_trace(self, client):
        assert client.get_trace("nonexistent") is None

    def test_list_traces(self, client):
        trace = {
            "source": "agent",
            "intent": "list test",
            "steps": [],
            "context": {"agent_id": "test-agent", "domain": "test"},
        }
        client.ingest_trace(trace)
        traces = client.list_traces()
        assert len(traces) == 1
        assert traces[0]["intent"] == "list test"

    def test_search_empty(self, client):
        results = client.search("nothing here")
        assert results == []


class TestCurate:
    def test_create_and_get_entity(self, client):
        node_id = client.create_entity("Redis", entity_type="system")
        assert node_id is not None

        entity = client.get_entity(node_id)
        assert entity is not None
        assert entity["properties"]["name"] == "Redis"

    def test_create_link(self, client):
        id1 = client.create_entity("Service A")
        id2 = client.create_entity("Service B")
        edge_id = client.create_link(id1, id2, edge_kind="depends_on")
        assert edge_id is not None


class TestPack:
    def test_assemble_pack(self, client):
        pack = client.assemble_pack("test intent")
        assert pack["intent"] == "test intent"
        assert "pack_id" in pack
