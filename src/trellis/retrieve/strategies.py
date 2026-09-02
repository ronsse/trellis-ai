"""Search strategies for pack assembly."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol, TypedDict, runtime_checkable

import structlog

from trellis.retrieve.excerpts import truncate_excerpt
from trellis.schemas.extraction import (
    EXTRACTION_STATUS_PROPERTY,
    EXTRACTION_STATUS_UNCONFIRMED,
)
from trellis.schemas.pack import PackItem
from trellis.schemas.parameters import ParameterScope
from trellis.schemas.well_known import (
    canonicalize_entity_type,
    expand_entity_type_query,
)
from trellis.stores.base.graph_query import FilterClause, NodeQuery

if TYPE_CHECKING:
    from collections.abc import Callable

    from trellis.ops.registry import ParameterRegistry
    from trellis.stores.registry import StoreRegistry

logger = structlog.get_logger()


#: Default half-life for recency decay (days). After this many days a
#: score is halved relative to its un-decayed value (above the floor).
DEFAULT_RECENCY_HALF_LIFE_DAYS = 30.0

#: Floor for recency decay — a very old item still retains at least this
#: fraction of its original relevance. Prevents high-importance archival
#: content from being suppressed entirely.
RECENCY_FLOOR = 0.3

#: Grace period before importance-score staleness decay starts. Below this
#: age (measured from ``importance_scored_at``) the legacy multiplier is
#: applied as-is; past it, the score decays with the same half-life math
#: as recency decay. See adr-importance-score-freshness §3.4.
DEFAULT_IMPORTANCE_FRESH_HORIZON_DAYS = 180.0

#: Floor for importance staleness decay — never zero out a stale score,
#: just dampen it. Same semantics as :data:`RECENCY_FLOOR`.
DEFAULT_IMPORTANCE_DECAY_FLOOR = 0.3

#: Only decay importance scores at or above this threshold. Low scores
#: barely move the multiplier already, so the freshness check would
#: cost more than it gains. See adr-importance-score-freshness §3.4.
DEFAULT_IMPORTANCE_DECAY_THRESHOLD = 0.5

#: Default scoring boosts inside :class:`GraphSearch`. Exposed as
#: module-level constants so they can be resolved through
#: :class:`ParameterRegistry` with these values as fallback defaults.
GRAPH_DOMAIN_MATCH_BOOST = 1.3
GRAPH_CURATED_BOOST = 1.3
GRAPH_DESCRIPTION_BOOST = 1.2
GRAPH_POSITION_DECAY_STEP = 0.05

# Component ids used when resolving registry overrides. Each SearchStrategy
# has its own scope so per-domain tuning stays isolated.
_KEYWORD_COMPONENT = "retrieve.strategies.KeywordSearch"
_SEMANTIC_COMPONENT = "retrieve.strategies.SemanticSearch"
_GRAPH_COMPONENT = "retrieve.strategies.GraphSearch"

#: Over-fetch multiplier for the semantic axis when a domain scope is active.
#: The vector stores cannot express the ``content_tags`` default-pass facet
#: filter (a scalar store-side filter would hard-exclude domain-less rows —
#: the #254 defect), so :class:`SemanticSearch` fetches extra candidates and
#: applies the Python-side default-pass post-filter, then slices back to
#: ``limit``. Over-fetching keeps a heavily-mismatched domain from thinning
#: semantic recall (the trade-off #254 accepted, closed here for #262).
_SEMANTIC_DOMAIN_OVERFETCH = 4

#: Over-fetch multiplier for the unseeded graph branch. ``GraphStore.query``
#: is ``ORDER BY created_at DESC LIMIT n`` on every shipped backend, so this
#: multiplier is not a recall knob — it is the *entire* candidate window, and
#: it is a fixed row count rather than a fraction of the graph. See
#: :data:`GRAPH_SELECTION_RECENCY_WINDOW`.
_GRAPH_RECENCY_OVERFETCH = 4

#: Value of ``PackItem.metadata["graph_selection"]`` when the graph axis
#: picked its candidates by recency because nothing supplied seeds. Stamped
#: on every item so the two selection modes are distinguishable in
#: ``PACK_ASSEMBLED.injected_items[]`` — the axis's query-independence is a
#: measurable property of a served pack, not a claim in a docstring.
GRAPH_SELECTION_RECENCY_WINDOW = "recency_window"

#: Value of ``PackItem.metadata["graph_selection"]`` when the graph axis
#: expanded a seed set — the only mode in which the axis is a function of
#: the caller's query.
GRAPH_SELECTION_SEEDED = "seeded"

#: Filter keys that are **retrieval controls addressed to the graph axis**,
#: not metadata predicates about a stored row.
#:
#: ``PackBuilder.build`` injects ``include_structural`` into the single
#: ``filters`` mapping it hands to *every* strategy, and a caller may pass
#: any of these through ``filters=`` directly. :class:`GraphSearch` ``pop``\ s
#: them; the document and vector stores do not know them, and both compile an
#: unknown filter key to **hard metadata equality**, which matches no row.
#: So a caller that asked for one extra category of graph node was silently
#: getting the keyword and semantic axes emptied: measured on a three-axis
#: pack, ``include_structural=True`` took it from 3 items to 1, with no
#: warning, no ``strategy_failures`` entry and no ``RejectedItem`` — the
#: strategies returned ``[]``, which is indistinguishable from "nothing
#: matched" (the #404 failure shape, one layer up).
#:
#: A closed allow-list of *controls* rather than a deny-list of metadata: the
#: opposite of :mod:`trellis.retrieve.servable`'s posture, and deliberately —
#: stored metadata keys are an open set that must stay servable by default,
#: while these are consumed by the graph axis and are added whenever it grows
#: a new ``pop``. That rule is enforced rather than asserted:
#: ``TestTheAllowListCoversEveryPop`` derives the popped keys from
#: ``GraphSearch``'s own AST, because the hand-written set this replaced
#: compared one literal against another and could not notice the three keys
#: it was already missing.
#:
#: ``seed_ids`` is *not* owned by :class:`GraphSearch` alone —
#: :class:`~trellis.retrieve.observation_strategy.ObservationSearch` reads it
#: as its subject set. That is why the stripping happens inside the two
#: strategies that forward filters to a store rather than at the collect
#: seam: a seam-level strip would take ``seed_ids`` away from a strategy that
#: needs it.
#:
#: Latent since the initial commit, and it stayed latent because nothing in
#: the repository passed one. #375/#436 changed that: surfacing a
#: newly-written meta-Activity now *requires* ``include_structural=True``
#: alongside ``include_meta=True``, so the documented escape hatch walked
#: straight onto it. The same escape hatch was still half-open after #443:
#: ``depth``, ``edge_types`` and ``node_type`` are advertised by
#: ``GraphSearch``'s own docstrings ("Consumes ``depth`` / ``edge_types``
#: from *filters*") and were omitted from this set, so ``filters={"depth": 3}``
#: emptied a measured three-axis pack to **zero** items — worse than the
#: original, because ``depth`` and ``edge_types`` are popped only in the
#: *seeded* branch and production always takes the unseeded one (#371), where
#: they fell through into ``query_props`` and hard-equality-filtered the graph
#: axis as well.
GRAPH_CONTROL_FILTER_KEYS = frozenset(
    {
        "seed_ids",
        "include_structural",
        "include_unconfirmed",
        "depth",
        "edge_types",
        "node_type",
    }
)


def _exclude_and_log(
    nodes: list[dict[str, Any]],
    keep: Callable[[dict[str, Any]], bool],
    event: str,
) -> list[dict[str, Any]]:
    """Apply one of ``GraphSearch``'s client-side node filters, counting it.

    These drops happen before a ``PackItem`` exists, so they produce no
    ``RejectedItem`` and never reach
    :func:`~trellis.retrieve.withholding.summarize_withheld`. The debug line
    is the only observable they have — which is not enough on its own (#404
    said so about exactly this shape) but is strictly more than nothing.
    Silent when the filter removed nothing, so an ``excluded=0`` line never
    dilutes the one that matters.
    """
    kept = [n for n in nodes if keep(n)]
    if len(kept) != len(nodes):
        logger.debug(event, excluded=len(nodes) - len(kept))
    return kept


def strip_graph_controls(filters: dict[str, Any] | None) -> dict[str, Any] | None:
    """Drop :data:`GRAPH_CONTROL_FILTER_KEYS` before a store-side filter call.

    A mapping that held nothing *but* controls returns ``None``, matching
    what the stores expect for "no filters" — a ``{}`` is a filter that some
    backends read as a predicate over nothing. An input that was *already*
    empty is passed through unchanged (``None`` stays ``None``, ``{}`` stays
    ``{}``), which is what ``test_empty_input_passes_through`` pins; the
    caller had nothing to strip, so there is nothing to normalise.
    """
    if not filters:
        return filters
    return {
        k: v for k, v in filters.items() if k not in GRAPH_CONTROL_FILTER_KEYS
    } or None


@runtime_checkable
class GraphSeedExtractor(Protocol):
    """Turns a retrieval intent into graph node ids to expand from.

    This is the *only* place query relevance can enter
    :class:`GraphSearch`. The unseeded branch calls
    ``GraphStore.query``, which every shipped backend implements as
    ``ORDER BY created_at DESC LIMIT n`` — it takes no query argument and
    has no text index to consult. So an extractor is not an optimisation
    on top of a search; without one there is no search.

    Implementations must be **total and cheap**: ``extract`` runs inline
    on the pack-assembly path, once per pack. Returning ``[]`` is a valid
    answer meaning "this intent anchors on no entity I can name", and
    :class:`GraphSearch` treats it as such — it falls back to the recency
    window rather than emptying the axis, because a seeding miss must not
    cost the caller an axis it had before.

    Known implementations:

    * :class:`~trellis.retrieve.semantic_seeds.SemanticSeedExtractor` —
      embeds the intent and maps top-K vector hits back to entity ids.
      Requires entity-summary documents in the vector store; read that
      module's docstring before wiring it, because a corpus without them
      makes it a measured no-op (#371); a production path needs #375 first.
    """

    def extract(self, intent: str) -> list[str]:
        """Return graph node ids to seed traversal from, best first."""
        ...  # pragma: no cover - protocol declaration


def _passes_domain_scope(metadata: dict[str, Any], domain: str) -> bool:
    """Default-pass domain check for semantic-axis hits (#254 / #262).

    Mirrors the keyword axis's ``content_tags`` facet semantics at the
    Python boundary — the vector stores only offer a hard-equality scalar
    metadata filter, which would hard-exclude domain-less rows. Vector rows
    carry full document metadata (``build_vector_row`` copies it), so a hit
    may hold ``domain`` in either storage location: scalar ``metadata.domain``
    or the ``metadata.content_tags.domain`` facet (a list).

    Semantics: pass when no domain is present in either location
    (default-pass — a domain-less memory is never hard-excluded); pass when
    either location matches; exclude only on explicit mismatch.
    """
    values: list[Any] = []
    scalar = metadata.get("domain")
    if scalar is not None:
        values.append(scalar)
    tags = metadata.get("content_tags")
    if isinstance(tags, dict):
        facet = tags.get("domain")
        if isinstance(facet, list):
            values.extend(facet)
        elif facet is not None:
            values.append(facet)
    if not values:
        return True
    return domain in values


def _resolve_param(
    registry: ParameterRegistry | None,
    component_id: str,
    domain: str | None,
    key: str,
    default: Any,
) -> Any:
    """Resolve a scoring param via registry, or fall back to ``default``.

    Scope is ``(component_id, domain)`` — per-(intent_family, tool_name)
    tuning is deferred to a follow-up when strategies gain intent-family
    awareness.
    """
    if registry is None:
        return default
    return registry.get(
        ParameterScope(component_id=component_id, domain=domain),
        key,
        default,
    )


class SearchStrategy(ABC):
    """Base class for retrieval strategies."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Strategy name for reporting."""

    @abstractmethod
    def search(
        self,
        query: str,
        *,
        limit: int = 20,
        filters: dict[str, Any] | None = None,
    ) -> list[PackItem]:
        """Execute search and return ranked PackItems."""


def _apply_importance(
    base_score: float,
    metadata: dict[str, Any],
    *,
    now: datetime | None = None,
    fresh_horizon_days: float = DEFAULT_IMPORTANCE_FRESH_HORIZON_DAYS,
    floor: float = DEFAULT_IMPORTANCE_DECAY_FLOOR,
    decay_threshold: float = DEFAULT_IMPORTANCE_DECAY_THRESHOLD,
    half_life_days: float = DEFAULT_RECENCY_HALF_LIFE_DAYS,
) -> float:
    """Apply importance weighting with bounded staleness decay.

    Decay is applied *only* when:

    * the raw importance is at or above ``decay_threshold``, AND
    * ``importance_scored_at`` (located on ``metadata["content_tags"]`` or
      directly on ``metadata``) is past the ``fresh_horizon_days`` horizon.

    Below those thresholds the function returns the legacy behavior:
    ``base_score * (1.0 + clamp(importance, 0, 1))``.

    Greenfield writer contract (adr-importance-score-freshness §3.5): if
    ``auto_importance`` is set above ``decay_threshold`` but
    ``importance_scored_at`` is missing, raises ``ValueError``. There is
    no fallback to ``classified_at`` and no "treat as fresh" path —
    every code path that writes ``auto_importance`` must also stamp.
    """
    importance = float(metadata.get("auto_importance", 0.0))
    if importance == 0.0:
        # No importance score → no multiplier, no freshness check needed.
        return base_score
    importance = max(0.0, min(1.0, importance))  # clamp 0-1
    if importance < decay_threshold:
        # Sub-threshold scores skip the freshness check entirely — the
        # multiplier is small enough that staleness barely moves it.
        return base_score * (1.0 + importance)

    # Above threshold: locate the freshness witness. ContentTags is the
    # canonical home; `metadata["importance_scored_at"]` is supported as
    # a flat alias for stores that flatten tags into top-level metadata.
    tags = metadata.get("content_tags") or {}
    raw_stamp = tags.get("importance_scored_at") if isinstance(tags, dict) else None
    if raw_stamp is None:
        raw_stamp = metadata.get("importance_scored_at")
    if raw_stamp is None:
        msg = (
            "auto_importance is set but importance_scored_at is missing — "
            "writer path is broken. Every code path that writes "
            "auto_importance must also stamp importance_scored_at "
            "(see adr-importance-score-freshness.md §3.5). "
            f"Item metadata keys={sorted(metadata.keys())}"
        )
        raise ValueError(msg)
    decayed = _decay_importance_if_stale(
        importance,
        raw_stamp,
        now=now,
        fresh_horizon_days=fresh_horizon_days,
        half_life_days=half_life_days,
        floor=floor,
    )
    return base_score * (1.0 + decayed)


class _ImportanceParams(TypedDict):
    """Typed bag for the per-(component, domain) importance-decay tunables.

    Mirrors the keyword arguments of :func:`_apply_importance` so callers
    can ``**`` -spread the registry-resolved values without losing types.
    """

    fresh_horizon_days: float
    floor: float
    decay_threshold: float
    half_life_days: float


def _resolve_importance_params(
    registry: ParameterRegistry | None,
    component_id: str,
    domain: str | None,
) -> _ImportanceParams:
    """Resolve per-(component, domain) importance-decay overrides.

    Mirrors the resolution shape of the recency params so callers can
    spread the result into ``_apply_importance(**params)``.
    """
    return _ImportanceParams(
        fresh_horizon_days=_resolve_param(
            registry,
            component_id,
            domain,
            "importance_fresh_horizon_days",
            DEFAULT_IMPORTANCE_FRESH_HORIZON_DAYS,
        ),
        floor=_resolve_param(
            registry,
            component_id,
            domain,
            "importance_decay_floor",
            DEFAULT_IMPORTANCE_DECAY_FLOOR,
        ),
        decay_threshold=_resolve_param(
            registry,
            component_id,
            domain,
            "importance_decay_threshold",
            DEFAULT_IMPORTANCE_DECAY_THRESHOLD,
        ),
        half_life_days=_resolve_param(
            registry,
            component_id,
            domain,
            "recency_half_life_days",
            DEFAULT_RECENCY_HALF_LIFE_DAYS,
        ),
    )


def _parse_stamp(value: Any) -> datetime | None:
    """Parse one recency stamp, or ``None`` when it is absent/unusable.

    Heterogeneous by necessity: SQLite hands back ISO strings and Postgres
    hands back ``datetime`` objects for the very same column.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None


def _decay_importance_if_stale(
    importance: float,
    raw_stamp: str | datetime,
    *,
    now: datetime | None = None,
    fresh_horizon_days: float,
    half_life_days: float,
    floor: float,
) -> float:
    """Decay an importance score past the freshness horizon.

    Mirrors :func:`_apply_recency_decay` but with a no-op grace period:
    inside ``fresh_horizon_days`` the score is returned unchanged; past
    it, the score decays with the same half-life math, capped at
    ``floor``. Unparseable stamps return the score unchanged (the caller
    enforces non-None at a higher level).
    """
    ts = _parse_stamp(raw_stamp)
    if ts is None:
        return importance
    reference = now or datetime.now(UTC)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    age_days = max(0.0, (reference - ts).total_seconds() / 86400.0)
    if age_days <= fresh_horizon_days:
        return importance
    # Past horizon: decay the *excess* age over the horizon with the
    # standard half-life formula, floored.
    excess = age_days - fresh_horizon_days
    decay: float = 0.5 ** (excess / half_life_days)
    return importance * (floor + (1.0 - floor) * decay)


def resolve_recency_stamp(metadata: Any, *row_stamps: Any) -> Any:
    """Resolve the one timestamp recency decay should read for an item.

    **The metadata bag wins over the store row's own columns**, and that
    ordering is the point of the function (#417).

    ``updated_at`` / ``created_at`` exist in two places with two different
    meanings. As *store columns* they are the row's write clock — when
    Trellis last touched this row. As *metadata keys* they are **the
    source's** clock, put there by an ingest path that knows the content
    predates its own write. That is the rule, not a roster of writers: any
    reader whose source carries a timestamp may propagate one, and two
    already do — :mod:`trellis.ingest_corpus.conversations` copies a
    claude.ai conversation's stamps, and the markdown handler passes YAML
    frontmatter through flat (neither key is reserved). Both then reach the
    vector row, because
    :func:`~trellis.retrieve.embed_ingest_hook.build_vector_row` splats
    document metadata and only ``setdefault``s its own ``created_at``.

    Recency decay asks *how old is this information*, and for an imported
    corpus only the source's clock can answer it. Measured on the reference
    deployment (2026-09-02): all **148** conversation documents were written
    by one import batch, so every one of them shares a single column
    ``created_at`` and 132 share a single column ``updated_at`` — the column
    ranks a 2024 conversation exactly as fresh as one from last week, across a
    corpus whose source stamps span 28 months. Reading the column there is not
    a conservative default; it is reading a stamp with no information in it.

    Before this function the two document-backed axes disagreed about which of
    the two they meant: ``KeywordSearch`` read the column, ``SemanticSearch``
    read the bag. Same document, two ages, decided by which strategy retrieved
    it — a median **2.20x** (max 3.17x) difference in the resulting recency
    multiplier across those 148 rows, which supplied 152 of 917 injected
    servings over 29 of 56 assembled packs. Both axes now call this, so a
    third document-backed axis cannot re-open the split by picking a side.

    Two neighbours deliberately keep the column, and neither is an oversight.
    ``GraphSearch`` is not routed through here: a graph node's ``properties``
    is an extractor-written bag with no source-clock convention and no writer
    producing one (0 of 1093 live production nodes carry either key), so
    extending a document-corpus rule to it would be an unmeasured change to a
    different store rather than consistency. And
    :mod:`trellis.mutate.retention` keeps reading the column for its
    ``older_than_days`` gate, because that gate asks a *different* question —
    how long Trellis has held this row, not how old the content is — and it
    answers it by deleting. Switching it to the source clock would make every
    one of those 148 documents retroactively prunable. Recency decay is a
    score with a floor; retention is destructive. They do not have to agree.

    Candidates are tried in order and the first one that *parses* wins — not
    the first one merely present. A malformed source stamp has to fall through
    to the row's clock rather than reach :func:`_apply_recency_decay`, whose
    fail-open on an unparseable value returns the score undecayed, i.e. makes
    the item maximally fresh. That is the wrong direction to fail for a value
    that came from outside.

    Args:
        metadata: The item's metadata bag (document or vector row).
        *row_stamps: The store row's own stamps, most-preferred first
            (typically ``updated_at`` then ``created_at``). A vector row has
            no columns of its own, so the semantic axis passes none.

    Returns:
        The first usable stamp, or ``None`` when nothing parses.
    """
    bag = metadata if isinstance(metadata, dict) else {}
    for candidate in (bag.get("updated_at"), bag.get("created_at"), *row_stamps):
        if _parse_stamp(candidate) is not None:
            return candidate
    return None


def _apply_recency_decay(
    base_score: float,
    # Not ``str | None``: the document store's columns come back as ``str``
    # from SQLite and ``datetime`` from Postgres, and always have.
    timestamp: Any,
    *,
    now: datetime | None = None,
    half_life_days: float = DEFAULT_RECENCY_HALF_LIFE_DAYS,
    floor: float = RECENCY_FLOOR,
) -> float:
    """Apply exponential recency decay to a relevance score.

    Items lose half their un-floored weight every ``half_life_days``.
    A floor ensures an old-but-relevant item still surfaces. Missing or
    unparseable timestamps leave the score unchanged (fail-open).

    Formula:
        decay = 0.5 ** (age_days / half_life_days)
        score = base_score * (floor + (1 - floor) * decay)
    """
    ts = _parse_stamp(timestamp)
    if ts is None:
        return base_score
    reference = now or datetime.now(UTC)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    age_days = max(0.0, (reference - ts).total_seconds() / 86400.0)
    decay: float = 0.5 ** (age_days / half_life_days)
    return base_score * (floor + (1.0 - floor) * decay)


class KeywordSearch(SearchStrategy):
    """Full-text keyword search via DocumentStore."""

    def __init__(
        self,
        document_store: Any,
        *,
        recency_half_life_days: float = DEFAULT_RECENCY_HALF_LIFE_DAYS,
        registry: ParameterRegistry | None = None,
    ) -> None:
        self._store = document_store
        self._recency_half_life_days = recency_half_life_days
        self._registry = registry

    @property
    def name(self) -> str:
        return "keyword"

    def search(
        self,
        query: str,
        *,
        limit: int = 20,
        filters: dict[str, Any] | None = None,
    ) -> list[PackItem]:
        domain = (filters or {}).get("domain")
        half_life = _resolve_param(
            self._registry,
            _KEYWORD_COMPONENT,
            domain,
            "recency_half_life_days",
            self._recency_half_life_days,
        )
        floor = _resolve_param(
            self._registry,
            _KEYWORD_COMPONENT,
            domain,
            "recency_floor",
            RECENCY_FLOOR,
        )
        importance_params = _resolve_importance_params(
            self._registry,
            _KEYWORD_COMPONENT,
            domain,
        )
        # ``domain`` is a scoping hint routed onto the ``content_tags`` facet
        # (default-pass, store-side) by :meth:`PackBuilder._apply_domain_scope`.
        # The scalar key is consumed here for per-(component, domain) param
        # resolution; forwarding it to the document store would re-introduce
        # the #254 scalar hard-equality that hard-excludes untagged rows.
        # ``domain`` is dropped for the #254 reason above; the graph-axis
        # control keys are dropped because the document store would read
        # them as metadata equality and return nothing (see
        # :data:`GRAPH_CONTROL_FILTER_KEYS`).
        store_filters = filters
        if filters and "domain" in filters:
            store_filters = {k: v for k, v in filters.items() if k != "domain"}
        store_filters = strip_graph_controls(store_filters)
        results = self._store.search(query, limit=limit, filters=store_filters)
        items = []
        for doc in results:
            metadata = doc.get("metadata", {})
            base_score = abs(doc.get("rank", 0.0))
            score = _apply_importance(base_score, metadata, **importance_params)
            # Source clock first, row clock second — see
            # :func:`resolve_recency_stamp`. The document store's columns are
            # this row's *write* clock; a conversation import's metadata
            # stamps are the content's own, and are what the semantic axis
            # has always read.
            score = _apply_recency_decay(
                score,
                resolve_recency_stamp(
                    metadata, doc.get("updated_at"), doc.get("created_at")
                ),
                half_life_days=half_life,
                floor=floor,
            )
            items.append(
                PackItem(
                    item_id=doc["doc_id"],
                    item_type="document",
                    excerpt=truncate_excerpt(doc.get("content", "")),
                    relevance_score=score,
                    metadata={"source_strategy": "keyword", **metadata},
                )
            )
        return sorted(items, key=lambda x: x.relevance_score, reverse=True)


class SemanticSearch(SearchStrategy):
    """Vector similarity search via VectorStore."""

    def __init__(
        self,
        vector_store: Any,
        embedding_fn: Any = None,
        *,
        recency_half_life_days: float = DEFAULT_RECENCY_HALF_LIFE_DAYS,
        registry: ParameterRegistry | None = None,
    ) -> None:
        self._store = vector_store
        self._embedding_fn = embedding_fn  # callable(str) -> list[float]
        self._recency_half_life_days = recency_half_life_days
        self._registry = registry

    @property
    def name(self) -> str:
        return "semantic"

    def search(
        self,
        query: str,
        *,
        limit: int = 20,
        filters: dict[str, Any] | None = None,
    ) -> list[PackItem]:
        if self._embedding_fn is None:
            logger.warning("semantic_search_no_embedding_fn")
            return []

        domain = (filters or {}).get("domain")
        half_life = _resolve_param(
            self._registry,
            _SEMANTIC_COMPONENT,
            domain,
            "recency_half_life_days",
            self._recency_half_life_days,
        )
        floor = _resolve_param(
            self._registry,
            _SEMANTIC_COMPONENT,
            domain,
            "recency_floor",
            RECENCY_FLOOR,
        )
        importance_params = _resolve_importance_params(
            self._registry,
            _SEMANTIC_COMPONENT,
            domain,
        )
        # The vector store speaks neither the ``content_tags`` default-pass
        # facet nor a default-pass scalar ``domain`` filter — a store-side
        # filter on either compiles to hard equality and hard-excludes
        # domain-less rows (#254). Strip both from the store call and apply
        # domain scoping as a Python-side default-pass post-filter over the
        # materialized hits (which carry full document metadata). Over-fetch
        # so a heavily-mismatched domain doesn't thin recall (#262).
        # The graph-axis control keys go too — the vector store compiles an
        # unknown filter key to hard metadata equality and returns nothing
        # (see :data:`GRAPH_CONTROL_FILTER_KEYS`).
        store_filters = None
        if filters:
            store_filters = {
                k: v for k, v in filters.items() if k not in ("domain", "content_tags")
            } or None
        store_filters = strip_graph_controls(store_filters)
        fetch_k = limit * _SEMANTIC_DOMAIN_OVERFETCH if domain else limit
        query_vector = self._embedding_fn(query)
        results = self._store.query(query_vector, top_k=fetch_k, filters=store_filters)
        if domain:
            results = [
                r
                for r in results
                if _passes_domain_scope(r.get("metadata", {}), domain)
            ][:limit]
        items = []
        for result in results:
            metadata = result.get("metadata", {})
            base_score = result.get("score", 0.0)
            score = _apply_importance(base_score, metadata, **importance_params)
            # A vector row has no columns of its own — its recency stamp is
            # inside the metadata snapshot, either the source's (splatted
            # from the document) or ``build_vector_row``'s embed-time
            # ``setdefault``. Same resolver as the keyword axis so the two
            # cannot decay off different clocks for one document (#417).
            score = _apply_recency_decay(
                score,
                resolve_recency_stamp(metadata),
                half_life_days=half_life,
                floor=floor,
            )
            items.append(
                PackItem(
                    item_id=result["item_id"],
                    item_type="vector",
                    # Vector metadata is written already-truncated by
                    # ``build_vector_row``, which is the last place the full
                    # document is in hand; this only bounds rows from some
                    # other producer.
                    excerpt=truncate_excerpt(
                        metadata.get("content", metadata.get("excerpt", ""))
                    ),
                    relevance_score=score,
                    metadata={"source_strategy": "semantic", **metadata},
                )
            )
        return sorted(items, key=lambda x: x.relevance_score, reverse=True)


class GraphSearch(SearchStrategy):
    """Graph traversal search via GraphStore.

    **Query-independent unless seeded — read this before trusting the axis
    to answer an intent** (#371). The strategy has two branches, and only
    one of them is a search:

    * **Seeded** — ``filters["seed_ids"]`` was supplied, or a
      :class:`GraphSeedExtractor` was injected and produced ids. The
      strategy expands ``get_subgraph(seeds, depth=...)``. The seeds are
      where query relevance enters; everything downstream is scoring.
    * **Recency window** — no seeds. The strategy calls
      ``GraphStore.query``, which is ``ORDER BY created_at DESC LIMIT n``
      on every shipped backend (`stores/sqlite/graph.py`,
      `stores/postgres/graph.py`, `stores/bolt_opencypher/graph.py`). It
      takes no query argument, and there is no text index behind it. The
      axis therefore returns **the most recently created nodes**, filtered
      structurally and scored — without consulting what was asked.

    The second branch is the production default, and the first has **no
    production producer at all**: ``build_strategies`` injects no
    extractor, and nothing in the repo puts ``seed_ids`` into the filters
    a pack is assembled with. The entity-neighbourhood surfaces
    (``GET /entities/{id}``, MCP ``get_graph``) call
    ``graph_store.get_subgraph`` *directly* and never reach this class; a
    section's ``entity_ids`` is a
    :class:`~trellis.retrieve.tier_mapping.TierMapper` routing filter over
    items already retrieved, not a seed. This is deliberate as of #371,
    not an oversight — see :func:`build_strategies` for why the obvious
    wiring was measured and refused — but the consequences are sharp and
    worth stating where a reader meets them:

    * The reachable set is a **fixed row count**
      (``limit * _GRAPH_RECENCY_OVERFETCH``), not a fraction of the graph,
      so **coverage decays as 1/N as the graph grows**. Measured on the
      reference deployment across the 37 packs assembled in the 30 days to
      2026-08-28: the window covered a median **8.6%** of servable nodes
      (range 7.2%-15.0%, falling monotonically as the graph grew from 286
      to 665 servable nodes) and spanned a median of **58 hours**.
    * An old, perfectly on-topic entity is **unreachable**, at any rank,
      for every intent.
    * A single bulk ingest of ``limit * _GRAPH_RECENCY_OVERFETCH`` nodes
      evicts the whole window. One pack in that measurement had a window
      spanning **0.0 hours** — every candidate came from one write batch.

    Every item carries ``metadata["graph_selection"]``
    (:data:`GRAPH_SELECTION_SEEDED` / :data:`GRAPH_SELECTION_RECENCY_WINDOW`)
    so which branch ran is legible in ``PACK_ASSEMBLED.injected_items[]``
    rather than inferred from the wiring.

    Structural nodes (``node_role == "structural"``) are excluded by default
    — they represent fine-grained plumbing (columns, parameters, file
    lines) that is retrieved only as part of its parent's context. Pass
    ``include_structural=True`` via filters to surface them anyway.

    Unconfirmed extraction mints (``properties.extraction_status ==
    "unconfirmed"``) are likewise excluded by default: extraction from
    prose attests only that something was *mentioned*, and serving those
    nodes teaches downstream agents claims the source never made
    (trellis-ai#300 — the same claims-are-gated principle as the
    ``signal_quality="noise"`` document filter). Pass
    ``include_unconfirmed=True`` via filters to surface them (curation /
    review tooling), or confirm the entity via ``entity.update`` to make
    it retrievable for good.

    Curated nodes (``node_role == "curated"``) are retained and receive a
    relevance boost (``curated_boost``, default 1.3) because they are
    pre-digested synthesis — the highest information density per token.
    """

    def __init__(
        self,
        graph_store: Any,
        *,
        curated_boost: float = GRAPH_CURATED_BOOST,
        recency_half_life_days: float = DEFAULT_RECENCY_HALF_LIFE_DAYS,
        registry: ParameterRegistry | None = None,
        seed_extractor: GraphSeedExtractor | None = None,
    ) -> None:
        """Build the graph axis.

        Args:
            graph_store: Any :class:`~trellis.stores.base.graph.GraphStore`.
            curated_boost: Score multiplier for ``node_role == "curated"``.
            recency_half_life_days: Half-life for the recency decay applied
                to *scores* — unrelated to the recency *selection* the
                unseeded branch performs.
            registry: Optional :class:`ParameterRegistry` for per-domain
                scoring overrides.
            seed_extractor: Optional :class:`GraphSeedExtractor`. **Default
                ``None`` is the query-independent recency window** described
                in the class docstring. Supply one to make the axis a
                function of the intent. An extractor that returns ``[]`` or
                raises falls back to the recency window — seeding is
                additive, and a seeding miss must never cost the caller an
                axis it would otherwise have had.
        """
        self._store = graph_store
        self._curated_boost = curated_boost
        self._recency_half_life_days = recency_half_life_days
        self._registry = registry
        self._seed_extractor = seed_extractor

    @property
    def name(self) -> str:
        return "graph"

    def _query_nodes(
        self,
        *,
        node_type: str | None,
        properties: dict[str, Any] | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Run a node query, expanding ``node_type`` to include legacy aliases.

        ADR Phase 2 (graph-ontology): a query for ``"Person"`` must
        bucket alongside legacy ``"person"`` rows during the migration
        period. We expand the requested type via
        :func:`~trellis.schemas.well_known.expand_entity_type_query`
        and, when the expansion yields more than one value, route
        through the canonical DSL with an ``in`` clause. Single-value
        types (open-string or canonicals with no aliases) keep using
        the legacy ``query`` path so backends that haven't shipped a
        DSL compiler yet still work.
        """
        # ``self._store`` is typed ``Any`` (graph store backends share
        # an open ABC), so we annotate locally to keep the ``Any`` taint
        # from leaking into ``GraphSearch.search``'s caller chain.
        rows: list[dict[str, Any]]
        if node_type is None:
            rows = self._store.query(
                node_type=None,
                properties=properties,
                limit=limit,
            )
            return rows

        expanded = expand_entity_type_query(node_type)
        if len(expanded) == 1:
            # No alias fan-out — the legacy single-string filter is
            # sufficient and avoids the DSL hop.
            rows = self._store.query(
                node_type=expanded[0],
                properties=properties,
                limit=limit,
            )
            return rows

        # Multi-value expansion routes through the DSL so backends
        # compile a single ``node_type IN (...)`` query rather than
        # forcing N round-trips. All shipped backends (sqlite,
        # postgres, neo4j) implement Phase 2 of the canonical-graph-
        # layer ADR.
        clauses: list[FilterClause] = [
            FilterClause(field="node_type", op="in", value=tuple(expanded)),
        ]
        for key, value in (properties or {}).items():
            clauses.append(
                FilterClause(field=f"properties.{key}", op="eq", value=value)
            )
        rows = self._store.execute_node_query(
            NodeQuery(filters=tuple(clauses), limit=limit),
        )
        return rows

    def _seeds_from_extractor(self, query: str) -> list[str]:
        """Ask the injected extractor for seeds; never let it break a pack.

        Returns ``[]`` when no extractor is configured, when the extractor
        declines, or when it raises. The caller reads an empty list as "run
        the recency window" — the seeding path is additive by contract.
        """
        if self._seed_extractor is None:
            return []
        try:
            seeds = list(self._seed_extractor.extract(query))
        # GRACEFUL-DEGRADATION: an extractor typically embeds the intent
        # and queries the vector store. Neither is required for the graph
        # axis to return something, so a seeding failure degrades to the
        # unseeded branch rather than costing the caller an axis. Mirrors
        # PackBuilder's per-strategy failure handling one level down.
        except Exception:
            logger.exception(
                "graph_seed_extractor_failed",
                extractor=type(self._seed_extractor).__name__,
            )
            return []
        if not seeds:
            logger.debug(
                "graph_seed_extractor_returned_none",
                extractor=type(self._seed_extractor).__name__,
            )
        return seeds

    def _expand_seeds(
        self,
        seed_ids: list[str],
        *,
        filters: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """The seeded branch: expand a neighbourhood around known ids.

        This is the only branch in which the caller's intent has reached
        the store — via ``filters["seed_ids"]`` or a
        :class:`GraphSeedExtractor`. Consumes ``depth`` / ``edge_types``
        from *filters*.
        """
        depth = filters.pop("depth", 2)
        edge_types = filters.pop("edge_types", None)
        subgraph = self._store.get_subgraph(
            seed_ids,
            depth=depth,
            edge_types=edge_types,
        )
        nodes: list[dict[str, Any]] = subgraph.get("nodes", [])
        logger.debug(
            "graph_search_seeded",
            seed_count=len(seed_ids),
            depth=depth,
            nodes_returned=len(nodes),
        )
        return nodes

    def _recency_window_nodes(
        self,
        *,
        filters: dict[str, Any],
        limit: int,
    ) -> list[dict[str, Any]]:
        """The unseeded branch: the newest ``limit * overfetch`` node rows.

        **The caller's query is not an input here and cannot be.**
        ``GraphStore.query`` is ``ORDER BY created_at DESC LIMIT n`` on
        every shipped backend and has no text index behind it, so this
        method selects on recency and structure alone. See the class
        docstring for what that costs. Consumes ``node_type`` from
        *filters*.
        """
        node_type = filters.pop("node_type", None)
        # ``domain`` and ``content_tags`` are scoping hints, not graph
        # properties: ``domain`` is applied client-side with default-pass
        # semantics by the caller (a domain-less node is never
        # hard-excluded, mirroring the other axes for #262), and
        # ``content_tags`` is a document-store facet the graph store cannot
        # interpret. Neither is forwarded as a property filter — a
        # store-side property filter compiles to hard equality and would
        # hard-exclude every domain-less node (#254).
        # Graph-axis controls are excluded too. ``node_type`` is popped just
        # above, and ``seed_ids`` / ``include_*`` are popped in ``search``
        # before either branch — but ``depth`` and ``edge_types`` are popped
        # only in the *seeded* branch, so on the unseeded branch (the one
        # production always takes, #371) they would otherwise arrive here and
        # become hard-equality node-property filters, emptying the graph axis
        # for a caller who was configuring a traversal.
        query_props = {
            k: v
            for k, v in filters.items()
            if k not in ("domain", "content_tags")
            and k not in GRAPH_CONTROL_FILTER_KEYS
        }
        # The multiplier is the whole candidate window (see the class
        # docstring), not headroom over a relevance-ordered result — it
        # exists so the client-side structural / unconfirmed filters have
        # rows to discard before the slice to ``limit``.
        scan_limit = limit * _GRAPH_RECENCY_OVERFETCH
        nodes = self._query_nodes(
            node_type=node_type,
            properties=query_props or None,
            limit=scan_limit,
        )
        logger.debug(
            "graph_search_recency_window",
            scan_limit=scan_limit,
            rows_returned=len(nodes),
            # True means the window was saturated: every row older than the
            # oldest one returned is unreachable, for *any* intent.
            window_saturated=len(nodes) >= scan_limit,
        )
        return nodes

    def search(
        self,
        query: str,
        *,
        limit: int = 20,
        filters: dict[str, Any] | None = None,
    ) -> list[PackItem]:
        filters = dict(filters) if filters else {}
        seed_ids: list[str]
        if "seed_ids" in filters:
            # An explicit seed set always wins: a caller that passed
            # ``seed_ids`` has already decided which neighbourhood it
            # wants, and re-deriving seeds from prose would silently widen
            # a deliberately narrow request. No in-repo production caller
            # does this today — see the class docstring.
            seed_ids = filters.pop("seed_ids")
        else:
            seed_ids = self._seeds_from_extractor(query)

        include_structural = bool(filters.pop("include_structural", False))
        include_unconfirmed = bool(filters.pop("include_unconfirmed", False))

        # Extract domain for scoring (keep in filters for graph query too)
        request_domain = filters.get("domain")

        if seed_ids:
            selection = GRAPH_SELECTION_SEEDED
            nodes = self._expand_seeds(seed_ids, filters=filters)
        else:
            selection = GRAPH_SELECTION_RECENCY_WINDOW
            nodes = self._recency_window_nodes(filters=filters, limit=limit)

        # Filter structural nodes client-side unless explicitly requested.
        #
        # This drop happens *before* a ``PackItem`` exists, so it produces no
        # ``RejectedItem`` and is invisible to
        # :func:`~trellis.retrieve.withholding.summarize_withheld` — see that
        # module's "What this cannot see" section. Since #375/#436 the
        # population it removes includes every newly-written meta-Activity,
        # which used to be counted by ``PACK_ASSEMBLED.meta_filtered_count``
        # and no longer is. The debug line is the only observable the drop
        # has; it exists for parity with the ``include_unconfirmed`` filter
        # below, which has had one since #301.
        if not include_structural:
            nodes = _exclude_and_log(
                nodes,
                lambda n: n.get("node_role") != "structural",
                "graph_search_structural_excluded",
            )

        # Filter unconfirmed extraction mints unless explicitly requested
        # — client-side like the structural filter, so both the query and
        # subgraph branches are covered and a store-side property filter
        # can't hard-exclude the (status-less) majority of nodes.
        if not include_unconfirmed:
            nodes = _exclude_and_log(
                nodes,
                lambda n: (
                    (n.get("properties") or {}).get(EXTRACTION_STATUS_PROPERTY)
                    != EXTRACTION_STATUS_UNCONFIRMED
                ),
                "graph_search_unconfirmed_excluded",
            )

        # Domain scoping — the same default-pass contract as the keyword
        # facet and the semantic post-filter (#254): a node carrying an
        # explicitly mismatched domain (scalar ``properties.domain`` or the
        # ``properties.content_tags.domain`` facet) is excluded; a
        # domain-less node passes; a match passes and keeps the
        # ``domain_match_boost`` below.
        if request_domain:
            nodes = [
                n
                for n in nodes
                if _passes_domain_scope(n.get("properties", {}), request_domain)
            ]

        # Resolve all tuneable scoring params once per .search() call.
        domain_match_boost = _resolve_param(
            self._registry,
            _GRAPH_COMPONENT,
            request_domain,
            "domain_match_boost",
            GRAPH_DOMAIN_MATCH_BOOST,
        )
        curated_boost = _resolve_param(
            self._registry,
            _GRAPH_COMPONENT,
            request_domain,
            "curated_boost",
            self._curated_boost,
        )
        description_boost = _resolve_param(
            self._registry,
            _GRAPH_COMPONENT,
            request_domain,
            "description_boost",
            GRAPH_DESCRIPTION_BOOST,
        )
        position_decay_step = _resolve_param(
            self._registry,
            _GRAPH_COMPONENT,
            request_domain,
            "position_decay_step",
            GRAPH_POSITION_DECAY_STEP,
        )
        half_life = _resolve_param(
            self._registry,
            _GRAPH_COMPONENT,
            request_domain,
            "recency_half_life_days",
            self._recency_half_life_days,
        )
        floor = _resolve_param(
            self._registry,
            _GRAPH_COMPONENT,
            request_domain,
            "recency_floor",
            RECENCY_FLOOR,
        )
        importance_params = _resolve_importance_params(
            self._registry,
            _GRAPH_COMPONENT,
            request_domain,
        )

        items = []
        for i, node in enumerate(nodes[:limit]):
            props = node.get("properties", {})
            node_type_val = node.get("node_type", "")
            node_role_val = node.get("node_role") or "semantic"

            # Base score from position (decays)
            base_score = max(0.0, 1.0 - (i * position_decay_step))

            # Domain match boost: nodes matching requested domain score higher
            if request_domain and props.get("domain") == request_domain:
                base_score *= domain_match_boost

            # Curated nodes are pre-digested synthesis — boost them.
            if node_role_val == "curated":
                base_score *= curated_boost

            # Importance boost
            score = _apply_importance(base_score, props, **importance_params)

            # Prefer entities with descriptions — they carry more context
            if props.get("description") or props.get("comment"):
                score *= description_boost

            # Recency decay — older nodes score progressively lower
            score = _apply_recency_decay(
                score,
                node.get("updated_at") or node.get("created_at"),
                half_life_days=half_life,
                floor=floor,
            )

            excerpt = props.get(
                "description",
                props.get("name", props.get("title", "")),
            )
            # ADR Phase 2: stamp the canonical bucket key alongside the
            # raw stored type so downstream group-by analytics don't
            # need to call canonicalize themselves.
            canonical_type = canonicalize_entity_type(node_type_val)
            items.append(
                PackItem(
                    item_id=node["node_id"],
                    item_type="entity",
                    excerpt=truncate_excerpt(str(excerpt)),
                    relevance_score=score,
                    metadata={
                        "source_strategy": "graph",
                        **{
                            k: v
                            for k, v in props.items()
                            if k not in ("name", "description", "comment")
                        },
                        # All four sit after the property spread,
                        # deliberately: ``properties`` is an open bag, so a
                        # node is free to carry a key of any of these names,
                        # and each is a fact about the *row* or about *this*
                        # search that a stored property must not get a vote on.
                        #
                        # ``node_role`` is the load-bearing one — PackBuilder
                        # reads it back as a *decision*, dropping items whose
                        # ``metadata["node_role"] == "structural"``. Spread
                        # last, a stored property could hide a structural row
                        # from that filter, or forge a structural verdict for
                        # a semantic one. ``node_type`` gates the
                        # meta-Activity filter the same way, and since #375
                        # gate 4 both also land in
                        # ``PACK_ASSEMBLED.injected_items[]``.
                        "node_type": node_type_val,
                        "node_type_canonical": canonical_type,
                        "node_role": node_role_val,
                        "graph_selection": selection,
                    },
                )
            )
        return sorted(items, key=lambda x: x.relevance_score, reverse=True)


# ---------------------------------------------------------------------------
# Factory helper
# ---------------------------------------------------------------------------


def build_strategies(
    registry: StoreRegistry,
    embedding_fn: Any | None = None,
    *,
    parameter_registry: ParameterRegistry | None = None,
    graph_seed_extractor: GraphSeedExtractor | None = None,
) -> list[SearchStrategy]:
    """Build the standard strategy list from a registry.

    Always includes KeywordSearch and GraphSearch.  Adds SemanticSearch when
    both a VectorStore and an ``embedding_fn`` callable are available.

    **The graph axis is query-independent unless you pass
    ``graph_seed_extractor``**, and the default is deliberately ``None``.
    See :class:`GraphSearch` for what that means for a served pack. The
    reasoning behind the default, recorded so it is not re-litigated on
    taste (#371):

    * The obvious wiring — construct a
      :class:`~trellis.retrieve.semantic_seeds.SemanticSeedExtractor`
      whenever ``embedding_fn`` resolves — was **measured, not argued**.
      Replayed over all 37 real intents from the reference deployment's
      30-day ``PACK_ASSEMBLED`` history, against that deployment's own
      Postgres graph + pgvector stores and a live embedder, it produced
      **0 seeds on 37/37 intents** and changed the returned item set on
      **0/37**. The extractor filters vector hits to entity-summary
      documents and the corpus holds none, so it costs one embed per pack
      and returns the recency window regardless. Wiring it by default
      would have shipped a change that reports success and does nothing —
      exactly the failure shape `docs/design/swarm-handoff.md` §8 is about.
    * Auto-wiring on ``embedding_fn`` would also couple the graph axis to
      an embedder that this function otherwise treats as strictly
      optional, so a deployment without one would silently get a
      *differently-behaved* graph axis under the same name.
    * The axis measures well today (``useful_token_fraction`` 0.1744 vs
      semantic 0.1069, keyword 0.0241, 30d to 2026-08-28), so a
      speculative change risks a regression the headline would not show —
      the axis is only 7% of injected tokens.

    Making it non-``None`` is therefore an explicit, per-deployment
    decision, and the corpus has to be able to satisfy the extractor
    before it is worth making.

    Args:
        registry: The StoreRegistry providing stores.
        embedding_fn: Optional ``(str) -> list[float]`` callable.  When
            *None*, the helper checks ``registry.embedding_fn`` (which reads
            the ``embeddings`` config section).  If neither source provides
            one, SemanticSearch is skipped.
        parameter_registry: Optional :class:`ParameterRegistry` that
            strategies consult at call-time for per-(component, domain)
            scoring overrides.  When ``None`` the module-level defaults
            apply unchanged.
        graph_seed_extractor: Optional :class:`GraphSeedExtractor` handed
            to :class:`GraphSearch`.  ``None`` (the default, and what every
            in-repo caller passes) keeps the recency-window behaviour.
            Never derived from ``embedding_fn`` — see above.
    """
    strategies: list[SearchStrategy] = [
        KeywordSearch(registry.knowledge.document_store, registry=parameter_registry),
        GraphSearch(
            registry.knowledge.graph_store,
            registry=parameter_registry,
            seed_extractor=graph_seed_extractor,
        ),
    ]

    fn = embedding_fn or getattr(registry, "embedding_fn", None)
    if fn is not None:
        try:
            strategies.append(
                SemanticSearch(
                    registry.knowledge.vector_store,
                    fn,
                    registry=parameter_registry,
                )
            )
            logger.info("semantic_search_enabled")
        # GRACEFUL-DEGRADATION: semantic search is optional; a vector
        # backend that fails init must not block keyword + graph search
        # — log and continue without it.
        except Exception:
            logger.warning("semantic_search_init_failed", exc_info=True)

    return strategies
