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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from trellis.core.document_write import (
    TOMBSTONE_OVERWRITE_CODE,
    is_redaction_tombstone,
)
from trellis.core.text_safety import contains_nul_or_lone_surrogate
from trellis.ingest_corpus.models import is_chunk_doc_id
from trellis.llm.egress import EgressGate, EgressVerdict
from trellis.mutate.commands import CommandStatus
from trellis.mutate.executor import MutationExecutor
from trellis.mutate.policy_source import build_policy_gate
from trellis.stores.base.trace import as_utc
from trellis_workers.trace_embed.handler import (
    TraceSummaryIngestHandler,
    build_trace_summary_command,
)
from trellis_workers.trace_embed.render import (
    build_trace_metadata,
    render_trace_summary,
    trace_summary_doc_id,
)
from trellis_workers.trace_embed.restamp import restamp_summaries
from trellis_workers.trace_embed.watermark import TraceCursor, TraceEmbedWatermark

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from trellis.mutate.executor import PolicyGate
    from trellis.schemas.trace import Trace
    from trellis.stores.base.document import DocumentStore
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

#: How long a clean restamp sweep (:mod:`~trellis_workers.trace_embed.restamp`)
#: stands in for the next. It bounds how long a summary can outlive a relabel of a
#: document it cites; ``--reset-watermark`` forces one at once.
RESTAMP_INTERVAL = timedelta(hours=1)

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
    #: Rows classified permanently invalid before anything is rendered or
    #: executed (e.g. a NUL byte or lone surrogate already stored in the
    #: trace id) rather than a transient failure. Counted separately from
    #: ``failed`` because the pass advances past these -- see
    #: ``_process_one``: the same hazard rejects the row identically on
    #: every retry, so holding the watermark behind it forever would
    #: re-reject the same row every pass without ever making progress.
    skipped_invalid: int = 0
    #: Rows whose summary id was redacted (#1034): the write seam refuses an
    #: unflagged write over a redaction tombstone, and this worker carries no
    #: re-ingest flag because bringing a redacted id back is the owner's
    #: decision. The refusal is the same on every retry, so the pass advances
    #: past the row (see ``_process_one``) instead of pinning the watermark.
    #: Not part of ``status`` or the CLI exit code: a redaction honoured is
    #: the pass doing its job, not a gap in it.
    skipped_redacted: int = 0
    #: Summaries the egress gate kept off the embedder (``egress_withheld``) on a
    #: deployment whose ``embeddings.local`` is not declared true: a trace that
    #: cites a document labelled above ``internal``, or a summary that itself
    #: carries such a label. A permanent fact about what is stored, so the pass
    #: advances past it and it does not make ``status`` partial, as for
    #: ``skipped_redacted``. The summary document is stored all the same, with
    #: no vector row: egress governs what is sent, not what is stored.
    skipped_egress_withheld: int = 0
    #: Summaries kept off the embedder because a store could not be asked for a
    #: label (``egress_unreadable``), a cited document's included. A fault a later
    #: pass may clear: the cursor holds behind it and ``status`` is ``partial``.
    #: The summary is stored without a vector row, and the retry writes the
    #: same document again and embeds it.
    skipped_egress_unreadable: int = 0
    failed: int = 0
    #: The restamp sweep (:mod:`~trellis_workers.trace_embed.restamp`), which
    #: runs at most once per ``restamp_interval``: summaries judged, summaries
    #: raised to a relabelled citation's label (in a dry run, that would be), those
    #: left alone because a citation could not be read (a fault; ``status`` is
    #: ``partial``), and raises the executor refused (the same).
    restamp_scanned: int = 0
    restamp_raised: int = 0
    restamp_unreadable: int = 0
    restamp_failed: int = 0
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
            # ``skipped_empty`` joins ``failed``/``skipped_invalid`` here
            # because the CLI already exits non-zero for it alone
            # (src/trellis_cli/worker.py) -- a pass that left a trace
            # unreachable reporting "ok" next to that exit code is the
            # exact disagreement between payload and process result this
            # worker exists to remove.
            "status": (
                "partial"
                if self.failed
                or self.skipped_invalid
                or self.skipped_empty
                or self.skipped_egress_unreadable
                or self.restamp_unreadable
                or self.restamp_failed
                else "ok"
            ),
            "scanned": self.scanned,
            "embedded": self.embedded,
            "skipped_existing": self.skipped_existing,
            "skipped_empty": self.skipped_empty,
            "skipped_invalid": self.skipped_invalid,
            "skipped_redacted": self.skipped_redacted,
            "skipped_egress_withheld": self.skipped_egress_withheld,
            "skipped_egress_unreadable": self.skipped_egress_unreadable,
            "failed": self.failed,
            "restamp_scanned": self.restamp_scanned,
            "restamp_raised": self.restamp_raised,
            "restamp_unreadable": self.restamp_unreadable,
            "restamp_failed": self.restamp_failed,
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
        upper = min(as_utc(t.created_at) for t in page)

    # By instant: a trace read back keeps the ``created_at`` it was appended
    # with, naive or offset, and a naive one does not compare with an aware one.
    ordered = sorted(seen.values(), key=lambda t: (as_utc(t.created_at), t.trace_id))
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
    restamp_interval: timedelta = RESTAMP_INTERVAL,
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
        restamp_interval: Run the restamp sweep when none has finished
            within this long (always after ``reset_watermark``). A sweep raises a
            stored summary to the label a cited document now carries; the pass
            above never rewrites a summary that already has a vector.

    Raises:
        TraceEmbedUnavailableError: no embedder, or no vector store.
        ConfigError: the deployment's policy file exists and cannot be
            loaded. Raised before anything is read or written — see the
            gate build below.
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

    # Stage 2, on this path too (#424). This worker built its executor
    # directly, without a gate, so ``evidence.ingest`` ran ungoverned
    # *regardless of what policies.json declared* — the one write path that
    # did not route through ``build_curate_executor``. A policy that fires
    # almost everywhere is worse than none: it is indistinguishable from one
    # that fires everywhere until the moment it matters.
    #
    # Built **here**, above the trace scan, not beside the executor below.
    # ``build_policy_gate`` raises ``ConfigError`` on a damaged policy file,
    # and ``collect_candidates`` reads up to ``max_scan *
    # SCAN_CEILING_FACTOR`` rows before the executor is constructed — so
    # inline, the pass would walk the whole backlog and only then discover
    # it could not have written any of it. Refuse before the work: the same
    # shape ``run_advisory_fitness_loop`` uses for ``refuse_if_degraded``.
    #
    # The watermark is *not* part of that argument. ``reset()`` is in-memory
    # and only ``save()`` touches the file, so the cursor on disk survives
    # either ordering. Recorded because the first version of this comment
    # claimed otherwise, and the test written to defend the claim passed
    # against both orderings — coverage-shaped, and measuring nothing.
    policy_gate = build_policy_gate(registry)

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

    # One gate per pass, as ``admin reindex-vectors`` builds one per run, for the
    # dry run's screen; the handler builds its own for a live write. A summary
    # carries no label of its own: it is judged by the documents its trace
    # cites, so the gate needs the document store (a declared-local embedder
    # reads nothing and answers "yes" for every trace).
    egress = EgressGate.for_embedder(registry, registry.knowledge.document_store)
    handler = TraceSummaryIngestHandler(registry, embedding_fn)
    executor = MutationExecutor(
        event_log=registry.operational.event_log,
        handlers={"evidence.ingest": handler},
        policy_gate=policy_gate,
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
            handler=handler,
            egress=egress,
            registry=registry,
            vector_store=vector_store,
            document_store=registry.knowledge.document_store,
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

    _commit(
        registry,
        policy_gate,
        watermark,
        report,
        frontier=frontier,
        before=before,
        dry_run=dry_run,
        restamp_interval=restamp_interval,
    )
    report.watermark_after = (
        watermark.cursor.created_at.isoformat() if watermark.cursor else None
    )

    _log_skipped_invalid(report)

    logger.info(
        "trace_embed_pass_completed",
        **{k: v for k, v in report.to_dict().items() if k != "failures"},
    )
    return report


def _commit(
    registry: StoreRegistry,
    policy_gate: PolicyGate,
    watermark: TraceEmbedWatermark,
    report: TraceEmbedReport,
    *,
    frontier: TraceCursor | None,
    before: TraceCursor | None,
    dry_run: bool,
    restamp_interval: timedelta,
) -> None:
    """Move the cursor to ``frontier``, run a due restamp sweep, and persist both."""
    moved = frontier is not None and frontier != before
    if not dry_run and moved and frontier is not None:
        watermark.advance_to(frontier)
    marked = False
    if not report.stopped_early and watermark.restamp_due(
        datetime.now(UTC), restamp_interval
    ):
        marked = _restamp(registry, policy_gate, watermark, report, dry_run=dry_run)
    if not dry_run and (moved or marked):
        watermark.save()


def _restamp(
    registry: StoreRegistry,
    policy_gate: PolicyGate,
    watermark: TraceEmbedWatermark,
    report: TraceEmbedReport,
    *,
    dry_run: bool,
) -> bool:
    """Run the restamp sweep into ``report``; ``True`` when the watermark changed.

    Every sweep that judged something marks the time, clean or not: one
    citation that stays unreadable would otherwise make every pass a full sweep
    (N summaries by k citation reads). An unclean sweep still makes the report
    ``partial``, and its summaries are retried by the next sweep, an interval
    later.

    A second handler in ``embed="none"`` mode: the sweep rewrites a document that
    has its vector already, so it must call no embedder, and the core handler
    refuses a command whose mode is not its own. Same gate, same executor shape, so
    the sweep is governed like the pass.
    """
    executor = MutationExecutor(
        event_log=registry.operational.event_log,
        handlers={"evidence.ingest": TraceSummaryIngestHandler(registry, embed="none")},
        policy_gate=policy_gate,
    )
    sweep = restamp_summaries(registry, executor, dry_run=dry_run)
    report.restamp_scanned = sweep.scanned
    report.restamp_raised = sweep.raised
    report.restamp_unreadable = sweep.unreadable
    report.restamp_failed = sweep.failed
    report.failures.extend(sweep.failures)
    if dry_run or (sweep.clean and not sweep.scanned):
        # A dry run moves nothing; and a store with no summaries has nothing for
        # the mark to stand in for, so it does not make a watermark file.
        return False
    watermark.mark_restamped(datetime.now(UTC))
    return True


def _log_skipped_invalid(report: TraceEmbedReport) -> None:
    """One warning naming the count -- never a trace id, which is exactly
    the hazardous value ``skipped_invalid`` exists to keep out of a log
    line. Split out so the branch stays off ``run_trace_embed_pass``."""
    if report.skipped_invalid:
        logger.warning("trace_embed_skipped_invalid", count=report.skipped_invalid)


def _dry_run_one(
    trace: Trace,
    *,
    egress: EgressGate,
    registry: StoreRegistry,
    document_store: DocumentStore,
    report: TraceEmbedReport,
) -> bool:
    """Count what a live pass would do with ``trace``; write nothing.

    A redacted summary id is counted as redacted, cursor included: the row can
    never be embedded. It is asked before the egress screen, as the live write
    is (the write seam refuses before the handler screens), so a redacted
    summary is redacted whatever its trace cites. Anything else is judged by
    the same ``summary_verdict`` the handler asks, so the dry run and the live
    pass cannot disagree about a trace.
    """
    stored = document_store.get(trace_summary_doc_id(trace.trace_id))
    if stored is not None and is_redaction_tombstone(stored):
        report.skipped_redacted += 1
        return True
    verdict = egress.summary_verdict(
        trace_summary_doc_id(trace.trace_id),
        build_trace_metadata(trace),
        lambda: registry.operational.trace_store,
    )
    if verdict is not EgressVerdict.PERMITTED:
        return _count_withheld(verdict, report)
    report.embedded += 1
    return False


def _process_one(
    trace: Trace,
    *,
    executor: MutationExecutor,
    handler: TraceSummaryIngestHandler,
    egress: EgressGate,
    registry: StoreRegistry,
    vector_store: VectorStore,
    document_store: DocumentStore,
    dry_run: bool,
    include_step_errors: bool,
    report: TraceEmbedReport,
) -> bool:
    """Handle one trace. ``True`` iff it ends the pass confirmed embedded.

    A dry run returns ``False`` for anything it *would* have written: nothing
    was confirmed, so the cursor must not move past it. The exceptions are a
    trace classified ``skipped_invalid`` below and one whose summary id was
    redacted -- a dry run reports those rows the same way a real pass would,
    because neither can ever be embedded, dry run or not. The redaction
    check is a single read of the summary row through ``document_store``,
    the one thing a dry run needs to know that the real write would have
    told it; it still writes nothing.

    A trace whose stored id holds a NUL byte or lone surrogate is classified
    before anything is rendered or sent to the executor: the same hazard
    rejects it identically on every retry (``OperationRegistry.validate``
    refuses it unconditionally), so refusing to advance would pin the
    watermark on this row forever and re-attempt the same row every pass
    without ever making progress. Counted under ``skipped_invalid`` and
    returns ``True`` so the cursor moves past it, the same way an
    already-embedded trace does. A trace whose summary id would have the
    minted chunk shape (a ``trace_id`` ending ``#chunk-<digits>``) is
    classified the same way: evidence ingest refuses that id on every
    attempt. A summary id that was redacted is refused by the write seam
    (``redaction_tombstone_overwrite``, #1034) on every retry too, so it is
    counted under ``skipped_redacted`` and returns ``True`` as well: holding
    the cursor behind it would rescan the same oldest rows every pass, embed
    nothing newer and eventually trip ``TraceEmbedScanLimitError``. Every
    other non-``SUCCESS`` result from the
    executor is a plain failure: it holds the cursor, because nothing about
    it says the next attempt cannot succeed.

    A summary is judged by the documents its trace cites (``evidence_used``) and
    by its own label, unless the embedder is declared local. That is asked in the
    handler, after the summary document is written (egress governs what is sent,
    not what is stored): a withheld summary is stored with no vector row
    (``skipped_egress_withheld``, and the cursor advances: it is the same on
    every retry); one whose cited document no store could answer for is stored
    the same way and holds the cursor (``skipped_egress_unreadable``), as a
    failure does, so the next pass writes it again and tries the embed again.
    A summary that was redacted is refused by the write seam before the screen
    and counts as redacted. A dry run asks the same question and writes nothing.
    """
    # Before the vector-store lookup, not after it: a backend that binds the
    # id as a SQL parameter (Postgres refuses a NUL in a text value) would
    # raise on the lookup itself and fail the whole pass.
    if contains_nul_or_lone_surrogate(trace.trace_id) or is_chunk_doc_id(
        trace_summary_doc_id(trace.trace_id)
    ):
        report.skipped_invalid += 1
        return True

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
        return _dry_run_one(
            trace,
            egress=egress,
            registry=registry,
            document_store=document_store,
            report=report,
        )

    result = executor.execute(
        build_trace_summary_command(
            doc_id=trace_summary_doc_id(trace.trace_id),
            trace_id=trace.trace_id,
            content=content,
            metadata=build_trace_metadata(trace),
            created_at=trace.created_at.isoformat(),
        )
    )
    advance = True
    if result.status is CommandStatus.SUCCESS:
        advance = _count_success(handler, report)
    elif (
        result.status is CommandStatus.REJECTED
        and result.metadata.get("rejection_reason") == TOMBSTONE_OVERWRITE_CODE
    ):
        report.skipped_redacted += 1
    else:
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
        advance = False
    return advance


def _count_success(
    handler: TraceSummaryIngestHandler, report: TraceEmbedReport
) -> bool:
    """Count a ``SUCCESS`` as embedded or as declined by the egress gate.

    ``False`` holds the cursor: a summary kept off the embedder because a store
    could not be asked is a fault a later pass may clear, as a failure is. A
    summary withheld for a label is a fact about what is stored, the same on
    every retry, so the pass advances past it as it does past a redacted one.
    """
    withheld = handler.last_embed_withheld
    if withheld is None:
        report.embedded += 1
        return True
    return _count_withheld(withheld, report)


def _count_withheld(verdict: EgressVerdict, report: TraceEmbedReport) -> bool:
    """Count one summary kept off the embedder; ``False`` holds the cursor.

    ``UNREADABLE`` is a fault a later pass may clear; ``WITHHELD`` is a fact
    about what is stored and is the same on every retry, so the pass advances.
    """
    if verdict is EgressVerdict.UNREADABLE:
        report.skipped_egress_unreadable += 1
        return False
    report.skipped_egress_withheld += 1
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
        total.skipped_invalid += r.skipped_invalid
        total.skipped_redacted += r.skipped_redacted
        total.skipped_egress_withheld += r.skipped_egress_withheld
        total.skipped_egress_unreadable += r.skipped_egress_unreadable
        total.failed += r.failed
        total.restamp_scanned += r.restamp_scanned
        total.restamp_raised += r.restamp_raised
        total.restamp_unreadable += r.restamp_unreadable
        total.restamp_failed += r.restamp_failed
        total.failures.extend(r.failures)
    return total.to_dict()
