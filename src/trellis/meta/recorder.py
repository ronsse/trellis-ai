"""Record Trellis-internal analyses as graph ``Activity`` nodes.

:func:`record_meta_analysis` is the Phase 0 primitive of Item 6 of the
self-improvement program (``docs/design/plan-dogfooding-meta-traces.md``).
Use it to wrap any analyzer / tuner / promoter invocation so the work
leaves a graph artifact: an ``Activity`` node connected by
``wasInformedBy`` edges to the consumed operational inputs (event IDs
or observation IDs) and ``wasGeneratedBy`` edges from the produced
findings (Observation / Advisory / WellKnownCandidate node IDs).

The primitive is intentionally minimal: it owns the lifecycle of the
Activity node and the edge writes. CLI wiring, PackBuilder filtering,
and the eval scenario all land in a follow-up PR (cohort F2).

### Merge-within-window dedup

Two invocations of the same ``(agent_id, analyzer_name)`` within the
configurable merge window resolve to the **same** Activity — the
second invocation appends its consumed/produced edges to the first's
Activity instead of minting a new one. This keeps the graph
proportional to *change*, not to *invocation frequency* (scheduled
tasks that run an analyzer every minute should produce roughly one
Activity per non-trivial change, not 60 per hour).

The default merge window is 5 minutes (300 seconds), matching the ADR.

### Env var

``TRELLIS_META_TRACES`` is read at context-manager entry:

* ``"on"`` or unset → record the Activity.
* ``"off"`` → return a no-op record that accepts ``consumed_*`` /
  ``produced_*`` calls but writes nothing.
* anything else → raise :class:`ValueError` (POC directive — no
  silent default flip on a misconfigured value).
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog

from trellis.core.base import utc_now
from trellis.core.ids import generate_ulid
from trellis.meta.agents import ensure_meta_agent
from trellis.schemas import well_known as wk

if TYPE_CHECKING:
    from collections.abc import Iterator

    from trellis.stores.registry import StoreRegistry

logger = structlog.get_logger(__name__)

#: Default merge window. Per ADR §2.4: a second invocation of the same
#: analyzer within this window appends to the prior Activity instead of
#: creating a new one.
DEFAULT_MERGE_WINDOW_SECONDS: int = 300

#: Env var that toggles meta-trace recording. ``"on"`` (or unset) means
#: record; ``"off"`` means no-op context manager; anything else raises.
META_TRACES_ENV_VAR: str = "TRELLIS_META_TRACES"

_VALID_META_TRACES_VALUES = frozenset({"on", "off"})


def _meta_traces_enabled() -> bool:
    """Return ``True`` if meta-trace recording is enabled.

    Reads :data:`META_TRACES_ENV_VAR`. Default (unset) is ``"on"``.

    Raises:
        ValueError: If the env var is set to anything other than
            ``"on"`` / ``"off"``. No silent fallback per the POC
            directive (``plan-self-improvement-program.md`` §2).
    """
    raw = os.environ.get(META_TRACES_ENV_VAR)
    if raw is None:
        return True
    if raw not in _VALID_META_TRACES_VALUES:
        msg = (
            f"{META_TRACES_ENV_VAR}={raw!r} is invalid; must be one of "
            f"{sorted(_VALID_META_TRACES_VALUES)} (or unset, which "
            "means 'on'). Refusing to silently fall back."
        )
        raise ValueError(msg)
    return raw == "on"


class MetaAnalysisRecord:
    """Handle for a single meta-Activity recording session.

    Returned by :func:`record_meta_analysis`. Inside the ``with`` block,
    callers add provenance via:

    * :meth:`consumed_event` — adds a ``wasInformedBy`` edge from the
      Activity to the consumed event's correlation node.
    * :meth:`consumed_observation` — same, for a consumed Observation.
    * :meth:`produced_finding` — adds a ``wasGeneratedBy`` edge from
      the finding node back to the Activity.

    All edges carry the five provenance columns introduced by Item 2
    (``adr-graph-ontology.md`` §6.4):

    * ``agent_id`` — the synthetic meta-analyzer agent.
    * ``source_trace_id`` — always ``None`` (Activities are not traces).
    * ``confidence`` — always ``1.0`` (the analyzer is asserting fact).
    * ``evidence_ref`` — the Activity node ID, so a downstream query
      can rejoin every edge in the same invocation.
    * ``extractor_tier`` — always ``"DETERMINISTIC"``.

    When ``enabled`` is ``False`` (the no-op variant returned when
    ``TRELLIS_META_TRACES=off``), every method is a silent no-op and
    :attr:`activity_id` is ``None``.
    """

    def __init__(
        self,
        *,
        registry: StoreRegistry,
        analyzer_name: str,
        agent_id: str,
        activity_id: str | None,
        enabled: bool,
    ) -> None:
        self._registry = registry
        self._analyzer_name = analyzer_name
        self._agent_id = agent_id
        self._activity_id = activity_id
        self._enabled = enabled

    @property
    def activity_id(self) -> str | None:
        """Node ID of the recorded Activity, or ``None`` when disabled."""
        return self._activity_id

    @property
    def analyzer_name(self) -> str:
        """Logical analyzer name passed at construction."""
        return self._analyzer_name

    @property
    def agent_id(self) -> str:
        """Synthetic meta-agent ID this Activity is associated with."""
        return self._agent_id

    @property
    def enabled(self) -> bool:
        """``True`` when recording is active (env var on)."""
        return self._enabled

    # ------------------------------------------------------------------
    # Provenance edges
    # ------------------------------------------------------------------

    def consumed_event(self, event_id: str) -> None:
        """Record that this Activity consumed an operational event.

        Writes a ``wasInformedBy`` edge from the Activity node to the
        event-correlation node identified by ``event_id``. The event
        node is **not** materialised here — the operational EventLog
        owns the canonical record; this edge is a *pointer* into it.

        Callers that pre-sample (e.g., via
        :func:`trellis.meta.sampling.reservoir_sample`) should pass the
        sampled IDs in stream order. This recorder does not sample
        internally — the caller decides the cap.
        """
        if not self._enabled:
            return
        self._write_provenance_edge(
            source_id=self._activity_id,
            target_id=event_id,
            edge_kind=wk.WAS_INFORMED_BY,
        )

    def consumed_observation(self, observation_id: str) -> None:
        """Record that this Activity consumed an Observation node.

        Same edge kind as :meth:`consumed_event` (``wasInformedBy``) —
        the distinction is purely in the kind of source: an event is
        a pointer into the operational plane, an observation is a
        first-class knowledge-plane node.
        """
        if not self._enabled:
            return
        self._write_provenance_edge(
            source_id=self._activity_id,
            target_id=observation_id,
            edge_kind=wk.WAS_INFORMED_BY,
        )

    def produced_finding(self, finding_id: str, finding_type: str) -> None:
        """Record that this Activity produced ``finding_id``.

        Writes a ``wasGeneratedBy`` edge from the finding node back to
        the Activity (PROV-O direction: the output points to the
        Activity that generated it). ``finding_type`` is the open
        string node_type of the finding (``Observation``, ``Advisory``,
        ``WellKnownCandidate``, …) — it is recorded in the edge
        ``properties`` so downstream consumers can filter on it
        without joining to the finding node.

        Args:
            finding_id: Node ID of the produced finding.
            finding_type: Open-string node_type of the finding (used
                for filtering, not validated).
        """
        if not self._enabled:
            return
        self._write_provenance_edge(
            source_id=finding_id,
            target_id=self._activity_id,
            edge_kind=wk.WAS_GENERATED_BY,
            properties={"finding_type": finding_type},
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _write_provenance_edge(
        self,
        *,
        source_id: str | None,
        target_id: str | None,
        edge_kind: str,
        properties: dict[str, str] | None = None,
    ) -> None:
        """Write one provenance edge with the canonical five columns."""
        if source_id is None or target_id is None or self._activity_id is None:
            # Defensive — we already gated on ``self._enabled``; this
            # branch should be unreachable for the on-path. Raise
            # rather than silently dropping (POC directive).
            msg = (
                "record_meta_analysis: cannot write provenance edge "
                "without an activity_id — recorder must be inside a "
                "live with-block"
            )
            raise RuntimeError(msg)
        graph_store = self._registry.knowledge.graph_store
        # ``upsert_edge`` accepts the five provenance kwargs on every
        # built-in backend (see Item 2). The ABC signature does not
        # declare them yet — the dict-spread pattern below mirrors
        # ``trellis_cli.admin_migrate_provenance`` and lets the typed
        # call site stay clean without a per-line ``# type: ignore``.
        provenance: dict[str, Any] = {
            "source_trace_id": None,
            "agent_id": self._agent_id,
            "confidence": 1.0,
            "evidence_ref": self._activity_id,
            "extractor_tier": "DETERMINISTIC",
        }
        graph_store.upsert_edge(
            source_id=source_id,
            target_id=target_id,
            edge_type=edge_kind,
            properties=properties or {},
            **provenance,
        )


def _find_recent_activity(
    registry: StoreRegistry,
    *,
    agent_id: str,
    analyzer_name: str,
    window_seconds: int = DEFAULT_MERGE_WINDOW_SECONDS,
) -> str | None:
    """Return the most recent matching Activity inside the merge window.

    Scans current Activity nodes (``valid_to IS NULL``) whose
    ``properties.agent_id`` and ``properties.analyzer_name`` match the
    args. Returns the most recently-created Activity whose
    ``created_at`` is within ``window_seconds`` of now, or ``None``.

    The merge-window check requires the GraphStore to be readable. If
    the read raises, the exception propagates — per the ADR, silent
    fallback to "create a fresh Activity" would let backend hiccups
    pollute the graph with duplicates.
    """
    graph_store = registry.knowledge.graph_store
    cutoff = utc_now() - timedelta(seconds=window_seconds)

    # ``query`` filters on top-level fields via SQL and applies
    # property filters Python-side; for two scalar string filters that
    # is a one-table-scan join, fine for the typical Activity count.
    candidates = graph_store.query(
        node_type=wk.ACTIVITY,
        properties={
            "agent_id": agent_id,
            "analyzer_name": analyzer_name,
        },
        limit=50,
    )
    # ``created_at`` is an ISO-8601 string on every backend. Parse and
    # filter by the window cutoff; pick the most recently-created.
    eligible: list[tuple[datetime, str]] = []
    for node in candidates:
        created_raw = node.get("created_at")
        if not created_raw:
            continue
        try:
            created_at = datetime.fromisoformat(str(created_raw))
        except ValueError:
            # Stored timestamps must round-trip. A malformed timestamp
            # means the store is in an unexpected state — raise rather
            # than silently skip.
            msg = (
                f"_find_recent_activity: node {node.get('node_id')!r} "
                f"has unparseable created_at={created_raw!r}"
            )
            raise ValueError(msg) from None
        # SQLite stores naive timestamps when the ISO has no offset.
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        if created_at >= cutoff:
            eligible.append((created_at, node["node_id"]))
    if not eligible:
        return None
    eligible.sort(key=lambda pair: pair[0], reverse=True)
    return eligible[0][1]


@contextmanager
def record_meta_analysis(
    *,
    analyzer_name: str,
    agent_id: str,
    registry: StoreRegistry,
    include_meta: bool | None = None,
    merge_window_seconds: int = DEFAULT_MERGE_WINDOW_SECONDS,
) -> Iterator[MetaAnalysisRecord]:
    """Context manager that records a meta-Activity.

    On enter:

    1. Checks the env var (:data:`META_TRACES_ENV_VAR`). If ``off``,
       returns a no-op record and writes nothing.
    2. Ensures the synthetic Agent node for ``agent_id`` exists
       (:func:`trellis.meta.agents.ensure_meta_agent`).
    3. Checks for a recent Activity with the same
       ``(agent_id, analyzer_name)``. If one exists inside the merge
       window, the recorder reuses its ID — subsequent edge writes
       append to that Activity.
    4. Otherwise, creates a new Activity node with
       ``node_type="Activity"`` and the canonical properties.

    Inside the ``with`` block, the caller calls
    :meth:`MetaAnalysisRecord.consumed_event`,
    :meth:`MetaAnalysisRecord.consumed_observation`, and
    :meth:`MetaAnalysisRecord.produced_finding` to add provenance
    edges.

    On exit: nothing extra — Activity / edge writes happen eagerly
    inside the block. The context-manager wrapping is for API
    symmetry with the eventual Phase 1 wiring (which will add an
    "events_consumed counter" at exit time once the eval scenario
    needs it).

    Args:
        analyzer_name: Stable name of the analyzer
            (``"context-effectiveness"``, ``"schema-evolution"``, …).
        agent_id: Synthetic-agent ID under the ``trellis_meta_``
            namespace. Use :data:`trellis.meta.agents.DEFAULT_META_AGENT_ID`
            unless you have a reason to partition by subsystem.
        registry: Store registry — knowledge plane only.
        include_meta: Reserved for the Phase 1 PackBuilder filter
            wiring; accepted now so callers do not have to be
            re-edited later. Currently ignored.
        merge_window_seconds: Override the default 5-minute merge
            window. Tests use a small value to verify the window
            boundary; CI / cron callers use a larger value.

    Yields:
        A :class:`MetaAnalysisRecord` for adding provenance edges.

    Raises:
        ValueError: If the env var is set to an invalid value.
    """
    del include_meta  # Phase 1 hook; accepted but unused here.

    enabled = _meta_traces_enabled()
    if not enabled:
        logger.info(
            "meta_analysis_disabled",
            analyzer_name=analyzer_name,
            agent_id=agent_id,
            env_var=META_TRACES_ENV_VAR,
        )
        yield MetaAnalysisRecord(
            registry=registry,
            analyzer_name=analyzer_name,
            agent_id=agent_id,
            activity_id=None,
            enabled=False,
        )
        return

    # Synthetic Agent node — creation fails loud if the ID does not
    # match the reserved prefix, per the ADR.
    ensure_meta_agent(registry, agent_id)

    # Merge-within-window: reuse an Activity for rapid-fire analyzer
    # invocations.
    activity_id = _find_recent_activity(
        registry,
        agent_id=agent_id,
        analyzer_name=analyzer_name,
        window_seconds=merge_window_seconds,
    )
    if activity_id is None:
        activity_id = _create_activity(
            registry,
            analyzer_name=analyzer_name,
            agent_id=agent_id,
        )
        logger.debug(
            "meta_activity_created",
            activity_id=activity_id,
            analyzer_name=analyzer_name,
            agent_id=agent_id,
        )
    else:
        logger.debug(
            "meta_activity_merged",
            activity_id=activity_id,
            analyzer_name=analyzer_name,
            agent_id=agent_id,
            merge_window_seconds=merge_window_seconds,
        )

    yield MetaAnalysisRecord(
        registry=registry,
        analyzer_name=analyzer_name,
        agent_id=agent_id,
        activity_id=activity_id,
        enabled=True,
    )


def _create_activity(
    registry: StoreRegistry,
    *,
    analyzer_name: str,
    agent_id: str,
) -> str:
    """Create the Activity node and its ``wasAssociatedWith`` Agent edge.

    The Activity carries the canonical properties named in the ADR
    (``analyzer_name``, ``agent_id``, ``started_at``). Counters
    (``events_consumed``, ``observations_emitted``) belong on the
    Activity per the ADR's example shape — Phase 1's CLI wiring
    populates them at exit time once the wrap-up phase exists. Phase
    0 leaves them off rather than stamping zero values that get
    mistaken for "no work happened".

    Args:
        registry: Store registry.
        analyzer_name: Stable analyzer name.
        agent_id: Synthetic agent ID (already verified by
            ``ensure_meta_agent``).

    Returns:
        The new Activity node ID.
    """
    activity_id = generate_ulid()
    graph_store = registry.knowledge.graph_store
    started_at = utc_now().isoformat()

    graph_store.upsert_node(
        node_id=activity_id,
        node_type=wk.ACTIVITY,
        properties={
            "name": f"{analyzer_name}@{started_at}",
            "analyzer_name": analyzer_name,
            "agent_id": agent_id,
            "started_at": started_at,
        },
    )
    # Stamp the wasAssociatedWith edge so PackBuilder's eventual filter
    # ("Activities whose wasAssociatedWith target starts with
    # trellis_meta_") matches without scanning Activity properties.
    # ``**`` spread keeps mypy happy until the ABC widens to declare
    # the provenance kwargs (see Item 2 follow-up).
    provenance: dict[str, Any] = {
        "source_trace_id": None,
        "agent_id": agent_id,
        "confidence": 1.0,
        "evidence_ref": activity_id,
        "extractor_tier": "DETERMINISTIC",
    }
    graph_store.upsert_edge(
        source_id=activity_id,
        target_id=agent_id,
        edge_type=wk.WAS_ASSOCIATED_WITH,
        properties={"analyzer_name": analyzer_name},
        **provenance,
    )
    return activity_id


__all__ = [
    "DEFAULT_MERGE_WINDOW_SECONDS",
    "META_TRACES_ENV_VAR",
    "MetaAnalysisRecord",
    "record_meta_analysis",
]
