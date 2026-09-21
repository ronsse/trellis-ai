"""Tests for TrellisClient (sync, HTTP-only)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from tests.chunk_corpus import seed_chunked
from trellis.testing import in_memory_client
from trellis_sdk.client import TrellisClient
from trellis_sdk.exceptions import TrellisClientError


@pytest.fixture
def client(tmp_path: Path):
    """An in-memory client backed by a tmp_path StoreRegistry."""
    with in_memory_client(tmp_path / "stores") as c:
        yield c


def _seed_chunked_via_registry() -> list[str]:
    """Seed parents plus their chunk slices into the live registry.

    ``in_memory_client`` builds the registry itself and binds it to the API
    app module, so that module is the only handle a client-side test has on
    the store behind the route.
    """
    import trellis_api.app as app_module

    return seed_chunked(app_module._registry.knowledge.document_store)


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

    def test_search_excludes_chunk_rows_by_default(self, client):
        """The SDK inherits the route's default (#396), it does not set one.

        ``client.search`` targets ``GET /api/v1/search``, so the chunk
        exclusion applies to SDK agents too. Asserted here rather than
        only on the route because the SDK's docstring claims it does not
        pin a second copy of the default — a claim nothing enforced until
        this test and its opt-in sibling below.
        """
        parents = _seed_chunked_via_registry()
        assert {d["doc_id"] for d in client.search("distinctive")} == set(parents)

    def test_search_omits_include_chunks_when_unset(self, client, monkeypatch):
        """The tri-state's whole justification, pinned.

        ``include_chunks: bool | None = None`` exists so the route owns the
        default and the SDK does not carry a second copy that could drift.
        A regression from ``is not None`` to plain truthiness would send
        ``include_chunks=false`` — identical behaviour today, and a live bug
        the moment the route's default flips. The only way to see the
        difference is on the wire.
        """
        sent: list[dict] = []
        original = client._request

        def _record(method, path, **kwargs):
            sent.append(dict(kwargs.get("params") or {}))
            return original(method, path, **kwargs)

        monkeypatch.setattr(client, "_request", _record)

        client.search("anything")
        client.search("anything", include_chunks=False)

        assert "include_chunks" not in sent[0]
        assert sent[1]["include_chunks"] is False

    def test_search_include_chunks_opt_in_reaches_the_route(self, client):
        """``include_chunks=True`` is forwarded; omitting it sends nothing.

        The tri-state (``bool | None``) is the whole mechanism: were the
        SDK to default the parameter to ``False`` it would be a second
        copy of the route's default, free to drift from it.
        """
        _seed_chunked_via_registry()
        results = client.search("distinctive", include_chunks=True)
        assert len(results) == 12
        assert any("#chunk-" in d["doc_id"] for d in results)


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

    def test_create_link_allow_dangling(self, client):
        """allow_dangling lets an edge write before its target exists (#211)."""
        src = client.create_entity("Source Table", entity_type="table")
        # Default: a link to a non-existent target is rejected by the FK
        # pre-flight and surfaces as a 4xx client error.
        with pytest.raises(TrellisClientError):
            client.create_link(src, "ghost-target", edge_kind="references_table")
        # Opt in: the same edge writes through.
        edge_id = client.create_link(
            src, "ghost-target", edge_kind="references_table", allow_dangling=True
        )
        assert edge_id is not None


class TestPack:
    def test_assemble_pack(self, client):
        pack = client.assemble_pack("test intent")
        assert pack["intent"] == "test intent"
        assert "pack_id" in pack

    @staticmethod
    def _capture_pack_body(**kwargs) -> dict:
        """Assemble a pack against a scripted transport; return the body."""
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured.update(json.loads(request.content))
            return httpx.Response(200, json={"pack_id": "p1", "items": []})

        http = httpx.Client(
            transport=httpx.MockTransport(handler), base_url="http://testserver"
        )
        with TrellisClient(http=http, verify_version=False) as client:
            client.assemble_pack("test intent", **kwargs)
        return captured

    def test_attribution_is_sent_when_set(self):
        body = self._capture_pack_body(run_id="run-7", intent_family="custom")
        assert body["run_id"] == "run-7"
        assert body["intent_family"] == "custom"

    def test_attribution_keys_are_omitted_when_unset(self):
        """Version skew: ``WireRequestModel`` is ``extra="forbid"``, so an
        SDK newer than its API must not send keys that API can't parse —
        sending ``None`` would turn every pack assembly into a 422."""
        body = self._capture_pack_body()
        assert "run_id" not in body
        assert "intent_family" not in body

    @pytest.mark.parametrize(
        "method_name",
        ["get_objective_context", "get_task_context"],
    )
    def test_sectioned_context_renders_empty_pack_routing(
        self, method_name: str
    ) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "pack_id": "p1",
                    "sections": [{"name": "only", "items": []}],
                    "withholding": {
                        "total": 0,
                        "by_reason": {},
                        "withheld_item_ids": ["sectioned-secret-id"],
                        "non_absence_reasons": [],
                        "section_filtered": 2,
                        "served_count": 0,
                    },
                },
            )

        http = httpx.Client(
            transport=httpx.MockTransport(handler), base_url="http://testserver"
        )
        with TrellisClient(http=http, verify_version=False) as client:
            rendered = getattr(client, method_name)("deploy checklist")

        assert "**Section routing:** this pack is empty because 2 items" in rendered
        assert rendered.index("**Section routing:**") < rendered.index("## Section:")
        assert "sectioned-secret-id" not in rendered
