"""A graph node's ``evidence_ref`` through the REST write doors (#1057).

A node that carries ``evidence_ref`` is the item another item's ``superseded_by``
resolves to. ``entity.create``, ``entity.update`` and ``observation.record`` are
its governed writers, so they keep the field, but a value a caller supplies must
name a document that exists and that the caller may read, and a node that already
holds one keeps it: a different value is refused, the unchanged value is
accepted. A refusal is a typed rejection (``mutation.rejected`` carries the code)
and writes nothing. Doors: ``POST /commands/batch``, ``POST /entities`` and
``POST /observations``.
Real SQLite stores, the real routes and handlers.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

import trellis_api.app as app_module
from tests.unit.retrieve.test_sensitivity import label
from trellis.auth import generate_api_key
from trellis.mutate.evidence_pointer_keys import (
    NODE_EVIDENCE_REF_CHANGED,
    NODE_EVIDENCE_REF_NOT_A_STRING,
    NODE_EVIDENCE_REF_UNREADABLE,
)
from trellis.schemas.well_known import OBSERVATION
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry
from trellis_api.app import create_app

OPEN = "failover runbook open: drain the write queue, promote the replica. " * 4
SECRET = "failover runbook secret: rotate the vault seal keys. " * 4
NODE_ID = "node-target"
HELD = "doc-held"
OTHER = "doc-other"
RESTRICTED_DOC = "doc-secret"
CAN_WRITE = ["read", "mutate"]
CAN_WRITE_RESTRICTED = ["read", "mutate", "restricted"]
DOORS = ["entity.create", "entity.update", "observation.record"]


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.delenv("TRELLIS_API_KEY", raising=False)
    monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")
    reg = StoreRegistry(stores_dir=tmp_path / "stores")
    app_module._registry = reg
    docs = reg.knowledge.document_store
    docs.put(HELD, OPEN, {"title": "Held"})
    docs.put(OTHER, OPEN, {"title": "Other"})
    docs.put(RESTRICTED_DOC, SECRET, {"title": "Secret", **label("restricted")})
    yield reg
    reg.close()
    app_module._registry = None


@pytest.fixture
def client(registry):
    return TestClient(create_app())


def _headers(registry, scopes=CAN_WRITE):
    token, record = generate_api_key("k", scopes)
    registry.operational.api_key_store.create(record)
    return {"Authorization": f"Bearer {token}"}


def _write(
    client: TestClient,
    headers: dict[str, str],
    door: str,
    ref: Any,
    *,
    omit_ref: bool = False,
) -> tuple[bool, str]:
    """Send one write through ``door``; return ``(accepted, message)``."""
    if door == "observation.record":
        body: dict[str, Any] = {
            "observation_id": NODE_ID,
            "subject_entity_id": "subject-1",
            "subject_entity_type": "service",
            "observer_agent_id": "agent-1",
            "content": "p99 latency rose",
            "confidence": 0.7,
        }
        if not omit_ref:
            body["evidence_ref"] = ref
        resp = client.post("/api/v1/observations", json=body, headers=headers)
        if resp.is_success:
            return True, resp.text
        return False, str(resp.json()["detail"])
    properties = {} if omit_ref else {"evidence_ref": ref}
    if door == "entity.create":
        args = {
            "entity_type": "service",
            "name": "Target",
            "entity_id": NODE_ID,
            "properties": properties,
        }
    else:
        args = {"entity_id": NODE_ID, "properties": properties}
    resp = client.post(
        "/api/v1/commands/batch",
        json={
            "commands": [{"operation": door, "args": args}],
            "strategy": "continue_on_error",
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    result = resp.json()["results"][0]
    return result["status"] == "success", result["message"]


def _seed_node(registry, door: str, ref: Any = None) -> None:
    """The node ``door`` writes to, as it stands before the write."""
    properties: dict[str, Any] = {"name": "Target"}
    if ref is not None:
        properties["evidence_ref"] = ref
    if door == "observation.record":
        properties = {
            "observation_id": NODE_ID,
            "subject_entity_id": "subject-1",
            "subject_entity_type": "service",
            "observer_agent_id": "agent-1",
            "content": "p99 latency rose",
            "confidence": 0.7,
            "observed_at": "2026-01-01T00:00:00+00:00",
            **({"evidence_ref": ref} if ref is not None else {}),
        }
        node_type = OBSERVATION
    else:
        node_type = "service"
    registry.knowledge.graph_store.upsert_node(
        node_id=NODE_ID, node_type=node_type, properties=properties
    )


def _footprint(registry) -> dict[str, Any]:
    """The node (every version), its documents and the write events."""
    graph = registry.knowledge.graph_store
    events = registry.operational.event_log
    return {
        "history": graph.get_node_history(NODE_ID),
        "documents": registry.knowledge.document_store.count(),
        "written": {
            kind: events.count(event_type=kind)
            for kind in (
                EventType.ENTITY_CREATED,
                EventType.ENTITY_UPDATED,
                EventType.OBSERVATION_RECORDED,
            )
        },
    }


def _rejection_codes(registry) -> list[str]:
    events = registry.operational.event_log.get_events(
        event_type=EventType.MUTATION_REJECTED
    )
    return [e.payload.get("error_code") for e in events]


class TestADanglingReferenceIsRefused:
    @pytest.mark.parametrize("door", DOORS)
    @pytest.mark.parametrize("seeded", [False, True], ids=["new-node", "existing-node"])
    def test_a_document_that_does_not_exist(self, client, registry, door, seeded):
        if seeded or door == "entity.update":
            _seed_node(registry, door)
        before = _footprint(registry)
        accepted, message = _write(client, _headers(registry), door, "doc-nowhere")
        assert not accepted
        assert "does not name a document this credential can read" in message
        assert "doc-nowhere" in message
        assert "Nothing was written" in message
        assert _footprint(registry) == before
        assert _rejection_codes(registry) == [NODE_EVIDENCE_REF_UNREADABLE]

    @pytest.mark.parametrize("door", DOORS)
    def test_the_refusal_is_the_same_for_trusted_local_callers(
        self, client, registry, door
    ):
        """A restricted key reads everything, but a missing document is missing."""
        if door == "entity.update":
            _seed_node(registry, door)
        accepted, message = _write(
            client, _headers(registry, CAN_WRITE_RESTRICTED), door, "doc-nowhere"
        )
        assert not accepted
        assert "does not name a document this credential can read" in message

    @pytest.mark.parametrize("door", DOORS)
    def test_a_long_id_is_not_echoed_whole_into_the_audit_event(
        self, client, registry, door
    ):
        accepted, message = _write(client, _headers(registry), door, "x" * 5000)
        assert not accepted
        assert len(message) < 1500


class TestADocumentAboveTheClearanceIsRefused:
    @pytest.mark.parametrize("door", DOORS)
    def test_refused_in_the_words_used_for_a_missing_document(
        self, client, registry, door
    ):
        if door == "entity.update":
            _seed_node(registry, door)
        before = _footprint(registry)
        accepted, message = _write(client, _headers(registry), door, RESTRICTED_DOC)
        missing_accepted, missing = _write(
            client, _headers(registry), door, "doc-nowhere"
        )
        assert not accepted
        assert not missing_accepted
        assert message == missing.replace("doc-nowhere", RESTRICTED_DOC)
        assert _footprint(registry) == before
        assert _rejection_codes(registry) == [NODE_EVIDENCE_REF_UNREADABLE] * 2

    @pytest.mark.parametrize("door", DOORS)
    def test_a_key_that_may_read_it_is_accepted(self, client, registry, door):
        if door == "entity.update":
            _seed_node(registry, door)
        accepted, message = _write(
            client, _headers(registry, CAN_WRITE_RESTRICTED), door, RESTRICTED_DOC
        )
        assert accepted, message
        assert (
            registry.knowledge.graph_store.get_node(NODE_ID)["properties"][
                "evidence_ref"
            ]
            == RESTRICTED_DOC
        )


class TestANodeKeepsTheReferenceItHolds:
    @pytest.mark.parametrize("door", DOORS)
    @pytest.mark.parametrize("other", [OTHER, "doc-nowhere", None, "", 7])
    def test_a_different_value_is_refused(self, client, registry, door, other):
        if door == "observation.record" and other in (None, 7):
            # A null is "omitted" to an observation, and an int fails its schema.
            pytest.skip("an observation cannot carry this value")
        _seed_node(registry, door, HELD)
        before = _footprint(registry)
        accepted, message = _write(client, _headers(registry), door, other)
        assert not accepted
        assert "may not change a node's evidence_ref once it is set" in message
        assert HELD not in message
        assert _footprint(registry) == before
        assert registry.knowledge.graph_store.get_node(NODE_ID)["properties"][
            "evidence_ref"
        ] == (HELD)
        assert _rejection_codes(registry) == [NODE_EVIDENCE_REF_CHANGED]

    @pytest.mark.parametrize("door", DOORS)
    def test_the_unchanged_value_is_accepted(self, client, registry, door):
        _seed_node(registry, door, HELD)
        accepted, message = _write(client, _headers(registry), door, HELD)
        assert accepted, message
        node = registry.knowledge.graph_store.get_node(NODE_ID)
        assert node["properties"]["evidence_ref"] == HELD

    @pytest.mark.parametrize("door", DOORS)
    def test_the_unchanged_value_is_accepted_after_its_document_is_gone(
        self, client, registry, door
    ):
        """A node read back and sent again must keep working."""
        _seed_node(registry, door, "doc-deleted-since")
        accepted, message = _write(
            client, _headers(registry), door, "doc-deleted-since"
        )
        assert accepted, message

    def test_an_update_that_leaves_the_key_out_keeps_the_stored_reference(
        self, client, registry
    ):
        _seed_node(registry, "entity.update", HELD)
        accepted, message = _write(
            client, _headers(registry), "entity.update", None, omit_ref=True
        )
        assert accepted, message
        node = registry.knowledge.graph_store.get_node(NODE_ID)
        assert node["properties"]["evidence_ref"] == HELD


class TestAValueThatIsNotAnIdIsRefused:
    @pytest.mark.parametrize("door", ["entity.create", "entity.update"])
    @pytest.mark.parametrize("bad", [5, ["doc-held"], {"id": "doc-held"}, "", True])
    def test_on_a_node_that_holds_none(self, client, registry, door, bad):
        if door == "entity.update":
            _seed_node(registry, door)
        before = _footprint(registry)
        accepted, message = _write(client, _headers(registry), door, bad)
        assert not accepted
        assert "needs evidence_ref to be the id of a document" in message
        assert _footprint(registry) == before
        assert _rejection_codes(registry) == [NODE_EVIDENCE_REF_NOT_A_STRING]

    def test_an_empty_observation_reference(self, client, registry):
        accepted, message = _write(client, _headers(registry), "observation.record", "")
        assert not accepted
        assert "needs evidence_ref to be the id of a document" in message
        assert registry.knowledge.graph_store.get_node(NODE_ID) is None


class TestAnHonestWriteStoresTheSameRow:
    """Not a serving change: the row an honest write stores is the one it stored."""

    @pytest.mark.parametrize("door", DOORS)
    def test_a_readable_document_is_stored_as_sent(self, client, registry, door):
        if door == "entity.update":
            _seed_node(registry, door)
        accepted, message = _write(client, _headers(registry), door, HELD)
        assert accepted, message
        node = registry.knowledge.graph_store.get_node(NODE_ID)
        assert node["properties"]["evidence_ref"] == HELD
        if door == "observation.record":
            assert node["node_type"] == OBSERVATION
            assert node["properties"]["content"] == "p99 latency rose"
        else:
            assert node["properties"]["name"] == "Target"
            assert node["node_type"] == "service"

    def test_the_stored_row_equals_a_direct_store_write(self, client, registry):
        accepted, message = _write(client, _headers(registry), "entity.create", HELD)
        assert accepted, message
        via_route = registry.knowledge.graph_store.get_node(NODE_ID)
        registry.knowledge.graph_store.upsert_node(
            node_id="node-direct",
            node_type="service",
            properties={"name": "Target", "evidence_ref": HELD},
        )
        direct = registry.knowledge.graph_store.get_node("node-direct")
        for key in ("node_type", "node_role", "properties", "document_ids"):
            assert via_route[key] == direct[key], key

    @pytest.mark.parametrize("door", ["entity.create", "entity.update"])
    def test_a_null_on_a_node_that_holds_none_is_accepted(self, client, registry, door):
        if door == "entity.update":
            _seed_node(registry, door)
        accepted, message = _write(client, _headers(registry), door, None)
        assert accepted, message

    @pytest.mark.parametrize("door", DOORS)
    def test_a_write_naming_no_reference_is_not_judged(self, client, registry, door):
        if door == "entity.update":
            _seed_node(registry, door)
        accepted, message = _write(
            client, _headers(registry), door, None, omit_ref=True
        )
        assert accepted, message
        assert (
            "evidence_ref"
            not in (registry.knowledge.graph_store.get_node(NODE_ID)["properties"])
        )


class TestPostEntities:
    """``POST /entities`` reaches ``entity.create`` at the key's clearance."""

    def _post(self, client, headers, ref):
        return client.post(
            "/api/v1/entities",
            json={
                "entity_type": "service",
                "name": "Target",
                "entity_id": NODE_ID,
                "properties": {"evidence_ref": ref},
            },
            headers=headers,
        )

    def test_a_dangling_reference_is_a_400_and_nothing_is_written(
        self, client, registry
    ):
        resp = self._post(client, _headers(registry), "doc-nowhere")
        assert resp.status_code == 400, resp.text
        assert "does not name a document this credential can read" in resp.text
        assert registry.knowledge.graph_store.get_node(NODE_ID) is None
        assert _rejection_codes(registry) == [NODE_EVIDENCE_REF_UNREADABLE]

    def test_a_document_above_the_clearance_reads_as_a_missing_one(
        self, client, registry
    ):
        hidden = self._post(client, _headers(registry), RESTRICTED_DOC)
        missing = self._post(client, _headers(registry), "doc-nowhere")
        assert hidden.status_code == missing.status_code == 400
        assert hidden.json()["detail"] == missing.json()["detail"].replace(
            "doc-nowhere", RESTRICTED_DOC
        )
        assert registry.knowledge.graph_store.get_node(NODE_ID) is None

    def test_a_readable_document_is_accepted(self, client, registry):
        resp = self._post(
            client, _headers(registry, CAN_WRITE_RESTRICTED), RESTRICTED_DOC
        )
        assert resp.status_code == 200, resp.text
        node = registry.knowledge.graph_store.get_node(NODE_ID)
        assert node["properties"]["evidence_ref"] == RESTRICTED_DOC

    def test_a_node_that_holds_one_keeps_it(self, client, registry):
        assert self._post(client, _headers(registry), HELD).status_code == 200
        resp = self._post(client, _headers(registry), OTHER)
        assert resp.status_code == 400, resp.text
        assert "may not change a node's evidence_ref" in resp.text
        node = registry.knowledge.graph_store.get_node(NODE_ID)
        assert node["properties"]["evidence_ref"] == HELD


class TestAnUpsertThatLeavesTheKeyOutKeepsThePointer:
    """``entity.create`` over an id and a re-recorded observation replace the bag.

    A command that leaves ``evidence_ref`` out must not clear the pointer, or
    the rule that a node keeps it is one omitted key away from nothing.
    """

    def _held(self, registry):
        return registry.knowledge.graph_store.get_node(NODE_ID)["properties"].get(
            "evidence_ref"
        )

    def test_the_gate_probe_sequence_through_commands_batch(self, client, registry):
        headers = _headers(registry)
        accepted, message = _write(client, headers, "entity.create", HELD)
        assert accepted, message
        # The direct changes are refused ...
        for ref in (OTHER, None):
            accepted, _ = _write(client, headers, "entity.update", ref)
            assert not accepted
        # ... and so is the way around them: an upsert that omits the key.
        accepted, message = _write(
            client, headers, "entity.create", None, omit_ref=True
        )
        assert accepted, message
        assert self._held(registry) == HELD
        accepted, message = _write(client, headers, "entity.update", OTHER)
        assert not accepted
        assert "may not change a node's evidence_ref" in message
        assert self._held(registry) == HELD

    def test_the_rest_of_the_bag_is_still_replaced(self, client, registry):
        _seed_node(registry, "entity.create", HELD)
        registry.knowledge.graph_store.upsert_node(
            node_id=NODE_ID,
            node_type="service",
            properties={"name": "Target", "evidence_ref": HELD, "team": "old"},
        )
        resp = client.post(
            "/api/v1/entities",
            json={
                "entity_type": "service",
                "name": "Target",
                "entity_id": NODE_ID,
                "properties": {"team": "new"},
            },
            headers=_headers(registry),
        )
        assert resp.status_code == 200, resp.text
        props = registry.knowledge.graph_store.get_node(NODE_ID)["properties"]
        assert props == {"name": "Target", "team": "new", "evidence_ref": HELD}

    @pytest.mark.parametrize("held", [None, ""])
    def test_a_pointer_that_is_not_held_is_not_carried(self, client, registry, held):
        registry.knowledge.graph_store.upsert_node(
            node_id=NODE_ID,
            node_type="service",
            properties={"name": "Target", "evidence_ref": held},
        )
        accepted, message = _write(
            client, _headers(registry), "entity.create", None, omit_ref=True
        )
        assert accepted, message
        assert (
            "evidence_ref"
            not in (registry.knowledge.graph_store.get_node(NODE_ID)["properties"])
        )

    def test_a_node_without_one_is_unchanged_by_an_upsert(self, client, registry):
        _seed_node(registry, "entity.create")
        accepted, message = _write(
            client, _headers(registry), "entity.create", None, omit_ref=True
        )
        assert accepted, message
        assert self._held(registry) is None

    @pytest.mark.parametrize("path", ["observations", "commands"])
    def test_a_re_recorded_observation_keeps_it(self, client, registry, path):
        _seed_node(registry, "observation.record", HELD)
        headers = _headers(registry)
        body = {
            "observation_id": NODE_ID,
            "subject_entity_id": "subject-1",
            "subject_entity_type": "service",
            "observer_agent_id": "agent-1",
            "content": "p99 latency fell",
            "confidence": 0.9,
        }
        if path == "observations":
            resp = client.post("/api/v1/observations", json=body, headers=headers)
        else:
            resp = client.post(
                "/api/v1/commands/batch",
                json={
                    "commands": [
                        {
                            "operation": "observation.record",
                            "args": {"observation": body},
                        }
                    ],
                    "strategy": "continue_on_error",
                },
                headers=headers,
            )
        assert resp.status_code in (200, 201), resp.text
        props = registry.knowledge.graph_store.get_node(NODE_ID)["properties"]
        assert props["content"] == "p99 latency fell"
        assert props["evidence_ref"] == HELD
        accepted, _ = _write(client, headers, "observation.record", OTHER)
        assert not accepted
        assert (
            registry.knowledge.graph_store.get_node(NODE_ID)["properties"][
                "evidence_ref"
            ]
            == HELD
        )

    def test_a_measurement_recorded_over_the_id_keeps_it(self, client, registry):
        """``measurement.record`` replaces the bag and has no pointer of its own."""
        headers = _headers(registry)
        accepted, message = _write(client, headers, "entity.create", HELD)
        assert accepted, message
        resp = client.post(
            "/api/v1/commands/batch",
            json={
                "commands": [
                    {
                        "operation": "measurement.record",
                        "args": {
                            "measurement": {
                                "measurement_id": NODE_ID,
                                "subject_entity_id": "subject-1",
                                "subject_entity_type": "service",
                                "metric_name": "p99_ms",
                                "metric_value": 120.0,
                                "observer_agent_id": "agent-1",
                            }
                        },
                    }
                ],
                "strategy": "continue_on_error",
            },
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        props = registry.knowledge.graph_store.get_node(NODE_ID)["properties"]
        assert props["metric_name"] == "p99_ms"
        assert props["evidence_ref"] == HELD
        accepted, _ = _write(client, headers, "entity.update", OTHER)
        assert not accepted
        assert self._held(registry) == HELD
