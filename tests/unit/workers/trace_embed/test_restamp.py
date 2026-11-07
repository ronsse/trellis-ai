"""The restamp sweep: a relabelled citation reaches a summary that already has a vector.

The pass never rewrites a summary whose trace has a vector row, so a document
labelled ``confidential`` after its summary was embedded left the summary at
``internal`` and served to a caller who cannot read the document it digests (#1208).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from trellis.mutate.commands import Command, Operation
from trellis.mutate.handlers.classification import ClassificationSetHandler
from trellis.schemas.classification import DATA_CLASSIFICATION_KEY
from trellis_workers.trace_embed import run_trace_embed_pass, trace_summary_doc_id
from trellis_workers.trace_embed.handler import CITED_LABEL_CLASSIFIER
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
    with monkeypatch.context() as patched:
        _raise_for(patched, registry, "doc-a")
        report = _sweep(registry, watermark_path)
    assert (report.restamp_unreadable, report.restamp_raised) == (1, 0)
    assert report.to_dict()["status"] == "partial"
    assert _stored_label(registry, trace) is None
    # Not marked done, so the next pass sweeps again even on the default interval.
    assert TraceEmbedWatermark(watermark_path).restamp_due(
        datetime.now(UTC), timedelta(hours=1)
    )

    again = run_trace_embed_pass(registry, watermark_path=watermark_path)

    assert (again.restamp_raised, again.restamp_unreadable) == (1, 0)
    assert _stored_label(registry, trace)["sensitivity"] == "confidential"


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
