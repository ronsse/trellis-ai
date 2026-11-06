"""A promoted precedent is judged by its source's label on every route it is served.

The listings withhold a precedent the caller's clearance cannot read
(``test_precedent_listing_clearance.py``). A precedent carries no label of its
own, so four other routes served its title, description and excerpt to a key
below the label its source later received (or had when the precedent was
promoted before the write-time rule): ``GET /entities/{id}``,
``GET /documents/{id}``, ``GET /search`` and ``POST /packs``. ``GET
/precedents/domains`` counted such a precedent too.

Every route asks one predicate,
:class:`~trellis.retrieve.precedents.PrecedentClearance`. Each route has the
same three cases:

* a key below the source's label: the precedent is withheld (never rewritten);
* a key cleared for it: the precedent is served;
* the source's lookup raises: the precedent is withheld (fails closed) and is
  counted apart from a label that said no.

Everything promotes through the real promotion path onto real SQLite stores.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import pytest
import structlog
from fastapi.testclient import TestClient

import trellis_api.app as app_module
from tests.unit.retrieve.test_precedent_listing_clearance import (
    BODY,
    LEAKS,
    promote,
    relabel,
)
from trellis.auth import generate_api_key
from trellis.retrieve.pack_builder import PackBuilder
from trellis.retrieve.precedents import (
    PRECEDENT_WITHHELD_LABELLED,
    PRECEDENT_WITHHELD_UNREADABLE,
    PrecedentClearance,
)
from trellis.retrieve.sensitivity import (
    SENSITIVITY_REJECTION_REASON,
    SENSITIVITY_UNREADABLE_REJECTION_REASON,
)
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry
from trellis_api.app import create_app

if TYPE_CHECKING:
    from collections.abc import Callable

SOURCE = "doc-source"
#: A key below the source's label, and one cleared for it.
BELOW = ["read"]
CLEARED = ["read", "confidential"]
#: The log event a lookup that could not answer writes.
LOOKUP_FAILED = "precedent_source_lookup_failed"


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.delenv("TRELLIS_API_KEY", raising=False)
    monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")
    reg = StoreRegistry(stores_dir=tmp_path / "stores")
    app_module._registry = reg
    reg.knowledge.graph_store.upsert_node("entity://shared", "entity", {"name": "x"})
    yield reg
    reg.close()
    app_module._registry = None


@pytest.fixture
def client(registry):
    return TestClient(create_app())


@pytest.fixture
def precedent(registry: StoreRegistry) -> str:
    """A precedent promoted while its source was unlabelled, then labelled.

    The promotion copied source text (``LEAKS``) into the precedent, whose own
    row carries no label; the source was labelled ``confidential`` afterwards,
    so only the source's label can keep it from a ``read`` key.
    """
    docs = registry.knowledge.document_store
    docs.put(SOURCE, BODY, {"title": "Source"})
    entity_id = promote(registry, "cand:one", SOURCE)
    row = docs.get(entity_id)
    assert row is not None
    assert all(leak in row["content"] for leak in LEAKS), row["content"]
    relabel(registry, SOURCE, "confidential")
    return entity_id


def _headers(registry: StoreRegistry, scopes: list[str]) -> dict[str, str]:
    token, record = generate_api_key("k", scopes)
    registry.operational.api_key_store.create(record)
    return {"Authorization": f"Bearer {token}"}


def _fail_source_reads(registry: StoreRegistry, monkeypatch) -> None:
    """Make the document store raise when asked for the source, only."""
    docs = registry.knowledge.document_store
    real_get = docs.get

    def get(doc_id: str, *args: Any, **kwargs: Any) -> Any:
        if doc_id == SOURCE:
            msg = "store unavailable"
            raise RuntimeError(msg)
        return real_get(doc_id, *args, **kwargs)

    monkeypatch.setattr(docs, "get", get)


# -- one probe per route: did the caller get the precedent? ------------------


def _entity(client, headers, entity_id: str) -> bool:
    resp = client.get(f"/api/v1/entities/{quote(entity_id, safe='')}", headers=headers)
    assert resp.status_code in {200, 404}, resp.text
    if resp.status_code == 404:
        return False
    assert resp.json()["entity"]["node_id"] == entity_id
    return True


def _document(client, headers, entity_id: str) -> bool:
    resp = client.get(f"/api/v1/documents/{quote(entity_id, safe='')}", headers=headers)
    assert resp.status_code in {200, 404}, resp.text
    return resp.status_code == 200


def _search(client, headers, entity_id: str) -> bool:
    resp = client.get("/api/v1/search", params={"q": "ZEBRAWORD"}, headers=headers)
    assert resp.status_code == 200, resp.text
    return entity_id in [r["doc_id"] for r in resp.json()["results"]]


def _pack_body(client, headers) -> dict[str, Any]:
    resp = client.post(
        "/api/v1/packs", json={"intent": "Heron ZEBRAWORD"}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    return body


def _pack(client, headers, entity_id: str) -> bool:
    return entity_id in [i["item_id"] for i in _pack_body(client, headers)["items"]]


def _domains(client, headers, entity_id: str) -> bool:
    del entity_id
    resp = client.get("/api/v1/precedents/domains", headers=headers)
    assert resp.status_code == 200, resp.text
    return bool(resp.json()["total"])


PROBES: dict[str, Callable[..., bool]] = {
    "entity": _entity,
    "document": _document,
    "search": _search,
    "pack": _pack,
    "domains": _domains,
}


@pytest.fixture
def domain_event(registry: StoreRegistry, precedent: str) -> None:
    """Give the promotion a ``domain`` so ``/precedents/domains`` counts it."""
    event = registry.operational.event_log.get_events(
        event_type=EventType.PRECEDENT_PROMOTED, limit=10
    )[0]
    registry.operational.event_log.emit(
        EventType.PRECEDENT_PROMOTED,
        "mutation_executor",
        entity_id=precedent,
        entity_type="precedent",
        payload={**event.payload, "domain": "ops"},
    )


@pytest.mark.parametrize("route", sorted(PROBES))
class TestEveryRoute:
    def test_a_key_cleared_for_the_source_is_served(
        self, client, registry, precedent, domain_event, route
    ):
        assert PROBES[route](client, _headers(registry, CLEARED), precedent)

    def test_a_key_below_the_sources_label_is_not_served(
        self, client, registry, precedent, domain_event, route
    ):
        # The premise: the same precedent is served to a key that may read it.
        assert PROBES[route](client, _headers(registry, CLEARED), precedent)

        assert not PROBES[route](client, _headers(registry, BELOW), precedent)

    def test_a_label_that_said_no_is_not_a_lookup_failure(
        self, client, registry, precedent, domain_event, route
    ):
        with structlog.testing.capture_logs() as logs:
            assert not PROBES[route](client, _headers(registry, BELOW), precedent)

        assert LOOKUP_FAILED not in [log["event"] for log in logs]

    def test_a_source_lookup_that_raises_withholds_and_is_counted(
        self, client, registry, precedent, domain_event, route, monkeypatch
    ):
        # The label is cleared for this key: only the outage can withhold it.
        relabel(registry, SOURCE, "internal")
        headers = _headers(registry, BELOW)
        assert PROBES[route](client, headers, precedent)
        _fail_source_reads(registry, monkeypatch)

        with structlog.testing.capture_logs() as logs:
            served = PROBES[route](client, headers, precedent)

        assert not served
        failed = [log for log in logs if log["event"] == LOOKUP_FAILED]
        if route == "pack":
            # A pack counts it in its own withheld line instead of a log.
            assert _withheld_reasons(_pack_body(client, headers)) == {
                SENSITIVITY_UNREADABLE_REJECTION_REASON: 1
            }
        else:
            assert failed, logs


def _withheld_reasons(body: dict[str, Any]) -> dict[str, int]:
    return dict((body.get("withholding") or {}).get("by_reason") or {})


class TestPack:
    def test_the_withheld_line_counts_a_label_that_said_no(
        self, client, registry, precedent
    ):
        body = _pack_body(client, _headers(registry, BELOW))

        assert precedent not in [i["item_id"] for i in body["items"]]
        # The source is above this key too, so it is withheld by the document
        # gate under the same reason; the precedent is the second row.
        assert _withheld_reasons(body) == {SENSITIVITY_REJECTION_REASON: 2}
        assert body["withholding"]["total"] == 2

    def test_a_cleared_key_has_nothing_withheld_for_sensitivity(
        self, client, registry, precedent
    ):
        body = _pack_body(client, _headers(registry, CLEARED))

        assert precedent in [i["item_id"] for i in body["items"]]
        assert SENSITIVITY_REJECTION_REASON not in _withheld_reasons(body)

    def test_withheld_precedent_is_recorded_as_a_rejected_item(
        self, client, registry, precedent
    ):
        body = _pack_body(client, _headers(registry, ["read", "restricted"]))
        assert precedent in [i["item_id"] for i in body["items"]]

        built = _build_pack(registry, "internal")

        rejected = {r.item_id: r.reason for r in built.retrieval_report.rejected_items}
        assert rejected[precedent] == SENSITIVITY_REJECTION_REASON

    def test_an_unlabelled_store_yields_identical_packs_with_and_without_the_gate(
        self, client, registry, monkeypatch
    ):
        registry.knowledge.document_store.put(SOURCE, BODY, {"title": "Source"})
        entity_id = promote(registry, "cand:one", SOURCE)
        headers = _headers(registry, BELOW)

        def served() -> list[tuple[Any, ...]]:
            items = _pack_body(client, headers)["items"]
            return [
                (i["item_id"], i["excerpt"], i["relevance_score"], i["item_type"])
                for i in items
            ]

        with_gate = served()
        assert entity_id in [row[0] for row in with_gate]
        monkeypatch.setattr(
            PackBuilder,
            "_apply_precedent_source_gate",
            lambda _self, items, _clearance: (items, []),
        )

        assert served() == with_gate


def _build_pack(registry: StoreRegistry, clearance: Any) -> Any:
    from trellis.retrieve.builder_factory import build_pack_builder

    builder = build_pack_builder(registry, surface="rest", project=None)
    return builder.build(intent="Heron ZEBRAWORD", clearance=clearance)


class TestNeighbours:
    def test_a_withheld_neighbour_and_its_edges_are_left_out(
        self, client, registry, precedent
    ):
        below = _subgraph(client, _headers(registry, BELOW))
        cleared = _subgraph(client, _headers(registry, CLEARED))

        assert precedent in {n["node_id"] for n in cleared["nodes"]}
        assert any(
            precedent in (e["source_id"], e["target_id"]) for e in cleared["edges"]
        )
        assert precedent not in {n["node_id"] for n in below["nodes"]}
        assert not any(
            precedent in (e["source_id"], e["target_id"]) for e in below["edges"]
        )
        # Nothing of the precedent's text reaches the response either.
        assert not any(leak in json.dumps(below) for leak in LEAKS)


def _subgraph(client, headers) -> dict[str, Any]:
    resp = client.get(
        f"/api/v1/entities/{quote('entity://shared', safe='')}", headers=headers
    )
    assert resp.status_code == 200, resp.text
    sub: dict[str, Any] = resp.json()["subgraph"]
    return sub


class TestPromotedUnderTheSourceLabelRule:
    def test_a_precedent_that_holds_nothing_of_its_source_is_served(
        self, client, registry
    ):
        """The exemption: promoted after the source carried its label."""
        docs = registry.knowledge.document_store
        docs.put(SOURCE, BODY, {"title": "Source"})
        relabel(registry, SOURCE, "confidential")
        entity_id = promote(registry, "cand:two", SOURCE)
        node = registry.knowledge.graph_store.get_node(entity_id)
        assert node is not None
        assert node["properties"]["source_excerpt_status"] == "source_labelled"

        assert _entity(client, _headers(registry, BELOW), entity_id)
        assert _document(client, _headers(registry, BELOW), entity_id)


class TestPredicate:
    """The shared predicate, asked directly."""

    def test_the_two_withheld_reasons_are_distinct(self, registry, precedent):
        gate = PrecedentClearance(
            registry.knowledge.document_store,
            "internal",
            registry.knowledge.graph_store,
        )
        assert gate.verdict_of(precedent) == PRECEDENT_WITHHELD_LABELLED
        assert gate.counts == {
            PRECEDENT_WITHHELD_LABELLED: 1,
            PRECEDENT_WITHHELD_UNREADABLE: 0,
        }

    def test_a_source_read_that_raises_is_counted_apart_from_a_label(
        self, registry, precedent, monkeypatch
    ):
        relabel(registry, SOURCE, "internal")
        _fail_source_reads(registry, monkeypatch)
        gate = PrecedentClearance(
            registry.knowledge.document_store,
            "internal",
            registry.knowledge.graph_store,
        )

        assert gate.verdict_of(precedent) == PRECEDENT_WITHHELD_UNREADABLE
        assert gate.counts == {
            PRECEDENT_WITHHELD_LABELLED: 0,
            PRECEDENT_WITHHELD_UNREADABLE: 1,
        }

    def test_a_graph_lookup_that_raises_is_unreadable(
        self, registry, precedent, monkeypatch
    ):
        graph = registry.knowledge.graph_store

        def boom(_node_id: str) -> Any:
            msg = "graph unavailable"
            raise RuntimeError(msg)

        monkeypatch.setattr(graph, "get_node", boom)
        gate = PrecedentClearance(registry.knowledge.document_store, "internal", graph)

        assert gate.verdict_of(precedent) == PRECEDENT_WITHHELD_UNREADABLE

    def test_restricted_and_non_precedents_are_never_judged(self, registry, precedent):
        docs = registry.knowledge.document_store
        assert PrecedentClearance(docs, "restricted").verdict_of(precedent) is None
        gate = PrecedentClearance(docs, "internal", registry.knowledge.graph_store)
        assert gate.verdict_of(SOURCE) is None
        assert gate.verdict_of("entity://shared") is None
