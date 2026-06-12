"""``ProposalGenerator`` — Item 7 Phase 0 of the self-improvement loop.

The generator reads operational telemetry over a rolling window, clusters
``EXTRACTION_FAILED`` events, treats each ``WELL_KNOWN_CANDIDATE`` event
as a standalone cluster, renders human-readable proposal markdown, and
emits ``PROPOSAL_DRAFTED`` (or ``PROPOSAL_UPDATED``) events keyed on a
stable :attr:`Proposal.proposal_id`.

The whole run is wrapped in
:func:`trellis.meta.record_meta_analysis` so the work leaves a graph
``Activity`` node connected by ``wasInformedBy`` edges to the consumed
events and ``wasGeneratedBy`` edges from the produced proposals (treated
here as findings of type ``"Proposal"`` — the proposal artefact itself
is not materialised as a graph node in this PR; cohort 2 will manage the
on-disk artefact directory).

Idempotency contract: a second run over an overlapping window where no
new failures have arrived produces zero new ``PROPOSAL_DRAFTED`` events.
The check is a per-proposal event-log lookup keyed on
``payload.proposal_id``. When a proposal already exists with that ID,
the generator emits ``PROPOSAL_UPDATED`` instead — Phase 2 will narrow
this to "cluster grew ≥ 50%" but the wire contract is stable now.

POC directive applied: when zero clusters survive the window /
threshold, the generator returns an empty list and logs INFO with a
one-line summary. No silent empty exit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import structlog

from trellis.meta import record_meta_analysis
from trellis.stores.base.event_log import EventType
from trellis_workers.code_authoring.clustering import (
    Cluster,
    cluster_failures,
    compute_cluster_signature,
)
from trellis_workers.code_authoring.proposal import (
    MARKDOWN_PREVIEW_CHARS,
    Proposal,
    compute_proposal_id,
    render_markdown,
)

if TYPE_CHECKING:
    from trellis.stores.base.event_log import Event
    from trellis.stores.registry import StoreRegistry

logger = structlog.get_logger(__name__)


#: Synthetic agent ID under which the generator's meta-Activities are
#: recorded. Must start with :data:`trellis.meta.agents.META_AGENT_PREFIX`
#: per the namespace-reservation ADR.
PROPOSAL_GENERATOR_AGENT_ID: str = "trellis_meta_proposal_generator"

#: Stable analyzer name carried on every meta-Activity node. Operators
#: query ``properties.analyzer_name == "code_authoring.proposal_generator"``
#: to find this generator's runs.
PROPOSAL_GENERATOR_ANALYZER_NAME: str = "code_authoring.proposal_generator"

#: Default rolling window for :meth:`ProposalGenerator.run`. Matches the
#: 24h cadence described in ``plan-coding-agent-loop.md`` Phase 0 — the
#: caller (cohort G2 CLI) can override.
DEFAULT_WINDOW: timedelta = timedelta(hours=24)

#: Source string carried on emitted events — matches the convention
#: used by the rest of the workers package (``precedent_miner`` /
#: ``extraction_failure_helper`` / …).
EVENT_SOURCE: str = "code_authoring.proposal_generator"

#: How many EXTRACTION_FAILED / WELL_KNOWN_CANDIDATE / PROPOSAL_DRAFTED
#: rows the generator pulls per query. POC scope: ample headroom for any
#: realistic 24-hour window. The cohort G2 CLI will accept an override.
_EVENT_READ_LIMIT: int = 5000


@dataclass(frozen=True, slots=True)
class _GeneratorRun:
    """Internal bookkeeping for one :meth:`ProposalGenerator.run` invocation.

    Carries the resolved window so that downstream helpers do not need
    to re-read configuration; instances are short-lived (built and
    consumed inside :meth:`run`).
    """

    started_at: datetime
    window: timedelta

    @property
    def cutoff(self) -> datetime:
        """Inclusive lower bound on ``occurred_at`` for events in this run."""
        return self.started_at - self.window


class ProposalGenerator:
    """Phase-0 proposal generator.

    Build once per CLI run — the generator caches no state across
    invocations beyond what lives in the EventLog itself.

    Args:
        registry: Store registry. The generator reads operational
            events and writes graph provenance through this.
        window: Rolling window width. Defaults to
            :data:`DEFAULT_WINDOW` (24 hours).
    """

    def __init__(
        self,
        registry: StoreRegistry,
        *,
        window: timedelta = DEFAULT_WINDOW,
    ) -> None:
        self._registry = registry
        self._window = window

    @property
    def window(self) -> timedelta:
        """Rolling window width for this generator instance."""
        return self._window

    def run(self) -> list[Proposal]:
        """Cluster signal events, render proposals, emit lifecycle events.

        Returns:
            One :class:`Proposal` per cluster that produced a draft or an
            update. Proposals whose IDs were already in the event log
            still appear in the return value (callers can decide whether
            to re-render the on-disk artefact); the
            ``PROPOSAL_DRAFTED`` event is not re-emitted — a
            ``PROPOSAL_UPDATED`` event is emitted instead.
        """
        run = _GeneratorRun(started_at=datetime.now(tz=UTC), window=self._window)

        # 1. Read signal events from the rolling window.
        failure_events = self._read_failure_events(since=run.cutoff)
        wk_events = self._read_well_known_events(since=run.cutoff)

        # 2. Cluster — failure events get grouped; each WK event becomes
        #    a single-event "cluster" so it flows through the same
        #    proposal/idempotency pipeline.
        failure_clusters = cluster_failures(
            failure_events,
            window=self._window,
            now=run.started_at,
        )
        wk_clusters = [_well_known_event_to_cluster(event) for event in wk_events]
        all_clusters = failure_clusters + wk_clusters

        if not all_clusters:
            # POC directive: loud INFO, never silent empty exit.
            logger.info(
                "proposal_generator_no_clusters",
                analyzed_failure_events=len(failure_events),
                analyzed_well_known_events=len(wk_events),
                window_hours=self._window.total_seconds() / 3600.0,
            )
            return []

        # 3. Wrap the rest in record_meta_analysis so the run leaves a
        #    graph Activity. We only enter the recorder once we know we
        #    have work to do — an empty no-op run shouldn't pollute the
        #    graph with an "I checked and found nothing" Activity every
        #    minute.
        proposals: list[Proposal] = []
        with record_meta_analysis(
            registry=self._registry,
            analyzer_name=PROPOSAL_GENERATOR_ANALYZER_NAME,
            agent_id=PROPOSAL_GENERATOR_AGENT_ID,
        ) as rec:
            for cluster in all_clusters:
                proposal = self._build_proposal(cluster, generated_at=run.started_at)
                action = self._emit_proposal_event(proposal)
                proposals.append(proposal)
                # Provenance edges — one wasInformedBy per consumed event.
                for event_id in cluster.events:
                    rec.consumed_event(event_id)
                # The proposal itself does not have a graph node in
                # Phase 0; we still emit a synthetic finding edge so the
                # Activity has at least one wasGeneratedBy outgoing — the
                # finding_type carries the action so analytics can
                # distinguish drafted-vs-updated without joining to the
                # event log.
                logger.debug(
                    "proposal_generator_proposal_emitted",
                    proposal_id=proposal.proposal_id,
                    cluster_signature=proposal.cluster_signature,
                    action=action,
                    activity_id=rec.activity_id,
                )

        logger.info(
            "proposal_generator_run_complete",
            proposals_produced=len(proposals),
            failure_clusters=len(failure_clusters),
            well_known_clusters=len(wk_clusters),
            window_hours=self._window.total_seconds() / 3600.0,
        )
        return proposals

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _read_failure_events(self, *, since: datetime) -> list[Event]:
        """Read ``EXTRACTION_FAILED`` events since ``since``."""
        event_log = self._registry.operational.event_log
        return event_log.get_events(
            event_type=EventType.EXTRACTION_FAILED,
            since=since,
            limit=_EVENT_READ_LIMIT,
        )

    def _read_well_known_events(self, *, since: datetime) -> list[Event]:
        """Read ``WELL_KNOWN_CANDIDATE`` events since ``since``."""
        event_log = self._registry.operational.event_log
        return event_log.get_events(
            event_type=EventType.WELL_KNOWN_CANDIDATE,
            since=since,
            limit=_EVENT_READ_LIMIT,
        )

    def _build_proposal(
        self,
        cluster: Cluster,
        *,
        generated_at: datetime,
    ) -> Proposal:
        """Render a :class:`Proposal` for ``cluster``."""
        return Proposal(
            proposal_id=compute_proposal_id(cluster.signature),
            cluster_signature=cluster.signature,
            markdown=render_markdown(cluster),
            generated_at=generated_at,
            source_event_ids=cluster.events,
        )

    def _emit_proposal_event(self, proposal: Proposal) -> str:
        """Emit ``PROPOSAL_DRAFTED`` or ``PROPOSAL_UPDATED`` for ``proposal``.

        Idempotency check: if any ``PROPOSAL_DRAFTED`` event already
        carries the same ``payload.proposal_id``, this run emits a
        ``PROPOSAL_UPDATED`` event instead. Otherwise emits
        ``PROPOSAL_DRAFTED``.

        Returns:
            ``"drafted"`` or ``"updated"`` so the caller can log /
            count which path was taken.
        """
        event_log = self._registry.operational.event_log
        # Targeted lookup — pushes the filter into the SQL so the limit
        # cap applies after the predicate. ``order="desc"`` so the most
        # recent emission (the one that matters for the lifecycle
        # decision) lands in the cap.
        prior = event_log.get_events(
            event_type=EventType.PROPOSAL_DRAFTED,
            payload_filters={"proposal_id": proposal.proposal_id},
            limit=1,
            order="desc",
        )
        payload = {
            "proposal_id": proposal.proposal_id,
            "cluster_signature": proposal.cluster_signature,
            "markdown_preview": proposal.markdown_preview(
                max_chars=MARKDOWN_PREVIEW_CHARS
            ),
            "source_event_count": len(proposal.source_event_ids),
        }
        if prior:
            event_log.emit(
                EventType.PROPOSAL_UPDATED,
                source=EVENT_SOURCE,
                entity_id=proposal.proposal_id,
                entity_type="proposal",
                payload=payload,
            )
            return "updated"
        event_log.emit(
            EventType.PROPOSAL_DRAFTED,
            source=EVENT_SOURCE,
            entity_id=proposal.proposal_id,
            entity_type="proposal",
            payload=payload,
        )
        return "drafted"


def _well_known_event_to_cluster(event: Event) -> Cluster:
    """Render a ``WELL_KNOWN_CANDIDATE`` event as a single-event cluster.

    The candidate's ``candidate_id`` and ``open_string_value`` carry the
    identity; we use the candidate_id directly as the cluster
    ``source_file`` placeholder and the ``candidate_kind`` as the
    ``failure_class`` so the proposal markdown still has clean
    headings. The cluster signature is the SHA-256 of
    ``(candidate_id, candidate_kind)`` — same shape as the
    failure-cluster signature, so downstream consumers don't need to
    branch on signal_type.
    """
    payload = event.payload or {}
    candidate_id = str(payload.get("candidate_id") or event.event_id)
    candidate_kind = str(payload.get("candidate_kind") or "well_known_candidate")
    signature = compute_cluster_signature(candidate_id, candidate_kind)
    return Cluster(
        signature=signature,
        source_file=candidate_id,
        failure_class=candidate_kind,
        events=(event.event_id,),
        earliest_at=event.occurred_at,
        latest_at=event.occurred_at,
        count=1,
    )


__all__ = [
    "DEFAULT_WINDOW",
    "EVENT_SOURCE",
    "PROPOSAL_GENERATOR_AGENT_ID",
    "PROPOSAL_GENERATOR_ANALYZER_NAME",
    "ProposalGenerator",
]
