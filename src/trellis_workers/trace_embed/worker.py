"""One trace-embed pass — collect, render, embed, advance.

Why a batch worker and not a write-path hook: it covers the backlog as well as
new writes, it adds no latency to the auto-capture path (which has already had
one fragility outage), and it never touches the immutable trace record.

The pass in four steps:

1. **Resolve the embedder and vector store, or refuse.** Following
   ``trellis admin reindex-vectors``: invoking the command is the opt-in, so
   no feature flag is required, but a missing embedder or vector store exits
   loudly instead of reporting a clean pass over zero rows.
2. **Collect candidates.** Everything at or after the watermark, walked
   backwards from now via :meth:`TraceStore.query`'s ``until`` (the ABC's
   ``query`` returns newest-first and takes no offset, so backwards is the
   only direction that pages without gaps), then sorted **ascending**.
3. **Process ascending, advancing a contiguous prefix.** Each trace is either
   already embedded (skipped), embedded now (success), or failed. The cursor
   follows the run of confirmed successes from the start and stops at the
   first failure — while the rest of the batch is still attempted, so one bad
   trace does not stall the pass.
4. **Save the cursor.** Never in a dry run, and never past a failure.

The failure this design is built against is a tracking gap that skips rows in
silence. Two independent things prevent it. The cursor is an optimisation, not
a record of work: :func:`trace_is_embedded` asks the vector store directly, so
a cursor that somehow ran ahead can only cause a *re-check*, never a skip, on
any trace the pass still looks at. And the cursor cannot run ahead in the
first place, because it only ever advances through confirmed successes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from trellis.mutate.commands import CommandStatus
from trellis.mutate.executor import MutationExecutor
from trellis_workers.trace_embed.handler import (
    TraceSummaryIngestHandler,
    build_trace_summary_command,
)
from trellis_workers.trace_embed.render import (
    build_trace_metadata,
    render_trace_summary,
    trace_summary_doc_id,
)
from trellis_workers.trace_embed.watermark import TraceCursor, TraceEmbedWatermark

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from trellis.schemas.trace import Trace
    from trellis.stores.base.trace import TraceStore
    from trellis.stores.base.vector import VectorStore
    from trellis.stores.registry import StoreRegistry

logger = structlog.get_logger(__name__)

__all__ = [
    "DEFAULT_MAX_SCAN",
    "DEFAULT_PAGE_SIZE",
    "ENV_WATERMARK",
    "SCAN_CEILING_FACTOR",
    "TraceEmbedReport",
    "TraceEmbedScanLimitError",
    "TraceEmbedUnavailableError",
    "collect_candidates",
    "default_watermark_path",
    "run_trace_embed_pass",
    "trace_is_embedded",
]

#: Traces fetched per :meth:`TraceStore.query` round trip while collecting.
DEFAULT_PAGE_SIZE = 200

#: Ceiling on how many traces one pass will *process*. Bounds a first run over
#: a large backlog; the cursor advances each pass, so repeated runs converge.
#: It is not a ceiling on reads — see :func:`collect_candidates`.
DEFAULT_MAX_SCAN = 5000

#: Multiple of ``max_scan`` this worker will read in one pass before refusing.
#: The oldest-first slice requires reading the whole range, so an unanticipated
#: backlog has to raise; quietly reading less is how rows go missing.
SCAN_CEILING_FACTOR = 20

ENV_WATERMARK = "TRELLIS_TRACE_EMBED_WATERMARK"


class TraceEmbedUnavailableError(RuntimeError):
    """No embedder or no vector store — the pass would be a silent no-op."""


class TraceEmbedScanLimitError(RuntimeError):
    """Too many traces to read in one pass to slice the oldest end soundly."""


@dataclass
class TraceEmbedReport:
    """What one pass did. Every count is a decision the pass actually made."""

    scanned: int = 0
    embedded: int = 0
    skipped_existing: int = 0
    skipped_empty: int = 0
    failed: int = 0
    stopped_early: bool = False
    #: More traces remained after ``max_scan`` cut this pass. Reported, not
    #: logged only: a scheduled pass that silently leaves a backlog behind
    #: looks identical to one that finished.
    more_remaining: bool = False
    dry_run: bool = False
    watermark_before: str | None = None
    watermark_after: str | None = None
    failures: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "ok" if self.failed == 0 else "partial",
            "scanned": self.scanned,
            "embedded": self.embedded,
            "skipped_existing": self.skipped_existing,
            "skipped_empty": self.skipped_empty,
            "failed": self.failed,
            "stopped_early": self.stopped_early,
            "more_remaining": self.more_remaining,
            "dry_run": self.dry_run,
            "watermark_before": self.watermark_before,
            "watermark_after": self.watermark_after,
            "failures": self.failures,
        }


def default_watermark_path() -> Path:
    """``$TRELLIS_TRACE_EMBED_WATERMARK``, else ``<config dir>/…``."""
    override = os.environ.get(ENV_WATERMARK, "").strip()
    if override:
        return Path(override)
    config_dir = Path(
        os.environ.get("TRELLIS_CONFIG_DIR", str(Path.home() / ".trellis"))
    )
    return config_dir / "trace-embed-watermark.json"


def trace_is_embedded(vector_store: VectorStore, trace_id: str) -> bool:
    """Whether *trace_id*'s summary already has a vector row.

    **This is the authority on what has been done**, not the watermark. It is
    the vector row rather than the document row on purpose: the document is
    written first and the embed can fail after it, so a document-existence
    check would report a trace done that no semantic query can reach — the
    exact silent skip this worker exists to remove.
    """
    return vector_store.get(trace_summary_doc_id(trace_id)) is not None


def _cursor_for(trace: Trace) -> TraceCursor:
    return TraceCursor(created_at=trace.created_at, trace_id=trace.trace_id)


def collect_candidates(
    trace_store: TraceStore,
    *,
    after: TraceCursor | None,
    page_size: int = DEFAULT_PAGE_SIZE,
    max_scan: int = DEFAULT_MAX_SCAN,
    now: datetime | None = None,
) -> tuple[list[Trace], bool]:
    """Every trace at or after *after*, oldest first.

    Returns ``(traces, truncated)``. ``truncated`` is ``True`` when there were
    more than ``max_scan`` traces to do, so this pass carries the oldest
    ``max_scan`` of them and the next one picks up where it stopped.

    **``max_scan`` bounds the work, not the walk**, and the difference is a
    silently-skipped row. The walk goes *backwards*, so stopping it early
    yields the **newest** ``max_scan`` traces — and the driver, which advances
    its cursor through the run it was handed, would then jump the cursor over
    every older trace that was never collected. Those rows are skipped
    permanently, and the pass reports ``status: ok``. So the walk always runs
    to the cursor and the *slice* is taken from the oldest end.

    :meth:`TraceStore.query` returns newest-first and has no offset, so paging
    forwards from the cursor would silently drop everything between the cursor
    and the newest page. This walks backwards with ``until`` instead — safe
    against an append-only store, since anything written during the walk is
    newer than where it started and is picked up next pass.

    ``until`` is **inclusive**, so each step re-reads the rows sharing the
    previous page's oldest timestamp; ``seen`` dedups them. That re-read is
    what makes ties safe, and it is also the one way this walk can stall: a
    cluster of traces sharing a timestamp that is at least as large as the
    page makes a page of pure duplicates and no progress. The page size is
    doubled in that case until the cluster fits and the walk reaches strictly
    older rows. Stalling silently here would drop every trace older than the
    cluster — which is a skip, not a slowdown.

    Raises:
        TraceEmbedScanLimitError: more than ``max_scan * SCAN_CEILING_FACTOR``
            traces sit after the cursor. Reading the whole range is what makes
            the oldest-first slice sound, so there is no correct way to give
            up quietly here: the pass says so and stops.
    """
    since = after.created_at if after is not None else None
    upper: datetime | None = now or datetime.now(UTC)
    seen: dict[str, Trace] = {}
    ceiling = max_scan * SCAN_CEILING_FACTOR
    window = max(1, page_size)

    while True:
        page = trace_store.query(since=since, until=upper, limit=window)
        if not page:
            break
        fresh = [t for t in page if t.trace_id not in seen]
        for trace in fresh:
            seen[trace.trace_id] = trace
        if len(seen) > ceiling:
            msg = (
                f"more than {ceiling} traces sit after the cursor "
                f"(max_scan={max_scan}). The oldest-first slice is only sound "
                "if the whole range is read, so this pass refuses rather than "
                "advancing its cursor over rows it never looked at. Raise "
                "--max-scan, or run once with --limit to drain the backlog."
            )
            raise TraceEmbedScanLimitError(msg)
        if len(page) < window:
            # A short page is the whole remainder at or before ``upper``.
            break
        if not fresh:
            # Every row in a full page was already seen: the tie cluster at
            # ``upper`` is at least ``window`` wide. Widen and retry the same
            # position rather than stepping over the rest of the cluster.
            window *= 2
            continue
        upper = min(t.created_at for t in page)

    ordered = sorted(seen.values(), key=lambda t: (t.created_at, t.trace_id))
    if after is not None:
        # ``since`` is inclusive, so the cursor's own trace comes back. Drop
        # everything at or before it — it is confirmed done by definition.
        ordered = [t for t in ordered if _cursor_for(t) > after]
    truncated = len(ordered) > max_scan
    # From the OLDEST end. Slicing the newest would hand the driver a run that
    # does not start at the cursor, and the cursor would leap the gap.
    return ordered[:max_scan], truncated


def run_trace_embed_pass(
    registry: StoreRegistry,
    *,
    watermark_path: Path | None = None,
    limit: int = 0,
    page_size: int = DEFAULT_PAGE_SIZE,
    max_scan: int = DEFAULT_MAX_SCAN,
    dry_run: bool = False,
    reset_watermark: bool = False,
    include_step_errors: bool = True,
    should_continue: Callable[[], bool] | None = None,
) -> TraceEmbedReport:
    """Run one pass. See the module docstring for the ordering guarantees.

    Args:
        registry: The active :class:`StoreRegistry`.
        watermark_path: Cursor file. Defaults to
            :func:`default_watermark_path`.
        limit: Stop after considering this many traces (0 = no limit).
        page_size: Traces per :meth:`TraceStore.query` round trip.
        max_scan: Ceiling on traces collected in one pass.
        dry_run: Count what would be embedded; write nothing and leave the
            cursor exactly where it was.
        reset_watermark: Forget the cursor first and re-scan from the start.
            Costs time, never rows — every embedded trace is skipped by
            :func:`trace_is_embedded`.
        include_step_errors: Render recorded step errors into the summary.
        should_continue: Cooperative stop. Checked before each trace; when it
            returns ``False`` the pass stops, saves the contiguous prefix it
            reached, and reports ``stopped_early``. This is what a SIGINT
            handler drives — an interrupted pass ends at a known-good cursor
            instead of a guessed one.

    Raises:
        TraceEmbedUnavailableError: no embedder, or no vector store.
    """
    try:
        embedding_fn = registry.embedding_fn
    except Exception as exc:  # pragma: no cover - config-shaped failure
        msg = f"could not resolve the embedder: {exc}"
        raise TraceEmbedUnavailableError(msg) from exc
    vector_store = getattr(registry.knowledge, "vector_store", None)
    if embedding_fn is None or vector_store is None:
        missing = []
        if embedding_fn is None:
            missing.append(
                "embeddings config (embeddings: block or TRELLIS_EMBEDDING_FN)"
            )
        if vector_store is None:
            missing.append("vector store")
        msg = (
            f"embed-traces requires: {', '.join(missing)}. Without it the pass "
            "would report a clean run having made no trace retrievable."
        )
        raise TraceEmbedUnavailableError(msg)

    path = watermark_path or default_watermark_path()
    watermark = TraceEmbedWatermark(path)
    if reset_watermark:
        watermark.reset()
    before = watermark.cursor

    report = TraceEmbedReport(
        dry_run=dry_run,
        watermark_before=before.created_at.isoformat() if before else None,
    )

    candidates, truncated = collect_candidates(
        registry.operational.trace_store,
        after=before,
        page_size=page_size,
        max_scan=max_scan,
    )
    if limit > 0:
        if len(candidates) > limit:
            truncated = True
        candidates = candidates[:limit]
    report.more_remaining = truncated

    executor = MutationExecutor(
        event_log=registry.operational.event_log,
        handlers={
            "evidence.ingest": TraceSummaryIngestHandler(registry, embedding_fn),
        },
    )

    frontier: TraceCursor | None = before
    contiguous = True

    for trace in candidates:
        if should_continue is not None and not should_continue():
            report.stopped_early = True
            break
        report.scanned += 1
        outcome = _process_one(
            trace,
            executor=executor,
            vector_store=vector_store,
            dry_run=dry_run,
            include_step_errors=include_step_errors,
            report=report,
        )
        if outcome and contiguous:
            frontier = _cursor_for(trace)
        elif not outcome:
            # Stop advancing here — but keep going, so one unembeddable trace
            # does not stall every newer one. The cursor stays pinned behind
            # this trace and the next pass reaches it again.
            contiguous = False

    if not dry_run and frontier is not None and frontier != before:
        watermark.advance_to(frontier)
        watermark.save()
    report.watermark_after = (
        watermark.cursor.created_at.isoformat() if watermark.cursor else None
    )

    logger.info(
        "trace_embed_pass_completed",
        **{k: v for k, v in report.to_dict().items() if k != "failures"},
    )
    return report


def _process_one(
    trace: Trace,
    *,
    executor: MutationExecutor,
    vector_store: VectorStore,
    dry_run: bool,
    include_step_errors: bool,
    report: TraceEmbedReport,
) -> bool:
    """Handle one trace. ``True`` iff it ends the pass confirmed embedded.

    A dry run returns ``False`` for anything it *would* have written: nothing
    was confirmed, so the cursor must not move past it.
    """
    if trace_is_embedded(vector_store, trace.trace_id):
        report.skipped_existing += 1
        return True

    content = render_trace_summary(trace, include_step_errors=include_step_errors)
    if not content.strip():
        # A trace with no intent should not have validated. Count it rather
        # than crashing the pass, and refuse to advance past it — a row this
        # worker cannot render is a row it has not handled.
        report.skipped_empty += 1
        report.failures.append(
            {"trace_id": trace.trace_id, "error": "rendered summary was empty"}
        )
        return False

    if dry_run:
        report.embedded += 1
        return False

    result = executor.execute(
        build_trace_summary_command(
            doc_id=trace_summary_doc_id(trace.trace_id),
            trace_id=trace.trace_id,
            content=content,
            metadata=build_trace_metadata(trace),
            created_at=trace.created_at.isoformat(),
        )
    )
    if result.status is not CommandStatus.SUCCESS:
        report.failed += 1
        report.failures.append(
            {
                "trace_id": trace.trace_id,
                "status": result.status.value,
                "error": result.message,
            }
        )
        logger.warning(
            "trace_embed_command_failed",
            trace_id=trace.trace_id,
            status=result.status.value,
            message=result.message,
        )
        return False

    report.embedded += 1
    return True


def summarize(
    reports: Sequence[TraceEmbedReport],
) -> dict[str, Any]:  # pragma: no cover
    """Fold several passes into one dict (loop mode)."""
    total = TraceEmbedReport()
    for r in reports:
        total.scanned += r.scanned
        total.embedded += r.embedded
        total.skipped_existing += r.skipped_existing
        total.skipped_empty += r.skipped_empty
        total.failed += r.failed
        total.failures.extend(r.failures)
    return total.to_dict()
