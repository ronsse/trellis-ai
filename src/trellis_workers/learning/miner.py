"""Learning worker — outcome analysis and precedent extraction from traces."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal

import structlog
from pydantic import ValidationError

from trellis.core.hashing import content_hash
from trellis.core.ids import generate_ulid
from trellis.extract.telemetry import (
    ExtractionFailureError,
    emit_extraction_failure,
)
from trellis.llm import Message
from trellis.llm.json_response import (
    JSONParseOutcome,
    coerce_finite_float,
    parse_json_response,
)
from trellis.retrieve.precedents import MINED_CANDIDATE_ACTION, MINER_EVENT_SOURCE
from trellis.schemas.enums import OutcomeStatus
from trellis.schemas.precedent import Precedent
from trellis.schemas.trace import Trace
from trellis.stores.base.event_log import EventType

if TYPE_CHECKING:
    from collections.abc import Sequence

    from trellis.llm import LLMClient
    from trellis.stores.base.event_log import EventLog
    from trellis.stores.base.trace import TraceStore

logger = structlog.get_logger(__name__)

# Heuristic confidence scores per outcome status.
_OUTCOME_CONFIDENCE: dict[OutcomeStatus, float] = {
    OutcomeStatus.SUCCESS: 0.7,
    OutcomeStatus.PARTIAL: 0.4,
    OutcomeStatus.FAILURE: 0.3,
    OutcomeStatus.UNKNOWN: 0.2,
}


#: Most traces one scan reads looking for failures. A store that is nearly all
#: successes would otherwise be read to its end on every run; a scan that stops
#: here says so (``FailureScan.bound_hit``) instead of reporting nothing found.
DEFAULT_TRACE_SCAN_CAP = 5_000

# The first read is at least this wide, so a small ``limit`` does not start a
# run of tiny reads that each widen by one doubling.
_SCAN_FIRST_WINDOW = 100

# Most failure traces shown to the LLM in one request.
_MAX_ANALYZED = 20

# Most earlier candidates a skipped run names back to the operator.
_PRIOR_CANDIDATES_SHOWN = 20

_FAILURE_STATUSES = (OutcomeStatus.FAILURE, OutcomeStatus.PARTIAL)

MiningOutcome = Literal[
    "mined",
    "would_mine",
    "skipped_unchanged",
    "no_failures",
    "below_min_traces",
    "read_bound_hit",
    "no_candidates",
    "llm_error",
    "no_llm",
]


@dataclass(frozen=True)
class FailureScan:
    """The failure/partial traces one scan found and how far it read to find them.

    ``traces`` is newest first and holds at most the ``limit`` asked for.
    ``examined`` is how many traces were read, failures or not. The two flags
    say why the scan stopped short of ``limit`` failures, and are never both
    true: ``exhausted`` means the store held nothing older than what was read,
    ``bound_hit`` means the read stopped at ``scan_cap`` traces with older ones
    left unread. A scan that found ``limit`` failures sets neither.
    """

    traces: list[Trace]
    examined: int
    exhausted: bool
    bound_hit: bool


def _is_failure(trace: Trace) -> bool:
    return trace.outcome is not None and trace.outcome.status in _FAILURE_STATUSES


def scan_failure_traces(
    trace_store: TraceStore,
    *,
    domain: str | None,
    limit: int,
    scan_cap: int = DEFAULT_TRACE_SCAN_CAP,
) -> FailureScan:
    """Read newest-first until ``limit`` failure/partial traces are found.

    ``limit`` counts failures. Filtering the newest ``limit`` traces would count
    the successes too, so a run of recent successes hides every older failure.
    ``TraceStore.query`` has no outcome filter and a trace's outcome lives in
    its JSON, so this widens the read instead: the newest ``limit`` traces,
    then twice that, up to ``scan_cap``. A widening read, not a cursor, because
    ``until`` is inclusive and traces can share a ``created_at``, which would
    leave a cursor on a row it had already read. The cost is that a scan reaching
    the cap reads about twice ``scan_cap`` rows in all.
    """
    if limit < 1:
        return FailureScan(traces=[], examined=0, exhausted=False, bound_hit=False)
    cap = max(scan_cap, 1)
    window = min(max(limit, _SCAN_FIRST_WINDOW), cap)
    while True:
        # One row past the window says whether the store has more behind it, so
        # a store of exactly ``cap`` traces is not reported as cut off.
        batch = trace_store.query(domain=domain, limit=window + 1)
        more_behind = len(batch) > window
        read = batch[:window]
        failures = [t for t in read if _is_failure(t)]
        if len(failures) >= limit:
            return FailureScan(failures[:limit], len(read), False, False)
        if not more_behind:
            return FailureScan(failures, len(read), True, False)
        if window >= cap:
            return FailureScan(failures, len(read), False, True)
        window = min(window * 2, cap)


def compute_source_digest(analyzed: Sequence[Trace], domain: str | None) -> str:
    """Name the exact input a mining run gives the LLM: these traces, this scope.

    Two runs with the same digest ask the model the same question, so the
    second would only mint a duplicate of the first's candidates. It is a
    digest of what was analysed, not of the candidates the model happened to
    return, so it is never a judgement that two candidates mean the same thing.
    """
    return content_hash(
        json.dumps(
            {"domain": domain, "trace_ids": sorted(t.trace_id for t in analyzed)}
        )
    )


@dataclass(frozen=True)
class MiningReport:
    """What a mining run (or a dry run of one) read, decided and produced.

    ``outcome`` is one of :data:`MiningOutcome`. An empty ``candidates`` has
    several causes and ``outcome`` names which: ``no_failures`` (the store was
    read to its end and holds none), ``read_bound_hit`` (the read stopped at the
    cap short of ``min_traces`` failures, so older ones were not looked at),
    ``below_min_traces``, ``skipped_unchanged`` (these traces were mined
    before), ``no_candidates`` (the model returned none) and ``llm_error``.
    ``read_bound_hit`` also rides on a ``mined`` run that stopped short of
    ``limit`` failures at the cap.
    """

    outcome: MiningOutcome
    failures_in_scope: int
    traces_examined: int
    scan_cap: int
    read_bound_hit: bool
    source_digest: str | None = None
    already_mined: tuple[str, ...] = ()
    candidates: tuple[Precedent, ...] = ()
    analyzed: tuple[Trace, ...] = ()


class PrecedentMiner:
    """Extracts precedent candidates from traces.

    Analyzes traces — especially failure/partial outcomes — to identify
    patterns and generate precedent candidates using LLM analysis.

    Every event it emits is :attr:`EventType.PRECEDENT_CANDIDATE_MINED`,
    never :attr:`EventType.PRECEDENT_PROMOTED`: a mined candidate is
    ungoverned (it goes straight to the event log, past ``MutationExecutor``)
    and has had no human approval, so it must not be served as a promoted
    precedent. Promotion is a separate, governed ``precedent.promote``.
    """

    def __init__(
        self,
        trace_store: TraceStore,
        event_log: EventLog | None = None,
        llm: LLMClient | None = None,
    ) -> None:
        self._trace_store = trace_store
        self._event_log = event_log
        self._llm = llm

    # ------------------------------------------------------------------
    # Deterministic extraction (no LLM)
    # ------------------------------------------------------------------

    def extract_precedent_from_trace(self, trace_id: str) -> Precedent | None:
        """Create a precedent directly from a single trace.

        Deterministic — no LLM.  Extracts structure from the trace.
        Returns ``None`` if the trace is not found or has no outcome.
        """
        trace = self._trace_store.get(trace_id)
        if trace is None or trace.outcome is None:
            return None

        description = trace.outcome.summary or f"Trace {trace.source} execution"
        domain = trace.context.domain if trace.context else None
        applicability = [domain] if domain else []

        confidence = _OUTCOME_CONFIDENCE.get(trace.outcome.status, 0.2)

        precedent = Precedent(
            precedent_id=generate_ulid(),
            source_trace_ids=[trace.trace_id],
            title=f"{trace.intent[:80]}",
            description=description,
            pattern=None,
            applicability=applicability,
            confidence=confidence,
            promoted_by="precedent_miner",
            evidence_refs=[ref.evidence_id for ref in trace.evidence_used],
        )

        if self._event_log is not None:
            self._event_log.emit(
                EventType.PRECEDENT_CANDIDATE_MINED,
                source=MINER_EVENT_SOURCE,
                entity_id=precedent.precedent_id,
                entity_type="precedent",
                payload={
                    "trace_id": trace_id,
                    "outcome_status": trace.outcome.status.value,
                    "action": "precedent_extracted",
                },
            )

        return precedent

    # ------------------------------------------------------------------
    # LLM-driven candidate generation
    # ------------------------------------------------------------------

    def plan(
        self,
        *,
        domain: str | None = None,
        min_traces: int = 3,
        limit: int = 100,
        scan_cap: int = DEFAULT_TRACE_SCAN_CAP,
    ) -> MiningReport:
        """Read the failures and decide whether a run would ask the LLM.

        Calls no LLM and writes nothing. ``would_mine`` means a run would;
        every other outcome says why it would not.
        """
        scan = scan_failure_traces(
            self._trace_store, domain=domain, limit=limit, scan_cap=scan_cap
        )
        found = len(scan.traces)
        report = MiningReport(
            outcome="no_failures",
            failures_in_scope=found,
            traces_examined=scan.examined,
            scan_cap=scan_cap,
            read_bound_hit=scan.bound_hit,
        )
        if found < min_traces:
            if scan.bound_hit:
                return replace(report, outcome="read_bound_hit")
            if found:
                return replace(report, outcome="below_min_traces")
            return report

        analyzed = tuple(scan.traces[:_MAX_ANALYZED])
        digest = compute_source_digest(analyzed, domain)
        already = self._candidates_mined_from(digest)
        return replace(
            report,
            outcome="skipped_unchanged" if already else "would_mine",
            source_digest=digest,
            already_mined=already,
            analyzed=analyzed,
        )

    async def mine(
        self,
        *,
        domain: str | None = None,
        min_traces: int = 3,
        limit: int = 100,
        scan_cap: int = DEFAULT_TRACE_SCAN_CAP,
    ) -> MiningReport:
        """Mine candidates and report what happened, including why none came.

        A run over the same failure traces as an earlier one that minted
        candidates asks nothing and mints nothing: ``skipped_unchanged``. A new
        failure among the newest analysed changes the input, so it mines again.
        """
        llm = self._llm
        if llm is None:
            return MiningReport(
                outcome="no_llm",
                failures_in_scope=0,
                traces_examined=0,
                scan_cap=scan_cap,
                read_bound_hit=False,
            )
        report = self.plan(
            domain=domain, min_traces=min_traces, limit=limit, scan_cap=scan_cap
        )
        if report.outcome != "would_mine":
            return report
        precedents = await self._ask_llm(
            llm, list(report.analyzed), domain, report.source_digest
        )
        if precedents is None:
            return replace(report, outcome="llm_error")
        return replace(
            report,
            outcome="mined" if precedents else "no_candidates",
            candidates=tuple(precedents),
        )

    async def generate_precedent_candidates(
        self,
        *,
        domain: str | None = None,
        min_traces: int = 3,
        limit: int = 100,
        scan_cap: int = DEFAULT_TRACE_SCAN_CAP,
    ) -> list[Precedent]:
        """Generate precedent candidates from failure/partial traces using LLM.

        Analyzes failure patterns across traces and generates generalized
        precedents (lessons learned). :meth:`mine` is the same run with its
        reasoning attached.

        Args:
            domain: Optional domain filter.
            min_traces: Minimum failure traces required.
            limit: Max failure/partial traces to analyze. Counts failures, not
                traces read; see :func:`scan_failure_traces`.
            scan_cap: Most traces read looking for them.

        Returns:
            List of generated Precedent objects.
        """
        report = await self.mine(
            domain=domain, min_traces=min_traces, limit=limit, scan_cap=scan_cap
        )
        return list(report.candidates)

    async def _ask_llm(
        self,
        llm: LLMClient,
        analyzed: list[Trace],
        domain: str | None,
        digest: str | None,
    ) -> list[Precedent] | None:
        """One LLM request over ``analyzed``; ``None`` when the call failed."""
        # Build summaries for LLM analysis.
        summaries = "\n".join(
            f"- Intent: {t.intent}, "
            f"Outcome: {t.outcome.status.value}, "
            f"Domain: {t.context.domain or 'N/A'}, "
            f"Summary: {t.outcome.summary or 'N/A'}"
            for t in analyzed
            if t.outcome
        )

        prompt = (
            f"Analyze these {len(analyzed)} failed/partial traces "
            f"and identify common patterns.\n"
            f"For each pattern, provide:\n"
            f"- title: Short descriptive title\n"
            f"- description: What the pattern is\n"
            f"- pattern: Generalized pattern\n"
            f"- confidence: 0.0-1.0\n\n"
            f"Traces:\n{summaries}\n\n"
            f"Respond in JSON format:\n"
            f'[{{"title": "...", "description": "...", '
            f'"pattern": "...", "confidence": 0.8}}]'
        )

        try:
            response = await llm.generate(
                messages=[
                    Message(
                        role="system",
                        content=(
                            "You are an experience analyst. "
                            "Identify patterns in failed traces."
                        ),
                    ),
                    Message(role="user", content=prompt),
                ],
            )
        # GRACEFUL-DEGRADATION: precedent mining is a best-effort
        # background pass — an LLM outage must not crash the miner job
        # (workers run on cron); the empty list signals "no candidates
        # this round" and the next run retries.
        except Exception:
            logger.exception("precedent_generation_llm_error")
            return None

        if response.usage is not None:
            logger.info(
                "precedent_generation_llm_usage",
                prompt_tokens=response.usage.prompt_tokens,
                completion_tokens=response.usage.completion_tokens,
                total_tokens=response.usage.total_tokens,
                model=response.model,
            )

        return self._parse_candidates(
            response.content, analyzed, domain, source_digest=digest
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _candidates_mined_from(self, digest: str) -> tuple[str, ...]:
        """Ids of earlier candidates minted over exactly this input, if any.

        Empty without an event log: with nothing recorded there is nothing to
        compare against, and a run cannot skip on a record it cannot read. A
        read that fails propagates; swallowing it would mint duplicates without
        saying so. A candidate minted before the digest was recorded carries no
        key, so the first run after that change over the same failures mints once more.
        """
        if self._event_log is None:
            return ()
        events = self._event_log.get_events(
            event_type=EventType.PRECEDENT_CANDIDATE_MINED,
            source=MINER_EVENT_SOURCE,
            payload_filters={"source_digest": digest},
            limit=_PRIOR_CANDIDATES_SHOWN,
        )
        return tuple(e.entity_id for e in events if e.entity_id)

    def _parse_candidates(
        self,
        response: str,
        analyzed: list[Trace],
        domain: str | None,
        *,
        source_digest: str | None = None,
    ) -> list[Precedent]:
        """Parse LLM response into ``Precedent`` objects.

        Emits :class:`EventType.EXTRACTION_FAILED` and raises
        :class:`ExtractionFailureError` on malformed JSON or validation
        errors. The caller — :meth:`generate_precedent_candidates` —
        decides whether to degrade gracefully (POC directive: no silent
        ``return []``).
        """
        parse_result = parse_json_response(response)
        if parse_result.outcome is JSONParseOutcome.MALFORMED:
            exc = json.JSONDecodeError(
                parse_result.error or "invalid JSON response", response, 0
            )
            emit_extraction_failure(
                event_log=self._event_log,
                extractor_id="PrecedentMiner",
                extractor_tier="llm",
                failure_kind="parse_error",
                source_hint="precedent_miner",
                source_excerpt_hash=content_hash(response) if response else None,
                model=None,
                error_class=type(exc).__name__,
                error_excerpt=str(exc),
            )
            logger.warning("precedent_parse_error", response=response[:200])
            msg = f"PrecedentMiner: failed to parse JSON response: {exc}"
            raise ExtractionFailureError(
                msg,
                failure_kind="parse_error",
                extractor_id="PrecedentMiner",
            ) from exc
        parsed = parse_result.value

        if not isinstance(parsed, list):
            return []

        source_ids = [t.trace_id for t in analyzed]
        precedents: list[Precedent] = []

        for item in parsed:
            if not isinstance(item, dict):
                continue
            title = item.get("title", "")
            description = item.get("description", "")
            if not title or not description:
                continue

            confidence = coerce_finite_float(item.get("confidence", 0.5))
            if confidence is None:
                confidence = 0.5

            try:
                precedent = Precedent(
                    precedent_id=generate_ulid(),
                    source_trace_ids=source_ids,
                    title=title,
                    description=description,
                    pattern=item.get("pattern"),
                    applicability=[domain] if domain else [],
                    confidence=min(max(confidence, 0.0), 1.0),
                    promoted_by="precedent_miner",
                )
            except ValidationError as exc:
                emit_extraction_failure(
                    event_log=self._event_log,
                    extractor_id="PrecedentMiner",
                    extractor_tier="llm",
                    failure_kind="validation_error",
                    source_hint="precedent_miner",
                    source_excerpt_hash=content_hash(response) if response else None,
                    model=None,
                    error_class=type(exc).__name__,
                    error_excerpt=str(exc),
                )
                logger.warning("precedent_validation_error", error=str(exc)[:200])
                msg = f"PrecedentMiner: precedent failed Pydantic validation: {exc}"
                raise ExtractionFailureError(
                    msg,
                    failure_kind="validation_error",
                    extractor_id="PrecedentMiner",
                ) from exc

            precedents.append(precedent)

            if self._event_log is not None:
                self._event_log.emit(
                    EventType.PRECEDENT_CANDIDATE_MINED,
                    source=MINER_EVENT_SOURCE,
                    entity_id=precedent.precedent_id,
                    entity_type="precedent",
                    payload={
                        "action": MINED_CANDIDATE_ACTION,
                        "title": precedent.title,
                        "confidence": precedent.confidence,
                        # What a rerun compares against to skip these traces.
                        **({"source_digest": source_digest} if source_digest else {}),
                    },
                )

        logger.info(
            "precedent_candidates_generated",
            count=len(precedents),
            domain=domain,
        )

        return precedents
