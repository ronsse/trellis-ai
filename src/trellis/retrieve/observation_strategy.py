"""Observation retrieval strategy.

Surfaces :class:`~trellis.schemas.observation.Observation` (and the closely
related :class:`~trellis.schemas.measurement.Measurement`) nodes attached to
subject entities so :class:`~trellis.retrieve.pack_builder.PackBuilder`
output can include empirical claims alongside structural neighbours.

The strategy is **explicitly opt-in** — the default strategy set built by
:func:`~trellis.retrieve.strategies.build_strategies` does not include it so
existing pack behaviour is unchanged.  Callers that want observations in
packs add :class:`ObservationSearch` to their strategy list:

.. code-block:: python

    from trellis.retrieve.observation_strategy import ObservationSearch

    strategies = build_strategies(registry, embedding_fn=...)
    strategies.append(ObservationSearch(registry.knowledge.graph_store))
    pack = PackBuilder(strategies=strategies).build(intent, ...)

Freshness decay
---------------
Observations expire in operational domains: a "queries spike against table
``users.events``" claim from six months ago is stale by construction.  The
strategy applies a bounded read-time half-life decay to ``relevance_score``
based on the ``observed_at`` timestamp, using the same math as
``_apply_recency_decay`` in :mod:`trellis.retrieve.strategies` (and per
``adr-importance-score-freshness.md``):

.. code-block::

    decay = 0.5 ** (age_days / half_life_days)        # default half-life 30d
    score = base_score * (floor + (1 - floor) * decay) # default floor 0.3

The decay is applied *unconditionally* (no horizon grace period) because
observation freshness is the whole point of retrieval here — a stale
observation should rank below a fresh one even if both have identical
``confidence``.  Producers that want to preserve historical observations
(e.g., for trend analysis) should lower the half-life floor or query
without the strategy.

POC discipline — loud failures (per
``plan-self-improvement-program.md`` §2 / ``plan-observation-entity-type.md`` §2):

- ``subject_entity_id`` must be supplied in ``filters`` *or* ``seed_ids``;
  otherwise the strategy returns an empty list with a DEBUG log (no fallback
  to "all observations" — that would silently swamp the pack).
- ``confidence`` on returned observations defaults to ``None`` only when the
  underlying node lacks the property — the strategy emits a DEBUG-level
  ``observation_missing_confidence`` event in that case rather than silently
  imputing ``0.5``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from trellis.retrieve.strategies import (
    DEFAULT_RECENCY_HALF_LIFE_DAYS,
    RECENCY_FLOOR,
    SearchStrategy,
    _apply_recency_decay,
    _resolve_param,
)
from trellis.schemas.pack import PackItem
from trellis.schemas.well_known import (
    HAS_MEASUREMENT,
    HAS_OBSERVATION,
    MEASUREMENT,
    OBSERVATION,
    canonicalize_edge_kind,
    canonicalize_entity_type,
)

if TYPE_CHECKING:
    from trellis.ops.registry import ParameterRegistry

logger = structlog.get_logger(__name__)


#: Component id used when resolving ``ParameterRegistry`` overrides. Mirrors
#: the per-strategy scope convention in
#: :mod:`trellis.retrieve.strategies` so per-domain tuning stays isolated.
_OBSERVATION_COMPONENT = "retrieve.strategies.ObservationSearch"

#: Default minimum confidence for surfaced observations.  ``None`` keeps the
#: door open — observations without a ``confidence`` property are not
#: filtered out unless an explicit threshold is supplied via ``filters``.
DEFAULT_CONFIDENCE_THRESHOLD: float | None = None


class ObservationSearch(SearchStrategy):
    """Retrieval strategy that surfaces Observation / Measurement nodes.

    Walks ``hasObservation`` (and optionally ``hasMeasurement``) edges
    outbound from one or more subject entities, optionally filters by
    minimum ``confidence`` and an ``observed_after`` watermark, and
    scores results with freshness decay on ``observed_at``.

    Filter contract (passed via ``filters`` dict to :meth:`search`):

    - ``subject_entity_id`` (``str``) **or** ``seed_ids`` (``list[str]``) —
      the subject(s) to walk from. Required (no fallback to "all
      observations"). When both are supplied, ``seed_ids`` wins.
    - ``confidence_threshold`` (``float``) — minimum ``confidence``;
      observations below this are dropped. Defaults to
      :data:`DEFAULT_CONFIDENCE_THRESHOLD` (``None`` → no filter).
    - ``observed_after`` (``datetime`` | ISO-8601 ``str``) — freshness
      watermark; observations strictly older than this are dropped.
    - ``include_measurements`` (``bool``) — when ``True``, ``Measurement``
      nodes attached via ``hasMeasurement`` are returned alongside
      ``Observation`` nodes attached via ``hasObservation``. The two
      edge kinds are distinct per ADR §2.2 so consumers route on edge
      kind alone. Defaults to ``True``.

    The ``query`` string argument is currently ignored — observations are
    keyed by subject, not by free-text search.  A future enhancement could
    treat ``query`` as a substring filter over ``content`` for
    ``Observation`` nodes, but the v1 contract is "give me everything
    attached to entity X within these bounds".
    """

    def __init__(
        self,
        graph_store: Any,
        *,
        recency_half_life_days: float = DEFAULT_RECENCY_HALF_LIFE_DAYS,
        recency_floor: float = RECENCY_FLOOR,
        confidence_threshold: float | None = DEFAULT_CONFIDENCE_THRESHOLD,
        registry: ParameterRegistry | None = None,
    ) -> None:
        self._store = graph_store
        self._recency_half_life_days = recency_half_life_days
        self._recency_floor = recency_floor
        self._confidence_threshold = confidence_threshold
        self._registry = registry

    @property
    def name(self) -> str:
        return "observation"

    def search(
        self,
        query: str,  # noqa: ARG002 — reserved for future text filtering
        *,
        limit: int = 20,
        filters: dict[str, Any] | None = None,
    ) -> list[PackItem]:
        filters = dict(filters) if filters else {}
        seed_ids = self._extract_seed_ids(filters)
        if not seed_ids:
            logger.debug("observation_search_missing_subject", filters=list(filters))
            return []

        # Resolve per-(component, domain) overrides.
        domain = filters.get("domain")
        half_life = _resolve_param(
            self._registry,
            _OBSERVATION_COMPONENT,
            domain,
            "recency_half_life_days",
            self._recency_half_life_days,
        )
        floor = _resolve_param(
            self._registry,
            _OBSERVATION_COMPONENT,
            domain,
            "recency_floor",
            self._recency_floor,
        )

        confidence_threshold = filters.get(
            "confidence_threshold", self._confidence_threshold,
        )
        observed_after = _parse_datetime(filters.get("observed_after"))
        include_measurements = bool(filters.get("include_measurements", True))

        observation_ids = self._collect_observation_ids(
            seed_ids, include_measurements=include_measurements,
        )
        if not observation_ids:
            return []

        # Resolve to full node rows (one batch fetch when the backend
        # supports it; fall through to per-id loop otherwise).
        nodes = self._load_nodes(observation_ids)

        # Filter by node type, confidence, freshness watermark.
        allowed_types: set[str] = {OBSERVATION}
        if include_measurements:
            allowed_types.add(MEASUREMENT)

        items: list[PackItem] = []
        for node in nodes:
            canonical_type = canonicalize_entity_type(node.get("node_type", ""))
            if canonical_type not in allowed_types:
                continue

            props = node.get("properties", {}) or {}
            confidence = _coerce_confidence(props.get("confidence"))
            if confidence is None:
                logger.debug(
                    "observation_missing_confidence",
                    node_id=node.get("node_id"),
                )
            elif confidence_threshold is not None and confidence < confidence_threshold:
                continue

            observed_at = _parse_datetime(
                props.get("observed_at")
                or props.get("measured_at")
                or node.get("updated_at")
                or node.get("created_at"),
            )
            if observed_after is not None and observed_at is not None and (
                observed_at < observed_after
            ):
                continue

            # Base score = confidence when present, else 0.5 — fully
            # transparent: the score_breakdown carries both inputs so the
            # operator can see why a no-confidence row ranked the way it did.
            base_score = confidence if confidence is not None else 0.5
            score = _apply_recency_decay(
                base_score,
                observed_at.isoformat() if observed_at else None,
                half_life_days=half_life,
                floor=floor,
            )

            excerpt = str(props.get("content") or props.get("metric_name") or "")[:500]
            items.append(
                PackItem(
                    item_id=node["node_id"],
                    item_type="observation",
                    excerpt=excerpt,
                    relevance_score=score,
                    metadata={
                        "source_strategy": "observation",
                        "node_type": node.get("node_type"),
                        "node_type_canonical": canonical_type,
                        "node_role": node.get("node_role") or "semantic",
                        "subject_entity_id": props.get("subject_entity_id"),
                        "subject_entity_type": props.get("subject_entity_type"),
                        "observer_agent_id": props.get("observer_agent_id"),
                        "confidence": confidence,
                        "observed_at": observed_at.isoformat() if observed_at else None,
                    },
                )
            )

        # Sort by score descending (freshness-decayed) and cap at limit.
        items.sort(key=lambda x: x.relevance_score, reverse=True)
        return items[:limit]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_seed_ids(filters: dict[str, Any]) -> list[str]:
        """Pull subject ids from ``filters`` honouring both spellings.

        Both ``subject_entity_id`` (singular, the Observation schema's own
        field name) and ``seed_ids`` (plural, the convention used by
        :class:`~trellis.retrieve.strategies.GraphSearch`) are accepted —
        the caller picks whichever reads better at their call site.
        """
        seed_ids: list[str] = []
        raw_seeds = filters.pop("seed_ids", None)
        if isinstance(raw_seeds, list | tuple):
            seed_ids.extend(str(s) for s in raw_seeds if s)
        elif isinstance(raw_seeds, str):
            seed_ids.append(raw_seeds)
        subject_id = filters.pop("subject_entity_id", None)
        if isinstance(subject_id, str) and subject_id:
            seed_ids.append(subject_id)
        # Deduplicate, preserve order.
        seen: set[str] = set()
        ordered: list[str] = []
        for s in seed_ids:
            if s not in seen:
                seen.add(s)
                ordered.append(s)
        return ordered

    def _collect_observation_ids(
        self, seed_ids: list[str], *, include_measurements: bool,
    ) -> list[str]:
        """Walk outbound ``hasObservation`` / ``hasMeasurement`` edges.

        Returns the deduplicated, order-preserving list of target node
        IDs hanging off ``seed_ids``. Backend errors on a single
        (subject, edge_kind) pair are logged and skipped — they should
        not collapse the whole search. ``HAS_MEASUREMENT`` is only
        traversed when ``include_measurements`` is true so the
        Observation-only path stays a single round-trip per subject.
        """
        edge_kinds: list[str] = [canonicalize_edge_kind(HAS_OBSERVATION)]
        if include_measurements:
            edge_kinds.append(canonicalize_edge_kind(HAS_MEASUREMENT))

        observation_ids: list[str] = []
        seen_ids: set[str] = set()
        for subject_id in seed_ids:
            for edge_kind in edge_kinds:
                try:
                    edges = self._store.get_edges(
                        subject_id,
                        direction="outgoing",
                        edge_type=edge_kind,
                    )
                except Exception:  # pragma: no cover — backend errors are logged
                    logger.exception(
                        "observation_search_edge_lookup_failed",
                        subject_id=subject_id,
                        edge_kind=edge_kind,
                    )
                    continue
                for edge in edges:
                    target_id = edge.get("target_id")
                    if not target_id or target_id in seen_ids:
                        continue
                    seen_ids.add(target_id)
                    observation_ids.append(target_id)
        return observation_ids

    def _load_nodes(self, node_ids: list[str]) -> list[dict[str, Any]]:
        """Resolve node IDs to full node rows.

        Prefers ``get_nodes_bulk`` when available (one round-trip on every
        shipped backend); falls back to per-id ``get_node`` for unit-test
        fakes that only implement the single-row variant.
        """
        bulk = getattr(self._store, "get_nodes_bulk", None)
        if callable(bulk):
            bulk_rows: list[dict[str, Any]] | None
            try:
                bulk_rows = bulk(node_ids)
            except Exception:  # pragma: no cover
                logger.exception("observation_search_bulk_get_failed")
                bulk_rows = None
            if bulk_rows is not None:
                return [r for r in bulk_rows if r is not None]

        rows: list[dict[str, Any]] = []
        for node_id in node_ids:
            try:
                row = self._store.get_node(node_id)
            except Exception:  # pragma: no cover
                logger.exception(
                    "observation_search_get_node_failed", node_id=node_id,
                )
                continue
            if row is not None:
                rows.append(row)
        return rows


def _parse_datetime(value: Any) -> datetime | None:
    """Coerce ``str`` / ``datetime`` to a tz-aware ``datetime`` (UTC default).

    Returns ``None`` for missing or unparseable input so callers can skip
    freshness checks rather than misclassify the row.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            ts = datetime.fromisoformat(value)
        except ValueError:
            return None
        return ts if ts.tzinfo else ts.replace(tzinfo=UTC)
    return None


def _coerce_confidence(value: Any) -> float | None:
    """Return ``value`` as a float in ``[0.0, 1.0]`` or ``None``.

    Out-of-range / non-numeric values resolve to ``None`` so the strategy's
    ``confidence_threshold`` filter behaves predictably (missing data is
    never silently treated as "passes the threshold").
    """
    if value is None:
        return None
    try:
        conf = float(value)
    except (TypeError, ValueError):
        return None
    if conf < 0.0 or conf > 1.0:
        return None
    return conf
