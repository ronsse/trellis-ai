"""A graph node's ``evidence_ref`` through the MCP write doors (#1057).

``execute_mutation`` (``entity.create`` and ``entity.update``) and
``record_observation`` keep the field, since they are its governed writers, but a
value a caller supplies must name a document that exists and that the caller may
read, and a node that already holds one keeps it. A refusal is a typed rejection
and writes nothing. The REST doors have their own file; the rule is one helper
(:func:`~trellis.mutate.evidence_pointer_keys.node_evidence_ref_refusal`).
Real SQLite stores and the real handlers.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

import trellis.mcp.server as server_mod
from tests.unit.mcp.conftest import unwrap_tool
from tests.unit.mcp.test_sensitivity_surfaces import as_http, as_stdio
from tests.unit.retrieve.test_sensitivity import label
from trellis.mutate.evidence_pointer_keys import (
    NODE_EVIDENCE_REF_CHANGED,
    NODE_EVIDENCE_REF_NOT_A_STRING,
    NODE_EVIDENCE_REF_UNREADABLE,
)
from trellis.stores.base.event_log import EventType

if TYPE_CHECKING:
    from trellis.stores.registry import StoreRegistry

execute_mutation = unwrap_tool(server_mod.execute_mutation)
record_observation = unwrap_tool(server_mod.record_observation)

OPEN = "failover runbook open: drain the write queue, promote the replica. " * 4
RESTRICTED_BODY = "failover runbook restricted: rotate the vault seal keys. " * 4
NODE_ID = "node-target"
HELD = "doc-held"
OTHER = "doc-other"
RESTRICTED_DOC = "doc-restricted"
MUTATE = ["mutate"]
MUTATE_RESTRICTED = ["mutate", "restricted"]
ENTITY_DOORS = ["entity.create", "entity.update"]


@pytest.fixture(autouse=True)
def _documents(temp_registry: StoreRegistry) -> None:
    docs = temp_registry.knowledge.document_store
    docs.put(HELD, OPEN, {"title": "Held"})
    docs.put(OTHER, OPEN, {"title": "Other"})
    docs.put(RESTRICTED_DOC, RESTRICTED_BODY, {"title": "R", **label("restricted")})


def _seed_node(registry: StoreRegistry, ref: Any = None) -> None:
    properties: dict[str, Any] = {"name": "Target"}
    if ref is not None:
        properties["evidence_ref"] = ref
    registry.knowledge.graph_store.upsert_node(
        node_id=NODE_ID, node_type="service", properties=properties
    )


def _entity(door: str, properties: dict[str, Any]) -> dict[str, Any]:
    if door == "entity.create":
        return {
            "entity_type": "service",
            "name": "Target",
            "entity_id": NODE_ID,
            "properties": properties,
        }
    return {"entity_id": NODE_ID, "properties": properties}


def _run(door: str, properties: dict[str, Any]) -> dict[str, Any]:
    return json.loads(execute_mutation(operation=door, args=_entity(door, properties)))


def _observe(ref: Any) -> dict[str, Any]:
    return json.loads(
        record_observation(
            subject_entity_id="subject-1",
            subject_entity_type="service",
            observer_agent_id="agent-1",
            content="p99 latency rose",
            confidence=0.7,
            evidence_ref=ref,
        )
    )


def _footprint(registry: StoreRegistry) -> dict[str, Any]:
    events = registry.operational.event_log
    return {
        "history": registry.knowledge.graph_store.get_node_history(NODE_ID),
        "nodes": len(registry.knowledge.graph_store.query(limit=1000)),
        "documents": registry.knowledge.document_store.count(),
        "written": [
            events.count(event_type=kind)
            for kind in (
                EventType.ENTITY_CREATED,
                EventType.ENTITY_UPDATED,
                EventType.OBSERVATION_RECORDED,
            )
        ],
    }


def _rejection_codes(registry: StoreRegistry) -> list[str]:
    return [
        e.payload.get("error_code")
        for e in registry.operational.event_log.get_events(
            event_type=EventType.MUTATION_REJECTED
        )
    ]


class TestExecuteMutation:
    @pytest.mark.parametrize("door", ENTITY_DOORS)
    def test_a_dangling_reference_is_rejected_and_nothing_is_written(
        self, temp_registry: StoreRegistry, door: str
    ) -> None:
        if door == "entity.update":
            _seed_node(temp_registry)
        before = _footprint(temp_registry)
        with as_http(MUTATE):
            result = _run(door, {"evidence_ref": "doc-nowhere"})
        assert result["status"] == "rejected"
        assert "does not name a document this credential can read" in result["message"]
        assert _footprint(temp_registry) == before
        assert _rejection_codes(temp_registry) == [NODE_EVIDENCE_REF_UNREADABLE]

    @pytest.mark.parametrize("door", ENTITY_DOORS)
    def test_a_missing_document_is_refused_for_a_trusted_local_caller_too(
        self, temp_registry: StoreRegistry, door: str
    ) -> None:
        if door == "entity.update":
            _seed_node(temp_registry)
        with as_stdio():
            result = _run(door, {"evidence_ref": "doc-nowhere"})
        assert result["status"] == "rejected"

    @pytest.mark.parametrize("door", ENTITY_DOORS)
    def test_a_document_above_the_clearance_is_refused_like_a_missing_one(
        self, temp_registry: StoreRegistry, door: str
    ) -> None:
        if door == "entity.update":
            _seed_node(temp_registry)
        before = _footprint(temp_registry)
        with as_http(MUTATE):
            hidden = _run(door, {"evidence_ref": RESTRICTED_DOC})
            missing = _run(door, {"evidence_ref": "doc-nowhere"})
        assert hidden["status"] == missing["status"] == "rejected"
        assert hidden["message"] == missing["message"].replace(
            "doc-nowhere", RESTRICTED_DOC
        )
        assert _footprint(temp_registry) == before

    @pytest.mark.parametrize("door", ENTITY_DOORS)
    def test_a_key_that_may_read_the_document_is_accepted(
        self, temp_registry: StoreRegistry, door: str
    ) -> None:
        if door == "entity.update":
            _seed_node(temp_registry)
        with as_http(MUTATE_RESTRICTED):
            result = _run(door, {"evidence_ref": RESTRICTED_DOC})
        assert result["status"] == "success", result
        node = temp_registry.knowledge.graph_store.get_node(NODE_ID)
        assert node["properties"]["evidence_ref"] == RESTRICTED_DOC

    @pytest.mark.parametrize("door", ENTITY_DOORS)
    @pytest.mark.parametrize("other", [OTHER, "doc-nowhere", None, ""])
    def test_a_node_keeps_the_reference_it_holds(
        self, temp_registry: StoreRegistry, door: str, other: Any
    ) -> None:
        _seed_node(temp_registry, HELD)
        before = _footprint(temp_registry)
        with as_http(MUTATE):
            result = _run(door, {"evidence_ref": other})
        assert result["status"] == "rejected"
        assert "may not change a node's evidence_ref" in result["message"]
        assert HELD not in result["message"]
        assert _footprint(temp_registry) == before
        assert _rejection_codes(temp_registry) == [NODE_EVIDENCE_REF_CHANGED]

    @pytest.mark.parametrize("door", ENTITY_DOORS)
    def test_the_unchanged_reference_is_accepted_even_if_its_document_is_gone(
        self, temp_registry: StoreRegistry, door: str
    ) -> None:
        _seed_node(temp_registry, "doc-deleted-since")
        with as_http(MUTATE):
            result = _run(door, {"evidence_ref": "doc-deleted-since"})
        assert result["status"] == "success", result

    @pytest.mark.parametrize("door", ENTITY_DOORS)
    @pytest.mark.parametrize("bad", [5, ["doc-held"], "", True])
    def test_a_value_that_is_not_an_id_is_refused(
        self, temp_registry: StoreRegistry, door: str, bad: Any
    ) -> None:
        if door == "entity.update":
            _seed_node(temp_registry)
        before = _footprint(temp_registry)
        with as_http(MUTATE):
            result = _run(door, {"evidence_ref": bad})
        assert result["status"] == "rejected"
        assert "needs evidence_ref to be the id of a document" in result["message"]
        assert _footprint(temp_registry) == before
        assert _rejection_codes(temp_registry) == [NODE_EVIDENCE_REF_NOT_A_STRING]

    @pytest.mark.parametrize("door", ENTITY_DOORS)
    def test_an_honest_write_stores_the_row_it_always_stored(
        self, temp_registry: StoreRegistry, door: str
    ) -> None:
        if door == "entity.update":
            _seed_node(temp_registry)
        with as_http(MUTATE):
            result = _run(door, {"evidence_ref": HELD, "team": "payments"})
        assert result["status"] == "success", result
        node = temp_registry.knowledge.graph_store.get_node(NODE_ID)
        assert node["node_type"] == "service"
        assert node["properties"] == {
            "name": "Target",
            "evidence_ref": HELD,
            "team": "payments",
        }


class TestRecordObservation:
    def test_a_dangling_reference_is_rejected_and_nothing_is_written(
        self, temp_registry: StoreRegistry
    ) -> None:
        before = _footprint(temp_registry)
        with as_http(MUTATE):
            result = _observe("doc-nowhere")
        assert result["status"] == "rejected"
        assert "does not name a document this credential can read" in result["message"]
        assert _footprint(temp_registry) == before
        assert _rejection_codes(temp_registry) == [NODE_EVIDENCE_REF_UNREADABLE]

    def test_a_missing_document_is_refused_for_a_trusted_local_caller_too(
        self, temp_registry: StoreRegistry
    ) -> None:
        with as_stdio():
            assert _observe("doc-nowhere")["status"] == "rejected"

    def test_a_document_above_the_clearance_is_refused_like_a_missing_one(
        self, temp_registry: StoreRegistry
    ) -> None:
        with as_http(MUTATE):
            hidden = _observe(RESTRICTED_DOC)
            missing = _observe("doc-nowhere")
        assert hidden["status"] == missing["status"] == "rejected"
        assert hidden["message"] == missing["message"].replace(
            "doc-nowhere", RESTRICTED_DOC
        )

    def test_a_key_that_may_read_the_document_is_accepted(
        self, temp_registry: StoreRegistry
    ) -> None:
        with as_http(MUTATE_RESTRICTED):
            result = _observe(RESTRICTED_DOC)
        assert result["status"] == "ok", result
        node = temp_registry.knowledge.graph_store.get_node(result["observation_id"])
        assert node["properties"]["evidence_ref"] == RESTRICTED_DOC

    def test_an_honest_observation_stores_the_row_it_always_stored(
        self, temp_registry: StoreRegistry
    ) -> None:
        with as_http(MUTATE):
            result = _observe(HELD)
        assert result["status"] == "ok", result
        node = temp_registry.knowledge.graph_store.get_node(result["observation_id"])
        assert node["properties"]["evidence_ref"] == HELD
        assert node["properties"]["content"] == "p99 latency rose"

    def test_an_observation_naming_no_reference_is_not_judged(
        self, temp_registry: StoreRegistry
    ) -> None:
        with as_http(MUTATE):
            result = _observe(None)
        assert result["status"] == "ok", result
        node = temp_registry.knowledge.graph_store.get_node(result["observation_id"])
        assert "evidence_ref" not in node["properties"]
