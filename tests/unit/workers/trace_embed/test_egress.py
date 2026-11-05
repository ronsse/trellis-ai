"""The trace-embed worker keeps labelled summaries off an undeclared embedder.

A trace summary carries no label of its own today, so the label is injected
through ``build_trace_metadata`` (the one place the worker builds a summary's
metadata), the way a future labelled-trace source would supply it. The
assertions are made against the vector store and the recording embedder, not
against the report the worker wrote about itself.
"""

from __future__ import annotations

import pytest

from tests.unit.retrieve.test_sensitivity import label
from trellis.llm.egress import EgressVerdict
from trellis.stores.base.event_log import EventType
from trellis_workers.trace_embed import run_trace_embed_pass, trace_summary_doc_id
from trellis_workers.trace_embed import worker as worker_mod

from .conftest import seed_traces


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
