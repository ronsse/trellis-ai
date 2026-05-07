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
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from trellis.extract.commands import result_to_batch
from trellis.extract.dispatcher import ExtractionDispatcher
from trellis.extract.registry import ExtractorRegistry
from trellis.mutate.commands import CommandStatus, Operation
from trellis.mutate.executor import MutationExecutor
from trellis.mutate.handlers import create_curate_handlers
from trellis.schemas.extraction import EdgeDraft, ExtractionResult
from trellis.schemas.well_known import canonicalize_edge_kind
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
    "depends_on": "dependsOn",
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
    index: dict[str, str] = {}
    for node in registry.knowledge.graph_store.query(limit=5000):
        entity_id = node["node_id"]
        properties = node.get("properties") or {}
        name = properties.get("name", "")
        if name:
            index.setdefault(name, entity_id)
        # Last segment after the final dot — covers `stg_customers`,
        # `customers`, etc. for both models and sources.
        if "." in entity_id:
            short = entity_id.rsplit(".", 1)[-1]
            if short:
                index.setdefault(short, entity_id)
        # For source entities, expose `<source_name>.<table>` so query
        # text mentioning "raw.customers" matches the source.
        if entity_id.startswith("source."):
            source_name = properties.get("source_name", "")
            if source_name and name:
                index.setdefault(f"{source_name}.{name}", entity_id)
    return index


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
        # Use the dot/dot/dash variants where . may be present (sources).
        # ``re.escape`` handles dots safely. Word-boundary on both sides.
        pattern = r"\b" + re.escape(name.lower()) + r"\b"
        for match in re.finditer(pattern, intent_lower):
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
