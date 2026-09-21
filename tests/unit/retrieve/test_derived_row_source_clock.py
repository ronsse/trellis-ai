"""Derived-row producers state whose clock their rows carry.

``updated_at`` / ``created_at`` name two different facts. As store *columns*
they are the row's write clock; as *metadata keys* they are the **source's**
clock, and :func:`~trellis.retrieve.strategies.resolve_recency_stamp` (#417)
prefers the bag over the column on both document-backed axes. So a row whose
content Trellis composed out of another record has a decision to make that a
primary write does not: propagate the source's clock, or let the axis score
the derived row by the instant the composing pass happened to run.

#417 fixed that for the strategies and left one producer — chunking —
explicitly open as #463. The plan for #463 says the thing this module exists
to honour, and it is not "fix the chunk path":

    **Then sweep the roster, do not fix the one case.** Three successive
    ``updated_at`` reader lists in this repo were each wrong, and #443
    declared 3 control keys against 6 sites. Find **every** derived-row
    path, not just chunking.

The sweep found **three** derived-row producers, and they resolve three
different ways — which is the finding, and is why a patch to the chunk path
alone would have been the wrong shape of answer:

``build_vector_row``
    Already correct, and correct in the *callee*, so all three of its call
    sites inherit it.

``build_trace_metadata``
    Was wrong, fixed here. Latent — zero production rows — but the worker is
    a **backfill by design**, so its first run over an existing trace store
    would have stamped a whole history with one import instant: #417's own
    measured shape, guaranteed rather than merely possible.

``ingest_corpus.sync._write_chunks``
    **Declined, on measurement.** The stamp collapses a chunk onto exactly
    its parent's recency multiplier, after which the longer parent wins the
    tiebreak on raw relevance — and the stamped parents are the worst-cited
    class in the corpus. The full reasoning sits at the code, in
    ``_write_chunks``; the number carrying the decision is a stamped parent
    at 1 helpful citation in 219 servings.
    :class:`TestTheDeclinedDecisionIsPinned` runs a real corpus sync and
    asserts the chunk rows carry **no** clock key while their parent does, so
    a later silent "fix" fails here and sends its author to that comment.

Why the AST roster that used to live here is gone
-------------------------------------------------

This module was first written around a hand-classified roster of every
``document_store.put`` seam in ``src/`` — 27 of them, each labelled derived
or primary, with the ``preserve_updated_at=True`` literal re-asserted per
site by AST. That method died with **#569**, which routed every document
write through :func:`trellis.core.document_write.put_document`. The scan it
depended on now finds **13** sites and one of them is the seam itself; the
14 it classified individually no longer exist as distinct call sites, and
``tests/unit/core/test_document_write_rule.py`` enforces the routing that
replaced them.

Keeping a superseded roster would have been worse than deleting it: every
floor in it was already unreachable, so it could only ever have been
satisfied by lowering the floors — which is the #457 shape, a guard that
passes more easily as the population it divides by shrinks. What survives is
the half that was never a roster: the producers are **called**, and the bag
is **read**. A declared propagation that does not happen is exactly the
failure #461 found one layer over, so these assert behaviour, not prose.

The clock *disposition* of a ``put_document`` caller is no longer guarded by
anything, and that is a real gap rather than a solved problem — see #463's
follow-up. It wants a rule written against the new single seam, not a
resurrection of the old per-site one.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from trellis.ingest_corpus.models import chunk_doc_id, corpus_doc_id
from trellis.ingest_corpus.sync import sync_corpus
from trellis.retrieve.embed_ingest_hook import build_vector_row
from trellis.schemas.enums import TraceSource
from trellis.schemas.trace import Trace, TraceContext
from trellis.stores.sqlite.document import SQLiteDocumentStore
from trellis.stores.sqlite.event_log import SQLiteEventLog
from trellis.stores.sqlite.vector import SQLiteVectorStore
from trellis_workers.trace_embed.render import build_trace_metadata

#: The two metadata keys ``resolve_recency_stamp`` reads out of the bag.
CLOCK_KEYS = ("updated_at", "created_at")


def _embed(text: str) -> list[float]:
    """Deterministic 8-dim embedding — geometry is irrelevant here."""
    vector = [0.0] * 8
    for index, char in enumerate(text[:256]):
        vector[index % 8] += ord(char) % 7
    norm = sum(value * value for value in vector) ** 0.5 or 1.0
    return [value / norm for value in vector]


class TestDerivedRowsPropagateTheSourceClock:
    """The ``DERIVED_PROPAGATES`` half, exercised.

    A roster entry naming a propagation that does not happen is exactly the
    failure #461 found one layer over: a declared accept event the tool never
    emits. So the two producers are called, and the bag is read.
    """

    def test_build_vector_row_carries_the_documents_source_clock(self) -> None:
        row = build_vector_row(
            "doc-1",
            "body text",
            {"created_at": "2024-02-03T10:00:00+00:00"},
            _embed,
        )
        assert row["metadata"]["created_at"] == "2024-02-03T10:00:00+00:00"

    def test_the_bags_clock_outranks_the_row_clock_argument(self) -> None:
        """``setdefault``, and the precedence is the point.

        The argument is only ever the row's write clock; a stamp already in
        the bag is the *source's*. The propagation lives in the callee, which
        is why all three ``build_vector_row`` call sites inherit it.
        """
        row = build_vector_row(
            "doc-1",
            "body text",
            {"created_at": "2024-02-03T10:00:00+00:00"},
            _embed,
            created_at="2026-09-12T00:00:00+00:00",
        )
        assert row["metadata"]["created_at"] == "2024-02-03T10:00:00+00:00"

    def test_a_bag_with_no_clock_falls_back_to_the_row_clock(self) -> None:
        row = build_vector_row(
            "doc-1", "body text", {}, _embed, created_at="2026-09-12T00:00:00+00:00"
        )
        assert row["metadata"]["created_at"] == "2026-09-12T00:00:00+00:00"

    def test_trace_summary_metadata_carries_the_traces_clock(self) -> None:
        """#463's fixed producer.

        Latent — zero production rows — but the worker is a backfill by
        design, so its first run over an existing trace store would stamp a
        whole history with one import instant.
        """
        trace = Trace(
            trace_id="trace-abc",
            source=TraceSource.AGENT,
            intent="rebuild the index",
            context=TraceContext(domain="trellis"),
            created_at=datetime(2024, 2, 3, 10, 0, tzinfo=UTC),
        )
        metadata = build_trace_metadata(trace)
        assert metadata["created_at"] == "2024-02-03T10:00:00+00:00"

    def test_the_trace_clock_survives_the_document_metadata_seam(self) -> None:
        """``DocumentMetadata`` is the seam the stamp has to cross.

        ``created_at`` is not a core field, so it lands in ``custom`` and
        re-flattens verbatim as a top-level key — which is the key
        ``resolve_recency_stamp`` reads. A seam that started nesting it would
        break the propagation while leaving the producer's own code correct.
        """
        trace = Trace(
            trace_id="trace-abc",
            source=TraceSource.AGENT,
            intent="rebuild the index",
            context=TraceContext(domain="trellis"),
            created_at=datetime(2024, 2, 3, 10, 0, tzinfo=UTC),
        )
        metadata = build_trace_metadata(trace)
        assert "created_at" in metadata, "the stamp did not survive to top level"
        assert isinstance(metadata["created_at"], str)


@pytest.fixture
def registry(tmp_path: Path) -> MagicMock:
    reg = MagicMock()
    reg.knowledge.document_store = SQLiteDocumentStore(tmp_path / "docs.db")
    reg.knowledge.vector_store = SQLiteVectorStore(tmp_path / "vectors.db")
    reg.operational.event_log = SQLiteEventLog(tmp_path / "events.db")
    reg.embedding_fn = _embed
    return reg


@pytest.fixture
def stamped_vault(tmp_path: Path) -> Path:
    """A long note whose frontmatter carries a source clock.

    The production shape: the markdown handler passes frontmatter through
    flat and ``created_at`` is not a reserved key, so the parent row's bag
    carries the *source's* clock while the column carries the sync's.
    """
    root = tmp_path / "vault"
    root.mkdir()
    body = "\n\n".join(
        f"## Section {index}\n\n" + ("Paragraph text about the subject. " * 60).strip()
        for index in range(4)
    )
    (root / "note.md").write_text(
        '---\ntitle: Stamped Note\ncreated_at: "2024-02-03T10:00:00+00:00"\n---\n\n'
        + body
        + "\n"
    )
    return root


class TestTheDeclinedDecisionIsPinned:
    """``DERIVED_CLOCK_DECLINED``, asserted as behaviour and not as prose.

    The decision is that a chunk row carries **no** source clock, so one
    document has two ages. That is a cost, it was measured, and it was taken
    deliberately — so it is pinned here rather than left to drift. A later
    agent who adds the one key to ``_write_chunks`` fails this test, and the
    failure names the comment carrying the measurement.
    """

    def test_the_parent_carries_the_source_clock(
        self, registry: MagicMock, stamped_vault: Path
    ) -> None:
        """Guard on the fixture: without this the next assertion is vacuous."""
        sync_corpus(registry, stamped_vault, source_system="obsidian")
        parent = registry.knowledge.document_store.get(
            corpus_doc_id("obsidian", "note.md")
        )
        assert parent is not None
        assert parent["metadata"]["created_at"] == "2024-02-03T10:00:00+00:00"

    def test_chunk_rows_carry_no_source_clock(
        self, registry: MagicMock, stamped_vault: Path
    ) -> None:
        report = sync_corpus(registry, stamped_vault, source_system="obsidian")
        chunks = report.counts()["chunks_written"]
        assert chunks > 1, "fixture produced no chunks to check"

        parent_id = corpus_doc_id("obsidian", "note.md")
        store = registry.knowledge.document_store
        for index in range(chunks):
            chunk = store.get(chunk_doc_id(parent_id, index))
            assert chunk is not None
            carried = sorted(key for key in CLOCK_KEYS if key in chunk["metadata"])
            assert not carried, (
                f"chunk {index} carries {carried} — #463 declined propagating "
                "the parent's clock to chunk rows, on measurement. If that "
                "decision is being reversed, the evidence to overturn is in "
                "the NO SOURCE CLOCK comment in ingest_corpus/sync.py::"
                "_write_chunks and in docs/design/decision-ledger.md, not here."
            )
