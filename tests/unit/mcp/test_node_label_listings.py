"""MCP ``query_observations`` and ``get_lessons`` judge a node's own label (#1264).

Follow-up to ``test_node_label_doors.py`` (#1029). A node whose
``properties["data_classification"]`` is above the caller's clearance is left
out; a node without the key is served at every clearance; a malformed record
reads ``restricted``. A stdio caller keeps full clearance.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

import trellis.mcp.server as server_mod
from tests.unit.mcp.conftest import unwrap_tool
from tests.unit.mcp.test_sensitivity_surfaces import as_http, as_stdio
from trellis.auth import SCOPE_CONFIDENTIAL, SCOPE_RESTRICTED
from trellis.retrieve.precedents import (
    PRECEDENT_WITHHELD_LABELLED,
    PRECEDENT_WITHHELD_UNREADABLE,
    PrecedentClearance,
)
from trellis.schemas.classification import DATA_CLASSIFICATION_KEY
from trellis.schemas.well_known import OBSERVATION
from trellis.stores.base.event_log import EventType

if TYPE_CHECKING:
    from trellis.stores.registry import StoreRegistry

query_observations = unwrap_tool(server_mod.query_observations)
get_lessons = unwrap_tool(server_mod.get_lessons)

LABELS: dict[str, Any] = {
    "secret": {"sensitivity": "restricted"},
    "conf": {"sensitivity": "confidential"},
    "bad": "secret-ish",
    "plain": None,
}
SCOPES = {
    "internal": ["read"],
    "confidential": ["read", SCOPE_CONFIDENTIAL],
    "restricted": ["read", SCOPE_RESTRICTED],
}
SERVED = {
    "internal": {"plain"},
    "confidential": {"plain", "conf"},
    "restricted": {"plain", "conf", "secret", "bad"},
}


def _props(key: str, **extra: Any) -> dict[str, Any]:
    props: dict[str, Any] = dict(extra)
    if LABELS[key] is not None:
        props[DATA_CLASSIFICATION_KEY] = LABELS[key]
    return props


@pytest.fixture
def seeded(temp_registry: StoreRegistry) -> StoreRegistry:
    graph = temp_registry.knowledge.graph_store
    for key in LABELS:
        graph.upsert_node(
            f"obs-{key}",
            OBSERVATION,
            _props(
                key,
                observation_id=f"obs-{key}",
                subject_entity_id="entity://subject",
                summary=f"observed {key}",
            ),
        )
        graph.upsert_node(
            f"prec-{key}",
            "precedent",
            _props(key, name=f"Lesson-{key}", description=f"About-{key}"),
        )
        temp_registry.operational.event_log.emit(
            EventType.PRECEDENT_PROMOTED,
            "mutation_executor",
            entity_id=f"prec-{key}",
            entity_type="precedent",
            payload={
                "title": f"Lesson-{key}",
                "description": f"About-{key}",
                "domain": "ops",
                "trace_id": f"trace-{key}",
            },
        )
    return temp_registry


def _observed(raw: str) -> set[str]:
    body = json.loads(raw)
    assert body["status"] == "ok", body
    return {r["node_id"].removeprefix("obs-") for r in body["observations"]}


def _lessons(raw: str) -> set[str]:
    return {key for key in LABELS if f"Lesson-{key}" in raw or f"About-{key}" in raw}


@pytest.mark.parametrize("level", sorted(SCOPES))
def test_query_observations_withholds_a_node_labelled_above_the_caller(
    seeded: StoreRegistry, level: str
) -> None:
    with as_http(SCOPES[level]):
        raw = query_observations(limit=50)
    assert _observed(raw) == SERVED[level]


@pytest.mark.parametrize("level", sorted(SCOPES))
def test_get_lessons_withholds_a_node_labelled_above_the_caller(
    seeded: StoreRegistry, level: str
) -> None:
    with as_http(SCOPES[level]):
        raw = get_lessons(limit=50, max_tokens=20000)
    assert _lessons(raw) == SERVED[level]


def test_a_stdio_caller_keeps_full_clearance(seeded: StoreRegistry) -> None:
    with as_stdio():
        observed = _observed(query_observations(limit=50))
        lessons = _lessons(get_lessons(limit=50, max_tokens=20000))
    assert observed == set(LABELS)
    assert lessons == set(LABELS)


class TestBarredEventsCounts:
    """The listing's withheld-precedent accounting names a labelled node."""

    def test_a_labelled_node_is_counted_as_labelled(
        self, seeded: StoreRegistry
    ) -> None:
        graph = seeded.knowledge.graph_store
        events = seeded.operational.event_log.get_events(
            event_type=EventType.PRECEDENT_PROMOTED, limit=50
        )
        gate = PrecedentClearance(
            seeded.knowledge.document_store, "confidential", graph
        )
        barred = gate.barred_events(events, lambda e: graph.get_node(e.entity_id or ""))
        assert {e.entity_id for e in events if e.event_id in barred} == {
            "prec-secret",
            "prec-bad",
        }
        assert gate.counts == {
            PRECEDENT_WITHHELD_LABELLED: 2,
            PRECEDENT_WITHHELD_UNREADABLE: 0,
        }


class TestUnlabelledNodeAtPublic:
    """A node without the key is not judged by its label, even at ``public``.

    ``sensitivity_of`` reads a bag without the key as ``internal``, which a
    ``public`` caller may not read, so this is the one clearance at which
    judging an unlabelled node would change the answer. No door hands a
    ``public`` caller a graph node (each refuses it first), so the pin is on
    the predicate the doors share.
    """

    @staticmethod
    def _node(props: Any) -> dict[str, Any]:
        return {"node_id": "entity://plain", "node_type": "entity", "properties": props}

    def test_verdict_of_serves_an_unlabelled_node_to_public(
        self, temp_registry: StoreRegistry
    ) -> None:
        gate = PrecedentClearance(
            temp_registry.knowledge.document_store,
            "public",
            temp_registry.knowledge.graph_store,
        )
        assert gate.node_label_reason(self._node({"name": "x"})) is None
        assert gate.verdict_of("entity://plain", self._node({"name": "x"})) is None
        public = self._node({DATA_CLASSIFICATION_KEY: {"sensitivity": "internal"}})
        assert gate.verdict_of("entity://plain", public) == (
            PRECEDENT_WITHHELD_LABELLED
        ), "a labelled node is still judged at public"
