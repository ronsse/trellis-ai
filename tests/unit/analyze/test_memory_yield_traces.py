"""``summarize_memory_yield``: the trace block.

Traces are the most common write and are not document rows. A trace reaches a
pack in two forms whose id names the trace: the ``trace-summary:<id>`` document
(and its vector row) the trace-embed worker writes, and the ``trace:<id>``
Activity node trace extraction mints. These tests pin that the trace block
credits a trace through exactly those two forms, by the same counting rules
as the document rows, and that no existing document-row number moves.

Summary documents are written through the worker's real handler
(``TraceSummaryIngestHandler`` behind a ``MutationExecutor``), so the creation
path the document rows give them is the one the deployed worker stamps.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from trellis.analyze.memory_yield import (
    TRACE_FORM_ACTIVITY,
    TRACE_FORM_SUMMARY,
    MemoryYieldReport,
    summarize_memory_yield,
)
from trellis.core.pack_holdout import HOLDOUT_KEY
from trellis.feedback.models import PackFeedback
from trellis.feedback.recording import record_feedback
from trellis.mutate.commands import CommandStatus
from trellis.mutate.executor import MutationExecutor
from trellis.mutate.policy_source import build_policy_gate
from trellis.schemas.enums import OutcomeStatus, TraceSource
from trellis.schemas.trace import Outcome, Trace, TraceContext
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry
from trellis_workers.trace_embed.handler import (
    REQUESTED_BY,
    TraceSummaryIngestHandler,
    build_trace_summary_command,
)
from trellis_workers.trace_embed.render import (
    build_trace_metadata,
    render_trace_summary,
    trace_summary_doc_id,
)

NOW = datetime.now(tz=UTC)


@pytest.fixture
def registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> StoreRegistry:
    data_dir = tmp_path / "data"
    stores_dir = data_dir / "stores"
    stores_dir.mkdir(parents=True)
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))
    return StoreRegistry(stores_dir=stores_dir)


def _trace(
    registry: StoreRegistry,
    trace_id: str,
    status: OutcomeStatus | None = OutcomeStatus.SUCCESS,
    *,
    age_days: float = 1,
) -> Trace:
    trace = Trace(
        trace_id=trace_id,
        source=TraceSource.AGENT,
        intent=f"invented task {trace_id}",
        outcome=Outcome(status=status, summary="invented outcome")
        if status is not None
        else None,
        context=TraceContext(agent_id="test-agent"),
        created_at=NOW - timedelta(days=age_days),
    )
    registry.operational.trace_store.append(trace)
    return trace


def _summarize(registry: StoreRegistry, trace: Trace) -> str:
    """Write the trace's summary document through the worker's own handler."""
    executor = MutationExecutor(
        event_log=registry.operational.event_log,
        handlers={"evidence.ingest": TraceSummaryIngestHandler(registry, embed="none")},
        policy_gate=build_policy_gate(registry),
    )
    doc_id = trace_summary_doc_id(trace.trace_id)
    result = executor.execute(
        build_trace_summary_command(
            doc_id=doc_id,
            trace_id=trace.trace_id,
            content=render_trace_summary(trace),
            metadata=build_trace_metadata(trace),
            created_at=trace.created_at.isoformat(),
            embed_mode="none",
        )
    )
    assert result.status == CommandStatus.SUCCESS, result.message
    return doc_id


def _pack(
    registry: StoreRegistry,
    pack_id: str,
    items: list[tuple[str, str]],
    *,
    holdout: bool = False,
    sectioned: bool = False,
) -> None:
    payload: dict[str, Any] = {
        "injected_items": [
            {
                "item_id": item_id,
                "item_type": item_type,
                "estimated_tokens": 10,
                "rank": rank,
                **({"section": "main"} if sectioned else {}),
            }
            for rank, (item_id, item_type) in enumerate(items)
        ]
    }
    if sectioned:
        payload["sections"] = [{"name": "main"}]
    if holdout:
        payload[HOLDOUT_KEY] = True
    registry.operational.event_log.emit(
        EventType.PACK_ASSEMBLED,
        source="test",
        entity_id=pack_id,
        entity_type="pack",
        payload=payload,
    )


def _grade(
    registry: StoreRegistry,
    tmp_path: Path,
    pack_id: str,
    *,
    helpful: tuple[str, ...] = (),
    unhelpful: tuple[str, ...] = (),
) -> None:
    feedback = PackFeedback.from_agent_signal(
        run_id=f"run-{pack_id}-{len(helpful)}-{len(unhelpful)}",
        rating=0.9 if helpful else 0.1,
        helpful_item_ids=helpful,
        unhelpful_item_ids=unhelpful,
        pack_id=pack_id,
    )
    record_feedback(
        feedback,
        log_dir=tmp_path / "feedback",
        event_log=registry.operational.event_log,
        pack_id=pack_id,
    )


def _report(registry: StoreRegistry, *, traces: bool = True) -> MemoryYieldReport:
    return summarize_memory_yield(
        registry.knowledge.document_store,
        registry.operational.event_log,
        trace_store=registry.operational.trace_store if traces else None,
        days=30,
    )


def _seed(registry: StoreRegistry, tmp_path: Path) -> None:
    """One trace per outcome the block must distinguish.

    t-never: recorded, never served in any form.
    t-sum: served as its summary document, graded helpful.
    t-node: served as its Activity node, graded unhelpful.
    t-mixed: served as both forms in one pack; summary helpful, node unhelpful.
    t-ungraded: served as its summary's vector row, never graded.
    t-held: served only in a held-out pack (graded there): never served here.
    t-old: recorded before the window; summary and node served inside it.
    """
    _trace(registry, "t-never")
    s_sum = _summarize(registry, _trace(registry, "t-sum"))
    _trace(registry, "t-node", OutcomeStatus.FAILURE)
    s_mixed = _summarize(registry, _trace(registry, "t-mixed", OutcomeStatus.FAILURE))
    s_ungraded = _summarize(registry, _trace(registry, "t-ungraded", None))
    _trace(registry, "t-held", OutcomeStatus.PARTIAL)
    s_old = _summarize(registry, _trace(registry, "t-old", age_days=60))

    _pack(
        registry,
        "p1",
        [(s_sum, "document"), ("trace:t-node", "entity"), (s_ungraded, "vector")],
    )
    _grade(registry, tmp_path, "p1", helpful=(s_sum,), unhelpful=("trace:t-node",))
    _pack(
        registry,
        "p2",
        [(s_mixed, "document"), ("trace:t-mixed", "entity")],
        sectioned=True,
    )
    _grade(registry, tmp_path, "p2", helpful=(s_mixed,), unhelpful=("trace:t-mixed",))
    _pack(registry, "p-held", [("trace:t-held", "entity")], holdout=True)
    _grade(registry, tmp_path, "p-held", helpful=("trace:t-held",))
    _pack(registry, "p-old", [(s_old, "document"), ("trace:t-old", "entity")])


class TestTraceBlock:
    def test_outcomes_partition_the_recorded_traces(
        self, registry: StoreRegistry, tmp_path: Path
    ) -> None:
        _seed(registry, tmp_path)
        t = _report(registry).traces
        assert t is not None
        totals = t.totals
        # t-old is out of the window; the other six are in it.
        assert totals.recorded == 6
        assert (totals.served, totals.never_served) == (4, 2)
        # p1: 3, p2: 2. p-held dropped, p-old out of scope.
        assert totals.servings == 5
        assert (totals.graded_helpful, totals.graded_unhelpful) == (2, 2)
        assert (totals.helpful_servings, totals.unhelpful_servings) == (2, 2)
        assert (totals.mixed, totals.served_ungraded) == (1, 1)

    def test_servings_are_counted_per_form(
        self, registry: StoreRegistry, tmp_path: Path
    ) -> None:
        _seed(registry, tmp_path)
        t = _report(registry).traces
        assert t is not None
        assert t.servings_by_form == {TRACE_FORM_ACTIVITY: 2, TRACE_FORM_SUMMARY: 3}

    def test_a_pack_serving_one_trace_twice_counts_two_servings(
        self, registry: StoreRegistry, tmp_path: Path
    ) -> None:
        trace = _trace(registry, "t-twice")
        doc_id = _summarize(registry, trace)
        _pack(registry, "p", [(doc_id, "document"), ("trace:t-twice", "entity")])
        t = _report(registry).traces
        assert t is not None
        assert (t.totals.served, t.totals.servings) == (1, 2)

    def test_broken_down_by_outcome_status(
        self, registry: StoreRegistry, tmp_path: Path
    ) -> None:
        _seed(registry, tmp_path)
        t = _report(registry).traces
        assert t is not None
        rows = {row.outcome: row for row in t.by_outcome}
        assert set(rows) == {"success", "failure", "other"}
        # success: t-never, t-sum. failure: t-node, t-mixed.
        # other: t-ungraded (no outcome), t-held (partial).
        assert (rows["success"].recorded, rows["success"].served) == (2, 1)
        assert (rows["failure"].recorded, rows["failure"].served) == (2, 2)
        assert rows["failure"].graded_unhelpful == 2
        assert (rows["other"].recorded, rows["other"].served) == (2, 1)
        assert sum(r.recorded for r in t.by_outcome) == t.totals.recorded
        assert sum(r.servings for r in t.by_outcome) == t.totals.servings

    def test_a_held_out_pack_credits_nothing(
        self, registry: StoreRegistry, tmp_path: Path
    ) -> None:
        _trace(registry, "t-held")
        _pack(registry, "p-held", [("trace:t-held", "entity")], holdout=True)
        _grade(registry, tmp_path, "p-held", helpful=("trace:t-held",))
        report = _report(registry)
        assert report.traces is not None
        assert (report.traces.totals.served, report.traces.totals.never_served) == (
            0,
            1,
        )
        assert report.limits.holdout_packs_dropped == 1

    def test_a_trace_before_the_window_is_out_of_scope(
        self, registry: StoreRegistry, tmp_path: Path
    ) -> None:
        old = _trace(registry, "t-old", age_days=60)
        doc_id = _summarize(registry, old)
        _pack(registry, "p", [(doc_id, "document"), ("trace:t-old", "entity")])
        report = _report(registry)
        assert report.traces is not None
        assert report.traces.totals.recorded == 0
        assert report.traces.totals.servings == 0
        # Its summary row was written inside the window, so the document rows
        # own that serving as before; the node serving stays unattributed.
        assert report.totals.servings == 1
        assert report.limits.unattributed_servings == 1

    def test_no_trace_store_reports_no_block(
        self, registry: StoreRegistry, tmp_path: Path
    ) -> None:
        _seed(registry, tmp_path)
        assert _report(registry, traces=False).traces is None

    def test_a_truncated_trace_scan_is_reported(
        self, registry: StoreRegistry, tmp_path: Path
    ) -> None:
        for n in range(3):
            _trace(registry, f"t-{n}", age_days=1 + n)
        report = summarize_memory_yield(
            registry.knowledge.document_store,
            registry.operational.event_log,
            trace_store=registry.operational.trace_store,
            days=30,
            limit=2,
        )
        assert report.traces is not None
        assert report.traces.truncated is True
        assert report.traces.totals.recorded == 2
        assert report.traces.scan_limit == 2


class TestExistingRowsUnchanged:
    def test_document_rows_do_not_move(
        self, registry: StoreRegistry, tmp_path: Path
    ) -> None:
        _seed(registry, tmp_path)
        with_traces = _report(registry)
        without = _report(registry, traces=False)
        assert with_traces.paths == without.paths
        assert with_traces.totals == without.totals
        assert with_traces.scan == without.scan
        changed = {
            name
            for name in type(without.limits).model_fields
            if getattr(with_traces.limits, name) != getattr(without.limits, name)
        }
        assert changed <= {
            "unattributed_servings",
            "unattributed_servings_by_item_type",
        }

    def test_the_summary_document_row_owns_its_serving(
        self, registry: StoreRegistry, tmp_path: Path
    ) -> None:
        _seed(registry, tmp_path)
        report = _report(registry)
        rows = {row.path: row for row in report.paths}
        # Four summaries, all written in the window by the worker, under its
        # own requested_by: the document rows counted them before this block.
        worker = rows[REQUESTED_BY]
        assert (worker.created, worker.servings) == (4, 4)
        assert report.traces is not None
        # Three of those servings are of in-window traces: the trace block
        # names them as already counted under the document rows.
        assert report.traces.servings_counted_as_documents == 3

    def test_unattributed_falls_by_exactly_the_attributed_servings(
        self, registry: StoreRegistry, tmp_path: Path
    ) -> None:
        _seed(registry, tmp_path)
        with_traces = _report(registry)
        without = _report(registry, traces=False)
        assert with_traces.traces is not None
        newly = (
            with_traces.traces.totals.servings
            - with_traces.traces.servings_counted_as_documents
        )
        # trace:t-node, trace:t-mixed. trace:t-old is out of scope.
        assert newly == 2
        assert (
            without.limits.unattributed_servings
            - with_traces.limits.unattributed_servings
            == newly
        )
        assert without.limits.unattributed_servings_by_item_type == {"entity": 3}
        assert with_traces.limits.unattributed_servings_by_item_type == {"entity": 1}
