"""Search strategies for pack assembly."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from trellis.schemas.pack import PackItem
from trellis.schemas.parameters import ParameterScope
from trellis.schemas.well_known import (
    canonicalize_entity_type,
    expand_entity_type_query,
)
from trellis.stores.base.graph_query import FilterClause, NodeQuery

if TYPE_CHECKING:
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


def _apply_importance(base_score: float, metadata: dict[str, Any]) -> float:
    """Apply importance weighting: base_score * (1.0 + importance)."""
    importance = float(metadata.get("auto_importance", 0.0))
    importance = max(0.0, min(1.0, importance))  # clamp 0-1
    return base_score * (1.0 + importance)


def _apply_recency_decay(
    base_score: float,
    timestamp: str | None,
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
    if not timestamp:
        return base_score
    try:
        ts = datetime.fromisoformat(str(timestamp))
    except (ValueError, TypeError):
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
        results = self._store.search(query, limit=limit, filters=filters)
        items = []
        for doc in results:
            metadata = doc.get("metadata", {})
            base_score = abs(doc.get("rank", 0.0))
            score = _apply_importance(base_score, metadata)
            score = _apply_recency_decay(
                score,
                doc.get("updated_at") or doc.get("created_at"),
                half_life_days=half_life,
                floor=floor,
            )
            items.append(
                PackItem(
                    item_id=doc["doc_id"],
                    item_type="document",
                    excerpt=doc.get("content", "")[:500],
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
        query_vector = self._embedding_fn(query)
        results = self._store.query(query_vector, top_k=limit, filters=filters)
        items = []
        for result in results:
            metadata = result.get("metadata", {})
            base_score = result.get("score", 0.0)
            score = _apply_importance(base_score, metadata)
            score = _apply_recency_decay(
                score,
                metadata.get("updated_at") or metadata.get("created_at"),
                half_life_days=half_life,
                floor=floor,
            )
            items.append(
                PackItem(
                    item_id=result["item_id"],
                    item_type="vector",
                    excerpt=metadata.get("content", metadata.get("excerpt", ""))[:500],
                    relevance_score=score,
                    metadata={"source_strategy": "semantic", **metadata},
                )
            )
        return sorted(items, key=lambda x: x.relevance_score, reverse=True)


class GraphSearch(SearchStrategy):
    """Graph traversal search via GraphStore.

    Structural nodes (``node_role == "structural"``) are excluded by default
    — they represent fine-grained plumbing (columns, parameters, file
    lines) that is retrieved only as part of its parent's context. Pass
    ``include_structural=True`` via filters to surface them anyway.

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
    ) -> None:
        self._store = graph_store
        self._curated_boost = curated_boost
        self._recency_half_life_days = recency_half_life_days
        self._registry = registry

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

    def search(
        self,
        query: str,  # noqa: ARG002
        *,
        limit: int = 20,
        filters: dict[str, Any] | None = None,
    ) -> list[PackItem]:
        filters = dict(filters) if filters else {}
        seed_ids: list[str] = []
        if "seed_ids" in filters:
            seed_ids = filters.pop("seed_ids")

        include_structural = bool(filters.pop("include_structural", False))

        # Extract domain for scoring (keep in filters for graph query too)
        request_domain = filters.get("domain")

        if seed_ids:
            depth = filters.pop("depth", 2)
            subgraph = self._store.get_subgraph(seed_ids, depth=depth)
            nodes = subgraph.get("nodes", [])
        else:
            node_type = filters.pop("node_type", None)
            # Pass domain as a property filter to the graph store
            query_props = {k: v for k, v in filters.items() if k != "domain"}
            if request_domain:
                query_props["domain"] = request_domain
            # Over-fetch 4x to leave room for structural filtering before
            # slicing to the caller's limit.
            nodes = self._query_nodes(
                node_type=node_type,
                properties=query_props or None,
                limit=limit * 4,
            )

        # Filter structural nodes client-side unless explicitly requested.
        if not include_structural:
            nodes = [n for n in nodes if n.get("node_role") != "structural"]

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
            score = _apply_importance(base_score, props)

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
                    excerpt=str(excerpt)[:500],
                    relevance_score=score,
                    metadata={
                        "source_strategy": "graph",
                        "node_type": node_type_val,
                        "node_type_canonical": canonical_type,
                        "node_role": node_role_val,
                        **{
                            k: v
                            for k, v in props.items()
                            if k not in ("name", "description", "comment")
                        },
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
) -> list[SearchStrategy]:
    """Build the standard strategy list from a registry.

    Always includes KeywordSearch and GraphSearch.  Adds SemanticSearch when
    both a VectorStore and an ``embedding_fn`` callable are available.

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
    """
    strategies: list[SearchStrategy] = [
        KeywordSearch(registry.knowledge.document_store, registry=parameter_registry),
        GraphSearch(registry.knowledge.graph_store, registry=parameter_registry),
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
        except Exception:
            logger.warning("semantic_search_init_failed", exc_info=True)

    return strategies
