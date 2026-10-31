"""GET /precedents leaves out a ``source_item_id`` the key may not read.

A learning promotion's ``payload.source_item_id`` is the id of the served item the
precedent came from, often a document. The listing serves each promotion event's
``payload`` as stored, so below the document's label it would name a document the
key cannot read. The id is dropped from the served copy of each such row, judged
once the rows are chosen so no title, description, count or order moves, and the
immutable event keeps it.

Every fixture row but ``prec-bare`` was promoted under the source-label rule
(``source_excerpt_status="source_labelled"``, node description equal to the
event's, source id in neither served text): it holds nothing of its source, so
the listing serves it and conceals only the pointer. ``prec-bare`` has the same
node but its title names its source id, so it is withheld below ``restricted``
like any row that holds source text (``test_precedent_listing_clearance.py``).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import trellis.retrieve.precedents as precedents_mod
import trellis_api.app as app_module
from tests.unit.retrieve.test_sensitivity import label
from trellis.auth import generate_api_key
from trellis.retrieve.precedents import list_precedents
from trellis.stores.base.event_log import Event, EventLog, EventType
from trellis.stores.registry import StoreRegistry
from trellis_api.app import create_app

#: What each document holds. ``doc-open`` has no label; ``doc-conf`` is
#: confidential; ``doc-secret`` is restricted.
DOCUMENTS = {
    "doc-open": None,
    "doc-conf": "confidential",
    "doc-secret": "restricted",
}

#: ``(entity_id, payload extras, node properties or None)``, promoted oldest
#: first, so the listing serves them in reverse. ``prec-bare`` is a promotion
#: whose own title is the bare-id name of its source and whose node holds
#: another name: ``_served_title`` reads ``payload.source_item_id`` to judge it,
#: so a concealment that ran first would rename it. That title names the source
#: id, so below ``restricted`` the row is withheld, and at ``restricted`` it is
#: served with the title as promoted.
LABELLED = "source_labelled"
ROWS: list[tuple[str, dict[str, Any], dict[str, Any] | None]] = [
    ("prec-secret", {"source_item_id": "doc-secret"}, {"name": "title of prec-secret"}),
    ("prec-conf", {"source_item_id": "doc-conf"}, {"name": "title of prec-conf"}),
    ("prec-open", {"source_item_id": "doc-open"}, {"name": "title of prec-open"}),
    ("prec-node", {"source_item_id": "node-plain"}, {"name": "title of prec-node"}),
    ("prec-trace", {"trace_id": "trace-1"}, None),
    (
        "prec-bare",
        {
            "source_item_id": "doc-secret",
            "title": "Learning: deploys :: doc-secret",
        },
        {"name": "Edited name", "intent_family": "deploys"},
    ),
]
SOURCES = {
    "prec-secret": "doc-secret",
    "prec-conf": "doc-conf",
    "prec-open": "doc-open",
    "prec-node": "node-plain",
    "prec-bare": "doc-secret",
}
#: The entity ids each scope set may see a ``source_item_id`` on.
READ = {"prec-open", "prec-node"}
CONFIDENTIAL = READ | {"prec-conf"}
RESTRICTED = CONFIDENTIAL | {"prec-secret", "prec-bare"}
#: Rows served below ``restricted``: all but the one whose title names its source.
BELOW_RESTRICTED = [e for e, _, _ in reversed(ROWS) if e != "prec-bare"]


def _seed(registry: StoreRegistry) -> None:
    for doc_id, level in DOCUMENTS.items():
        registry.knowledge.document_store.put(
            doc_id,
            f"body of {doc_id}",
            {"title": doc_id, **(label(level) if level else {})},
        )
    log = registry.operational.event_log
    for entity_id, extras, node in ROWS:
        if node is not None:
            registry.knowledge.graph_store.upsert_node(
                entity_id,
                "precedent",
                {
                    **node,
                    "description": f"description of {entity_id}",
                    "source_excerpt_status": LABELLED,
                },
            )
        log.emit(
            EventType.PRECEDENT_PROMOTED,
            "mutation_executor",
            entity_id=entity_id,
            entity_type="precedent",
            payload={
                "title": f"title of {entity_id}",
                "description": f"description of {entity_id}",
                "domain": "ops",
                **extras,
            },
        )


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.delenv("TRELLIS_API_KEY", raising=False)
    monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")
    reg = StoreRegistry(stores_dir=tmp_path / "stores")
    app_module._registry = reg
    _seed(reg)
    yield reg
    reg.close()
    app_module._registry = None


@pytest.fixture
def client(registry):
    return TestClient(create_app())


def _headers(registry, scopes):
    token, record = generate_api_key("k", scopes)
    registry.operational.api_key_store.create(record)
    return {"Authorization": f"Bearer {token}"}


def _listing(client, registry, scopes) -> list[dict[str, Any]]:
    resp = client.get("/api/v1/precedents", headers=_headers(registry, scopes))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["count"] == len(body["precedents"])
    return body["precedents"]


def _sources(rows: list[dict[str, Any]]) -> dict[str, str]:
    return {
        r["entity_id"]: r["payload"]["source_item_id"]
        for r in rows
        if "source_item_id" in r["payload"]
    }


class TestRestListing:
    @pytest.mark.parametrize(
        ("scopes", "visible"),
        [
            (["read"], READ),
            (["read", "confidential"], CONFIDENTIAL),
            (["read", "restricted"], RESTRICTED),
            (["admin"], RESTRICTED),
        ],
    )
    def test_source_item_id_follows_the_documents_label(
        self, client, registry, scopes, visible
    ):
        rows = _listing(client, registry, scopes)
        assert set(_sources(rows)) == visible
        # A kept id is the stored one, not a rewritten stand-in.
        assert _sources(rows) == {k: SOURCES[k] for k in visible}

    def test_unlabelled_document_and_non_document_ids_are_served_below_label(
        self, client, registry
    ):
        sources = _sources(_listing(client, registry, ["read"]))
        assert sources["prec-open"] == "doc-open"
        # A graph node or trace id has no document row: no verdict, so no cut.
        assert sources["prec-node"] == "node-plain"

    def test_titles_descriptions_count_and_order_do_not_depend_on_the_key(
        self, client, registry
    ):
        low = _listing(client, registry, ["read"])
        high = _listing(client, registry, ["read", "restricted"])
        assert [r["entity_id"] for r in low] == BELOW_RESTRICTED
        assert [r["entity_id"] for r in high] == [e for e, _, _ in reversed(ROWS)]
        served = [r for r in high if r["entity_id"] != "prec-bare"]
        for field in ("event_id", "title", "description", "domain", "occurred_at"):
            assert [r[field] for r in low] == [r[field] for r in served]
        # The row whose title names its source id is withheld below
        # ``restricted`` and served as promoted at it, not renamed.
        (bare,) = (r for r in high if r["entity_id"] == "prec-bare")
        assert bare["title"] == "Learning: deploys :: doc-secret"
        assert all(r["entity_id"] != "prec-bare" for r in low)

    def test_only_source_item_id_is_left_out_of_a_payload(self, client, registry):
        low = _listing(client, registry, ["read"])
        high = _listing(client, registry, ["read", "restricted"])
        served = [r for r in high if r["entity_id"] != "prec-bare"]
        for lo, hi in zip(low, served, strict=True):
            assert {
                k: v for k, v in lo["payload"].items() if k != "source_item_id"
            } == {k: v for k, v in hi["payload"].items() if k != "source_item_id"}
        (secret_lo,) = (r for r in low if r["entity_id"] == "prec-secret")
        assert set(secret_lo["payload"]) == {"title", "description", "domain"}

    def test_a_trace_precedent_is_unchanged(self, client, registry):
        for scopes in (["read"], ["read", "restricted"]):
            (row,) = (
                r
                for r in _listing(client, registry, scopes)
                if r["entity_id"] == "prec-trace"
            )
            assert row["payload"]["trace_id"] == "trace-1"
            assert "source_item_id" not in row["payload"]

    def test_the_event_keeps_the_id_after_a_below_label_listing(self, client, registry):
        _listing(client, registry, ["read"])
        stored = registry.operational.event_log.get_events(
            event_type=EventType.PRECEDENT_PROMOTED, limit=50
        )
        kept = {
            e.entity_id: e.payload["source_item_id"]
            for e in stored
            if "source_item_id" in e.payload
        }
        assert kept == SOURCES


class TestCore:
    def test_defaults_serve_every_source_item_id(self, registry):
        rows = list_precedents(registry.operational.event_log)
        assert _sources(rows) == SOURCES

    def test_restricted_serves_every_source_item_id_without_a_store_read(
        self, registry
    ):
        store = MagicMock(spec=registry.knowledge.document_store)
        rows = list_precedents(
            registry.operational.event_log,
            graph_store=registry.knowledge.graph_store,
            document_store=store,
            clearance="restricted",
        )
        assert _sources(rows) == SOURCES
        store.get.assert_not_called()

    def test_below_restricted_with_no_store_leaves_every_id_out(self, registry):
        rows = list_precedents(
            registry.operational.event_log,
            graph_store=registry.knowledge.graph_store,
            clearance="internal",
        )
        # Every id reads as unreadable with no store: the row whose title names
        # its source id is withheld, the rest are served without a pointer.
        assert [r["entity_id"] for r in rows] == BELOW_RESTRICTED
        assert _sources(rows) == {}

    def test_a_store_that_cannot_answer_withholds_the_row(self, registry):
        # Reads as unreadable: a row it cannot judge is left out, not served bare.
        store = MagicMock(spec=registry.knowledge.document_store)
        store.get.side_effect = RuntimeError("down")
        rows = list_precedents(
            registry.operational.event_log,
            graph_store=registry.knowledge.graph_store,
            document_store=store,
            clearance="confidential",
        )
        assert rows == []

    def test_ids_are_judged_in_one_call_however_many_rows_name_them(
        self, registry, monkeypatch
    ):
        calls: list[list[str]] = []
        real = precedents_mod.readable_ids

        def spy(store, ids, clearance):
            calls.append(list(ids))
            return real(store, ids, clearance)

        monkeypatch.setattr(precedents_mod, "readable_ids", spy)
        list_precedents(
            registry.operational.event_log,
            graph_store=registry.knowledge.graph_store,
            document_store=registry.knowledge.document_store,
            clearance="internal",
        )
        # Every precedent's own id and every source id, each once.
        (ids,) = calls
        assert sorted(ids) == sorted({e for e, _, _ in ROWS} | set(SOURCES.values()))

    def test_no_source_to_judge_reads_only_the_precedents_own_row(self, registry):
        log = MagicMock(spec=EventLog)
        log.get_events.return_value = [
            Event(
                event_type=EventType.PRECEDENT_PROMOTED,
                source="mutation_executor",
                entity_id="prec-trace",
                payload={"title": "t", "trace_id": "trace-1"},
            )
        ]
        store = MagicMock(spec=registry.knowledge.document_store)
        (row,) = list_precedents(log, document_store=store, clearance="internal")
        assert row["payload"]["trace_id"] == "trace-1"
        store.get.assert_called_once_with("prec-trace")

    def test_the_events_own_payload_is_never_written_to(self, registry):
        # An SQLite log hands back fresh dicts, so only a log that returns the
        # same ``Event`` objects can show an in-place write.
        payload = {
            "title": "title of prec-secret",
            "description": "description of prec-secret",
            "source_item_id": "doc-secret",
        }
        events = [
            Event(
                event_type=EventType.PRECEDENT_PROMOTED,
                source="mutation_executor",
                entity_id="prec-secret",
                payload=dict(payload),
            )
        ]
        log = MagicMock(spec=EventLog)
        log.get_events.return_value = events
        (row,) = list_precedents(
            log,
            graph_store=registry.knowledge.graph_store,
            document_store=registry.knowledge.document_store,
            clearance="internal",
        )
        assert "source_item_id" not in row["payload"]
        assert row["payload"] is not events[0].payload
        assert events[0].payload == payload
