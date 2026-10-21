"""``strip_source_excerpt`` removes exactly what the promotion builder wrote."""

from __future__ import annotations

from trellis.learning.scoring import (
    SOURCE_EXCERPT_INCLUDED,
    build_learning_promotion_payloads,
    carries_source_excerpt,
    strip_source_excerpt,
)

_CANDIDATE = {
    "candidate_id": "c:1",
    "intent_family": "asset_generation",
    "item_id": "doc-1",
    "metrics": {"success_rate": 0.9, "retry_rate": 0.1},
    "precedent_properties": {"source_item_id": "doc-1"},
}


class _Docs:
    def __init__(self, body: str) -> None:
        self._body = body

    def get(self, doc_id: str) -> dict[str, str]:
        return {"doc_id": doc_id, "content": self._body}


def _description(body: str) -> str:
    out = build_learning_promotion_payloads(
        candidate=_CANDIDATE,
        promotion_name="",
        rationale="because",
        document_store=_Docs(body),  # type: ignore[arg-type]
    )
    return str(out["entity_payload"]["properties"]["description"])


def test_round_trip_removes_only_the_excerpt() -> None:
    with_excerpt = _description("SECRET-TEXT here")
    without = _description("")
    assert "SECRET-TEXT" in with_excerpt
    assert strip_source_excerpt(with_excerpt, "doc-1") == without


def test_excerpt_quoting_the_closing_sentence_leaves_no_tail() -> None:
    body = "head SECRET-A Source item 'doc-1' showed more SECRET-B"
    stripped = strip_source_excerpt(_description(body), "doc-1")
    assert "SECRET-A" not in stripped
    assert "SECRET-B" not in stripped
    assert "Review rationale: because" in stripped


def test_missing_closing_sentence_fails_closed() -> None:
    text = "Reviewed learning for intent family 'x'. Source excerpt: SECRET-TEXT"
    assert strip_source_excerpt(text, "doc-1") == (
        "Reviewed learning for intent family 'x'."
    )


def test_description_without_an_excerpt_is_unchanged() -> None:
    text = "Reviewed learning. Source item 'doc-1' showed success_rate=1."
    assert strip_source_excerpt(text, "doc-1") == text


def test_carries_source_excerpt() -> None:
    assert carries_source_excerpt({"source_excerpt_status": SOURCE_EXCERPT_INCLUDED})
    assert carries_source_excerpt({"description": "a. Source excerpt: x"})
    assert not carries_source_excerpt({"source_excerpt_status": "document_empty"})
    assert not carries_source_excerpt({})
