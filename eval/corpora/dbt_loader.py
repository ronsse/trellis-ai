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
from dataclasses import dataclass
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


DEFAULT_MANIFEST_PATH = (
    Path(__file__).parent / "jaffle_shop" / "manifest.json"
)


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


def _index_descriptions(
    registry: StoreRegistry, result: ExtractionResult
) -> int:
    """Index entity descriptions into the document store.

    Uses the same id scheme as the synthetic baseline (``doc:{entity_id}``)
    so retrieval queries can target docs by predictable name.
    """
    document_store = registry.knowledge.document_store
    indexed = 0
    for entity in result.entities:
        description = entity.properties.get("description", "") if entity.properties else ""
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
    # Two-pass indexing so models win the bare-name slot when a model and
    # a source share a name (``customers`` mart vs ``raw.customers``
    # source). Without this, an intent like "the customers mart" can
    # bind ``customers`` -> source, then upstream-lineage expansion has
    # nowhere to go (the source is a leaf). Pass 1 = models, pass 2 =
    # sources + the rest.
    index: dict[str, str] = {}
    nodes = list(registry.knowledge.graph_store.query(limit=5000))

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

    for node in nodes:
        if node["node_id"].startswith("model."):
            _index_node(node)
    for node in nodes:
        if not node["node_id"].startswith("model."):
            _index_node(node)
    return index


_DBT_TEST_TYPE_PREFIXES: Final = {
    "not_null": ["not null", "not-null", "no-null", "no null", "non-null"],
    "unique": ["unique", "uniqueness", "unique-key"],
    "relationships": [
        "relationships test", "relationship test", "foreign-key",
        "foreign key", "relationship between",
    ],
    "accepted_values": [
        "accepted values", "accepted-values", "enum test", "value check",
    ],
}


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
    """
    layer_buckets: dict[str, list[str]] = {}
    test_type_buckets: dict[str, list[str]] = {}
    for node in registry.knowledge.graph_store.query(limit=5000):
        entity_id = node["node_id"]
        node_type = node.get("node_type", "")
        properties = node.get("properties") or {}
        # Layer classification by `schema` property.
        schema = properties.get("schema", "")
        if schema:
            layer_buckets.setdefault(schema.lower(), []).append(entity_id)
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
                for candidate in (short, entity_id.split(".")[-2] if entity_id.count(".") >= 2 else ""):
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

    return index


def build_lineage_index(
    registry: StoreRegistry,
) -> dict[str, list[str]]:
    """Build an entity_id → list[ancestor entity_ids] index by transitive
    closure of outbound ``dependsOn`` edges.

    For each non-source entity, walks outbound edges (BFS) until exhaustion
    and records every reachable entity. The result is a per-entity
    "full upstream lineage" set that can be injected as additional seeds
    when an intent says "upstream of X" / "lineage of X" / "ancestors of X".

    This precompute exists because :meth:`GraphStore.get_subgraph` is
    bidirectional — calling it from a leaf seed at depth=2 pulls in
    *inbound* test edges and 2-hop test-of-staging neighbors, which
    crowd out the required staging/raw entities under the 8-item pack
    budget. Computing the directional closure at load time and seeding
    GraphSearch with ``depth=0`` returns exactly the lineage entities,
    nothing else.

    Returns ``entity_id`` → ordered list of ancestor entity_ids
    (excluding the entity itself).
    """
    g = registry.knowledge.graph_store
    all_nodes = [n["node_id"] for n in g.query(limit=5000)]
    index: dict[str, list[str]] = {}
    for node_id in all_nodes:
        ancestors: list[str] = []
        seen: set[str] = {node_id}
        frontier: list[str] = [node_id]
        while frontier:
            next_frontier: list[str] = []
            for current in frontier:
                for edge in g.get_edges(current, direction="outgoing"):
                    target = edge.get("target_id")
                    if not target or target in seen:
                        continue
                    # Follow only ``dependsOn`` edges. Some edges have
                    # ``edge_kind`` returned as None on this backend; tests
                    # showed the canonicalization happens upstream so the
                    # filter is layered on edge_kind only when set.
                    edge_kind = edge.get("edge_kind") or edge.get("edge_type")
                    if edge_kind not in (None, DEPENDS_ON, "depends_on"):
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
    short-name. Without this, the github name_index (1154 entries) ×
    rounds × scenarios drives ~6-figure recompiles per session at
    ~10µs each. The cache is keyed on the lower-cased short-name so
    a stable name_index pays at most once."""
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
