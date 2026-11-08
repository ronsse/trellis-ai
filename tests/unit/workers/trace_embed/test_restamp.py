"""The restamp sweep: a relabelled citation reaches a summary that already has a vector.

The pass never rewrites a summary whose trace has a vector row, so a document
labelled ``confidential`` after its summary was embedded left the summary at
``internal`` and served to a caller who cannot read the document it digests (#1208).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from trellis.core.document_write import put_document
from trellis.llm.egress import EgressGate
from trellis.mutate.commands import Command, Operation
from trellis.mutate.handlers.classification import ClassificationSetHandler
from trellis.schemas.classification import DATA_CLASSIFICATION_KEY
from trellis_workers.trace_embed import run_trace_embed_pass, trace_summary_doc_id
from trellis_workers.trace_embed.handler import CITED_LABEL_CLASSIFIER
from trellis_workers.trace_embed.restamp import UNATTRIBUTED
from trellis_workers.trace_embed.watermark import TraceEmbedWatermark

from .test_egress import (
    _append_citing,
    _raise_for,
    _redact_summary,
    _served_ids,
    _store_document,
    _stored_label,
)

NOW = timedelta(0)


def _sweep(registry, watermark_path, **kwargs):
    """One pass that sweeps, whatever the clock says."""
    return run_trace_embed_pass(
        registry, watermark_path=watermark_path, restamp_interval=NOW, **kwargs
    )


def _person_sets(registry, doc_id: str, sensitivity: str) -> None:
    ClassificationSetHandler(registry).handle(
        Command(
            operation=Operation.CLASSIFICATION_SET,
            args={"doc_id": doc_id, "sensitivity": sensitivity},
            requested_by="test",
        )
    )


def _embedded_summary(registry, watermark_path, *cited: str, local: bool = False):
    """A summary that already has its vector, citing ``cited`` as labelled now.

    ``local`` declares the embedder local, so a summary of a labelled citation is
    embedded too (a cloud route withholds it, and there is no vector to outlive).
    """
    registry._embedding_config = {"local": True} if local else None
    trace = _append_citing(registry, 1, *cited)
    first = run_trace_embed_pass(registry, watermark_path=watermark_path)
    assert first.embedded == 1
    assert registry.knowledge.vector_store.get(trace_summary_doc_id(trace.trace_id))
    return trace


def test_a_summary_follows_a_citation_relabelled_after_its_vector_exists(
    registry, watermark_path, recorder
) -> None:
    _store_document(registry, "doc-a", None)
    trace = _embedded_summary(registry, watermark_path, "doc-a")
    summary_id = trace_summary_doc_id(trace.trace_id)
    assert _stored_label(registry, trace) is None
    assert summary_id in _served_ids(registry, trace, "internal")[0]

    _store_document(registry, "doc-a", "confidential")
    report = _sweep(registry, watermark_path)

    assert (report.restamp_raised, report.restamp_failed) == (1, 0)
    assert report.to_dict()["status"] == "ok"
    stored = _stored_label(registry, trace)
    assert stored["sensitivity"] == "confidential"
    assert stored["classified_by"] == [CITED_LABEL_CLASSIFIER]
    served, rejected = _served_ids(registry, trace, "internal")
    assert summary_id not in served
    assert summary_id in rejected
    assert summary_id in _served_ids(registry, trace, "confidential")[0]
    # A second sweep over the same labels writes nothing: raise only when stricter.
    again = _sweep(registry, watermark_path)
    assert (again.restamp_scanned, again.restamp_raised) == (1, 0)
    # The vector row stays (a row cannot be unsent) and carries the new label;
    # the sweep calls no embedder.
    vector = registry.knowledge.vector_store.get(summary_id)
    assert vector is not None
    assert vector["metadata"][DATA_CLASSIFICATION_KEY]["sensitivity"] == "confidential"
    # Only the pass embedded the summary (a pack query embeds its intent, not it).
    assert [t for t in recorder.texts if t.startswith("# ")] == recorder.texts[:1]


def test_a_person_raised_summary_is_never_lowered_by_a_sweep(
    registry, watermark_path, recorder
) -> None:
    _store_document(registry, "doc-a", None)
    trace = _embedded_summary(registry, watermark_path, "doc-a")
    summary_id = trace_summary_doc_id(trace.trace_id)
    _store_document(registry, "doc-a", "confidential")
    assert _sweep(registry, watermark_path).restamp_raised == 1

    _person_sets(registry, summary_id, "restricted")
    for level in ("internal", "confidential", "restricted"):
        _store_document(registry, "doc-a", level)
        report = _sweep(registry, watermark_path)
        assert (report.restamp_raised, report.restamp_failed) == (0, 0), level
        assert _stored_label(registry, trace)["sensitivity"] == "restricted", level
    assert summary_id not in _served_ids(registry, trace, "confidential")[0]


def test_a_lowered_citation_leaves_the_derived_stamp_where_it_is(
    registry, watermark_path, recorder
) -> None:
    """Raise only: over-withholding is the safe direction, and a person lowers it."""
    _store_document(registry, "doc-a", "confidential")
    trace = _embedded_summary(registry, watermark_path, "doc-a", local=True)
    _store_document(registry, "doc-a", "internal")

    report = _sweep(registry, watermark_path)

    assert (report.restamp_raised, report.restamp_failed) == (0, 0)
    assert _stored_label(registry, trace)["sensitivity"] == "confidential"


def test_a_person_label_below_the_citation_is_raised_in_place(
    registry, watermark_path, recorder
) -> None:
    _store_document(registry, "doc-a", None)
    trace = _embedded_summary(registry, watermark_path, "doc-a")
    summary_id = trace_summary_doc_id(trace.trace_id)
    _person_sets(registry, summary_id, "internal")
    before = _stored_label(registry, trace)
    assert before["classified_by"] == ["caller"]

    _store_document(registry, "doc-a", "confidential")
    report = _sweep(registry, watermark_path)

    assert (report.restamp_raised, report.restamp_failed) == (1, 0)
    after = _stored_label(registry, trace)
    assert after["sensitivity"] == "confidential"
    assert after["classified_by"] == ["caller", CITED_LABEL_CLASSIFIER]
    # And it is a person's label now: a lower citation does not take it back down.
    _store_document(registry, "doc-a", "internal")
    _sweep(registry, watermark_path)
    assert _stored_label(registry, trace)["sensitivity"] == "confidential"


def test_an_unreadable_citation_is_counted_not_raised_and_retried(
    registry, watermark_path, recorder, monkeypatch
) -> None:
    _store_document(registry, "doc-a", None)
    trace = _embedded_summary(registry, watermark_path, "doc-a")
    _store_document(registry, "doc-a", "confidential")
    before_sweep = datetime.now(UTC)
    with monkeypatch.context() as patched:
        _raise_for(patched, registry, "doc-a")
        report = _sweep(registry, watermark_path)
    assert (report.restamp_unreadable, report.restamp_raised) == (1, 0)
    assert report.to_dict()["status"] == "partial"
    assert _stored_label(registry, trace) is None
    # Marked like a clean sweep (later than the first pass's mark), so the retry
    # waits for the interval instead of making every pass a full sweep.
    assert not TraceEmbedWatermark(watermark_path).restamp_due(
        before_sweep, timedelta(0)
    )
    within = run_trace_embed_pass(registry, watermark_path=watermark_path)
    assert within.restamp_scanned == 0

    again = run_trace_embed_pass(
        registry, watermark_path=watermark_path, restamp_interval=timedelta(0)
    )

    assert (again.restamp_raised, again.restamp_unreadable) == (1, 0)
    assert _stored_label(registry, trace)["sensitivity"] == "confidential"


def _between_the_sweeps_read_and_the_write(monkeypatch, change) -> None:
    """Run ``change`` once, right after the sweep judges a summary.

    The sweep's judgement is the first ``judge_summary`` call of a pass with no
    new traces; the handler's re-judge inside the write is the second. ``change``
    lands between them.
    """
    real = EgressGate.judge_summary
    pending = [change]

    def judge_then_change(self, *args, **kwargs):
        judgement = real(self, *args, **kwargs)
        if pending:
            pending.pop()()
        return judgement

    monkeypatch.setattr(EgressGate, "judge_summary", judge_then_change)


def test_a_citation_unreadable_after_the_sweeps_read_does_not_leave_restricted(
    registry, watermark_path, recorder, monkeypatch
) -> None:
    """#1208: the sweep read ``confidential``, the write's re-read raised.

    The handler fails closed on a citation it cannot read, and a sweep only ever
    raises, so a ``restricted`` stamp written here would stay for good although the
    sweep only meant ``confidential``. The write is aborted instead: the summary
    keeps its label, the sweep counts it unreadable, and the next sweep retries.
    """
    _store_document(registry, "doc-a", None)
    trace = _embedded_summary(registry, watermark_path, "doc-a")
    _store_document(registry, "doc-a", "confidential")
    before_sweep = datetime.now(UTC)
    with monkeypatch.context() as patched:
        _between_the_sweeps_read_and_the_write(
            patched, lambda: _raise_for(patched, registry, "doc-a")
        )
        report = _sweep(registry, watermark_path)

    # The label first: it is the defect, and the counts below follow from it.
    assert _stored_label(registry, trace) is None
    assert (report.restamp_raised, report.restamp_unreadable) == (0, 1)
    assert report.restamp_failed == 0
    assert report.failures == []
    assert report.to_dict()["status"] == "partial"
    # Marked like any sweep, so the retry waits for the interval.
    assert not TraceEmbedWatermark(watermark_path).restamp_due(
        before_sweep, timedelta(0)
    )

    again = _sweep(registry, watermark_path)

    assert (again.restamp_raised, again.restamp_unreadable) == (1, 0)
    assert again.to_dict()["status"] == "ok"
    assert _stored_label(registry, trace)["sensitivity"] == "confidential"


def test_an_unreadable_write_leaves_a_label_a_person_set_alone(
    registry, watermark_path, recorder, monkeypatch
) -> None:
    _store_document(registry, "doc-a", None)
    trace = _embedded_summary(registry, watermark_path, "doc-a")
    summary_id = trace_summary_doc_id(trace.trace_id)
    _person_sets(registry, summary_id, "internal")
    _store_document(registry, "doc-a", "confidential")
    with monkeypatch.context() as patched:
        _between_the_sweeps_read_and_the_write(
            patched, lambda: _raise_for(patched, registry, "doc-a")
        )
        report = _sweep(registry, watermark_path)

    assert (report.restamp_raised, report.restamp_unreadable) == (0, 1)
    stored = _stored_label(registry, trace)
    assert stored["sensitivity"] == "internal"
    assert CITED_LABEL_CLASSIFIER not in stored["classified_by"]


def test_a_citation_raised_after_the_sweeps_read_gets_the_later_stricter_label(
    registry, watermark_path, recorder, monkeypatch
) -> None:
    """The write re-reads on purpose: a stricter label than the sweep's wins.

    The sweep read ``confidential``; by the write the citation is ``restricted``.
    The handler's read is the newer one and the label it stamps is justified by a
    citation, so it stands (the next sweep would have written the same).
    """
    _store_document(registry, "doc-a", None)
    trace = _embedded_summary(registry, watermark_path, "doc-a")
    _store_document(registry, "doc-a", "confidential")
    with monkeypatch.context() as patched:
        _between_the_sweeps_read_and_the_write(
            patched, lambda: _store_document(registry, "doc-a", "restricted")
        )
        report = _sweep(registry, watermark_path)

    assert (report.restamp_raised, report.restamp_unreadable) == (1, 0)
    assert report.to_dict()["status"] == "ok"
    stored = _stored_label(registry, trace)
    assert stored["sensitivity"] == "restricted"
    assert stored["classified_by"] == [CITED_LABEL_CLASSIFIER]


def test_a_citation_lowered_after_the_sweeps_read_keeps_the_sweeps_label(
    registry, watermark_path, recorder, monkeypatch
) -> None:
    """A lowered label is never applied by a sweep: the stale read stands.

    Over-withholding is the safe direction, and a person lowers the stamp.
    """
    _store_document(registry, "doc-a", None)
    trace = _embedded_summary(registry, watermark_path, "doc-a")
    _store_document(registry, "doc-a", "confidential")
    with monkeypatch.context() as patched:
        _between_the_sweeps_read_and_the_write(
            patched, lambda: _store_document(registry, "doc-a", None)
        )
        report = _sweep(registry, watermark_path)

    assert (report.restamp_raised, report.restamp_unreadable) == (1, 0)
    assert _stored_label(registry, trace)["sensitivity"] == "confidential"


def test_an_unreadable_citation_beside_a_withholding_one_is_still_unreadable(
    registry, watermark_path, recorder, monkeypatch
) -> None:
    """The withholding citation hides the fault in the verdict, not in the count.

    Judged by the verdict alone, the unreadable citation's ``restricted`` would be
    written, and a sweep only raises, so nobody would take it back down.
    """
    _store_document(registry, "doc-a", None)
    _store_document(registry, "doc-b", None)
    trace = _embedded_summary(registry, watermark_path, "doc-a", "doc-b")
    _store_document(registry, "doc-a", "confidential")
    with monkeypatch.context() as patched:
        _raise_for(patched, registry, "doc-b")
        report = _sweep(registry, watermark_path)
    assert (report.restamp_unreadable, report.restamp_raised) == (1, 0)
    assert _stored_label(registry, trace) is None

    again = _sweep(registry, watermark_path)

    assert (again.restamp_raised, again.restamp_unreadable) == (1, 0)
    assert _stored_label(registry, trace)["sensitivity"] == "confidential"


def test_a_label_naming_no_classifier_does_not_become_the_derived_stamp(
    registry, watermark_path, recorder
) -> None:
    """Exactly ``[trace-summary:cited]`` is what lets a later pass lower a label."""
    _store_document(registry, "doc-a", None)
    trace = _embedded_summary(registry, watermark_path, "doc-a")
    summary_id = trace_summary_doc_id(trace.trace_id)
    store = registry.knowledge.document_store
    row = store.get(summary_id)
    put_document(
        store,
        None,
        summary_id,
        row["content"],
        {
            **row["metadata"],
            DATA_CLASSIFICATION_KEY: {"sensitivity": "public", "classified_by": []},
        },
        preserve_updated_at=True,
    )
    _store_document(registry, "doc-a", "confidential")

    assert _sweep(registry, watermark_path).restamp_raised == 1

    after = _stored_label(registry, trace)
    assert after["sensitivity"] == "confidential"
    assert after["classified_by"] == [UNATTRIBUTED, CITED_LABEL_CLASSIFIER]


def test_a_dry_run_counts_the_raise_and_writes_nothing(
    registry, watermark_path, recorder
) -> None:
    _store_document(registry, "doc-a", None)
    trace = _embedded_summary(registry, watermark_path, "doc-a")
    _store_document(registry, "doc-a", "confidential")

    on_disk = watermark_path.read_text(encoding="utf-8")

    report = _sweep(registry, watermark_path, dry_run=True)

    assert report.restamp_raised == 1
    assert _stored_label(registry, trace) is None
    assert watermark_path.read_text(encoding="utf-8") == on_disk


def test_the_sweep_runs_once_per_interval_and_a_reset_forces_one(
    registry, watermark_path, recorder
) -> None:
    _store_document(registry, "doc-a", None)
    trace = _embedded_summary(registry, watermark_path, "doc-a")
    _store_document(registry, "doc-a", "confidential")

    within = run_trace_embed_pass(registry, watermark_path=watermark_path)

    assert within.restamp_scanned == 0  # the first pass swept a moment ago
    assert _stored_label(registry, trace) is None  # the window an interval bounds
    forced = run_trace_embed_pass(
        registry, watermark_path=watermark_path, reset_watermark=True
    )
    assert forced.restamp_raised == 1
    assert _stored_label(registry, trace)["sensitivity"] == "confidential"
    assert not TraceEmbedWatermark(watermark_path).restamp_due(
        datetime.now(UTC), timedelta(hours=1)
    )


def test_a_redacted_summary_is_not_brought_back(
    registry, watermark_path, recorder
) -> None:
    _store_document(registry, "doc-a", None)
    trace = _embedded_summary(registry, watermark_path, "doc-a")
    summary_id = _redact_summary(registry, trace)
    _store_document(registry, "doc-a", "confidential")

    report = _sweep(registry, watermark_path)

    assert (report.restamp_raised, report.restamp_failed) == (0, 0)
    row = registry.knowledge.document_store.get(summary_id)
    assert row is not None
    assert not row["content"]


def test_regulatory_tags_are_not_inherited_because_no_gate_reads_them(
    registry, watermark_path, recorder
) -> None:
    """The decision, pinned: a cited document's tags do not ride along.

    ``regulatory_tags`` are read by the handlers and the label proposer and by no
    retrieval, egress or auth gate, so inheriting them would change nothing a caller
    sees. Revisit this when a gate reads them.
    """
    tagged = {
        DATA_CLASSIFICATION_KEY: {
            "sensitivity": "internal",
            "regulatory_tags": ["phi"],
            "classified_by": ["test"],
        }
    }
    registry.knowledge.document_store.put("doc-a", "body", {**tagged})
    trace = _append_citing(registry, 1, "doc-a")

    run_trace_embed_pass(registry, watermark_path=watermark_path)
    _sweep(registry, watermark_path)

    assert _stored_label(registry, trace) is None


def test_the_report_names_the_sweep(registry, watermark_path, recorder) -> None:
    _store_document(registry, "doc-a", "internal")
    _embedded_summary(registry, watermark_path, "doc-a")
    payload = _sweep(registry, watermark_path).to_dict()
    assert payload["restamp_scanned"] == 1
    assert {"restamp_raised", "restamp_unreadable", "restamp_failed"} <= set(payload)
