"""``POST /vectors`` at a reference document's id keeps the row a summary row (#1250).

A reference document's vector row says that its excerpt is the summary
(``excerpt_source: "summary"``), carries the document's ``uri`` and has the
summary excerpt as ``content``. ``build_vector_row`` writes that row. The route
builds the row of a stored document from the *document*, so a ``mutate``-scoped
caller cannot undo it: a row that came back with the body excerpt and the marker
would serve body text under the "Summary only." line, and a caller must not be
able to stamp the marker and a ``uri`` on a plain document's row to forge a
"Source:" line either.

Real SQLite stores, the real route, the real pack builder and formatter. Text
here is invented.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

import trellis_api.app as app_module
from trellis.auth import generate_api_key
from trellis.llm.egress import EgressGate
from trellis.retrieve.embed_ingest_hook import EMBED_INPUT_CHAR_CAP, build_vector_row
from trellis.retrieve.excerpts import truncate_excerpt
from trellis.retrieve.formatters import format_pack_as_markdown
from trellis.retrieve.pack_builder import PackBuilder
from trellis.retrieve.reference import summary_of
from trellis.retrieve.strategies import SemanticSearch
from trellis.stores.registry import StoreRegistry
from trellis_api.app import create_app

#: A word only the body holds: its presence in a rendered pack is a body leak.
BODY_ONLY_TERM = "zephyrquartz"
BODY = f"{BODY_ONLY_TERM} opens the handbook. " + (
    "The body text goes on at some length about kiln schedules. " * 40
)
SUMMARY = (
    "Invented handbook for the quasar kiln: firing schedules, safety checks and "
    "a glossary. " * 9
).strip()
URI = "https://docs.example.test/kiln/handbook"
FORGED_URI = "https://evil.example.test/forged"
REFERENCE_META: dict[str, Any] = {
    "document_form": "reference",
    "summary": SUMMARY,
    "uri": URI,
}
RENDER_INTENT = "how a handbook is looked up"
EXCERPT_CHARS = 500


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.delenv("TRELLIS_API_KEY", raising=False)
    monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")
    reg = StoreRegistry(stores_dir=tmp_path / "stores")
    app_module._registry = reg
    docs = reg.knowledge.document_store
    docs.put("doc-ref", BODY, dict(REFERENCE_META))
    docs.put("doc-plain", BODY, {"title": "Plain"})
    docs.put(
        "doc-ref#chunk-0",
        "one section of the handbook, embedded as itself",
        {**REFERENCE_META, "parent_doc_id": "doc-ref", "chunk_index": 0},
    )
    yield reg
    reg.close()
    app_module._registry = None


@pytest.fixture
def client(registry):
    return TestClient(create_app())


def _headers(registry):
    token, record = generate_api_key("k", ["ingest"])
    registry.operational.api_key_store.create(record)
    return {"Authorization": f"Bearer {token}"}


def _vector(registry, seed=0.5):
    return [seed] * (registry.knowledge.vector_store.dimensions or 8)


def _post(client, registry, item_id, metadata=None):
    row: dict[str, Any] = {"item_id": item_id, "vector": _vector(registry)}
    if metadata is not None:
        row["metadata"] = metadata
    resp = client.post(
        "/api/v1/vectors", json={"vectors": [row]}, headers=_headers(registry)
    )
    assert resp.status_code == 200, resp.text
    return resp


def _stored(registry, item_id) -> dict[str, Any]:
    row = registry.knowledge.vector_store.get(item_id)
    assert row is not None
    return row["metadata"]


def _items(pack) -> list[dict[str, Any]]:
    return [
        {
            "item_id": item.item_id,
            "item_type": item.item_type,
            "excerpt": item.excerpt,
            "relevance_score": item.relevance_score,
            "estimated_tokens": item.estimated_tokens,
            "metadata": item.metadata,
        }
        for item in pack.items
    ]


def _rendered(registry) -> tuple[Any, str]:
    builder = PackBuilder(
        strategies=[
            SemanticSearch(
                registry.knowledge.vector_store,
                embedding_fn=lambda _text: _vector(registry),
            )
        ],
        document_store=registry.knowledge.document_store,
    )
    pack = builder.build("handbook", clearance="internal")
    return pack, format_pack_as_markdown(_items(pack), RENDER_INTENT)


def _row_as_built(registry, item_id: str) -> dict[str, Any]:
    """The metadata ``build_vector_row`` would write for the stored document."""
    doc = registry.knowledge.document_store.get(item_id)
    row = build_vector_row(
        item_id,
        doc["content"],
        doc["metadata"],
        lambda _text: _vector(registry),
        egress=EgressGate(None, local=True),
    )
    return row["metadata"]


class TestAReferencesRowStaysASummaryRow:
    def test_a_row_without_metadata_is_built_from_the_document(
        self, client, registry
    ) -> None:
        _post(client, registry, "doc-ref")

        row = _stored(registry, "doc-ref")
        assert row["content"] == truncate_excerpt(SUMMARY, EXCERPT_CHARS)
        assert row["excerpt_source"] == "summary"
        assert row["uri"] == URI
        assert "summary" not in row
        assert row["doc_id"] == "doc-ref"

    def test_the_row_build_vector_row_wrote_is_stored_as_it_was(
        self, client, registry
    ) -> None:
        built = _row_as_built(registry, "doc-ref")
        assert built["excerpt_source"] == "summary"

        _post(client, registry, "doc-ref", built)

        assert _stored(registry, "doc-ref") == built

    def test_a_row_that_carries_the_body_excerpt_is_corrected(
        self, client, registry
    ) -> None:
        # What a caller that builds its own row would send: the body as the
        # excerpt, the summary as a plain key, no marker.
        _post(
            client,
            registry,
            "doc-ref",
            {"content": truncate_excerpt(BODY, EXCERPT_CHARS), "summary": SUMMARY},
        )

        row = _stored(registry, "doc-ref")
        assert row["content"] == truncate_excerpt(SUMMARY, EXCERPT_CHARS)
        assert row["excerpt_source"] == "summary"
        assert "summary" not in row

    def test_a_semantic_pack_shows_the_summary_and_no_body_text(
        self, client, registry
    ) -> None:
        _post(client, registry, "doc-ref")

        pack, text = _rendered(registry)
        assert [item.item_id for item in pack.items] == ["doc-ref"]
        assert "Invented handbook for the quasar kiln" in text
        assert BODY_ONLY_TERM not in text
        assert "body text goes on" not in text
        assert f"Source: `{URI}`" in text
        assert "Full text: get_items" in text

    def test_a_marker_the_caller_leaves_out_is_put_back(self, client, registry) -> None:
        _post(client, registry, "doc-ref", {"excerpt_source": "body"})

        assert _stored(registry, "doc-ref")["excerpt_source"] == "summary"

    def test_a_uri_the_caller_changes_is_the_documents(self, client, registry) -> None:
        _post(client, registry, "doc-ref", {"uri": FORGED_URI})

        assert _stored(registry, "doc-ref")["uri"] == URI
        _, text = _rendered(registry)
        assert FORGED_URI not in text
        assert f"Source: `{URI}`" in text


class TestAPlainDocumentsRowCannotBeForged:
    def test_a_forged_marker_and_uri_are_not_kept(self, client, registry) -> None:
        _post(
            client,
            registry,
            "doc-plain",
            {"excerpt_source": "summary", "uri": FORGED_URI, "summary": "forged"},
        )

        row = _stored(registry, "doc-plain")
        assert "excerpt_source" not in row
        assert "uri" not in row
        assert row["content"] == truncate_excerpt(BODY, EXCERPT_CHARS)

        _, text = _rendered(registry)
        assert "Source:" not in text
        assert FORGED_URI not in text
        assert "Summary only" not in text

    def test_a_plain_documents_row_is_otherwise_as_it_was(
        self, client, registry
    ) -> None:
        _post(client, registry, "doc-plain", {"title": "Plain", "note": "kept"})

        assert _stored(registry, "doc-plain") == {
            "title": "Plain",
            "note": "kept",
            "content": truncate_excerpt(BODY, EXCERPT_CHARS),
            "doc_id": "doc-plain",
        }

    def test_a_reference_chunks_row_is_not_summary_served(
        self, client, registry
    ) -> None:
        _post(client, registry, "doc-ref#chunk-0", {"excerpt_source": "summary"})

        row = _stored(registry, "doc-ref#chunk-0")
        assert "excerpt_source" not in row
        assert row["content"] == "one section of the handbook, embedded as itself"


class TestACallersSummaryIsNeverKept:
    """A row's ``summary`` is its document's, so the rule reads the same on both."""

    def test_a_forged_summary_on_a_plain_documents_row_is_not_kept(
        self, client, registry
    ) -> None:
        _post(client, registry, "doc-plain", {"summary": "forged summary"})

        assert "summary" not in _stored(registry, "doc-plain")

    def test_a_forged_summary_on_an_over_cap_references_row_is_not_kept(
        self, client, registry
    ) -> None:
        registry.knowledge.document_store.put(
            "doc-long",
            BODY,
            {**REFERENCE_META, "summary": "w " * EMBED_INPUT_CHAR_CAP},
        )
        assert (
            summary_of(registry.knowledge.document_store.get("doc-long")["metadata"])
            is None
        )

        _post(
            client,
            registry,
            "doc-long",
            {"document_form": "reference", "summary": "forged summary"},
        )

        row = _stored(registry, "doc-long")
        assert row["summary"] == "w " * EMBED_INPUT_CHAR_CAP
        assert "forged summary" not in str(row)
        assert "excerpt_source" not in row
        assert row["content"] == truncate_excerpt(BODY, EXCERPT_CHARS)

    def test_a_references_summary_is_dropped_from_its_row_whatever_was_sent(
        self, client, registry
    ) -> None:
        _post(client, registry, "doc-ref", {"summary": "forged summary"})

        row = _stored(registry, "doc-ref")
        assert "summary" not in row
        assert row["excerpt_source"] == "summary"

    def test_a_plain_documents_own_summary_is_the_one_kept(
        self, client, registry
    ) -> None:
        registry.knowledge.document_store.put(
            "doc-noted", BODY, {"title": "Noted", "summary": "its own note"}
        )

        _post(client, registry, "doc-noted", {"summary": "forged summary"})

        assert _stored(registry, "doc-noted")["summary"] == "its own note"


class TestNoStoredDocument:
    def test_a_callers_marker_is_never_kept(self, client, registry) -> None:
        _post(
            client,
            registry,
            "free-standing",
            {"excerpt_source": "summary", "content": "an excerpt", "note": "kept"},
        )

        row = _stored(registry, "free-standing")
        assert "excerpt_source" not in row
        assert row["content"] == "an excerpt"
        assert row["note"] == "kept"
