"""A precedent listing withholds a row the caller's clearance cannot read.

A precedent carries no sensitivity label of its own, so the listing used to serve
every promoted row's title and description to every caller, and only dropped the
``source_item_id`` pointer. Two things can make a row unreadable below
``restricted``:

* its own document row (stored at the precedent's entity id, which the keyword
  and semantic axes already withhold) is labelled above the caller;
* its source is labelled above the caller and the promotion did not already
  refuse the source's text (``source_excerpt_status`` other than
  ``source_labelled``), so the description and title can still hold a copy of it.

The listing leaves such a row out rather than rewriting its text. Everything
below promotes through the real promotion path onto real SQLite stores.
"""

from __future__ import annotations

import copy
import json
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import trellis.mcp.server as server_mod
import trellis.retrieve.precedents as precedents_mod
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
from trellis.ingest_corpus.models import chunk_doc_id
from trellis.learning.scoring import (
    NAME_SOURCE_ITEM_ID,
    SOURCE_EXCERPT_INCLUDED,
    SOURCE_EXCERPT_LABELLED,
    build_learning_promotion_payloads,
    submit_learning_promotion,
)
from trellis.mutate import build_curate_executor
from trellis.retrieve.precedents import list_precedents
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry
from trellis_api.app import create_app

if TYPE_CHECKING:
    from pathlib import Path

get_lessons = unwrap_tool(server_mod.get_lessons)

FAMILY = "asset_generation"
TITLE = "Quartz Title Words"
BODY = "Heron opening line ZEBRAWORD and the rest of the body."
#: Text each promotion copies from its source while the source is readable.
LEAKS = ("ZEBRAWORD", TITLE)


def _candidate(candidate_id: str, item_id: str, *, title: str | None) -> dict[str, Any]:
    """A candidate as the analyzer writes it, before any readable-name fallback."""
    display = title or item_id
    return {
        "candidate_id": candidate_id,
        "intent_family": FAMILY,
        "recommendation_type": "promote_guidance",
        "item_id": item_id,
        "title": title,
        "precedent_name": f"Learning: {FAMILY} :: {display}",
        "precedent_name_source": "item_title" if title else NAME_SOURCE_ITEM_ID,
        "metrics": {"success_rate": 0.9, "retry_rate": 0.1},
        "citation_evidence": {"helpful_count": 2, "unhelpful_count": 0},
        "precedent_properties": {"intent_family": FAMILY, "source_item_id": item_id},
        "target_entity_ids": ["entity://shared"],
        "supporting_run_ids": ["run-1"],
        "phases": ["GENERATE"],
    }


def promote(
    registry: StoreRegistry, candidate_id: str, source_id: str, *, title: str = TITLE
) -> str:
    """Promote one candidate through the governed path; the precedent's entity id."""
    docs = registry.knowledge.document_store
    payloads = build_learning_promotion_payloads(
        candidate=copy.deepcopy(_candidate(candidate_id, source_id, title=title)),
        promotion_name="",
        rationale="because",
        document_store=docs,
    )
    result = submit_learning_promotion(
        build_curate_executor(registry, clearance="restricted"),
        payloads["entity_payload"],
        payloads["edge_payloads"],
        requested_by="test",
        graph_store=registry.knowledge.graph_store,
        event_log=registry.operational.event_log,
        document_store=docs,
    )
    assert result["status"] == "promoted", result
    entity_id: str = payloads["entity_id"]
    return entity_id


def relabel(registry: StoreRegistry, doc_id: str, level: str) -> None:
    """Label an existing document row, keeping the rest of its metadata."""
    docs = registry.knowledge.document_store
    row = docs.get(doc_id)
    assert row is not None
    docs.put(doc_id, row["content"], {**row["metadata"], **label(level)})


def listing(
    registry: StoreRegistry, clearance: Any = "internal", **kwargs: Any
) -> list[dict[str, Any]]:
    kwargs.setdefault("document_store", registry.knowledge.document_store)
    return list_precedents(
        registry.operational.event_log,
        graph_store=registry.knowledge.graph_store,
        clearance=clearance,
        **kwargs,
    )


def text_of(rows: list[dict[str, Any]]) -> str:
    return json.dumps(rows, sort_keys=True)


def ids_of(rows: list[dict[str, Any]]) -> list[str]:
    return [r["entity_id"] for r in rows]


@pytest.fixture
def registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("TRELLIS_API_KEY", raising=False)
    monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")
    stores_dir = tmp_path / "stores"
    stores_dir.mkdir()
    reg = StoreRegistry(stores_dir=stores_dir)
    monkeypatch.setattr(server_mod, "_registry", reg)
    monkeypatch.setattr(app_module, "_registry", reg)
    reg.knowledge.graph_store.upsert_node("entity://shared", "entity", {"name": "x"})
    yield reg
    reg.close()


@pytest.fixture
def source(registry: StoreRegistry) -> str:
    registry.knowledge.document_store.put("doc-source", BODY, {"title": "Source"})
    return "doc-source"


class TestOwnDocumentRow:
    """Check 1: the precedent's own document row, stored at its entity id."""

    def test_a_row_the_caller_cannot_read_is_withheld(
        self, registry: StoreRegistry, source: str
    ) -> None:
        entity_id = promote(registry, "cand:one", source)
        # The row exists, is unlabelled, and is served: the premise.
        assert registry.knowledge.document_store.get(entity_id) is not None
        assert ids_of(listing(registry, "internal")) == [entity_id]

        relabel(registry, entity_id, "confidential")

        assert listing(registry, "internal") == []
        assert ids_of(listing(registry, "confidential")) == [entity_id]
        assert ids_of(listing(registry, "restricted")) == [entity_id]

    def test_a_restricted_row_needs_a_restricted_caller(
        self, registry: StoreRegistry, source: str
    ) -> None:
        entity_id = promote(registry, "cand:one", source)
        relabel(registry, entity_id, "restricted")

        assert listing(registry, "internal") == []
        assert listing(registry, "confidential") == []
        assert ids_of(listing(registry, "restricted")) == [entity_id]


class TestSource:
    """Check 2: the source the description and title may still copy from."""

    def test_a_source_labelled_after_promotion_withholds_the_row(
        self, registry: StoreRegistry, source: str
    ) -> None:
        entity_id = promote(registry, "cand:one", source)
        node = registry.knowledge.graph_store.get_node(entity_id)
        assert node is not None
        assert node["properties"]["source_excerpt_status"] == SOURCE_EXCERPT_INCLUDED
        # The defect this closes: at ``internal`` the row serves the source's text.
        (served,) = listing(registry, "internal")
        assert all(leak in text_of([served]) for leak in LEAKS)

        relabel(registry, source, "confidential")

        assert listing(registry, "internal") == []
        for clearance in ("confidential", "restricted"):
            (row,) = listing(registry, clearance)
            assert row["payload"]["source_item_id"] == source
            assert all(leak in text_of([row]) for leak in LEAKS)

    def test_a_promotion_made_under_the_source_label_rule_is_served(
        self, registry: StoreRegistry, source: str
    ) -> None:
        relabel(registry, source, "confidential")
        entity_id = promote(registry, "cand:one", source)
        node = registry.knowledge.graph_store.get_node(entity_id)
        assert node is not None
        assert node["properties"]["source_excerpt_status"] == SOURCE_EXCERPT_LABELLED

        (row,) = listing(registry, "internal")

        assert row["entity_id"] == entity_id
        # The existing pointer concealment still applies; the text holds no copy.
        assert "source_item_id" not in row["payload"]
        assert source not in text_of([row])
        assert not any(leak in text_of([row]) for leak in LEAKS)
        (trusted,) = listing(registry, "restricted")
        assert trusted["payload"]["source_item_id"] == source

    def test_a_chunk_source_is_judged_by_its_parent(
        self, registry: StoreRegistry
    ) -> None:
        docs = registry.knowledge.document_store
        chunk = chunk_doc_id("doc-parent", 0)
        docs.put("doc-parent", "parent body", {})
        docs.put(chunk, BODY, {"title": "Chunk"})
        entity_id = promote(registry, "cand:one", chunk)
        assert ids_of(listing(registry, "internal")) == [entity_id]

        relabel(registry, "doc-parent", "confidential")

        assert listing(registry, "internal") == []
        assert ids_of(listing(registry, "confidential")) == [entity_id]

    def test_the_nodes_source_is_used_when_the_event_names_none(
        self, registry: StoreRegistry
    ) -> None:
        docs = registry.knowledge.document_store
        docs.put("doc-hidden", BODY, label("confidential"))
        registry.knowledge.graph_store.upsert_node(
            "prec-hand", "precedent", {"name": "n", "source_item_id": "doc-hidden"}
        )
        registry.operational.event_log.emit(
            EventType.PRECEDENT_PROMOTED,
            "mutation_executor",
            entity_id="prec-hand",
            entity_type="precedent",
            payload={"title": "t", "description": "d"},
        )

        assert listing(registry, "internal") == []
        assert ids_of(listing(registry, "confidential")) == ["prec-hand"]

    def test_a_row_naming_no_source_and_no_document_row_is_served(
        self, registry: StoreRegistry
    ) -> None:
        registry.operational.event_log.emit(
            EventType.PRECEDENT_PROMOTED,
            "mutation_executor",
            entity_id="trace-1",
            entity_type="trace",
            payload={"title": "t", "description": "d", "trace_id": "trace-1"},
        )

        assert ids_of(listing(registry, "internal")) == ["trace-1"]


class TestWindow:
    def _five(self, registry: StoreRegistry) -> list[str]:
        """Five promotions, oldest first, each from its own source."""
        entities = []
        for n in range(5):
            registry.knowledge.document_store.put(f"doc-{n}", BODY, {})
            entities.append(promote(registry, f"cand:{n}", f"doc-{n}"))
        return entities

    def test_limit_is_still_filled_when_withheld_rows_sit_in_the_first_window(
        self, registry: StoreRegistry
    ) -> None:
        entities = self._five(registry)
        for n in (2, 3, 4):  # the newest three
            relabel(registry, f"doc-{n}", "confidential")

        rows = listing(registry, "internal", limit=2)

        assert sorted(ids_of(rows)) == sorted(entities[:2])
        assert len(listing(registry, "confidential", limit=2)) == 2

    def test_one_judgement_per_window_and_no_id_judged_twice(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._five(registry)
        for n in (2, 3, 4):
            relabel(registry, f"doc-{n}", "confidential")
        calls: list[list[str]] = []
        real = precedents_mod.readable_ids

        def spy(store: Any, ids: Any, clearance: Any) -> list[str]:
            calls.append(list(ids))
            return real(store, ids, clearance)

        monkeypatch.setattr(precedents_mod, "readable_ids", spy)

        listing(registry, "internal", limit=2)

        # limit=2 reads a window of 2 (both withheld), then a window of 8.
        assert len(calls) == 2
        flat = [i for call in calls for i in call]
        assert len(flat) == len(set(flat))

    def test_a_single_window_is_one_call_for_every_id(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        entities = self._five(registry)
        calls: list[list[str]] = []
        real = precedents_mod.readable_ids

        def spy(store: Any, ids: Any, clearance: Any) -> list[str]:
            calls.append(list(ids))
            return real(store, ids, clearance)

        monkeypatch.setattr(precedents_mod, "readable_ids", spy)

        assert len(listing(registry, "internal", limit=20)) == 5

        (ids,) = calls
        assert sorted(ids) == sorted(entities + [f"doc-{n}" for n in range(5)])


class TestFailsClosed:
    def test_a_store_that_raises_withholds_the_row(
        self, registry: StoreRegistry, source: str
    ) -> None:
        promote(registry, "cand:one", source)
        store = MagicMock(spec=registry.knowledge.document_store)
        store.get.side_effect = RuntimeError("down")

        rows = listing(registry, "internal", document_store=store)

        assert rows == []
        store.get.assert_called()

    def test_a_judgement_that_raises_withholds_the_row(
        self, registry: StoreRegistry, source: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        promote(registry, "cand:one", source)

        def boom(*_: Any, **__: Any) -> list[str]:
            msg = "down"
            raise RuntimeError(msg)

        monkeypatch.setattr(precedents_mod, "readable_ids", boom)

        assert listing(registry, "internal") == []

    def test_below_restricted_with_no_store_withholds_what_it_cannot_judge(
        self, registry: StoreRegistry, source: str
    ) -> None:
        promote(registry, "cand:one", source)
        registry.operational.event_log.emit(
            EventType.PRECEDENT_PROMOTED,
            "mutation_executor",
            entity_id="trace-1",
            entity_type="trace",
            payload={"title": "t", "description": "d", "trace_id": "trace-1"},
        )

        def without_store(clearance: Any) -> list[str]:
            return ids_of(
                list_precedents(
                    registry.operational.event_log,
                    graph_store=registry.knowledge.graph_store,
                    clearance=clearance,
                )
            )

        # The learning promotion names a source and has a document row; the
        # trace precedent has neither.
        assert without_store("internal") == ["trace-1"]
        assert len(without_store("restricted")) == 2

    def test_restricted_never_reads_the_store(
        self, registry: StoreRegistry, source: str
    ) -> None:
        promote(registry, "cand:one", source)
        store = MagicMock(spec=registry.knowledge.document_store)

        rows = listing(registry, "restricted", document_store=store)

        assert len(rows) == 1
        store.get.assert_not_called()


class TestMcpGetLessons:
    def _setup(self, registry: StoreRegistry, source: str) -> None:
        promote(registry, "cand:one", source)
        relabel(registry, source, "confidential")

    def test_internal_clearance_omits_the_withheld_lesson(
        self, registry: StoreRegistry, source: str
    ) -> None:
        self._setup(registry, source)
        with as_http(READ):
            served = get_lessons()
        assert TITLE not in served
        assert "ZEBRAWORD" not in served
        assert source not in served

    @pytest.mark.parametrize(
        "caller", [lambda: as_http(ADMIN), lambda: as_http(RESTRICTED), as_stdio]
    )
    def test_a_cleared_or_trusted_caller_is_served_it(
        self, registry: StoreRegistry, source: str, caller: Any
    ) -> None:
        self._setup(registry, source)
        with caller():
            served = get_lessons()
        assert TITLE in served

    def test_an_unlabelled_store_reads_the_same_for_every_caller(
        self, registry: StoreRegistry, source: str
    ) -> None:
        promote(registry, "cand:one", source)
        with as_http(READ):
            low = get_lessons()
        with as_stdio():
            trusted = get_lessons()
        assert TITLE in low
        assert low == trusted


class TestRestListing:
    def _get(self, registry: StoreRegistry, scopes: list[str]) -> list[dict[str, Any]]:
        token, record = generate_api_key("k", scopes)
        registry.operational.api_key_store.create(record)
        resp = TestClient(create_app()).get(
            "/api/v1/precedents", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["count"] == len(body["precedents"])
        rows: list[dict[str, Any]] = body["precedents"]
        return rows

    def test_an_internal_key_does_not_see_the_withheld_precedent(
        self, registry: StoreRegistry, source: str
    ) -> None:
        entity_id = promote(registry, "cand:one", source)
        relabel(registry, source, "confidential")

        assert self._get(registry, ["read"]) == []
        assert ids_of(self._get(registry, ["read", "confidential"])) == [entity_id]
        assert ids_of(self._get(registry, ["admin"])) == [entity_id]

    def test_an_internal_key_does_not_see_a_withheld_own_row(
        self, registry: StoreRegistry, source: str
    ) -> None:
        entity_id = promote(registry, "cand:one", source)
        relabel(registry, entity_id, "confidential")

        assert self._get(registry, ["read"]) == []
        assert ids_of(self._get(registry, ["read", "confidential"])) == [entity_id]


class TestUnlabelledStoreIsUnchanged:
    def test_every_clearance_serves_the_same_bytes(
        self, registry: StoreRegistry
    ) -> None:
        docs = registry.knowledge.document_store
        docs.put("doc-parent", "parent body", {})
        docs.put(chunk_doc_id("doc-parent", 0), BODY, {})
        docs.put("doc-plain", BODY, {})
        for n, src in enumerate(["doc-plain", chunk_doc_id("doc-parent", 0)]):
            promote(registry, f"cand:{n}", src)
        promote(registry, "cand:untitled", "doc-plain", title="")
        registry.operational.event_log.emit(
            EventType.PRECEDENT_PROMOTED,
            "mutation_executor",
            entity_id="trace-1",
            entity_type="trace",
            payload={"title": "t", "description": "d", "trace_id": "trace-1"},
        )

        trusted = text_of(listing(registry, "restricted"))

        assert len(json.loads(trusted)) == 4
        for clearance in ("internal", "confidential"):
            assert text_of(listing(registry, clearance)) == trusted
