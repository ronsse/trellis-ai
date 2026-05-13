"""Cluster ``EXTRACTION_FAILED`` events for the coding-agent loop.

Item 7 Phase 0 of the self-improvement program
(``docs/design/plan-coding-agent-loop.md``). The
:class:`ProposalGenerator` reads operational telemetry and groups
related failures into single :class:`Cluster` objects so each proposal
addresses one root cause, not one occurrence.

Clustering key is ``(source_file, failure_class)`` over a configurable
time window:

* ``source_file`` is the ``source_hint`` field of the
  ``EXTRACTION_FAILED`` payload (e.g., ``"src/trellis/extract/llm.py"``).
* ``failure_class`` is the payload's ``failure_kind`` literal
  (``"parse_error"``, ``"validation_error"``, ...).

The cluster :attr:`Cluster.signature` is a deterministic SHA-256 hash of
``(source_file, failure_class)`` so that re-clustering the same logical
group across runs produces the same signature — that signature in turn
seeds the proposal's stable :attr:`Proposal.proposal_id` for idempotency.
We deliberately do *not* fold the time window or the count into the
signature: a cluster's identity is "what's broken", not "how often".

Events with missing payload fields (``source_hint`` / ``failure_kind``
absent or null) are skipped — they cannot be clustered without a key
and silently bucketing them into a synthetic ``unknown`` group would
generate proposals that recommend "fix unknown problem in unknown file",
which is worse than emitting nothing.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from collections.abc import Iterable

    from trellis.stores.base.event_log import Event

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Cluster:
    """One bucket of related ``EXTRACTION_FAILED`` events.

    Frozen / slotted so callers can hash by :attr:`signature` and compare
    clusters across runs. Field order matches the natural read order of a
    proposal: identity first (``signature``, ``source_file``,
    ``failure_class``), evidence next (``events``), then temporal /
    aggregate facets (``earliest_at`` / ``latest_at`` / ``count``).

    Attributes:
        signature: Deterministic SHA-256 hex digest of
            ``"{source_file}|{failure_class}"``. Same logical cluster
            across runs → same signature. Re-used by the proposal-id
            hash so idempotency checks short-circuit cleanly.
        source_file: Source-hint of the failing extractor (or whatever
            file-level label the emitter populated on the
            ``source_hint`` payload key).
        failure_class: The ``failure_kind`` literal from the payload —
            one of the values in
            :data:`trellis.extract.telemetry.ExtractionFailureKind`.
        events: The ``event_id`` strings of every clustered event, in
            insertion order. The generator uses these as
            ``wasInformedBy`` provenance for the meta-Activity, and the
            markdown template renders the first few as samples.
        earliest_at: ``occurred_at`` of the oldest clustered event.
        latest_at: ``occurred_at`` of the newest clustered event.
        count: ``len(events)`` — denormalised so consumers don't have to
            re-count when scoring or sorting clusters.
    """

    signature: str
    source_file: str
    failure_class: str
    events: tuple[str, ...] = field(default_factory=tuple)
    earliest_at: datetime | None = None
    latest_at: datetime | None = None
    count: int = 0


def compute_cluster_signature(source_file: str, failure_class: str) -> str:
    """Return the deterministic signature for ``(source_file, failure_class)``.

    SHA-256 hex digest of ``"{source_file}|{failure_class}"``. Stable
    across processes and Python versions — the proposal-id hash chains
    off of this so callers verifying idempotency in tests can compute
    the signature themselves without instantiating a ``Cluster``.
    """
    payload = f"{source_file}|{failure_class}".encode()
    return hashlib.sha256(payload).hexdigest()


def cluster_failures(
    events: Iterable[Event],
    *,
    window: timedelta,
    now: datetime | None = None,
) -> list[Cluster]:
    """Cluster ``EXTRACTION_FAILED`` events by ``(source_file, failure_class)``.

    Events older than ``now - window`` are excluded — the generator runs
    over a rolling window, so failures from before the window do not
    belong to the current proposal even if their cluster signature
    matches one of the current clusters. (The earlier window's run
    already had its chance to surface them.)

    Args:
        events: Iterable of ``Event`` rows from the EventLog. Caller is
            responsible for narrowing the read by ``event_type ==
            EXTRACTION_FAILED``; this function does not filter by type
            so it can be reused if the cluster key ever generalises.
        window: Width of the rolling window. ``now - window`` is the
            inclusive cutoff; events at exactly that boundary are kept.
        now: Reference timestamp for the window. Defaults to the latest
            event's ``occurred_at`` so tests with deterministic event
            timestamps don't need to monkeypatch clocks. Falls back to
            :func:`datetime.utcnow` (timezone-aware UTC) if ``events``
            is empty — though in that case the function short-circuits
            anyway.

    Returns:
        Clusters sorted by signature (deterministic order). Empty list
        when ``events`` is empty or every event falls outside the
        window / lacks a valid key.
    """
    # Materialise so we can compute ``now`` from the data, then iterate
    # a second time for clustering. EXTRACTION_FAILED reads are bounded
    # by the analyzer's caller (typically a few thousand rows) so this
    # is fine; if we ever stream millions of events this becomes a
    # generator coroutine instead.
    event_list = list(events)
    if not event_list:
        return []

    if now is None:
        # Default to "the newest event we saw" — keeps tests with synthetic
        # timestamps deterministic without monkeypatching the clock.
        now = max(e.occurred_at for e in event_list)

    cutoff = now - window

    buckets: dict[str, _ClusterBuilder] = {}
    skipped_missing_keys = 0
    skipped_outside_window = 0

    for event in event_list:
        if event.occurred_at < cutoff:
            skipped_outside_window += 1
            continue
        payload = event.payload or {}
        source_file = payload.get("source_hint")
        failure_class = payload.get("failure_kind")
        if not source_file or not failure_class:
            # Skip rather than bucket into a synthetic "unknown" group —
            # see module docstring.
            skipped_missing_keys += 1
            continue
        signature = compute_cluster_signature(
            str(source_file),
            str(failure_class),
        )
        builder = buckets.get(signature)
        if builder is None:
            builder = _ClusterBuilder(
                signature=signature,
                source_file=str(source_file),
                failure_class=str(failure_class),
            )
            buckets[signature] = builder
        builder.add(event)

    if skipped_missing_keys or skipped_outside_window:
        logger.debug(
            "cluster_failures_skipped",
            missing_keys=skipped_missing_keys,
            outside_window=skipped_outside_window,
        )

    # Sort by signature for stable ordering — the generator depends on
    # this to produce deterministic test fixtures.
    return [buckets[sig].build() for sig in sorted(buckets)]


class _ClusterBuilder:
    """Mutable accumulator that finalises into a frozen :class:`Cluster`.

    Kept private — callers receive only the immutable ``Cluster`` value.
    """

    __slots__ = (
        "_earliest",
        "_event_ids",
        "_latest",
        "failure_class",
        "signature",
        "source_file",
    )

    def __init__(
        self,
        *,
        signature: str,
        source_file: str,
        failure_class: str,
    ) -> None:
        self.signature = signature
        self.source_file = source_file
        self.failure_class = failure_class
        self._event_ids: list[str] = []
        self._earliest: datetime | None = None
        self._latest: datetime | None = None

    def add(self, event: Event) -> None:
        self._event_ids.append(event.event_id)
        ts = event.occurred_at
        if self._earliest is None or ts < self._earliest:
            self._earliest = ts
        if self._latest is None or ts > self._latest:
            self._latest = ts

    def build(self) -> Cluster:
        return Cluster(
            signature=self.signature,
            source_file=self.source_file,
            failure_class=self.failure_class,
            events=tuple(self._event_ids),
            earliest_at=self._earliest,
            latest_at=self._latest,
            count=len(self._event_ids),
        )


__all__ = [
    "Cluster",
    "cluster_failures",
    "compute_cluster_signature",
]
