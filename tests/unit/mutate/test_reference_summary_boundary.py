"""The write boundary for a reference document's summary (#1250).

A summary is embedded whole or not at all, so the evidence handler refuses, with
nothing written, a ``reference`` document whose ``summary`` is present but not a
string, empty once stripped, or longer than the embed cap. Both
``save_memory`` and ``POST /evidence`` reach this handler.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from trellis.mutate import build_curate_executor
from trellis.mutate.commands import CommandStatus
from trellis.mutate.evidence_ingest import build_evidence_ingest_command
from trellis.retrieve.embed_ingest_hook import EMBED_INPUT_CHAR_CAP
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry

BODY = "A long invented body about kiln firing schedules. " * 400
URI = "https://docs.example.test/kiln/handbook"


@pytest.fixture
def registry(tmp_path: Path) -> StoreRegistry:
    stores_dir = tmp_path / "stores"
    stores_dir.mkdir()
    return StoreRegistry(stores_dir=stores_dir)


def _ingest(registry: StoreRegistry, metadata: dict[str, Any]) -> Any:
    command = build_evidence_ingest_command(
        doc_id="ref-1",
        content=BODY,
        metadata=metadata,
        requested_by="test:reference",
    )
    return build_curate_executor(registry, clearance="restricted").execute(command)


def _store_claims(registry: StoreRegistry) -> list[Any]:
    return [
        event
        for event in registry.operational.event_log.get_events(
            event_type=EventType.MUTATION_EXECUTED, limit=50
        )
        if event.payload.get("operation") == "evidence.ingest"
    ]


@pytest.mark.parametrize(
    ("summary", "fragment"),
    [
        (7, "must be a string, not int"),
        (["a", "summary"], "must be a string, not list"),
        ("", "must not be empty"),
        ("  \n\t ", "must not be empty"),
        (
            "s" * (EMBED_INPUT_CHAR_CAP + 1),
            (
                f"summary is {EMBED_INPUT_CHAR_CAP + 1} characters; "
                f"the limit is {EMBED_INPUT_CHAR_CAP}"
            ),
        ),
    ],
)
def test_an_unusable_summary_on_a_reference_is_refused_with_nothing_written(
    registry: StoreRegistry, summary: object, fragment: str
) -> None:
    result = _ingest(registry, {"document_form": "reference", "summary": summary})

    assert result.status is CommandStatus.REJECTED
    assert fragment in (result.message or "")
    assert registry.knowledge.document_store.get("ref-1") is None
    assert registry.knowledge.document_store.count(include_chunks=True) == 0
    assert registry.knowledge.vector_store.get("ref-1") is None
    assert _store_claims(registry) == []


def test_a_summary_at_the_cap_is_accepted(registry: StoreRegistry) -> None:
    summary = "s" * EMBED_INPUT_CHAR_CAP
    result = _ingest(registry, {"document_form": "reference", "summary": summary})

    assert result.status is CommandStatus.SUCCESS
    stored = registry.knowledge.document_store.get("ref-1")
    assert stored is not None
    assert stored["content"] == BODY
    assert stored["metadata"]["summary"] == summary
    assert len(_store_claims(registry)) == 1


def test_a_valid_reference_keeps_its_link_and_body(registry: StoreRegistry) -> None:
    result = _ingest(
        registry,
        {"document_form": "reference", "summary": "Short summary.", "uri": URI},
    )
    assert result.status is CommandStatus.SUCCESS
    stored = registry.knowledge.document_store.get("ref-1")
    assert stored is not None
    assert stored["metadata"]["uri"] == URI


@pytest.mark.parametrize(
    "metadata",
    [
        {"summary": 7},
        {"summary": ""},
        {"summary": "s" * (EMBED_INPUT_CHAR_CAP + 1)},
        {"document_form": "note", "summary": "s" * (EMBED_INPUT_CHAR_CAP + 1)},
        {"document_form": "reference"},
    ],
)
def test_a_document_that_is_not_a_summary_carrying_reference_is_not_judged(
    registry: StoreRegistry, metadata: dict[str, Any]
) -> None:
    """``summary`` is a common frontmatter key; only a reference answers for it."""
    result = _ingest(registry, metadata)
    assert result.status is CommandStatus.SUCCESS
    assert registry.knowledge.document_store.get("ref-1") is not None
