"""Tests for TrellisClient."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from trellis_sdk.client import TrellisClient


@pytest.fixture
def local_client(tmp_path: Path):
    """Create a local-mode client with temp stores."""
    os.environ["TRELLIS_CONFIG_DIR"] = str(tmp_path / "config")
    os.environ["TRELLIS_DATA_DIR"] = str(tmp_path / "data")
    (tmp_path / "data" / "stores").mkdir(parents=True)
    client = TrellisClient()
    yield client
    client.close()
    del os.environ["TRELLIS_DATA_DIR"]
    del os.environ["TRELLIS_CONFIG_DIR"]


def test_local_client_is_not_remote(local_client):
    assert not local_client.is_remote


def test_remote_client_is_remote():
    client = TrellisClient(base_url="http://localhost:8420")
    assert client.is_remote
    client.close()


def test_ingest_and_get_trace(local_client):
    trace = {
        "source": "agent",
        "intent": "test task",
        "steps": [],
        "context": {"agent_id": "test-agent", "domain": "test"},
    }
    trace_id = local_client.ingest_trace(trace)
    assert trace_id is not None

    result = local_client.get_trace(trace_id)
    assert result is not None
    assert result["intent"] == "test task"


def test_get_nonexistent_trace(local_client):
    assert local_client.get_trace("nonexistent") is None


def test_list_traces(local_client):
    trace = {
        "source": "agent",
        "intent": "list test",
        "steps": [],
        "context": {"agent_id": "test-agent", "domain": "test"},
    }
    local_client.ingest_trace(trace)
    traces = local_client.list_traces()
    assert len(traces) == 1
    assert traces[0]["intent"] == "list test"


def test_search_empty(local_client):
    results = local_client.search("nothing here")
    assert results == []


def test_create_and_get_entity(local_client):
    node_id = local_client.create_entity("Redis", entity_type="system")
    assert node_id is not None

    entity = local_client.get_entity(node_id)
    assert entity is not None
    assert entity["properties"]["name"] == "Redis"


def test_create_link(local_client):
    id1 = local_client.create_entity("Service A")
    id2 = local_client.create_entity("Service B")
    edge_id = local_client.create_link(id1, id2, edge_kind="depends_on")
    assert edge_id is not None


def test_assemble_pack(local_client):
    pack = local_client.assemble_pack("test intent")
    assert pack["intent"] == "test intent"
    assert "pack_id" in pack
