"""A promotion copies no source text or id from a source above the floor label.

A precedent carries no sensitivity label of its own, so whatever it copies from
its source reaches every caller at ``internal`` clearance. The promotion read
the source's body, title and id without ever asking for the source's label:
the excerpt, a name built from the source's title or first line, and the
``Source item '<id>' showed ...`` sentence. A source labelled ``confidential``
or ``restricted`` now lends none of them. ``properties.source_item_id`` stays:
it is the structured pointer the read surfaces conceal and a redaction matches
on.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from trellis.learning.scoring import (
    NAME_SOURCE_ITEM_ID,
    NAME_SOURCE_NEUTRAL,
    NAME_SOURCE_REVIEWER,
    REFUSED_REDACTED,
    SOURCE_EXCERPT_INCLUDED,
    SOURCE_EXCERPT_LABELLED,
    _apply_readable_name_fallback,
    build_learning_promotion_payloads,
    neutral_precedent_name,
    resolve_source_excerpt,
    strip_source_excerpt,
    submit_learning_promotion,
)
from trellis.mutate import build_curate_executor
from trellis.mutate.handlers import REDACTION_TOMBSTONE_REASON
from trellis.schemas.classification import LIFECYCLE_KEY
from trellis.stores.registry import StoreRegistry
from trellis.stores.sqlite.document import SQLiteDocumentStore

FAMILY = "asset_generation"
SOURCE_ID = "doc-quartz-7f3"
BODY = "Heron opening line SECRETWORD and the rest of the body."
TITLE = "Quartz Title Words"
CANDIDATE_ID = "cand:one"


def _label(sensitivity: str) -> dict[str, Any]:
    return {"data_classification": {"sensitivity": sensitivity}}


def _candidate(item_id: str = SOURCE_ID, *, title: str | None = None) -> dict[str, Any]:
    """A candidate as the analyzer writes it, before any readable-name fallback."""
    display = title or item_id
    return {
        "candidate_id": CANDIDATE_ID,
        "intent_family": FAMILY,
        "recommendation_type": "promote_guidance",
        "item_id": item_id,
        "title": title,
        "precedent_name": f"Learning: {FAMILY} :: {display}",
        "precedent_name_source": "item_title" if title else NAME_SOURCE_ITEM_ID,
        "metrics": {"success_rate": 0.9, "retry_rate": 0.1},
        "citation_evidence": {"helpful_count": 2, "unhelpful_count": 0},
        "precedent_properties": {
            "intent_family": FAMILY,
            "source_item_id": item_id,
        },
        "target_entity_ids": ["entity://shared"],
        "supporting_run_ids": ["run-1"],
        "phases": ["GENERATE"],
    }


def _named_from_source(documents: SQLiteDocumentStore) -> dict[str, Any]:
    candidate = _candidate()
    _apply_readable_name_fallback(candidate, documents)
    assert candidate["precedent_name_source"] == "source_text"
    return candidate


def _build(
    candidate: dict[str, Any],
    documents: Any,
    *,
    promotion_name: str = "",
) -> dict[str, Any]:
    return build_learning_promotion_payloads(
        candidate=candidate,
        promotion_name=promotion_name,
        rationale="because",
        document_store=documents,
    )


def _entity(payloads: dict[str, Any]) -> dict[str, Any]:
    entity: dict[str, Any] = payloads["entity_payload"]
    return entity


def _assert_no_source_leak(payloads: dict[str, Any], *ids: str) -> None:
    """Nothing in the payload names the source but ``properties.source_item_id``."""
    entity = _entity(payloads)
    properties = dict(entity["properties"])
    kept = properties.pop("source_item_id", None)
    assert kept == ids[0], "the structured pointer stays for redaction to match on"
    everything = json.dumps(
        {
            "name": entity["name"],
            "properties": properties,
            "edges": payloads["edge_payloads"],
        }
    )
    for item_id in ids:
        assert item_id not in everything
    for text in ("SECRETWORD", "Heron", "Quartz Title Words"):
        assert text not in everything
    assert entity["name"] == neutral_precedent_name(payloads["entity_id"], FAMILY)
    assert properties["name_source"] == NAME_SOURCE_NEUTRAL
    assert properties["source_excerpt_status"] == SOURCE_EXCERPT_LABELLED
    assert "Source excerpt:" not in properties["description"]


@pytest.fixture
def documents(tmp_path: Path) -> SQLiteDocumentStore:
    return SQLiteDocumentStore(tmp_path / "documents.db")


@pytest.mark.parametrize("sensitivity", ["confidential", "restricted"])
@pytest.mark.parametrize("flavour", ["item_id", "item_title", "source_text"])
def test_a_labelled_source_lends_no_text_and_no_id(
    documents: SQLiteDocumentStore, sensitivity: str, flavour: str
) -> None:
    documents.put(SOURCE_ID, BODY, _label(sensitivity))
    candidate = {
        "item_id": _candidate(),
        "item_title": _candidate(title=TITLE),
        "source_text": _named_from_source(documents),
    }[flavour]

    payloads = _build(candidate, documents)

    _assert_no_source_leak(payloads, SOURCE_ID)
    assert _entity(payloads)["properties"]["description"] == (
        f"Reviewed learning for intent family '{FAMILY}'. "
        "The source item showed success_rate=0.9 and retry_rate=0.1. "
        "Graders cited it helpful 2 time(s) and unhelpful 0 time(s). "
        "Review rationale: because"
    )


@pytest.mark.parametrize("sensitivity", ["confidential", "restricted"])
def test_a_chunk_of_a_labelled_parent_lends_nothing(
    documents: SQLiteDocumentStore, sensitivity: str
) -> None:
    chunk_id = f"{SOURCE_ID}#chunk-0"
    documents.put(SOURCE_ID, BODY, _label(sensitivity))
    documents.put(chunk_id, BODY, {"parent_doc_id": SOURCE_ID})
    assert resolve_source_excerpt(chunk_id, documents) == (
        None,
        SOURCE_EXCERPT_LABELLED,
    )

    payloads = _build(_candidate(chunk_id), documents)

    _assert_no_source_leak(payloads, chunk_id, SOURCE_ID)


def test_a_chunk_of_an_unlabelled_parent_still_lends_its_text(
    documents: SQLiteDocumentStore,
) -> None:
    chunk_id = f"{SOURCE_ID}#chunk-0"
    documents.put(SOURCE_ID, BODY, {})
    documents.put(chunk_id, BODY, {"parent_doc_id": SOURCE_ID})

    excerpt, status = resolve_source_excerpt(chunk_id, documents)

    assert status == SOURCE_EXCERPT_INCLUDED
    assert excerpt is not None
    assert "SECRETWORD" in excerpt


def test_a_chunk_whose_row_is_gone_is_still_judged_by_its_parent(
    documents: SQLiteDocumentStore,
) -> None:
    """A re-chunk deletes the chunk row; the candidate still names the old id."""
    chunk_id = f"{SOURCE_ID}#chunk-0"
    documents.put(SOURCE_ID, BODY, _label("confidential"))
    assert documents.get(chunk_id) is None
    assert resolve_source_excerpt(chunk_id, documents) == (
        None,
        SOURCE_EXCERPT_LABELLED,
    )

    payloads = _build(_candidate(chunk_id, title=TITLE), documents)

    _assert_no_source_leak(payloads, chunk_id, SOURCE_ID)


def test_a_chunk_whose_row_is_gone_under_an_unlabelled_parent_is_unchanged(
    documents: SQLiteDocumentStore,
) -> None:
    chunk_id = f"{SOURCE_ID}#chunk-0"
    documents.put(SOURCE_ID, BODY, _label("internal"))

    entity = _entity(_build(_candidate(chunk_id), documents))

    assert entity["properties"]["source_excerpt_status"] == "document_not_found"
    assert entity["name"] == f"Learning: {FAMILY} :: {chunk_id}"
    assert f"Source item '{chunk_id}' showed " in entity["properties"]["description"]


@pytest.mark.parametrize(
    "metadata",
    [
        pytest.param({}, id="no-label"),
        pytest.param(_label("internal"), id="internal"),
        pytest.param(_label("public"), id="public"),
    ],
)
def test_an_unlabelled_source_is_byte_identical_to_before(
    documents: SQLiteDocumentStore, metadata: dict[str, Any]
) -> None:
    documents.put("doc-plain", "Plain body text.", metadata)

    payloads = _build(_candidate("doc-plain"), documents)

    entity = _entity(payloads)
    assert entity["name"] == f"Learning: {FAMILY} :: doc-plain"
    assert entity["properties"]["name_source"] == NAME_SOURCE_ITEM_ID
    assert entity["properties"]["source_excerpt_status"] == SOURCE_EXCERPT_INCLUDED
    assert entity["properties"]["description"] == (
        f"Reviewed learning for intent family '{FAMILY}'. "
        "Source excerpt: Plain body text. "
        "Source item 'doc-plain' showed success_rate=0.9 and retry_rate=0.1. "
        "Graders cited it helpful 2 time(s) and unhelpful 0 time(s). "
        "Review rationale: because"
    )
    assert entity["properties"]["source_item_id"] == "doc-plain"


def test_a_reviewer_supplied_name_is_kept_for_a_labelled_source(
    documents: SQLiteDocumentStore,
) -> None:
    documents.put(SOURCE_ID, BODY, _label("confidential"))

    payloads = _build(_candidate(), documents, promotion_name="Reviewer chosen title")

    entity = _entity(payloads)
    assert entity["name"] == "Reviewer chosen title"
    assert entity["properties"]["name_source"] == NAME_SOURCE_REVIEWER
    assert entity["properties"]["source_excerpt_status"] == SOURCE_EXCERPT_LABELLED
    assert SOURCE_ID not in entity["properties"]["description"]
    assert "SECRETWORD" not in entity["properties"]["description"]


def test_an_edit_of_a_source_text_name_does_not_survive_a_labelled_source(
    documents: SQLiteDocumentStore,
) -> None:
    """A touched-up derived name is still source text, so it is replaced too."""
    documents.put(SOURCE_ID, BODY, _label("confidential"))

    payloads = _build(
        _named_from_source(documents), documents, promotion_name="Heron opening"
    )

    _assert_no_source_leak(payloads, SOURCE_ID)


class _RaisingDocuments:
    """A document store that cannot answer any read."""

    def get(self, doc_id: str) -> dict[str, Any]:
        message = f"store unavailable for {doc_id}"
        raise RuntimeError(message)


def test_a_store_that_raises_on_the_label_read_fails_closed() -> None:
    assert resolve_source_excerpt(SOURCE_ID, _RaisingDocuments()) == (  # type: ignore[arg-type]
        None,
        "lookup_failed",
    )

    payloads = _build(_candidate(), _RaisingDocuments())

    entity = _entity(payloads)
    assert SOURCE_ID not in json.dumps(
        {"name": entity["name"], "description": entity["properties"]["description"]}
    )
    assert entity["name"] == neutral_precedent_name(payloads["entity_id"], FAMILY)
    assert entity["properties"]["source_excerpt_status"] == "lookup_failed"
    assert entity["properties"]["source_item_id"] == SOURCE_ID


def test_a_chunk_whose_parent_cannot_be_read_fails_closed(
    tmp_path: Path,
) -> None:
    chunk_id = f"{SOURCE_ID}#chunk-0"

    class _ParentOutage(SQLiteDocumentStore):
        def get(self, doc_id: str) -> dict[str, Any] | None:
            if doc_id == SOURCE_ID:
                message = "parent row unreadable"
                raise RuntimeError(message)
            return super().get(doc_id)

    documents = _ParentOutage(tmp_path / "outage.db")
    documents.put(chunk_id, BODY, {"parent_doc_id": SOURCE_ID})

    payloads = _build(_candidate(chunk_id), documents)

    entity = _entity(payloads)
    assert chunk_id not in entity["properties"]["description"]
    assert SOURCE_ID not in entity["properties"]["description"]
    assert "SECRETWORD" not in entity["properties"]["description"]
    assert entity["name"] == neutral_precedent_name(payloads["entity_id"], FAMILY)


def test_a_missing_document_is_unchanged(documents: SQLiteDocumentStore) -> None:
    payloads = _build(_candidate(), documents)

    entity = _entity(payloads)
    assert entity["properties"]["source_excerpt_status"] == "document_not_found"
    assert entity["name"] == f"Learning: {FAMILY} :: {SOURCE_ID}"
    assert entity["properties"]["name_source"] == NAME_SOURCE_ITEM_ID
    assert f"Source item '{SOURCE_ID}' showed " in entity["properties"]["description"]


def test_no_document_store_is_unchanged() -> None:
    entity = _entity(_build(_candidate(), None))

    assert entity["properties"]["source_excerpt_status"] == "no_document_store"
    assert entity["name"] == f"Learning: {FAMILY} :: {SOURCE_ID}"


def test_strip_source_excerpt_leaves_a_labelled_description_unchanged(
    documents: SQLiteDocumentStore,
) -> None:
    documents.put(SOURCE_ID, BODY, _label("confidential"))
    description = _entity(_build(_candidate(), documents))["properties"]["description"]

    assert strip_source_excerpt(description, SOURCE_ID) == description


def test_a_labelled_promotion_lands_and_a_re_run_is_already_promoted(
    tmp_path: Path,
) -> None:
    """The neutral name marker must not make the re-run read as a redaction."""
    stores_dir = tmp_path / "stores"
    stores_dir.mkdir()
    registry = StoreRegistry(stores_dir=stores_dir)
    graph = registry.knowledge.graph_store
    events = registry.operational.event_log
    docs = registry.knowledge.document_store
    graph.upsert_node("entity://shared", "entity", {"name": "shared"})
    docs.put(SOURCE_ID, BODY, _label("confidential"))
    executor = build_curate_executor(registry, clearance="restricted")
    candidate = _candidate(title=TITLE)

    def submit() -> dict[str, Any]:
        payloads = _build(copy.deepcopy(candidate), docs)
        return submit_learning_promotion(
            executor,
            payloads["entity_payload"],
            payloads["edge_payloads"],
            requested_by="test",
            graph_store=graph,
            event_log=events,
            document_store=docs,
        )

    first = submit()
    assert first["status"] == "promoted", first
    row = docs.get(_build(candidate, docs)["entity_id"])
    assert row is not None
    assert SOURCE_ID not in row["content"]
    assert "SECRETWORD" not in row["content"]
    assert TITLE not in row["content"]

    assert submit()["status"] == "already_promoted"


class _Promotions:
    """A real registry, and the promote-and-redact steps the tests below share."""

    def __init__(self, tmp_path: Path) -> None:
        stores_dir = tmp_path / "stores"
        stores_dir.mkdir()
        self.registry = StoreRegistry(stores_dir=stores_dir)
        self.graph = self.registry.knowledge.graph_store
        self.events = self.registry.operational.event_log
        self.documents = self.registry.knowledge.document_store
        self.executor = build_curate_executor(self.registry, clearance="restricted")
        self.graph.upsert_node("entity://shared", "entity", {"name": "shared"})

    def submit(self, candidate: dict[str, Any]) -> dict[str, Any]:
        payloads = _build(copy.deepcopy(candidate), self.documents)
        return submit_learning_promotion(
            self.executor,
            payloads["entity_payload"],
            payloads["edge_payloads"],
            requested_by="test",
            graph_store=self.graph,
            event_log=self.events,
            document_store=self.documents,
        )

    def promote_while_confidential(self) -> dict[str, Any]:
        """A candidate named from the source's text, promoted once the source is
        labelled, then the label lowered: the shape of a re-promotion."""
        self.documents.put(SOURCE_ID, BODY, _label("internal"))
        candidate = _named_from_source(self.documents)
        self.documents.put(SOURCE_ID, BODY, _label("confidential"))
        assert self.submit(candidate)["status"] == "promoted"
        self.documents.put(SOURCE_ID, BODY, _label("internal"))
        return candidate


@pytest.fixture
def promotions(tmp_path: Path):
    p = _Promotions(tmp_path)
    yield p
    p.registry.close()


def test_lowering_the_label_does_not_make_the_earlier_promotion_a_redaction(
    promotions: _Promotions,
) -> None:
    """The neutral name a labelled source earns is not a redaction marker."""
    candidate = promotions.promote_while_confidential()

    assert promotions.submit(candidate)["status"] == "promoted"


def test_a_real_redaction_of_a_labelled_source_is_still_refused(
    promotions: _Promotions,
) -> None:
    candidate = promotions.promote_while_confidential()
    promotions.documents.put(
        SOURCE_ID,
        "",
        {LIFECYCLE_KEY: {"deprecation_reason": REDACTION_TOMBSTONE_REASON}},
    )

    assert promotions.submit(candidate)["status"] == REFUSED_REDACTED
