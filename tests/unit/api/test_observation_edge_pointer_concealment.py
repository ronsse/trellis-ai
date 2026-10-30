"""Observation reads and entity edges name no document the caller cannot read.

Follows ``test_entities_pointer_concealment.py``. An observation node carries
the ``evidence_ref`` its writer gave, normally a document id, and an edge row
carries a top-level ``evidence_ref`` provenance column beside caller-supplied
``properties``. A node and an edge carry no label, so each is served whole; the
id of a document above the caller's clearance is not. The document itself
answers 404 at ``GET /documents/{id}``, so such an id would be a name for
something the key may not touch.

``GET /observations`` and MCP ``query_observations`` conceal the nodes;
``GET /entities/{id}`` conceals the subgraph's edges in the same pass as its
nodes. MCP ``get_graph`` fetches the same edges but renders only their endpoints
and type, which a last test pins.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

import trellis.mcp.server as server_mod
import trellis_api.app as app_module
from tests.unit.mcp.conftest import unwrap_tool
from tests.unit.mcp.test_sensitivity_surfaces import (
    ADMIN,
    READ,
    RESTRICTED,
    as_http,
    as_stdio,
)
from tests.unit.retrieve.test_sensitivity import label
from trellis.auth import generate_api_key
from trellis.retrieve.sensitivity import conceal_edge_pointers
from trellis.schemas.well_known import OBSERVATION
from trellis.stores.registry import StoreRegistry
from trellis_api.app import create_app

query_observations = unwrap_tool(server_mod.query_observations)
get_graph = unwrap_tool(server_mod.get_graph)

HIDDEN_ID = "doc-hidden"
OPEN_ID = "doc-open"
TRACE_REF = "trace-abc"
ROOT = "node-root"
#: Target node -> what the ROOT edge to it carries (see ``_seed``).
EDGE_COLUMN = "node-edge-column"
EDGE_PROP = "node-edge-prop"
EDGE_LIFECYCLE = "node-edge-lifecycle"
EDGE_OPEN = "node-edge-open"
EDGE_ODD = "node-edge-odd"
OBS_HIDDEN = "obs-hidden"
OBS_OPEN = "obs-open"
OBS_TRACE = "obs-trace"
OBS_ODD = "obs-odd"
OBS_BARE = "obs-bare"


def _seed(registry: StoreRegistry, level: str) -> None:
    docs = registry.knowledge.document_store
    docs.put(OPEN_ID, "open body", {})
    docs.put(HIDDEN_ID, "hidden body", label(level))
    graph = registry.knowledge.graph_store
    for node_id, evidence in (
        (OBS_HIDDEN, HIDDEN_ID),
        (OBS_OPEN, OPEN_ID),
        (OBS_TRACE, TRACE_REF),
        (OBS_ODD, 7),
    ):
        graph.upsert_node(
            node_id,
            OBSERVATION,
            {
                "subject_entity_id": "subject-1",
                "observer_agent_id": "agent-1",
                "content": f"claim {node_id}",
                "evidence_ref": evidence,
            },
        )
    graph.upsert_node(
        OBS_BARE,
        OBSERVATION,
        {
            "subject_entity_id": "subject-1",
            "observer_agent_id": "agent-1",
            "content": "claim bare",
        },
    )

    graph.upsert_node(ROOT, "entity", {"name": "root"})
    for node_id in (EDGE_COLUMN, EDGE_PROP, EDGE_LIFECYCLE, EDGE_OPEN, EDGE_ODD):
        graph.upsert_node(node_id, "entity", {"name": node_id})
    # The provenance column.
    graph.upsert_edge(ROOT, EDGE_COLUMN, "related_to", evidence_ref=HIDDEN_ID)
    # A pointer in the caller-supplied properties, with a neighbour that stays.
    graph.upsert_edge(
        ROOT,
        EDGE_PROP,
        "related_to",
        {"evidence_ref": HIDDEN_ID, "note": "kept"},
    )
    # A nested pointer, as a lifecycle stamp carries it.
    graph.upsert_edge(
        ROOT,
        EDGE_LIFECYCLE,
        "related_to",
        {"lifecycle": {"state": "superseded", "superseded_by": HIDDEN_ID}},
    )
    graph.upsert_edge(
        ROOT,
        EDGE_OPEN,
        "related_to",
        {"evidence_ref": OPEN_ID, "note": "kept"},
        evidence_ref=OPEN_ID,
    )
    # A property that is not a string names nothing.
    graph.upsert_edge(ROOT, EDGE_ODD, "related_to", {"evidence_ref": 7, "note": "kept"})


@pytest.fixture(params=["restricted", "confidential"])
def level(request) -> str:
    return request.param


@pytest.fixture
def registry(tmp_path, monkeypatch, level):
    monkeypatch.delenv("TRELLIS_API_KEY", raising=False)
    monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")
    reg = StoreRegistry(stores_dir=tmp_path / "stores")
    app_module._registry = reg
    server_mod._registry = reg
    _seed(reg, level)
    yield reg
    reg.close()
    app_module._registry = None
    server_mod._registry = None


@pytest.fixture
def client(registry):
    return TestClient(create_app())


@pytest.fixture
def reads(registry, monkeypatch) -> list[str]:
    """Every id the document store is asked for, from here on."""
    store = registry.knowledge.document_store
    real_get = store.get
    seen: list[str] = []

    def counting(doc_id):
        seen.append(doc_id)
        return real_get(doc_id)

    monkeypatch.setattr(store, "get", counting)
    return seen


def _headers(registry, scopes):
    token, record = generate_api_key("k", scopes)
    registry.operational.api_key_store.create(record)
    return {"Authorization": f"Bearer {token}"}


def _cleared(level: str) -> list[list[str]]:
    """The scope sets that may read a document labelled ``level``."""
    out = [["admin"], ["read", "restricted"]]
    if level == "confidential":
        out.append(["read", "confidential"])
    return out


def _blind(level: str) -> list[list[str]]:
    return [["read"]] + ([["read", "confidential"]] if level == "restricted" else [])


def _listed(client, headers=None) -> Any:
    return client.get(
        "/api/v1/observations",
        params={"subject_entity_id": "subject-1"},
        headers=headers or {},
    )


def _by_id(resp) -> dict[str, dict[str, Any]]:
    assert resp.status_code == 200, resp.text
    return {o["node_id"]: o for o in resp.json()["observations"]}


def _entity(client, headers=None, node_id=ROOT, **params) -> Any:
    return client.get(
        f"/api/v1/entities/{node_id}", params=params, headers=headers or {}
    )


def _edges(resp) -> dict[str, dict[str, Any]]:
    assert resp.status_code == 200, resp.text
    return {e["target_id"]: e for e in resp.json()["subgraph"]["edges"]}


class TestListObservationsConcealsHiddenPointers:
    def test_the_document_itself_is_withheld_so_an_id_is_a_name_for_nothing(
        self, client, registry
    ):
        resp = client.get(
            f"/api/v1/documents/{HIDDEN_ID}", headers=_headers(registry, ["read"])
        )
        assert resp.status_code == 404

    def test_a_key_below_the_label_sees_the_id_on_no_observation(
        self, client, registry, level
    ):
        for scopes in _blind(level):
            resp = _listed(client, _headers(registry, scopes))
            observations = _by_id(resp)
            assert HIDDEN_ID not in resp.text, scopes
            hidden = observations[OBS_HIDDEN]
            assert "evidence_ref" not in hidden
            assert hidden["content"] == f"claim {OBS_HIDDEN}"
            assert hidden["observer_agent_id"] == "agent-1"
            assert hidden["node_type"] == OBSERVATION

    def test_a_key_that_may_read_the_label_sees_the_id(self, client, registry, level):
        for scopes in _cleared(level):
            observations = _by_id(_listed(client, _headers(registry, scopes)))
            assert observations[OBS_HIDDEN]["evidence_ref"] == HIDDEN_ID, scopes

    def test_auth_off_reads_everything(self, client, monkeypatch):
        monkeypatch.setenv("TRELLIS_AUTH_MODE", "off")
        observations = _by_id(_listed(client))
        assert observations[OBS_HIDDEN]["evidence_ref"] == HIDDEN_ID

    def test_a_readable_id_a_non_document_ref_and_a_non_string_are_kept(
        self, client, registry
    ):
        observations = _by_id(_listed(client, _headers(registry, ["read"])))
        assert observations[OBS_OPEN]["evidence_ref"] == OPEN_ID
        assert observations[OBS_TRACE]["evidence_ref"] == TRACE_REF
        assert observations[OBS_ODD]["evidence_ref"] == 7
        assert "evidence_ref" not in observations[OBS_BARE]

    def test_the_list_does_not_change_shape(self, client, registry):
        blind = _listed(client, _headers(registry, ["read"])).json()
        full = _listed(client, _headers(registry, ["admin"])).json()
        assert [o["node_id"] for o in blind["observations"]] == [
            o["node_id"] for o in full["observations"]
        ]
        assert set(blind) == set(full) == {"observations"}

    def test_the_stored_row_is_unchanged(self, client, registry):
        _listed(client, _headers(registry, ["read"]))
        stored = registry.knowledge.graph_store.get_node(OBS_HIDDEN)
        assert stored["properties"]["evidence_ref"] == HIDDEN_ID

    def test_the_whole_page_is_judged_in_one_pass(self, client, registry, reads):
        graph = registry.knowledge.graph_store
        for name in ("obs-dup-a", "obs-dup-b"):
            graph.upsert_node(
                name,
                OBSERVATION,
                {"subject_entity_id": "subject-1", "evidence_ref": HIDDEN_ID},
            )
        resp = _listed(client, _headers(registry, ["read"]))
        assert len(resp.json()["observations"]) == 7
        assert reads.count(HIDDEN_ID) == 1
        assert reads.count(OPEN_ID) == 1

    @pytest.mark.parametrize("scopes", [["admin"], ["read", "restricted"]])
    def test_a_reader_of_everything_does_no_reads(
        self, client, registry, reads, scopes
    ):
        assert _listed(client, _headers(registry, scopes)).status_code == 200
        assert reads == []


class TestQueryObservationsConcealsHiddenPointers:
    def _query(self) -> dict[str, dict[str, Any]]:
        out = json.loads(query_observations(subject_entity_id="subject-1"))
        assert out["status"] == "ok"
        return {o["node_id"]: o for o in out["observations"]}

    def test_a_key_below_the_label_sees_the_id_on_no_observation(self, registry, level):
        for scopes in _blind(level):
            with as_http(scopes):
                raw = query_observations(subject_entity_id="subject-1")
            assert HIDDEN_ID not in raw, scopes
            hidden = {o["node_id"]: o for o in json.loads(raw)["observations"]}[
                OBS_HIDDEN
            ]
            assert "evidence_ref" not in hidden
            assert hidden["content"] == f"claim {OBS_HIDDEN}"

    def test_a_key_that_may_read_the_label_sees_the_id(self, registry, level):
        for scopes in [*_cleared(level)]:
            with as_http(scopes):
                assert self._query()[OBS_HIDDEN]["evidence_ref"] == HIDDEN_ID, scopes

    def test_stdio_reads_everything(self, registry):
        with as_stdio():
            assert self._query()[OBS_HIDDEN]["evidence_ref"] == HIDDEN_ID

    def test_a_readable_id_a_non_document_ref_and_a_non_string_are_kept(self, registry):
        with as_http(READ):
            observations = self._query()
        assert observations[OBS_OPEN]["evidence_ref"] == OPEN_ID
        assert observations[OBS_TRACE]["evidence_ref"] == TRACE_REF
        assert observations[OBS_ODD]["evidence_ref"] == 7

    def test_the_stored_row_is_unchanged(self, registry):
        with as_http(READ):
            self._query()
        stored = registry.knowledge.graph_store.get_node(OBS_HIDDEN)
        assert stored["properties"]["evidence_ref"] == HIDDEN_ID

    def test_the_whole_result_is_judged_in_one_pass(self, registry, reads):
        with as_http(READ):
            self._query()
        assert reads.count(HIDDEN_ID) == 1
        assert reads.count(OPEN_ID) == 1

    @pytest.mark.parametrize("scopes", [ADMIN, RESTRICTED])
    def test_a_reader_of_everything_does_no_reads(self, registry, reads, scopes):
        with as_http(scopes):
            self._query()
        assert reads == []


class TestEntityConcealsEdgePointers:
    def test_a_key_below_the_label_sees_the_id_on_no_edge(
        self, client, registry, level
    ):
        for scopes in _blind(level):
            resp = _entity(client, _headers(registry, scopes))
            edges = _edges(resp)
            assert set(edges) == {
                EDGE_COLUMN,
                EDGE_PROP,
                EDGE_LIFECYCLE,
                EDGE_OPEN,
                EDGE_ODD,
            }, scopes
            assert HIDDEN_ID not in resp.text, scopes
            assert "evidence_ref" not in edges[EDGE_COLUMN]
            assert "evidence_ref" not in edges[EDGE_PROP]["properties"]
            assert edges[EDGE_PROP]["properties"]["note"] == "kept"

    def test_a_key_below_the_label_keeps_the_rest_of_a_nested_pointers_record(
        self, client, registry, level
    ):
        for scopes in _blind(level):
            edges = _edges(_entity(client, _headers(registry, scopes)))
            lifecycle = edges[EDGE_LIFECYCLE]["properties"]["lifecycle"]
            assert lifecycle == {"state": "superseded"}, scopes

    def test_a_key_that_may_read_the_label_sees_every_pointer(
        self, client, registry, level
    ):
        for scopes in _cleared(level):
            edges = _edges(_entity(client, _headers(registry, scopes)))
            assert edges[EDGE_COLUMN]["evidence_ref"] == HIDDEN_ID, scopes
            assert edges[EDGE_PROP]["properties"]["evidence_ref"] == HIDDEN_ID
            lifecycle = edges[EDGE_LIFECYCLE]["properties"]["lifecycle"]
            assert lifecycle["superseded_by"] == HIDDEN_ID

    def test_auth_off_reads_everything(self, client, monkeypatch):
        monkeypatch.setenv("TRELLIS_AUTH_MODE", "off")
        edges = _edges(_entity(client))
        assert edges[EDGE_COLUMN]["evidence_ref"] == HIDDEN_ID
        assert edges[EDGE_PROP]["properties"]["evidence_ref"] == HIDDEN_ID

    def test_a_readable_id_and_a_non_string_value_are_left_as_stored(
        self, client, registry
    ):
        edges = _edges(_entity(client, _headers(registry, ["read"])))
        assert edges[EDGE_OPEN]["evidence_ref"] == OPEN_ID
        assert edges[EDGE_OPEN]["properties"] == {
            "evidence_ref": OPEN_ID,
            "note": "kept",
        }
        assert edges[EDGE_ODD]["properties"] == {"evidence_ref": 7, "note": "kept"}

    def test_the_edge_list_does_not_change_shape(self, client, registry):
        blind = _entity(client, _headers(registry, ["read"])).json()
        full = _entity(client, _headers(registry, ["admin"])).json()
        key = "edge_id"
        assert [e[key] for e in blind["subgraph"]["edges"]] == [
            e[key] for e in full["subgraph"]["edges"]
        ]
        for blind_edge, full_edge in zip(
            blind["subgraph"]["edges"], full["subgraph"]["edges"], strict=True
        ):
            assert set(blind_edge) - {"evidence_ref"} == set(full_edge) - {
                "evidence_ref"
            }

    def test_the_stored_edges_are_unchanged(self, client, registry):
        _entity(client, _headers(registry, ["read"]))
        stored = {
            e["target_id"]: e
            for e in registry.knowledge.graph_store.get_edges(
                ROOT, direction="outgoing"
            )
        }
        assert stored[EDGE_COLUMN]["evidence_ref"] == HIDDEN_ID
        assert stored[EDGE_PROP]["properties"]["evidence_ref"] == HIDDEN_ID
        lifecycle = stored[EDGE_LIFECYCLE]["properties"]["lifecycle"]
        assert lifecycle["superseded_by"] == HIDDEN_ID

    def test_nodes_and_edges_are_judged_in_one_pass(self, client, registry, reads):
        """One store read per distinct id, whichever kind of row names it."""
        registry.knowledge.graph_store.upsert_node(
            "node-names-both",
            "entity",
            {"name": "both", "evidence_ref": HIDDEN_ID},
            document_ids=[HIDDEN_ID, OPEN_ID],
        )
        registry.knowledge.graph_store.upsert_edge(
            ROOT, "node-names-both", "related_to", evidence_ref=HIDDEN_ID
        )
        resp = _entity(client, _headers(registry, ["read"]))
        assert "node-names-both" in _edges(resp)
        assert HIDDEN_ID not in resp.text
        assert reads.count(HIDDEN_ID) == 1
        assert reads.count(OPEN_ID) == 1

    @pytest.mark.parametrize("scopes", [["admin"], ["read", "restricted"]])
    def test_a_reader_of_everything_does_no_reads(
        self, client, registry, reads, scopes
    ):
        assert _entity(client, _headers(registry, scopes)).status_code == 200
        assert reads == []


class TestConcealEdgePointersHelper:
    def _edge(self, **fields: Any) -> dict[str, Any]:
        return {
            "edge_id": "e-1",
            "source_id": "a",
            "target_id": "b",
            "edge_type": "related_to",
            "properties": {},
            "evidence_ref": None,
            **fields,
        }

    def test_an_edge_that_names_nothing_unreadable_comes_back_as_itself(self, registry):
        store = registry.knowledge.document_store
        edge = self._edge(evidence_ref=OPEN_ID, properties={"evidence_ref": OPEN_ID})
        bare = self._edge()
        out = conceal_edge_pointers([edge, bare], store, "internal")
        assert out[0] is edge
        assert out[1] is bare

    def test_a_copy_is_returned_and_the_input_is_not_mutated(self, registry):
        store = registry.knowledge.document_store
        edge = self._edge(
            evidence_ref=HIDDEN_ID,
            properties={"evidence_ref": HIDDEN_ID, "note": "kept"},
        )
        (out,) = conceal_edge_pointers([edge], store, "internal")
        assert "evidence_ref" not in out
        assert out["properties"] == {"note": "kept"}
        assert edge["evidence_ref"] == HIDDEN_ID
        assert edge["properties"]["evidence_ref"] == HIDDEN_ID

    def test_a_store_error_hides_the_id(self, registry, monkeypatch):
        store = registry.knowledge.document_store

        def down(doc_id):
            msg = "store down"
            raise RuntimeError(msg)

        monkeypatch.setattr(store, "get", down)
        (out,) = conceal_edge_pointers(
            [self._edge(evidence_ref=OPEN_ID)], store, "internal"
        )
        assert "evidence_ref" not in out

    def test_no_store_and_restricted_leave_every_edge_as_it_is(self, registry):
        edge = self._edge(evidence_ref=HIDDEN_ID)
        store = registry.knowledge.document_store
        assert conceal_edge_pointers([edge], None, "internal") == [edge]
        assert conceal_edge_pointers([edge], store, "restricted")[0] is edge


class TestGetGraphRendersNoEdgePointer:
    """``get_graph`` fetches the subgraph's edges and renders only their ends.

    Pinned so that a formatter that starts printing an edge's ``evidence_ref`` or
    properties has to conceal them first.
    """

    @pytest.mark.parametrize("scopes", [READ, ADMIN])
    def test_no_http_caller_is_shown_an_edge_pointer(self, registry, scopes):
        with as_http(scopes):
            result = get_graph(ROOT)
        assert "Relationships" in result
        assert HIDDEN_ID not in result

    def test_a_stdio_caller_is_shown_none_either(self, registry):
        with as_stdio():
            result = get_graph(ROOT)
        assert "Relationships" in result
        assert HIDDEN_ID not in result
