"""Query-pattern Observer — deterministic Observation/Measurement extractor.

Reads a batch of query-log records (one entry per executed query) and
emits :class:`~trellis.schemas.extraction.EntityDraft` rows describing
*how often* and *how* each subject entity is touched.  The drafts are
pure data: this extractor never writes to a store.  Callers route the
result through :class:`~trellis.mutate.executor.MutationExecutor`.

This is the deterministic-tier reference implementation for Item 1 of the
self-improvement program — see
``docs/design/plan-observation-entity-type.md`` §6 for the planning notes
and ``docs/design/adr-observation-entity-type.md`` §3 for the design
rationale.  An LLM-tier observation producer ships as a separate worker
behind an opt-in flag in a follow-on.

What it produces
----------------
Given a query log targeting tables, the extractor emits:

- One :class:`~trellis.schemas.well_known.MEASUREMENT` ``EntityDraft`` per
  subject entity with ``metric_name="query_count"`` and
  ``metric_value=<query_count>``.
- One :class:`~trellis.schemas.well_known.OBSERVATION` ``EntityDraft`` per
  subject entity whose query count exceeds a small floor (default 1).
  The observation's ``content`` summarises the activity over the window.
- For every emitted Measurement *and* Observation, a
  ``hasObservation`` :class:`~trellis.schemas.extraction.EdgeDraft` from
  the subject to the new node.

Why both? See ADR §1: ``Measurement`` is the **machine-comparable scalar**
home (graphable, time-series friendly); ``Observation`` is the
**narrative, evidence-bearing** claim that PackBuilder surfaces as
context.  They co-exist deliberately.

Loud failures (per ``plan-self-improvement-program.md`` §2):

- Missing ``subject_entity_id`` on a record raises ``ValueError`` — the
  extractor does **not** skip malformed rows.
- ``timestamp`` strings that don't parse via ``datetime.fromisoformat``
  raise ``ValueError`` for the same reason.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar

import structlog

from trellis.core.base import utc_now
from trellis.extract.base import ExtractorTier
from trellis.schemas.extraction import (
    EdgeDraft,
    EntityDraft,
    ExtractionProvenance,
    ExtractionResult,
)
from trellis.schemas.well_known import (
    HAS_OBSERVATION,
    MEASUREMENT,
    OBSERVATION,
)

if TYPE_CHECKING:
    from trellis.extract.context import ExtractionContext

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class QueryLogRecord:
    """One record in the input query log.

    The deterministic shape we expect — extractors are pure on already-
    parsed input, so callers normalise their own log formats into this
    shape before invocation.

    Attributes:
        subject_entity_id: Which entity was queried (e.g.
            ``"dataset:warehouse/public/users"``). Required.
        subject_entity_type: Open-string entity type. Defaults to
            ``"Dataset"`` to match the most common case; producers can
            override per-row.
        timestamp: When the query ran. ISO-8601 string or ``datetime``.
            Required (no silent default — see module docstring).
        observer_agent_id: Identifier of the agent (or pipeline run) that
            generated the log. Falls back to the extractor's own name.
    """

    subject_entity_id: str
    timestamp: datetime
    subject_entity_type: str = "Dataset"
    observer_agent_id: str | None = None


@dataclass(frozen=True)
class _WindowedAggregate:
    """Intermediate aggregate produced by ``_aggregate``."""

    subject_entity_id: str
    subject_entity_type: str
    observer_agent_id: str
    query_count: int
    window_start: datetime
    window_end: datetime
    sample_observers: tuple[str, ...] = field(default_factory=tuple)


class QueryPatternObserver:
    """Deterministic-tier extractor: query logs → Observation/Measurement drafts.

    The extractor implements the
    :class:`~trellis.extract.base.Extractor` Protocol so it can be wired
    into :class:`~trellis.extract.dispatcher.ExtractionDispatcher`. It
    accepts either:

    - A list of :class:`QueryLogRecord` instances, **or**
    - A list of dicts shaped like one (``subject_entity_id``,
      ``timestamp``, optional ``subject_entity_type`` /
      ``observer_agent_id``). The dict form keeps the call site simple
      for tests / scripts that produce dicts already.

    The output ``ExtractionResult`` carries:

    - One Measurement EntityDraft per subject (``metric_name="query_count"``).
    - One Observation EntityDraft per subject whose ``query_count`` exceeds
      ``observation_min_query_count`` (default 1 — single-shot queries
      don't need a narrative claim, they're already captured by the
      measurement).
    - One ``hasObservation`` EdgeDraft per emitted Measurement/Observation.

    The extractor is pure: no store access, no mutation pipeline calls.
    """

    name = "query_pattern_observer"
    tier = ExtractorTier.DETERMINISTIC
    supported_sources: ClassVar[list[str]] = ["query-log"]
    version = "0.1.0"

    def __init__(
        self,
        *,
        observation_min_query_count: int = 1,
        default_observer_agent_id: str = "trellis_workers.query_pattern_observer",
    ) -> None:
        if observation_min_query_count < 0:
            msg = (
                "observation_min_query_count must be >= 0; "
                f"got {observation_min_query_count!r}"
            )
            raise ValueError(msg)
        self._observation_min = observation_min_query_count
        self._default_observer = default_observer_agent_id

    async def extract(
        self,
        raw_input: Any,
        *,
        source_hint: str | None = None,
        context: ExtractionContext | None = None,
    ) -> ExtractionResult:
        """Run the extractor.

        Args:
            raw_input: Iterable of :class:`QueryLogRecord` or dicts.
            source_hint: Routing hint; passed through into provenance.
            context: Per-call preferences; ignored by deterministic tier
                (no cost budget applies).

        Returns:
            An :class:`ExtractionResult` whose ``entities`` contain
            Measurement + Observation drafts and whose ``edges`` contain
            the ``hasObservation`` links.
        """
        del context  # deterministic — no cost budget

        records = list(self._normalise(raw_input))
        aggregates = self._aggregate(records)

        entities: list[EntityDraft] = []
        edges: list[EdgeDraft] = []
        for agg in aggregates:
            measurement_draft = self._build_measurement(agg)
            entities.append(measurement_draft)
            assert measurement_draft.entity_id is not None
            edges.append(
                EdgeDraft(
                    source_id=agg.subject_entity_id,
                    target_id=measurement_draft.entity_id,
                    edge_kind=HAS_OBSERVATION,
                    # Subjects are passed through to ``MutationExecutor``
                    # which checks FK on the subject. The subject is
                    # expected to already exist in the graph — but
                    # query-log producers should not be forced to ingest
                    # tables they merely observed, so allow dangling on
                    # the subject side.
                    allow_dangling=True,
                )
            )

            if agg.query_count >= self._observation_min:
                obs_draft = self._build_observation(agg)
                entities.append(obs_draft)
                assert obs_draft.entity_id is not None
                edges.append(
                    EdgeDraft(
                        source_id=agg.subject_entity_id,
                        target_id=obs_draft.entity_id,
                        edge_kind=HAS_OBSERVATION,
                        allow_dangling=True,
                    )
                )

        logger.info(
            "query_pattern_observer_extracted",
            records=len(records),
            subjects=len(aggregates),
            entities=len(entities),
            edges=len(edges),
            source_hint=source_hint,
        )

        return ExtractionResult(
            entities=entities,
            edges=edges,
            extractor_used=self.name,
            tier=self.tier.value,
            provenance=ExtractionProvenance(
                extractor_name=self.name,
                extractor_version=self.version,
                source_hint=source_hint,
            ),
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _normalise(self, raw_input: Any) -> Iterable[QueryLogRecord]:
        if not isinstance(raw_input, Iterable) or isinstance(raw_input, str | bytes):
            msg = (
                "QueryPatternObserver expects an iterable of QueryLogRecord "
                f"or dicts; got {type(raw_input).__name__}"
            )
            raise TypeError(msg)
        for idx, row in enumerate(raw_input):
            if isinstance(row, QueryLogRecord):
                yield row
                continue
            if not isinstance(row, dict):
                msg = (
                    "QueryPatternObserver row must be QueryLogRecord or dict; "
                    f"row {idx} is {type(row).__name__}"
                )
                raise TypeError(msg)
            subject_id = row.get("subject_entity_id")
            if not isinstance(subject_id, str) or not subject_id:
                msg = (
                    f"QueryPatternObserver row {idx} missing required "
                    "'subject_entity_id' (no silent skip — see module "
                    "docstring)"
                )
                raise ValueError(msg)
            ts_raw = row.get("timestamp")
            timestamp = _parse_timestamp(ts_raw, row_index=idx)
            yield QueryLogRecord(
                subject_entity_id=subject_id,
                timestamp=timestamp,
                subject_entity_type=str(row.get("subject_entity_type", "Dataset")),
                observer_agent_id=row.get("observer_agent_id"),
            )

    def _aggregate(
        self, records: list[QueryLogRecord],
    ) -> list[_WindowedAggregate]:
        """Group records by subject and produce one aggregate per subject."""
        if not records:
            return []

        by_subject: dict[str, list[QueryLogRecord]] = {}
        for rec in records:
            by_subject.setdefault(rec.subject_entity_id, []).append(rec)

        aggregates: list[_WindowedAggregate] = []
        for subject_id, rows in by_subject.items():
            timestamps = [r.timestamp for r in rows]
            window_start = min(timestamps)
            window_end = max(timestamps)
            # All rows for one subject must agree on entity_type
            # (different domains may classify the same id differently —
            # that's a producer-side normalisation problem, not ours).
            # We take the most common value to be deterministic and emit
            # a debug log when there's drift.
            type_counter = Counter(r.subject_entity_type for r in rows)
            most_common_type, _ = type_counter.most_common(1)[0]
            if len(type_counter) > 1:
                logger.debug(
                    "query_pattern_observer_mixed_subject_types",
                    subject_entity_id=subject_id,
                    types=dict(type_counter),
                )
            # Same idea for the observer agent id — take the dominant
            # value; fall through to the default when none supplied.
            observer_counter = Counter(
                (r.observer_agent_id or self._default_observer) for r in rows
            )
            most_common_observer, _ = observer_counter.most_common(1)[0]
            sample_observers = tuple(sorted(observer_counter))[:3]
            aggregates.append(
                _WindowedAggregate(
                    subject_entity_id=subject_id,
                    subject_entity_type=most_common_type,
                    observer_agent_id=most_common_observer,
                    query_count=len(rows),
                    window_start=window_start,
                    window_end=window_end,
                    sample_observers=sample_observers,
                )
            )
        # Deterministic ordering (subjects sorted by id) so re-running on
        # the same input produces byte-identical output.
        aggregates.sort(key=lambda a: a.subject_entity_id)
        return aggregates

    def _build_measurement(self, agg: _WindowedAggregate) -> EntityDraft:
        entity_id = (
            f"measurement:query_count:{agg.subject_entity_id}:"
            f"{agg.window_start.isoformat()}:{agg.window_end.isoformat()}"
        )
        properties: dict[str, Any] = {
            "subject_entity_id": agg.subject_entity_id,
            "subject_entity_type": agg.subject_entity_type,
            "metric_name": "query_count",
            "metric_value": float(agg.query_count),
            "unit": "count",
            "measured_at": utc_now().isoformat(),
            "observer_agent_id": agg.observer_agent_id,
            "window_start": agg.window_start.isoformat(),
            "window_end": agg.window_end.isoformat(),
            "method": self.name,
        }
        return EntityDraft(
            entity_id=entity_id,
            entity_type=MEASUREMENT,
            name=f"query_count({agg.subject_entity_id})",
            properties=properties,
        )

    def _build_observation(self, agg: _WindowedAggregate) -> EntityDraft:
        entity_id = (
            f"observation:query_pattern:{agg.subject_entity_id}:"
            f"{agg.window_start.isoformat()}:{agg.window_end.isoformat()}"
        )
        content = (
            f"Subject {agg.subject_entity_id} ({agg.subject_entity_type}) "
            f"observed in {agg.query_count} query log row(s) between "
            f"{agg.window_start.isoformat()} and {agg.window_end.isoformat()}."
        )
        # Confidence reflects sample size: a single-observation aggregate
        # is barely a pattern; many rows is convincing. Saturates around
        # 30 rows where every additional sample adds <1% confidence.
        confidence = _confidence_from_sample(agg.query_count)
        properties: dict[str, Any] = {
            "subject_entity_id": agg.subject_entity_id,
            "subject_entity_type": agg.subject_entity_type,
            "observer_agent_id": agg.observer_agent_id,
            "content": content,
            "confidence": confidence,
            "observed_at": utc_now().isoformat(),
            "kind": "query_pattern",
            "sample_size": agg.query_count,
            "window_start": agg.window_start.isoformat(),
            "window_end": agg.window_end.isoformat(),
            "method": self.name,
            "sample_observers": list(agg.sample_observers),
        }
        return EntityDraft(
            entity_id=entity_id,
            entity_type=OBSERVATION,
            name=f"query_pattern({agg.subject_entity_id})",
            properties=properties,
        )


def _parse_timestamp(value: Any, *, row_index: int) -> datetime:
    """Coerce a timestamp value to a tz-aware UTC ``datetime``.

    Raises ``ValueError`` on missing / unparseable inputs — the loud-fail
    discipline from the program plan §2.
    """
    if value is None:
        msg = f"row {row_index} missing required 'timestamp'"
        raise ValueError(msg)
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not isinstance(value, str):
        msg = (
            f"row {row_index} 'timestamp' must be a datetime or ISO-8601 string; "
            f"got {type(value).__name__}"
        )
        raise TypeError(msg)
    try:
        ts = datetime.fromisoformat(value)
    except ValueError as exc:
        msg = (
            f"row {row_index} 'timestamp' is not a valid ISO-8601 string: "
            f"{value!r}"
        )
        raise ValueError(msg) from exc
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def _confidence_from_sample(sample_size: int) -> float:
    """Map a sample size to a confidence in ``(0.0, 1.0)``.

    Uses a simple ``1 - exp(-n/k)`` shape with ``k=10`` so:

    - n=1   → ~0.10
    - n=3   → ~0.26
    - n=10  → ~0.63
    - n=30  → ~0.95

    This is opinionated but bounded: a single-row "pattern" doesn't get
    treated like a 100-row one, and the value is documented so operators
    can reason about why an observation ranks where it does.
    """
    if sample_size <= 0:
        return 0.0
    return round(1.0 - math.exp(-sample_size / 10.0), 4)
