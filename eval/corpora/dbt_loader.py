"""dbt manifest corpus loader for Phase B-1.

Reads a dbt ``manifest.json`` (real or hand-crafted fixture), runs it
through the shipped :class:`~trellis_workers.extract.DbtManifestExtractor`,
canonicalizes the snake_case ``depends_on`` edge kind to camelCase
``dependsOn`` per
[`adr-graph-ontology.md`](../../docs/design/adr-graph-ontology.md) §3.2,
and submits the resulting drafts through
:class:`~trellis.mutate.executor.MutationExecutor` with
:func:`~trellis.mutate.handlers.create_curate_handlers` — keeping the
"all mutations go through the governed pipeline" hard rule from
``CLAUDE.md`` intact.

Doc-store side channel: each entity's ``description`` (when present)
gets indexed at ``doc:{entity_id}`` with ``content_tags={
"signal_quality": "standard"}`` so PackBuilder's default noise filter
treats them the same shape as the synthetic baseline's entity-summary
docs.

The loader is intentionally backend-agnostic — it accepts any
``StoreRegistry`` and writes through the registry's stores. The eval
scenario constructs an in-memory SQLite registry; production callers
would supply a configured registry.
"""

from __future__ import annotations

import asyncio
import functools
import json
import re
import weakref
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import structlog

from trellis.extract.commands import result_to_batch
from trellis.extract.dispatcher import ExtractionDispatcher
from trellis.extract.registry import ExtractorRegistry
from trellis.mutate.commands import CommandStatus, Operation
from trellis.mutate.executor import MutationExecutor
from trellis.mutate.handlers import create_curate_handlers
from trellis.schemas.extraction import EdgeDraft, ExtractionResult
from trellis.schemas.well_known import DEPENDS_ON, canonicalize_edge_kind
from trellis.stores.registry import StoreRegistry
from trellis_workers.extract import DbtManifestExtractor

logger = structlog.get_logger(__name__)

# Minimum dot count in a dbt test ``entity_id`` for ``split(".")[-2]``
# (the segment carrying the test-type prefix) to be safe to index.
_DBT_TEST_ID_MIN_DOTS = 2


DEFAULT_MANIFEST_PATH = Path(__file__).parent / "jaffle_shop" / "manifest.json"


@dataclass
class LoadResult:
    """Counts surfaced after loading a dbt manifest into a registry."""

    entities_extracted: int
    edges_extracted: int
    nodes_created: int
    edges_created: int
    documents_indexed: int
    edge_kind_canonicalizations: int

    def as_metrics(self, prefix: str = "corpus") -> dict[str, float]:
        """Flatten into ``ScenarioReport.metrics``-shaped dict."""
        return {
            f"{prefix}.entities_extracted": float(self.entities_extracted),
            f"{prefix}.edges_extracted": float(self.edges_extracted),
            f"{prefix}.nodes_created": float(self.nodes_created),
            f"{prefix}.edges_created": float(self.edges_created),
            f"{prefix}.documents_indexed": float(self.documents_indexed),
            f"{prefix}.edge_kind_canonicalizations": float(
                self.edge_kind_canonicalizations
            ),
        }


# dbt-specific aliases that are not in the project-wide well_known
# alias registry. These map the snake_case names the
# :class:`DbtManifestExtractor` emits onto the canonical PROV-O / SKOS
# verbs from :mod:`trellis.schemas.well_known`. We do NOT extend the
# project-wide registry because these are *extractor-specific*
# convention — `"depends_on"` from dbt is a meaningfully different
# domain signal than the trace-side `"entity_depends_on"` legacy alias
# the well_known map already covers.
#
# When :mod:`trellis_workers.extract.dbt_manifest` migrates to emit
# canonical names directly (Phase 1 of `adr-graph-ontology.md` §6.4),
# this map collapses to empty and the function becomes a pure pass
# through `canonicalize_edge_kind`.
_DBT_EDGE_KIND_ALIASES: dict[str, str] = {
    "depends_on": DEPENDS_ON,
}

# Entity-id prefixes that win the bare-name slot in :func:`build_name_index`
# when multiple entities share a short-name (e.g., the ``customers`` mart
# and the ``raw.customers`` source both expose ``customers`` as a short-
# name candidate). dbt's manifest convention namespaces unique-ids as
# ``<resource_type>.<project>.<name>`` — listing ``"model."`` here makes
# the model the canonical owner of the short-name. Order matters: earlier
# prefixes win over later ones.
_NODE_PRIORITY_PREFIXES: Final[tuple[str, ...]] = ("model.",)


# ---------------------------------------------------------------------------
# Shared graph view — single fetch + memoized closure
# ---------------------------------------------------------------------------
#
# The three sibling builders (:func:`build_name_index`,
# :func:`build_category_index`, :func:`build_lineage_index`) used to each
# run their own ``graph_store.query(limit=5000)`` pass; ``build_lineage_index``
# also ran per-entity BFS over ``get_edges`` with no cross-entity memoization
# (worst-case O(N x E) — fine for the 21-node Jaffle Shop fixture but
# scales poorly to a real 10K-model dbt manifest).
#
# ``_GraphView`` bundles the data all three builders need in one shot:
# the node list (one ``query`` call) and per-node outbound ``dependsOn``
# adjacency (one ``get_edges`` call per node). ``build_lineage_index``
# then BFSes over the in-memory adjacency — eliminating the per-entity
# DB roundtrips that were the dominant cost on Postgres/Neo4j backends
# while preserving the BFS-layer ancestor ordering the legacy code
# produced (the downstream ``GraphSearch(seed_ids=..., depth=0)`` ranks
# items in seed order under budget pressure, so the order is part of
# the public surface). See :func:`build_lineage_index` for the
# asymptotic trade-off vs a true topo+DP closure.
#
# Cache: keyed on the graph store instance via ``WeakKeyDictionary`` so
# the three siblings called consecutively from the dbt convergence
# scenario share the same view. The cache is cleared at the top of
# :func:`load_jaffle_shop_corpus` so a fresh load gets fresh data.
@dataclass(frozen=True)
class _GraphView:
    """One-shot snapshot of the structural data the three index builders
    consume: nodes (full dicts) and per-node outbound ``dependsOn``
    adjacency (target ``node_id`` lists)."""

    nodes: list[dict[str, Any]] = field(default_factory=list)
    depends_on: dict[str, list[str]] = field(default_factory=dict)


_GRAPH_VIEW_CACHE: weakref.WeakKeyDictionary[Any, _GraphView] = (
    weakref.WeakKeyDictionary()
)


def _collect_graph_view(registry: StoreRegistry) -> _GraphView:
    """Return a cached :class:`_GraphView` for *registry*'s graph store.

    On first call for a given graph store: runs ONE
    ``graph_store.query(limit=5000)`` pass and ONE
    ``graph_store.get_edges(node_id, direction="outgoing")`` call per
    node, builds the outbound ``dependsOn`` adjacency, and caches.
    Subsequent calls for the same graph store reuse the snapshot.

    The cache is invalidated on each :func:`load_jaffle_shop_corpus`
    call so reload-then-rebuild flows see the updated graph.
    """
    graph_store = registry.knowledge.graph_store
    cached = _GRAPH_VIEW_CACHE.get(graph_store)
    if cached is not None:
        return cached
    nodes = list(graph_store.query(limit=5000))
    depends_on: dict[str, list[str]] = {}
    for node in nodes:
        node_id = node["node_id"]
        targets: list[str] = []
        for edge in graph_store.get_edges(node_id, direction="outgoing"):
            target = edge.get("target_id")
            if not target:
                continue
            # Some backends omit ``edge_kind`` on the returned dict; fall
            # through to ``edge_type``. ``None`` is treated as accept
            # because the legacy code did so (see git history of
            # :func:`build_lineage_index`).
            edge_kind = edge.get("edge_kind") or edge.get("edge_type")
            if edge_kind not in (None, DEPENDS_ON, "depends_on"):
                continue
            targets.append(target)
        depends_on[node_id] = targets
    view = _GraphView(nodes=nodes, depends_on=depends_on)
    _GRAPH_VIEW_CACHE[graph_store] = view
    return view


def _canonicalize_edges(edges: list[EdgeDraft]) -> tuple[list[EdgeDraft], int]:
    """Return canonicalized edges + count of values that changed.

    Two-stage canonicalization:
    1. dbt-extractor-specific mapping (snake_case → camelCase for
       `"depends_on"` → `"dependsOn"`).
    2. Project-wide :func:`canonicalize_edge_kind` for anything else
       (no-op for unknown strings, per ADR's open-string contract).

    The shipped :class:`DbtManifestExtractor` emits ``edge_kind="depends_on"``
    today. The canonical form per the ontology ADR §3.2 is
    ``"dependsOn"`` (camelCase). We canonicalize at the loader so the
    extractor stays unchanged and the migration to canonical edge kinds
    can land later without breaking the loader's output shape.
    """
    canonicalized: list[EdgeDraft] = []
    changed = 0
    for edge in edges:
        # Try dbt-specific aliases first, fall back to well_known.
        new_kind = _DBT_EDGE_KIND_ALIASES.get(
            edge.edge_kind, canonicalize_edge_kind(edge.edge_kind)
        )
        if new_kind != edge.edge_kind:
            changed += 1
            canonicalized.append(edge.model_copy(update={"edge_kind": new_kind}))
        else:
            canonicalized.append(edge)
    return canonicalized, changed


def _run_extraction(
    registry: StoreRegistry, manifest: dict[str, Any]
) -> ExtractionResult:
    """Dispatch the dbt extractor against a parsed manifest dict."""
    ext_registry = ExtractorRegistry()
    ext_registry.register(DbtManifestExtractor())  # type: ignore[arg-type]
    dispatcher = ExtractionDispatcher(
        ext_registry, event_log=registry.operational.event_log
    )
    return asyncio.run(
        dispatcher.dispatch(manifest, source_hint="dbt-manifest"),
    )


def _execute_through_governed_pipeline(
    registry: StoreRegistry, result: ExtractionResult
) -> tuple[int, int]:
    """Submit drafts as a CommandBatch and return ``(nodes, edges)``.

    Per CLAUDE.md hard rule: all mutations go through MutationExecutor.
    We instantiate it here with the registry's event_log + curate
    handlers (the same wiring CLI ``trellis ingest dbt-manifest`` uses).
    """
    handlers = create_curate_handlers(registry)
    executor = MutationExecutor(
        event_log=registry.operational.event_log,
        handlers=handlers,
    )
    batch = result_to_batch(result, requested_by="eval:dbt_loader")
    results = executor.execute_batch(batch)
    nodes = sum(
        1
        for r in results
        if r.operation == Operation.ENTITY_CREATE and r.status == CommandStatus.SUCCESS
    )
    edges = sum(
        1
        for r in results
        if r.operation == Operation.LINK_CREATE and r.status == CommandStatus.SUCCESS
    )
    return nodes, edges


def _index_descriptions(registry: StoreRegistry, result: ExtractionResult) -> int:
    """Index entity descriptions into the document store.

    Uses the same id scheme as the synthetic baseline (``doc:{entity_id}``)
    so retrieval queries can target docs by predictable name.
    """
    document_store = registry.knowledge.document_store
    indexed = 0
    for entity in result.entities:
        description = (
            entity.properties.get("description", "") if entity.properties else ""
        )
        if not description:
            continue
        # Tag content with signal_quality="standard" so PackBuilder's
        # default noise filter passes it. The effectiveness loop is what
        # should later flip under-performing docs to "noise".
        document_store.put(
            doc_id=f"doc:{entity.entity_id}",
            content=description,
            metadata={
                "source": "dbt",
                "entity_id": entity.entity_id,
                "entity_type": entity.entity_type,
                "name": entity.properties.get("name", entity.name),
                "content_type": "entity_summary",
                # No domain dimension on dbt corpus — single-domain
                # corpus until B-2 lands. PackBuilder filters that
                # require a domain key fall through gracefully.
                "content_tags": {"signal_quality": "standard"},
                # Mirrored on top-level so KeywordSearch/SemanticSearch
                # excerpt extraction reads it consistently.
                "content": description,
            },
        )
        indexed += 1
    return indexed


def build_name_index(registry: StoreRegistry) -> dict[str, str]:
    """Build a short-name → entity_id index from the loaded graph.

    Used by :func:`extract_seed_ids` to map natural-language entity
    references in a query intent (e.g., ``"customers"``,
    ``"stg_orders"``, ``"raw.payments"``) onto the corresponding
    ``entity_id`` so GraphSearch can use them as ``seed_ids``.

    Indexes:
    - Entity name (from ``properties.name``) → entity_id
    - Last-segment of entity_id → entity_id (e.g.,
      ``"stg_customers"`` → ``"model.jaffle_shop.stg_customers"``)
    - For sources, also indexes ``"<source_name>.<table_name>"``
      → entity_id (e.g., ``"raw.customers"`` → the source).

    Same name entry is overwritten only if it would map to the same
    entity_id; otherwise duplicates are kept by the *first* hit, which
    is deterministic given a stable graph iteration order.
    """
    # Two-pass indexing so :data:`_NODE_PRIORITY_PREFIXES` (currently
    # ``"model."``) win the bare-name slot when a model and a source
    # share a name (``customers`` mart vs ``raw.customers`` source).
    # Without this, an intent like "the customers mart" can bind
    # ``customers`` -> source, then upstream-lineage expansion has
    # nowhere to go (the source is a leaf).
    index: dict[str, str] = {}
    nodes = _collect_graph_view(registry).nodes

    def _index_node(node: dict[str, Any]) -> None:
        entity_id = node["node_id"]
        properties = node.get("properties") or {}
        name = properties.get("name", "")
        if name:
            index.setdefault(name, entity_id)
        if "." in entity_id:
            short = entity_id.rsplit(".", 1)[-1]
            if short:
                index.setdefault(short, entity_id)
        if entity_id.startswith("source."):
            source_name = properties.get("source_name", "")
            if source_name and name:
                # Source-qualified key — uniquely identifies the source.
                index.setdefault(f"{source_name}.{name}", entity_id)

    def _is_priority(node: dict[str, Any]) -> bool:
        return any(node["node_id"].startswith(p) for p in _NODE_PRIORITY_PREFIXES)

    for node in nodes:
        if _is_priority(node):
            _index_node(node)
    for node in nodes:
        if not _is_priority(node):
            _index_node(node)
    return index


_DBT_TEST_TYPE_PREFIXES: Final = {
    "not_null": ["not null", "not-null", "no-null", "no null", "non-null"],
    "unique": ["unique", "uniqueness", "unique-key"],
    "relationships": [
        "relationships test",
        "relationship test",
        "foreign-key",
        "foreign key",
        "relationship between",
    ],
    "accepted_values": [
        "accepted values",
        "accepted-values",
        "enum test",
        "value check",
    ],
}


# Lineage-of-X category phrase templates. ``{name}`` is substituted with
# the model's display name (and ``{name} mart`` for marts-schema models)
# in :func:`build_category_index`. Each template comes in two forms —
# bare and ``the``-prefixed — because the category index uses
# word-boundary literal matching (``\b...\b``) and natural-language
# intents commonly say "lineage of THE customers" rather than "lineage
# of customers". The longest-first sort in
# :func:`extract_category_seeds` plus consumed-span tracking means the
# more specific phrase ("full upstream lineage of the customers mart")
# wins when present, and shorter forms back-fill otherwise — all of
# them resolve to the same closure, so the union is a no-op.
_LINEAGE_OF_X_TEMPLATES: Final[tuple[str, ...]] = (
    "upstream of {name}",
    "upstream of the {name}",
    "lineage of {name}",
    "lineage of the {name}",
    "ancestors of {name}",
    "ancestors of the {name}",
    "full upstream lineage of {name}",
    "full upstream lineage of the {name}",
    "upstream lineage of {name}",
    "upstream lineage of the {name}",
)


def build_category_index(
    registry: StoreRegistry,
) -> dict[str, list[str]]:
    """Build a category-phrase → list[entity_id] index for the dbt corpus.

    Layered alongside :func:`build_name_index`. Where the name index
    answers "which entity does this short-name refer to?", the category
    index answers "which entities match this *category* of intent?" —
    intents like ``"all the mart-layer models"`` (schema='marts'),
    ``"no null order_id"`` (test_type=not_null), ``"relationships test"``
    (test_type=relationships).

    The index is a `phrase -> list[entity_id]` so callers can union it
    with name seeds and pass the combined set to
    `GraphSearch(filters={"seed_ids": ..., "depth": 0})` to make the
    matched entities first-class candidates without GraphSearch traversal
    pulling in their structural neighbors.

    Phrases are matched longest-first against the lowercased intent in
    :func:`extract_category_seeds`. Multiple phrases can map to the same
    entity_id list (e.g., ``"marts"`` and ``"mart-layer"`` both expand
    to the schema='marts' set).

    In addition to layer and test-type phrases, this index emits a
    "lineage-of-X" family per model with non-empty upstream lineage:
    phrases like ``"upstream of customers"``,
    ``"lineage of the customers mart"``, and
    ``"full upstream lineage of the customers"`` all map to
    ``[<model_id>, *<closure>]`` (the model itself plus its full
    transitive ``dependsOn`` closure). With ``depth=0`` GraphSearch this
    surfaces the entire upstream subgraph in one pass — closing the
    ``multi_hop_lineage`` skill gap where ranking + the 8-item pack
    budget previously dropped staging/raw layers from depth=2 traversal.
    """
    layer_buckets: dict[str, list[str]] = {}
    test_type_buckets: dict[str, list[str]] = {}
    # Per-model display name → entity_id, harvested in the same pass so
    # we can emit lineage-of-X phrases without a second graph walk. Skip
    # sources (no outbound dependsOn → no lineage closure) and tests
    # (their "lineage" is the single model they cover, already handled
    # by name + test-type indices).
    model_names: dict[str, str] = {}
    model_schemas: dict[str, str] = {}
    for node in _collect_graph_view(registry).nodes:
        entity_id = node["node_id"]
        node_type = node.get("node_type", "")
        properties = node.get("properties") or {}
        # Layer classification by `schema` property.
        schema = properties.get("schema", "")
        if schema:
            layer_buckets.setdefault(schema.lower(), []).append(entity_id)
        _harvest_model_name(
            entity_id, node_type, properties, schema, model_names, model_schemas
        )
        # Test-type classification by node_id naming convention. dbt
        # auto-generates test names with the test type as a prefix
        # (e.g., `test.<project>.not_null_<table>_<column>.<hash>`).
        if node_type == "dbt_test":
            short = entity_id.rsplit(".", 1)[-1] if "." in entity_id else entity_id
            # Walk down from longer prefixes so `accepted_values_x` doesn't
            # bucket as `accepted` somewhere unexpected.
            for prefix in _DBT_TEST_TYPE_PREFIXES:
                # Need to also handle the case where the short id is a
                # hash, not the test name. In dbt manifests the test
                # name precedes the hash so the second-to-last segment
                # carries the prefix; check both.
                has_test_type_segment = entity_id.count(".") >= _DBT_TEST_ID_MIN_DOTS
                test_type_segment = (
                    entity_id.split(".")[-2] if has_test_type_segment else ""
                )
                for candidate in (short, test_type_segment):
                    if candidate.startswith(prefix + "_") or candidate == prefix:
                        test_type_buckets.setdefault(prefix, []).append(entity_id)
                        break

    index: dict[str, list[str]] = {}

    def _add(phrase: str, ids: list[str]) -> None:
        if ids:
            index.setdefault(phrase.lower(), list(dict.fromkeys(ids)))

    # Layer phrases. ``mart-layer``/``marts``/``mart layer`` all expand
    # to the schema='marts' set; same for staging and raw.
    for schema_key, phrases in {
        "marts": ["marts", "mart-layer", "mart layer", "mart-layer models"],
        "staging": ["staging", "staging-layer", "staging layer", "staging models"],
        "raw": ["raw layer", "source layer", "raw sources"],
    }.items():
        ids = layer_buckets.get(schema_key, [])
        for p in phrases:
            _add(p, ids)

    # Test-type phrases.
    for test_type, phrases in _DBT_TEST_TYPE_PREFIXES.items():
        ids = test_type_buckets.get(test_type, [])
        for p in phrases:
            _add(p, ids)

    # Lineage-of-X phrases. For every model with non-empty closure, emit
    # the full template family keyed on display name (and on
    # ``"<name> mart"`` for marts-schema models so phrases like
    # ``"lineage of the customers mart"`` — the exact form used by the
    # multi_hop_lineage ground-truth intent — match too). The closure
    # comes from the same memoized graph view, so this adds no extra
    # graph_store roundtrips.
    _emit_lineage_of_x_phrases(
        index,
        model_names=model_names,
        model_schemas=model_schemas,
        lineage_index=build_lineage_index(registry),
    )

    return index


def _harvest_model_name(
    entity_id: str,
    node_type: str,
    properties: dict[str, Any],
    schema: str,
    model_names: dict[str, str],
    model_schemas: dict[str, str],
) -> None:
    """Record *entity_id*'s display name (and schema if marts/staging/etc)
    into the per-model lookup tables — but only if the node is a model
    with a non-empty ``name``. No-op for sources / tests / nameless rows."""
    if node_type != "dbt_model":
        return
    name = properties.get("name", "")
    if not name:
        return
    model_names[entity_id] = name
    if schema:
        model_schemas[entity_id] = schema.lower()


def _emit_lineage_of_x_phrases(
    index: dict[str, list[str]],
    *,
    model_names: dict[str, str],
    model_schemas: dict[str, str],
    lineage_index: dict[str, list[str]],
) -> None:
    """Mutate *index* with the lineage-of-X phrase family.

    For every model in *model_names* that has a non-empty closure in
    *lineage_index*, registers the cartesian product of name variants
    against :data:`_LINEAGE_OF_X_TEMPLATES`. Marts-schema models also get
    ``"<name> mart"`` variants so the canonical "the customers mart"
    phrasing matches. Each phrase resolves to ``[<model_id>, *closure]``
    so a downstream ``GraphSearch(..., depth=0)`` returns the full
    upstream subgraph in one pass.
    """
    for entity_id, name in model_names.items():
        closure = lineage_index.get(entity_id)
        if not closure:
            continue
        seeds = [entity_id, *closure]
        name_variants = [name]
        if model_schemas.get(entity_id) == "marts":
            name_variants.append(f"{name} mart")
        for variant in name_variants:
            for template in _LINEAGE_OF_X_TEMPLATES:
                phrase = template.format(name=variant).lower()
                index.setdefault(phrase, list(dict.fromkeys(seeds)))


def build_lineage_index(
    registry: StoreRegistry,
) -> dict[str, list[str]]:
    """Build an entity_id → list[ancestor entity_ids] index by transitive
    closure of outbound ``dependsOn`` edges.

    For each non-source entity, records every transitively reachable
    entity along outbound ``dependsOn`` edges. The result is a
    per-entity "full upstream lineage" set that can be injected as
    additional seeds when an intent says "upstream of X" / "lineage
    of X" / "ancestors of X".

    This precompute exists because :meth:`GraphStore.get_subgraph` is
    bidirectional — calling it from a leaf seed at depth=2 pulls in
    *inbound* test edges and 2-hop test-of-staging neighbors, which
    crowd out the required staging/raw entities under the 8-item pack
    budget. Computing the directional closure at load time and seeding
    GraphSearch with ``depth=0`` returns exactly the lineage entities,
    nothing else.

    Implementation: BFS over the in-memory ``dependsOn`` adjacency
    from :func:`_collect_graph_view`. Eliminates the per-entity
    ``get_edges`` DB roundtrips of the prior implementation (the
    dominant cost on real backends like Postgres/Neo4j) and shares
    the single-fetch view with :func:`build_name_index` and
    :func:`build_category_index`. List order is BFS-layer order to
    match the legacy output exactly (preserves downstream
    ``GraphSearch(seed_ids=..., depth=0)`` ranking under budget
    pressure).

    Asymptotic: ``O(sum(closure_size))`` per node, bounded by
    ``O(N * depth)`` in practice. For a real dbt manifest with low
    fanout and shallow depth, this is negligible compared to the
    eliminated DB roundtrips. A future refactor could swap to true
    O(N + E) topo-sort + DP at the cost of a different (set-equal
    but list-reordered) ancestor list — left as-is here so the
    public surface returns byte-identical output.
    """
    direct = _collect_graph_view(registry).depends_on
    index: dict[str, list[str]] = {}
    for node_id in direct:
        ancestors: list[str] = []
        seen: set[str] = {node_id}
        frontier: list[str] = [node_id]
        while frontier:
            next_frontier: list[str] = []
            for current in frontier:
                for target in direct.get(current, ()):
                    if target in seen:
                        continue
                    seen.add(target)
                    ancestors.append(target)
                    next_frontier.append(target)
            frontier = next_frontier
        if ancestors:
            index[node_id] = ancestors
    return index


_LINEAGE_INTENT_PATTERN = re.compile(
    r"\b(?:upstream|lineage|ancestors?|dependencies|depends?\s+on)\b",
    flags=re.IGNORECASE,
)


def expand_seeds_with_lineage(
    seeds: list[str], intent: str, lineage_index: dict[str, list[str]]
) -> list[str]:
    """When *intent* contains a lineage keyword, expand each seed with
    its precomputed ancestor list.

    No-op when the intent has no lineage signal — same shape as
    :func:`extract_category_seeds`, just sourced from a different
    precomputed index. The combination of (name seed: m_customers) +
    (lineage expansion) produces the closed set the intent is asking
    for, ready to pass as ``filters["seed_ids"]`` with ``depth=0``.
    """
    if not _LINEAGE_INTENT_PATTERN.search(intent):
        return seeds
    expanded: list[str] = list(seeds)
    seen = set(seeds)
    for s in seeds:
        for a in lineage_index.get(s, []):
            if a not in seen:
                expanded.append(a)
                seen.add(a)
    return expanded


def extract_category_seeds(
    intent: str, category_index: dict[str, list[str]]
) -> list[str]:
    """Find entity_ids matching category phrases in *intent*.

    Returns a deduplicated list. Each phrase maps to a *set* of entities
    (e.g., all dbt_test entities of test_type=not_null), and multiple
    phrases may match in a single intent (e.g., "not null tests on
    order_id"). The union is returned in insertion order.

    Used alongside :func:`extract_seed_ids` — the dbt scenario calls
    both and unions the results before passing as ``seed_ids`` to
    GraphSearch with ``depth=0`` so the matched entities are
    first-class pack candidates without graph-traversal noise.
    """
    seeds: list[str] = []
    seen: set[str] = set()
    intent_lower = intent.lower()
    consumed_spans: list[tuple[int, int]] = []
    for phrase in sorted(category_index, key=len, reverse=True):
        for match in _word_boundary_pattern(phrase).finditer(intent_lower):
            start, end = match.span()
            if any(s <= start < e or s < end <= e for s, e in consumed_spans):
                continue
            for eid in category_index[phrase]:
                if eid not in seen:
                    seeds.append(eid)
                    seen.add(eid)
            consumed_spans.append((start, end))
    return seeds


@functools.lru_cache(maxsize=4096)
def _word_boundary_pattern(name: str) -> re.Pattern[str]:
    """Cached compile of ``\\b{escaped(name)}\\b`` for the lower-cased
    short-name. Without this, the github name_index (1154 entries) x
    rounds x scenarios drives ~6-figure recompiles per session at
    ~10us each. The cache is keyed on the lower-cased short-name so
    a stable name_index pays at most once.

    The cache is process-global. This is sound across corpora because
    the compiled pattern depends only on the *name string*, not on
    which entity_id the caller's index maps it to — two scenarios that
    both have a ``"customers"`` name share the same regex even when
    they bind it to different entity_ids."""
    return re.compile(r"\b" + re.escape(name.lower()) + r"\b")


def extract_seed_ids(intent: str, name_index: dict[str, str]) -> list[str]:
    """Find entity ids referenced in an intent string.

    Returns the deduplicated list of ``entity_id`` values whose
    corresponding short-name appears in *intent* with word boundaries.
    Word-boundary matching means ``"customers"`` matches the
    ``customers`` mart but not the substring inside ``"stg_customers"``.

    Names are tried longest-first so multi-segment names like
    ``"raw.customers"`` win over the single-word ``"customers"`` when
    both could match. The name's matched span is recorded so subsequent
    shorter names don't claim it.

    Used by the dbt corpus convergence scenario to populate
    ``filters["seed_ids"]`` for GraphSearch on each round. Empty result
    means "intent doesn't reference any known entity by name" — the
    scenario's ``SeededGraphSearch`` wrapper interprets that as "skip
    GraphSearch this round" rather than fall back to GraphSearch's
    no-seed default of returning every non-structural node.
    """
    seeds: list[str] = []
    seen_entity_ids: set[str] = set()
    consumed_spans: list[tuple[int, int]] = []
    intent_lower = intent.lower()
    # Longest names first so ``raw.customers`` wins over ``customers``
    # when both sit in the index.
    for name in sorted(name_index, key=len, reverse=True):
        for match in _word_boundary_pattern(name).finditer(intent_lower):
            start, end = match.span()
            # Skip if this span was already claimed by a longer name.
            if any(s <= start < e or s < end <= e for s, e in consumed_spans):
                continue
            entity_id = name_index[name]
            if entity_id not in seen_entity_ids:
                seeds.append(entity_id)
                seen_entity_ids.add(entity_id)
            consumed_spans.append((start, end))
    return seeds


def load_jaffle_shop_corpus(
    registry: StoreRegistry,
    *,
    manifest_path: Path = DEFAULT_MANIFEST_PATH,
) -> LoadResult:
    """Load the Jaffle Shop manifest fixture into the registry.

    See [`eval/corpora/jaffle_shop/README.md`](jaffle_shop/README.md) for
    what the fixture contains and how to regenerate it from real dbt.
    """
    if not manifest_path.exists():
        msg = (
            f"Jaffle Shop manifest not found at {manifest_path}. "
            f"See eval/corpora/jaffle_shop/README.md for the fixture "
            f"location or regeneration steps."
        )
        raise FileNotFoundError(msg)

    raw = manifest_path.read_text(encoding="utf-8")
    manifest = json.loads(raw)

    # Strip our own metadata block — extractor would ignore it but the
    # log signal is cleaner without "extractor saw an extra key" noise.
    manifest.pop("_metadata", None)

    # Drop any cached :class:`_GraphView` for this graph store — the load
    # is about to mutate it, and the three sibling builders called after
    # this function must see the post-load nodes/edges.
    _GRAPH_VIEW_CACHE.pop(registry.knowledge.graph_store, None)

    logger.info(
        "dbt_loader.start",
        manifest_path=str(manifest_path),
        nodes_in_manifest=len(manifest.get("nodes", {})),
        sources_in_manifest=len(manifest.get("sources", {})),
    )

    extraction = _run_extraction(registry, manifest)
    extraction.edges, edge_canonicalizations = _canonicalize_edges(extraction.edges)
    nodes_created, edges_created = _execute_through_governed_pipeline(
        registry, extraction
    )
    documents_indexed = _index_descriptions(registry, extraction)

    result = LoadResult(
        entities_extracted=len(extraction.entities),
        edges_extracted=len(extraction.edges),
        nodes_created=nodes_created,
        edges_created=edges_created,
        documents_indexed=documents_indexed,
        edge_kind_canonicalizations=edge_canonicalizations,
    )
    logger.info(
        "dbt_loader.done",
        **result.as_metrics(prefix="corpus"),
    )
    return result
