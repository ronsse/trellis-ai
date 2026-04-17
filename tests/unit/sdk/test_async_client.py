"""Tests for AsyncTrellisClient."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from trellis_sdk.async_client import AsyncTrellisClient


@pytest.fixture
async def local_client(tmp_path: Path):
    """Create a local-mode async client with temp stores."""
    os.environ["TRELLIS_CONFIG_DIR"] = str(tmp_path / "config")
    os.environ["TRELLIS_DATA_DIR"] = str(tmp_path / "data")
    (tmp_path / "data" / "stores").mkdir(parents=True)
    client = AsyncTrellisClient()
    yield client
    await client.close()
    del os.environ["TRELLIS_DATA_DIR"]
    del os.environ["TRELLIS_CONFIG_DIR"]


class TestAsyncClientMode:
    """Mode detection mirrors sync client."""

    def test_local_client_is_not_remote(self, local_client: AsyncTrellisClient) -> None:
        assert not local_client.is_remote

    def test_remote_client_is_remote(self) -> None:
        client = AsyncTrellisClient(base_url="http://localhost:8420")
        assert client.is_remote

    @pytest.mark.asyncio
    async def test_context_manager(self, tmp_path: Path) -> None:
        os.environ["TRELLIS_CONFIG_DIR"] = str(tmp_path / "config2")
        os.environ["TRELLIS_DATA_DIR"] = str(tmp_path / "data2")
        (tmp_path / "data2" / "stores").mkdir(parents=True)
        try:
            async with AsyncTrellisClient() as client:
                assert not client.is_remote
        finally:
            del os.environ["TRELLIS_DATA_DIR"]
            del os.environ["TRELLIS_CONFIG_DIR"]


class TestAsyncIngest:
    """Ingest operations via async client."""

    @pytest.mark.asyncio
    async def test_ingest_and_get_trace(self, local_client: AsyncTrellisClient) -> None:
        trace = {
            "source": "agent",
            "intent": "async test task",
            "steps": [],
            "context": {"agent_id": "test-agent", "domain": "test"},
        }
        trace_id = await local_client.ingest_trace(trace)
        assert trace_id is not None

        result = await local_client.get_trace(trace_id)
        assert result is not None
        assert result["intent"] == "async test task"

    @pytest.mark.asyncio
    async def test_get_nonexistent_trace(
        self, local_client: AsyncTrellisClient
    ) -> None:
        assert await local_client.get_trace("nonexistent") is None


class TestAsyncRetrieve:
    """Retrieve operations via async client."""

    @pytest.mark.asyncio
    async def test_list_traces(self, local_client: AsyncTrellisClient) -> None:
        trace = {
            "source": "agent",
            "intent": "async list test",
            "steps": [],
            "context": {"agent_id": "test-agent", "domain": "test"},
        }
        await local_client.ingest_trace(trace)
        traces = await local_client.list_traces()
        assert len(traces) == 1
        assert traces[0]["intent"] == "async list test"

    @pytest.mark.asyncio
    async def test_search_empty(self, local_client: AsyncTrellisClient) -> None:
        results = await local_client.search("nothing here")
        assert results == []

    @pytest.mark.asyncio
    async def test_assemble_pack(self, local_client: AsyncTrellisClient) -> None:
        pack = await local_client.assemble_pack("async test intent")
        assert pack["intent"] == "async test intent"
        assert "pack_id" in pack


class TestAsyncCurate:
    """Curation operations via async client."""

    @pytest.mark.asyncio
    async def test_create_and_get_entity(
        self, local_client: AsyncTrellisClient
    ) -> None:
        node_id = await local_client.create_entity("AsyncRedis", entity_type="system")
        assert node_id is not None

        entity = await local_client.get_entity(node_id)
        assert entity is not None
        assert entity["properties"]["name"] == "AsyncRedis"

    @pytest.mark.asyncio
    async def test_create_link(self, local_client: AsyncTrellisClient) -> None:
        id1 = await local_client.create_entity("Async Service A")
        id2 = await local_client.create_entity("Async Service B")
        edge_id = await local_client.create_link(id1, id2, edge_kind="depends_on")
        assert edge_id is not None


class TestAsyncConcurrency:
    """Verify concurrent operations work correctly."""

    @pytest.mark.asyncio
    async def test_concurrent_ingests(self, local_client: AsyncTrellisClient) -> None:
        """Multiple concurrent ingest_trace calls should all succeed."""
        traces = [
            {
                "source": "agent",
                "intent": f"concurrent task {i}",
                "steps": [],
                "context": {"agent_id": "test-agent", "domain": "test"},
            }
            for i in range(5)
        ]
        results = await asyncio.gather(*(local_client.ingest_trace(t) for t in traces))
        assert len(results) == 5
        assert all(r is not None for r in results)

        # All traces should be retrievable
        all_traces = await local_client.list_traces(limit=10)
        assert len(all_traces) == 5

    @pytest.mark.asyncio
    async def test_concurrent_reads(self, local_client: AsyncTrellisClient) -> None:
        """Multiple concurrent reads should not interfere."""
        trace = {
            "source": "agent",
            "intent": "concurrent read target",
            "steps": [],
            "context": {"agent_id": "test-agent", "domain": "test"},
        }
        trace_id = await local_client.ingest_trace(trace)

        results = await asyncio.gather(
            *(local_client.get_trace(trace_id) for _ in range(10))
        )
        assert all(r is not None for r in results)
        assert all(r["intent"] == "concurrent read target" for r in results)
