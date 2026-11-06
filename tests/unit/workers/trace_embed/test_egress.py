"""The trace-embed worker keeps labelled content off an undeclared embedder.

A trace summary carries no label of its own, so the worker judges it by the
documents its trace cites (``evidence_used``), as precedent mining does for a
cloud LLM. Those tests stage real documents in the document store and real
traces citing them. The summary's own label, which nothing sets today, is still
honoured; it is injected through ``build_trace_metadata`` (the one place the
worker builds a summary's metadata), the way a future labelled-trace source
would supply it. The assertions are made against the vector store, the document
store and the recording embedder, not against the report the worker wrote about
itself.

Egress governs what is sent, not what is stored. A summary the gate declines is
therefore written to the document store (keyword and graph search find it) and
has no vector row until a later pass embeds it.
"""

from __future__ import annotations

import pytest

from tests.redaction_tombstone import make_tombstone
from tests.unit.retrieve.test_sensitivity import label
from trellis.core.document_write import is_redaction_tombstone, put_document
from trellis.core.vector_metadata import resolve_vector_store
from trellis.llm.egress import EgressGate, EgressVerdict
from trellis.ops.write_health import summarize_backend_health
from trellis.schemas.trace import EvidenceRef
from trellis.stores.base.event_log import EventType
from trellis_workers.trace_embed import run_trace_embed_pass, trace_summary_doc_id
from trellis_workers.trace_embed import worker as worker_mod

from .conftest import make_trace, seed_traces


@pytest.fixture
def labelled(monkeypatch: pytest.MonkeyPatch):
    """Stamp every summary with a ``restricted`` label."""
    real = worker_mod.build_trace_metadata

    def stamped(trace):
        return {**real(trace), **label("restricted")}

    monkeypatch.setattr(worker_mod, "build_trace_metadata", stamped)


def _vectors(registry, traces) -> int:
    return sum(
        registry.knowledge.vector_store.get(trace_summary_doc_id(t.trace_id))
        is not None
        for t in traces
    )


def _withheld_events(registry):
    return registry.operational.event_log.get_events(
        event_type=EventType.EMBED_WITHHELD, limit=100
    )


def test_labelled_summaries_are_counted_and_not_embedded(
    registry, watermark_path, recorder, labelled
) -> None:
    traces = seed_traces(registry, 3)
    report = run_trace_embed_pass(registry, watermark_path=watermark_path)

    assert report.skipped_egress_withheld == 3
    assert report.skipped_egress_unreadable == 0
    assert report.embedded == 0
    assert report.failed == 0
    assert report.to_dict()["skipped_egress_withheld"] == 3
    assert _vectors(registry, traces) == 0
    assert recorder.texts == [], "the embedder was handed labelled content"
    assert len(_withheld_events(registry)) == 3
    # Stored, not dropped: egress governs what is sent.
    assert all(_summary_stored(registry, t) for t in traces)


def test_the_pass_advances_past_a_withheld_summary(
    registry, watermark_path, recorder, labelled
) -> None:
    """A permanent fact about what is stored must not pin the cursor forever."""
    seed_traces(registry, 3)
    first = run_trace_embed_pass(registry, watermark_path=watermark_path)
    again = run_trace_embed_pass(registry, watermark_path=watermark_path)

    assert first.to_dict()["status"] == "ok"
    assert first.watermark_after is not None
    assert again.scanned == 0
    assert recorder.texts == []


def test_the_same_summaries_are_embedded_when_the_embedder_is_local(
    registry, watermark_path, recorder, labelled
) -> None:
    registry._embedding_config = {"local": True}
    traces = seed_traces(registry, 3)
    report = run_trace_embed_pass(registry, watermark_path=watermark_path)

    assert report.skipped_egress_withheld == 0
    assert report.embedded == 3
    assert _vectors(registry, traces) == 3
    assert _withheld_events(registry) == []


def test_unlabelled_summaries_are_embedded_either_way(
    registry, watermark_path, recorder
) -> None:
    traces = seed_traces(registry, 3)
    report = run_trace_embed_pass(registry, watermark_path=watermark_path)

    assert report.skipped_egress_withheld == 0
    assert _vectors(registry, traces) == 3


def test_an_unreadable_verdict_holds_the_cursor_and_is_counted_apart(
    registry, watermark_path, recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_traces(registry, 2)
    handler_cls = worker_mod.TraceSummaryIngestHandler
    real_handle = handler_cls.handle

    def unreadable(self, command):
        # A summary is judged by its own metadata, so a store fault cannot reach
        # it today; stage the verdict a chunk-shaped id would produce.
        result = real_handle(self, command)
        self.last_embed_withheld = EgressVerdict.UNREADABLE
        return result

    monkeypatch.setattr(handler_cls, "handle", unreadable)
    report = run_trace_embed_pass(registry, watermark_path=watermark_path)

    assert report.skipped_egress_unreadable == 2
    assert report.skipped_egress_withheld == 0
    assert report.to_dict()["status"] == "partial"


# --- a trace is judged by the documents it cites ----------------------------


def _store_document(registry, doc_id: str, level: str | None) -> None:
    metadata = label(level) if level else {}
    put_document(
        registry.knowledge.document_store,
        None,
        doc_id,
        f"body of {doc_id}",
        metadata,
        preserve_updated_at=False,
    )


def _append_citing(registry, n: int, *cited: str):
    """Append trace ``n`` citing ``cited``; return it."""
    trace = make_trace(n).model_copy(
        update={"evidence_used": [EvidenceRef(evidence_id=c) for c in cited]}
    )
    registry.operational.trace_store.append(trace)
    return trace


def _summary_stored(registry, trace) -> bool:
    return (
        registry.knowledge.document_store.get(trace_summary_doc_id(trace.trace_id))
        is not None
    )


def _found_by_keyword(registry, trace) -> bool:
    """Whether keyword search over the document store returns the summary."""
    # The seeded intent is ``Investigate widget-<n>``; the number is one token.
    number = trace.trace_id.rsplit("-", 1)[-1]
    hits = registry.knowledge.document_store.search(number)
    return trace_summary_doc_id(trace.trace_id) in {h["doc_id"] for h in hits}


def test_a_trace_citing_a_restricted_document_is_not_sent_to_an_undeclared_embedder(
    registry, watermark_path, recorder
) -> None:
    _store_document(registry, "open-doc", "public")
    _store_document(registry, "secret-doc", "restricted")
    first = _append_citing(registry, 1, "open-doc")
    withheld = _append_citing(registry, 2, "open-doc", "secret-doc")
    last = _append_citing(registry, 3)

    report = run_trace_embed_pass(registry, watermark_path=watermark_path)

    assert report.skipped_egress_withheld == 1
    assert report.skipped_egress_unreadable == 0
    assert report.embedded == 2
    assert report.failed == 0
    assert report.to_dict()["status"] == "ok"
    # Asserted on the embedder and the stores, not on the report above.
    assert _vectors(registry, [first, withheld, last]) == 2
    assert (
        registry.knowledge.vector_store.get(trace_summary_doc_id(withheld.trace_id))
        is None
    )
    assert not any("widget-0002" in text for text in recorder.texts)
    assert len(recorder.texts) == 2
    # The summary of a withheld trace is stored, so keyword search finds it, and
    # it has no vector row: egress governs what is sent, not what is stored.
    assert _summary_stored(registry, withheld)
    assert _summary_stored(registry, first)
    assert _found_by_keyword(registry, withheld)
    assert len(_withheld_events(registry)) == 1
    # A permanent fact does not pin the cursor behind it.
    again = run_trace_embed_pass(registry, watermark_path=watermark_path)
    assert again.scanned == 0


def test_the_same_trace_is_embedded_when_the_embedder_is_declared_local(
    registry, watermark_path, recorder
) -> None:
    _store_document(registry, "secret-doc", "restricted")
    trace = _append_citing(registry, 1, "secret-doc")
    registry._embedding_config = {"local": True}

    report = run_trace_embed_pass(registry, watermark_path=watermark_path)

    assert report.embedded == 1
    assert report.skipped_egress_withheld == 0
    assert _vectors(registry, [trace]) == 1
    assert _withheld_events(registry) == []


@pytest.mark.parametrize("level", ["public", "internal", None])
def test_a_trace_citing_only_documents_at_or_below_internal_is_embedded(
    registry, watermark_path, recorder, level
) -> None:
    _store_document(registry, "doc-a", level)
    # A cited id with no stored row (a graph entity, say) names no label.
    trace = _append_citing(registry, 1, "doc-a", "entity-with-no-document")

    report = run_trace_embed_pass(registry, watermark_path=watermark_path)

    assert report.embedded == 1
    assert report.skipped_egress_withheld == 0
    assert report.skipped_egress_unreadable == 0
    assert _vectors(registry, [trace]) == 1


@pytest.mark.parametrize("level", ["confidential", "restricted"])
def test_both_labels_above_internal_withhold(
    registry, watermark_path, recorder, level
) -> None:
    _store_document(registry, "doc-a", level)
    trace = _append_citing(registry, 1, "doc-a")

    report = run_trace_embed_pass(registry, watermark_path=watermark_path)

    assert report.skipped_egress_withheld == 1
    assert _vectors(registry, [trace]) == 0
    assert recorder.texts == []


def _raise_for(monkeypatch, registry, bad_id: str) -> None:
    """Make the document store fail to read ``bad_id`` and nothing else."""
    store = registry.knowledge.document_store
    real_get = store.get

    def get(doc_id, *args, **kwargs):
        if doc_id == bad_id:
            msg = "document store unavailable (staged)"
            raise RuntimeError(msg)
        return real_get(doc_id, *args, **kwargs)

    monkeypatch.setattr(store, "get", get)


def test_a_cited_document_that_cannot_be_read_holds_the_cursor_and_is_retried(
    registry, watermark_path, recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = _append_citing(registry, 1, "unreadable-doc")
    after = [_append_citing(registry, 2), _append_citing(registry, 3)]
    _raise_for(monkeypatch, registry, "unreadable-doc")

    first = run_trace_embed_pass(registry, watermark_path=watermark_path)

    assert first.skipped_egress_unreadable == 1
    assert first.skipped_egress_withheld == 0
    assert first.to_dict()["status"] == "partial"
    # The rest of the batch is still attempted; the cursor is pinned before the
    # trace that could not be judged.
    assert first.embedded == 2
    assert first.watermark_after is None
    assert _vectors(registry, [broken]) == 0
    assert _vectors(registry, after) == 2
    assert not any("widget-0001" in text for text in recorder.texts)
    # Stored and findable by keyword, with no vector, until a pass can judge it.
    assert _summary_stored(registry, broken)
    assert _found_by_keyword(registry, broken)

    # Once the store answers, the next pass reaches the trace again and embeds it.
    monkeypatch.undo()
    second = run_trace_embed_pass(registry, watermark_path=watermark_path)

    assert second.embedded == 1
    assert second.skipped_existing == 2
    assert second.skipped_egress_unreadable == 0
    assert _vectors(registry, [broken]) == 1
    assert second.watermark_after is not None


def test_a_withheld_citation_wins_over_an_unreadable_one(
    registry, watermark_path, recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    _store_document(registry, "secret-doc", "restricted")
    trace = _append_citing(registry, 1, "unreadable-doc", "secret-doc")
    _raise_for(monkeypatch, registry, "unreadable-doc")

    report = run_trace_embed_pass(registry, watermark_path=watermark_path)

    assert report.skipped_egress_withheld == 1
    assert report.skipped_egress_unreadable == 0
    assert report.to_dict()["status"] == "ok"
    assert _vectors(registry, [trace]) == 0


def test_a_dry_run_asks_the_gate_and_writes_nothing(
    registry, watermark_path, recorder
) -> None:
    _store_document(registry, "secret-doc", "restricted")
    _append_citing(registry, 1, "secret-doc")
    _append_citing(registry, 2)

    report = run_trace_embed_pass(registry, watermark_path=watermark_path, dry_run=True)

    assert report.skipped_egress_withheld == 1
    assert report.embedded == 1
    assert recorder.texts == []
    assert _withheld_events(registry) == []
    assert registry.knowledge.vector_store.count() == 0


# --- a stored summary without a vector gets one on a later pass -------------


def test_a_withheld_summary_gets_its_vector_once_the_citation_is_open(
    registry, watermark_path, recorder
) -> None:
    """The cursor passed it, so a rescan is how a user gets the vector.

    The verdict was a fact about what was cited, not a fault: the pass
    advanced. Relabelling the cited document changes the fact, and
    ``--reset-watermark`` is the way to ask again.
    """
    _store_document(registry, "doc-a", "restricted")
    trace = _append_citing(registry, 1, "doc-a")

    first = run_trace_embed_pass(registry, watermark_path=watermark_path)
    assert first.skipped_egress_withheld == 1
    assert _summary_stored(registry, trace)
    assert _vectors(registry, [trace]) == 0

    # Nothing about a plain later pass changes: the cursor is past it.
    plain = run_trace_embed_pass(registry, watermark_path=watermark_path)
    assert plain.scanned == 0
    assert _vectors(registry, [trace]) == 0

    _store_document(registry, "doc-a", "internal")
    rescan = run_trace_embed_pass(
        registry, watermark_path=watermark_path, reset_watermark=True
    )

    assert rescan.embedded == 1
    assert rescan.skipped_egress_withheld == 0
    assert _vectors(registry, [trace]) == 1
    assert len(recorder.texts) == 1


def test_a_withheld_summary_gets_its_vector_once_the_embedder_is_declared_local(
    registry, watermark_path, recorder
) -> None:
    _store_document(registry, "doc-a", "restricted")
    trace = _append_citing(registry, 1, "doc-a")
    run_trace_embed_pass(registry, watermark_path=watermark_path)
    assert _vectors(registry, [trace]) == 0

    registry._embedding_config = {"local": True}
    rescan = run_trace_embed_pass(
        registry, watermark_path=watermark_path, reset_watermark=True
    )

    assert rescan.embedded == 1
    assert _vectors(registry, [trace]) == 1


# --- an unreadable verdict is counted once while the cursor is held ---------


def test_a_held_unreadable_trace_emits_one_event_across_passes(
    registry, watermark_path, recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = _append_citing(registry, 1, "unreadable-doc")
    _raise_for(monkeypatch, registry, "unreadable-doc")

    reports = [
        run_trace_embed_pass(registry, watermark_path=watermark_path) for _ in range(3)
    ]

    # Every pass still reports it and still holds the cursor ...
    assert [r.skipped_egress_unreadable for r in reports] == [1, 1, 1]
    assert all(r.watermark_after is None for r in reports)
    # ... but the event log has it once, and so does `analyze health`.
    events = _withheld_events(registry)
    assert [e.entity_id for e in events] == [trace_summary_doc_id(broken.trace_id)]
    assert events[0].payload["verdict"] == EgressVerdict.UNREADABLE.value
    health = summarize_backend_health(registry.operational.event_log, days=7)
    assert health.embed_egress is not None
    assert health.embed_egress.unreadable == 1
    assert health.embed_egress.unreadable_docs == [
        trace_summary_doc_id(broken.trace_id)
    ]


def test_an_unreadable_trace_is_reported_again_after_the_reemit_window(
    registry, watermark_path, recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deduped, not dropped: a day on, a still-held trace is on record again."""
    from datetime import timedelta

    from trellis_workers.trace_embed import handler as handler_mod

    _append_citing(registry, 1, "unreadable-doc")
    _raise_for(monkeypatch, registry, "unreadable-doc")
    run_trace_embed_pass(registry, watermark_path=watermark_path)
    # Model the event having aged out of the window.
    monkeypatch.setattr(handler_mod, "UNREADABLE_REEMIT_AFTER", timedelta(0))
    run_trace_embed_pass(registry, watermark_path=watermark_path)

    assert len(_withheld_events(registry)) == 2
    health = summarize_backend_health(registry.operational.event_log, days=7)
    assert health.embed_egress is not None
    assert health.embed_egress.unreadable == 1, "one document, however many events"


# --- precedence: a withheld own label wins over an unreadable citation ------


def test_a_withheld_own_label_wins_over_an_unreadable_citation(
    registry, watermark_path, recorder, labelled, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Withheld is a decision; unreadable is a fault. The decision is final.

    Retrying a summary whose own label forbids the embedder would hold the
    cursor behind it for good, so the label must settle the verdict before the
    cited documents are even read.
    """
    trace = _append_citing(registry, 1, "unreadable-doc")
    _raise_for(monkeypatch, registry, "unreadable-doc")

    report = run_trace_embed_pass(registry, watermark_path=watermark_path)

    assert report.skipped_egress_withheld == 1
    assert report.skipped_egress_unreadable == 0
    assert report.to_dict()["status"] == "ok"
    assert report.watermark_after is not None, "the cursor moved past it"
    assert _vectors(registry, [trace]) == 0
    assert _summary_stored(registry, trace)
    (event,) = _withheld_events(registry)
    assert event.payload["verdict"] == EgressVerdict.WITHHELD.value


def test_the_gate_settles_a_withheld_label_before_it_reads_the_trace(registry) -> None:
    """At the gate: a WITHHELD own label never opens the trace store."""
    gate = EgressGate.for_embedder(registry, registry.knowledge.document_store)

    def not_opened():
        msg = "trace store opened for a summary whose own label already decides it"
        raise AssertionError(msg)

    verdict = gate.summary_verdict(
        trace_summary_doc_id("trace-0001"), label("restricted"), not_opened
    )

    assert verdict is EgressVerdict.WITHHELD


def test_the_gate_reads_a_missing_trace_as_unreadable(registry) -> None:
    gate = EgressGate.for_embedder(registry, registry.knowledge.document_store)

    verdict = gate.summary_verdict(
        trace_summary_doc_id("no-such-trace"),
        {},
        lambda: registry.operational.trace_store,
    )

    assert verdict is EgressVerdict.UNREADABLE


# --- a redacted summary is redacted, not withheld ---------------------------


def _redact_summary(registry, trace) -> str:
    doc_id = trace_summary_doc_id(trace.trace_id)
    make_tombstone(
        registry.knowledge.document_store, resolve_vector_store(registry), doc_id
    )
    return doc_id


def test_a_redacted_summary_of_a_withheld_trace_counts_as_redacted(
    registry, watermark_path, recorder
) -> None:
    _store_document(registry, "secret-doc", "restricted")
    trace = _append_citing(registry, 1, "secret-doc")
    doc_id = _redact_summary(registry, trace)

    report = run_trace_embed_pass(registry, watermark_path=watermark_path)

    assert report.skipped_redacted == 1
    assert report.skipped_egress_withheld == 0
    assert report.to_dict()["status"] == "ok"
    assert is_redaction_tombstone(registry.knowledge.document_store.get(doc_id))
    assert _withheld_events(registry) == []
    assert recorder.texts == []


def test_a_dry_run_counts_a_redacted_summary_as_redacted(
    registry, watermark_path, recorder
) -> None:
    _store_document(registry, "secret-doc", "restricted")
    trace = _append_citing(registry, 1, "secret-doc")
    _redact_summary(registry, trace)

    report = run_trace_embed_pass(registry, watermark_path=watermark_path, dry_run=True)

    assert report.skipped_redacted == 1
    assert report.skipped_egress_withheld == 0
    assert report.embedded == 0
