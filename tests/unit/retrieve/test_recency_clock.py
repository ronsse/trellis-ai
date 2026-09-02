"""Which clock recency decay reads, and that both axes read the same one.

#417. ``updated_at`` / ``created_at`` name two different facts. As store
*columns* they are the row's write clock; as *metadata keys* they are the
source's clock, written by an ingest path that knows the content predates
its own write. ``KeywordSearch`` used to read the column and
``SemanticSearch`` the bag, so one conversation document had two ages
depending on which strategy retrieved it.

These tests pin three things:

* the conversation ingest **does** produce a source clock, in the document
  metadata and on the vector row — the premise ``mcp/reconcile.py`` asserted
  the negative of;
* both document-backed axes apply the **same** recency multiplier to it;
* a source clock is honoured wherever it appears, so a *new* writer producing
  one gets the documented behaviour rather than a surprise.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from trellis.ingest_corpus.conversations import (
    conversation_doc_id,
    sync_conversations,
)
from trellis.retrieve.embed_ingest_hook import EMBED_ON_INGEST_FLAG
from trellis.retrieve.strategies import KeywordSearch, SemanticSearch
from trellis.stores.sqlite.document import SQLiteDocumentStore
from trellis.stores.sqlite.event_log import SQLiteEventLog
from trellis.stores.sqlite.vector import SQLiteVectorStore

_DIMS = 32

#: Deliberately far in the past relative to any test run: the store columns
#: are stamped at ingest (≈ now), so a stale source clock is what makes the
#: two clocks distinguishable at all.
_SOURCE_CREATED = "2024-06-01T10:00:00Z"
_SOURCE_UPDATED = "2024-06-01T10:20:00Z"

_CONVERSATION = {
    "uuid": "conv-417",
    "name": "Custodial Roth mechanics",
    "created_at": _SOURCE_CREATED,
    "updated_at": _SOURCE_UPDATED,
    "chat_messages": [
        {"sender": "human", "text": "How does a custodial Roth work?"},
        {"sender": "assistant", "text": "It needs the child to have earned income."},
    ],
}


def _embed(text: str) -> list[float]:
    vector = [0.0] * _DIMS
    for word in text.lower().split():
        digest = hashlib.md5(word.encode(), usedforsecurity=False).digest()
        vector[digest[0] % _DIMS] += 1.0
    norm = sum(v * v for v in vector) ** 0.5 or 1.0
    return [v / norm for v in vector]


@pytest.fixture
def ingested(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Run the real conversation ingest; return the rows it actually wrote."""
    monkeypatch.setenv(EMBED_ON_INGEST_FLAG, "1")
    registry = MagicMock()
    registry.knowledge.document_store = SQLiteDocumentStore(tmp_path / "docs.db")
    registry.knowledge.vector_store = SQLiteVectorStore(tmp_path / "vectors.db")
    registry.operational.event_log = SQLiteEventLog(tmp_path / "events.db")
    registry.embedding_fn = _embed

    export = tmp_path / "conversations.json"
    export.write_text(json.dumps([_CONVERSATION]))
    sync_conversations(registry, export, source_system="claude-ai")

    doc_id = conversation_doc_id("claude-ai", "conv-417")
    document = registry.knowledge.document_store.get(doc_id)
    assert document is not None
    vector_row = registry.knowledge.vector_store.get(doc_id)
    assert vector_row is not None
    return {"doc_id": doc_id, "document": document, "vector_row": vector_row}


def _keyword_score(doc_row: dict[str, Any], *, rank: float = -0.8) -> float:
    store = MagicMock()
    store.search.return_value = [{**doc_row, "rank": rank}]
    return KeywordSearch(store).search("roth")[0].relevance_score


def _semantic_score(vector_row: dict[str, Any], *, score: float = 0.8) -> float:
    store = MagicMock()
    store.query.return_value = [
        {"item_id": vector_row["item_id"], "score": score, **vector_row}
    ]
    return (
        SemanticSearch(store, lambda _text: [0.0] * _DIMS)
        .search("roth")[0]
        .relevance_score
    )


class TestConversationIngestProducesASourceClock:
    """The premise #411 stated the negative of, pinned as behaviour."""

    def test_document_metadata_carries_the_export_clock(
        self, ingested: dict[str, Any]
    ) -> None:
        metadata = ingested["document"]["metadata"]
        assert metadata["created_at"] == _SOURCE_CREATED
        assert metadata["updated_at"] == _SOURCE_UPDATED

    def test_vector_row_carries_it_too(self, ingested: dict[str, Any]) -> None:
        # ``build_vector_row`` splats document metadata and only then
        # ``setdefault``s its own ``created_at``, so the source clock — not
        # embed time — is what the semantic axis reads.
        metadata = ingested["vector_row"]["metadata"]
        assert metadata["created_at"] == _SOURCE_CREATED
        assert metadata["updated_at"] == _SOURCE_UPDATED

    def test_the_store_columns_disagree_with_it(self, ingested: dict[str, Any]) -> None:
        # The divergence this issue is about only exists because these two
        # are different facts. If the columns ever start carrying the
        # source's clock, the rest of these tests stop measuring anything.
        column = datetime.fromisoformat(str(ingested["document"]["updated_at"]))
        if column.tzinfo is None:
            column = column.replace(tzinfo=UTC)
        source = datetime.fromisoformat(_SOURCE_UPDATED)
        assert (column - source) > timedelta(days=180)


class TestBothAxesReadTheSameClock:
    def test_conversation_ranks_identically_on_both_axes(
        self, ingested: dict[str, Any]
    ) -> None:
        # Same document, same base score, one number. Before #417 the
        # keyword axis scored it off its ingest-time column (multiplier ≈ 1)
        # and the semantic axis off the 2024 source clock (multiplier ≈ the
        # floor) — on production, a median 2.20x apart.
        keyword = _keyword_score(ingested["document"], rank=-0.8)
        semantic = _semantic_score(ingested["vector_row"], score=0.8)
        assert keyword == pytest.approx(semantic)

    def test_keyword_axis_ignores_the_column_when_a_source_clock_exists(
        self, ingested: dict[str, Any]
    ) -> None:
        # Move the column a decade and the score must not budge: a 2024
        # conversation is 2024 content whatever the row's write clock says,
        # and a metadata-only re-write that bumps the column must not make
        # it look fresh.
        aged = {
            **ingested["document"],
            "updated_at": (datetime.now(UTC) - timedelta(days=3650)).isoformat(),
        }
        assert _keyword_score(aged) == pytest.approx(
            _keyword_score(ingested["document"])
        )

    def test_keyword_axis_still_reads_the_column_without_a_source_clock(
        self,
    ) -> None:
        # The overwhelming majority of documents. Nothing changes for them.
        stale = {
            "doc_id": "d1",
            "content": "no source clock here",
            "metadata": {},
            "updated_at": (datetime.now(UTC) - timedelta(days=3650)).isoformat(),
        }
        fresh = {**stale, "updated_at": datetime.now(UTC).isoformat()}
        assert _keyword_score(fresh) > _keyword_score(stale)


class TestSourceClockIsHonouredWhereverItAppears:
    """So a *new* writer producing the key gets the documented behaviour."""

    def test_any_document_metadata_stamp_outranks_the_column(self) -> None:
        row = {
            "doc_id": "d1",
            "content": "some other importer's document",
            "metadata": {
                "updated_at": (datetime.now(UTC) - timedelta(days=3650)).isoformat()
            },
            "updated_at": datetime.now(UTC).isoformat(),
        }
        column_only = {**row, "metadata": {}}
        assert _keyword_score(row) < _keyword_score(column_only)

    def test_malformed_source_stamp_falls_through_to_the_column(self) -> None:
        # ``_apply_recency_decay`` fails *open* on an unparseable stamp — it
        # returns the score undecayed, i.e. maximally fresh. A value that
        # came from outside must never be able to buy that, so the resolver
        # skips it and the row's own clock is used instead.
        stale_column = (datetime.now(UTC) - timedelta(days=3650)).isoformat()
        garbage = {
            "doc_id": "d1",
            "content": "x",
            "metadata": {"updated_at": "last Tuesday"},
            "updated_at": stale_column,
        }
        clean = {**garbage, "metadata": {}}
        assert _keyword_score(garbage) == pytest.approx(_keyword_score(clean))
        # …and it really was decayed, not passed through undecayed.
        assert _keyword_score(garbage) < 0.8


class TestAFutureSourceStampCannotBuyFreshness:
    """The other half of the fail-open guard, and the easier route in.

    ``_apply_recency_decay`` clamps age at zero, so a stamp dated 2099 earns
    exactly the maximum multiplier an unparseable one does — while parsing
    cleanly. Both live producers copy these keys verbatim out of a file, so
    the value is caller-supplied.
    """

    def _row(self, meta_stamp: str) -> dict[str, Any]:
        return {
            "doc_id": "d1",
            "content": "x",
            "metadata": {"updated_at": meta_stamp},
            "updated_at": (datetime.now(UTC) - timedelta(days=3650)).isoformat(),
        }

    def test_future_stamp_falls_through_to_the_column(self) -> None:
        future = self._row("2099-01-01T00:00:00+00:00")
        column_only = {**future, "metadata": {}}
        assert _keyword_score(future) == pytest.approx(_keyword_score(column_only))

    def test_and_the_column_really_was_decayed(self) -> None:
        # Without the guard this is 0.8 (undecayed) rather than 0.8 * floor.
        assert _keyword_score(self._row("2099-01-01T00:00:00+00:00")) < 0.8 * 0.5

    def test_a_past_stamp_is_still_preferred(self) -> None:
        # The guard must not reject ordinary source clocks.
        recent = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        assert _keyword_score(self._row(recent)) > _keyword_score(
            {**self._row(recent), "metadata": {}}
        )

    def test_naive_stamp_inside_tolerance_is_not_rejected(self) -> None:
        # A naive source clock written "now" in a +13 zone reads as hours in
        # the future once coerced to UTC; that must not count as hostile.
        skewed = (datetime.now(UTC) + timedelta(hours=13)).replace(tzinfo=None)
        assert self._resolved({"updated_at": skewed.isoformat()}) is not None

    def _resolved(self, bag: dict[str, Any]) -> Any:
        from trellis.retrieve.strategies import resolve_recency_stamp

        return resolve_recency_stamp(bag)

    def test_semantic_axis_gets_the_same_guard(self, ingested: dict[str, Any]) -> None:
        poisoned = {
            **ingested["vector_row"],
            "metadata": {
                **ingested["vector_row"]["metadata"],
                "updated_at": "2099-01-01T00:00:00+00:00",
            },
        }
        # Falls through to the bag's own ``created_at`` (the 2024 source
        # clock), not to an undecayed 0.8.
        assert _semantic_score(poisoned) == pytest.approx(
            _semantic_score(ingested["vector_row"]), rel=1e-3
        )


class TestResolveRecencyStamp:
    """Unit-level ordering contract of the shared resolver."""

    def _resolve(self, *args: Any) -> Any:
        from trellis.retrieve.strategies import (
            resolve_recency_stamp,
        )

        return resolve_recency_stamp(*args)

    def test_metadata_updated_at_wins(self) -> None:
        assert (
            self._resolve(
                {"updated_at": "2024-01-01", "created_at": "2023-01-01"}, "2026-01-01"
            )
            == "2024-01-01"
        )

    def test_metadata_created_at_beats_the_row_columns(self) -> None:
        assert self._resolve({"created_at": "2023-01-01"}, "2026-01-01") == "2023-01-01"

    def test_row_stamps_are_tried_in_order(self) -> None:
        assert self._resolve({}, None, "2026-01-01") == "2026-01-01"

    def test_none_when_nothing_parses(self) -> None:
        assert self._resolve({"updated_at": "nope"}, "also nope") is None

    def test_non_dict_metadata_is_tolerated(self) -> None:
        assert self._resolve(None, "2026-01-01") == "2026-01-01"

    def test_datetime_values_are_accepted(self) -> None:
        stamp = datetime(2024, 1, 1, tzinfo=UTC)
        assert self._resolve({"updated_at": stamp}) is stamp

    def test_future_candidates_are_skipped_in_order(self) -> None:
        assert (
            self._resolve(
                {"updated_at": "2099-01-01", "created_at": "2023-01-01"},
                "2026-01-01",
            )
            == "2023-01-01"
        )

    def test_none_when_every_candidate_is_future(self) -> None:
        # Same multiplier a future stamp would have produced, so this costs
        # nothing; the guard only ever prefers a non-future candidate.
        assert self._resolve({"updated_at": "2099-01-01"}, "2098-01-01") is None


class TestSemanticAxisResidual:
    """What the guard does *not* buy, pinned so it stays a known limit.

    ``resolve_recency_stamp`` falls through an unusable candidate, which only
    helps when a later one is usable. ``KeywordSearch`` always has one — a
    store row has columns. ``SemanticSearch`` passes none, because
    ``VectorStore.query`` returns ``item_id`` / ``score`` / ``metadata`` and
    no columns on every backend; its only candidates are the bag's two keys.
    ``build_vector_row`` normally supplies embed time via ``setdefault``, but
    ``setdefault`` does nothing when the key is *present and unusable*. So a
    document whose own ``created_at`` is malformed or future still reaches
    the semantic axis with nothing to fall through to and scores undecayed —
    the maximum multiplier, exactly as it would have before the guard.

    Not a regression, and not silently accepted: the fix is to return the
    vector row's ``created_at`` column from ``VectorStore.query``, which is a
    contract change across four backends. These tests fail the day someone
    makes it, which is the point.
    """

    _FLOOR = 0.3

    def test_keyword_axis_the_guard_bites(self) -> None:
        stale = (datetime.now(UTC) - timedelta(days=3650)).isoformat()
        hostile = {
            "doc_id": "d1",
            "content": "x",
            "metadata": {"updated_at": "2099-01-01T00:00:00+00:00"},
            "updated_at": stale,
        }
        # Decayed all the way to the floor off the row's own clock.
        assert _keyword_score(hostile) == pytest.approx(0.8 * self._FLOOR, rel=1e-3)

    @pytest.mark.parametrize(
        "stamp",
        ["2099-01-01T00:00:00+00:00", "last tuesday"],
        ids=["future", "garbage"],
    )
    def test_semantic_axis_has_nothing_to_fall_through_to(self, stamp: str) -> None:
        # Both keys unusable and no row stamp: undecayed, i.e. maximally
        # fresh. The honest baseline is what a real old stamp earns.
        row = {"item_id": "v1", "metadata": {"updated_at": stamp, "created_at": stamp}}
        honest = {
            "item_id": "v1",
            "metadata": {
                "created_at": (datetime.now(UTC) - timedelta(days=3650)).isoformat()
            },
        }
        assert _semantic_score(row) == pytest.approx(0.8)
        assert _semantic_score(honest) == pytest.approx(0.8 * self._FLOOR, rel=1e-3)

    def test_setdefault_cannot_rescue_a_present_but_unusable_key(self) -> None:
        # Why the residual is reachable rather than theoretical: the embed
        # hook's own stamp is a ``setdefault``, so a document carrying a
        # garbage ``created_at`` keeps it.
        from trellis.retrieve.embed_ingest_hook import build_vector_row

        row = build_vector_row(
            "d1",
            "x",
            {"created_at": "last tuesday"},
            _embed,
            created_at=datetime.now(UTC).isoformat(),
        )
        assert row["metadata"]["created_at"] == "last tuesday"
