"""Seed helpers — populate stores with the corpus the loop will curate.

Implemented for the reference-driver build of F6 (issue #249). The
three helpers carve the corpus into the slices the inner loop operates
on:

- :func:`seed_under_populated_nodes` — graph nodes with a name but no
  description and no consolidated summary document. Created through
  the **governed pipeline** (``ENTITY_CREATE`` via
  ``build_curate_executor``) so the scenario exercises the production
  write path, not direct store writes.
- :func:`seed_documents_for_nodes` — fragmented source notes, one fact
  per document. The reference curator's job is to consolidate these.
  Fragmentation is what makes retrieval lift *measurable*: a pack with
  a small ``max_items`` budget can only cover a fraction of a node's
  facts while they live one-per-document.
- :func:`seed_baseline_corpus` — a background trace/document corpus
  (the scenario-5.2 generator) that adds retrieval competition and
  supplies a *stability panel* of queries unrelated to the enrichment
  work, so the scenario can show enrichment helped its own queries
  without disturbing others.

Document writes here are direct ``document_store.put`` calls — the
sanctioned eval-scenario seeding exception (see CLAUDE.md); the events
a governed path would emit are reproducible from the deterministic
seed.
"""

from __future__ import annotations

from typing import Any

import structlog

from eval.generators.trace_generator import generate_corpus
from trellis.mutate import Command, CommandStatus, Operation, build_curate_executor
from trellis.stores.registry import StoreRegistry

logger = structlog.get_logger(__name__)

#: Domain stamped on every skill-loop node, document, and panel query.
#: Distinct from the generator's domains so the enrichment panel and the
#: stability panel never share retrieval candidates by accident.
SKILL_DOMAIN = "skill_loop"


def node_name(node_id: str) -> str:
    """Human-readable name for a seeded node id (``skill:node:007`` → topic)."""
    index = node_id.rsplit(":", 1)[-1]
    return f"skill_topic_{index}"


def facts_for_node(node_id: str, *, docs_per_node: int) -> list[str]:
    """Deterministic fact tokens for a node — one per source document.

    These tokens are the ground truth the query panel's
    ``required_coverage`` is built from. They deliberately look like
    nothing else in the corpus so keyword retrieval hits are
    unambiguous.
    """
    name = node_name(node_id)
    return [f"{name}_fact_{j}" for j in range(docs_per_node)]


def seed_under_populated_nodes(
    registry: StoreRegistry,
    *,
    seed: int,
    node_count: int,
) -> list[str]:
    """Upsert ``node_count`` under-populated graph nodes; return their ids.

    "Under-populated" is concrete: the node has a ``name`` property and
    nothing else — no ``description``, no consolidated summary document.
    The reference curator's enrichment adds both. Writes go through the
    governed mutation pipeline (``ENTITY_CREATE``), matching how a
    production curator skill would land its output.

    ``seed`` participates in the id space so two differently-seeded runs
    in one registry cannot collide.
    """
    executor = build_curate_executor(registry)
    node_ids: list[str] = []
    for i in range(node_count):
        node_id = f"skill:{seed:04d}:node:{i:03d}"
        result = executor.execute(
            Command(
                operation=Operation.ENTITY_CREATE,
                args={
                    "entity_id": node_id,
                    "entity_type": "concept",
                    "name": node_name(node_id),
                    "properties": {"name": node_name(node_id)},
                },
                requested_by="eval:skill_loop_convergence:seed",
            )
        )
        if result.status is not CommandStatus.SUCCESS:
            msg = f"seeding node {node_id!r} failed: {result.message}"
            raise RuntimeError(msg)
        node_ids.append(node_id)
    logger.info("skill_loop.seeded_nodes", count=len(node_ids))
    return node_ids


def seed_documents_for_nodes(
    registry: StoreRegistry,
    node_ids: list[str],
    *,
    seed: int,  # noqa: ARG001 — content is a pure function of node identity
    docs_per_node: int,
) -> int:
    """Populate the document store with fragmented source notes.

    One fact per document. Every note mentions the node's name (so the
    panel query retrieves it at baseline) but carries only a single fact
    token — pre-enrichment packs must spend one budget slot per fact,
    which is exactly the inefficiency the curator's consolidated summary
    removes. Returns the number of documents written.
    """
    document_store = registry.knowledge.document_store
    written = 0
    for node_id in node_ids:
        name = node_name(node_id)
        for k, fact in enumerate(facts_for_node(node_id, docs_per_node=docs_per_node)):
            document_store.put(
                doc_id=f"doc:src:{node_id}:{k}",
                content=(
                    f"Working note {k} on {name}. Observed detail: {fact}. "
                    f"Raw capture, not yet consolidated."
                ),
                metadata={
                    "entity_id": node_id,
                    "domain": SKILL_DOMAIN,
                    "domains": [SKILL_DOMAIN],
                    "content_type": "source_note",
                    # Pass PackBuilder's default signal_quality filter.
                    "content_tags": {"signal_quality": "standard"},
                },
            )
            written += 1
    logger.info("skill_loop.seeded_documents", count=written)
    return written


def seed_baseline_corpus(
    registry: StoreRegistry,
    *,
    seed: int,
    traces_per_domain: int,
    entities_per_trace: int,
) -> dict[str, Any]:
    """Seed the background trace/document corpus + return its manifest.

    Wraps :func:`eval.generators.trace_generator.generate_corpus` the
    same way ``agent_loop_convergence`` does: one entity node + one
    ``entity_summary`` document per generated entity. These documents
    compete in keyword retrieval and back the **stability panel** — the
    generator's per-domain queries, whose scores should stay flat while
    the skill-loop panel's scores climb.

    The returned manifest is JSON-serialisable and lands in the report's
    ``convergence_stats`` for grep-ability.
    """
    corpus = generate_corpus(
        seed=seed,
        traces_per_domain=traces_per_domain,
        entities_per_trace=entities_per_trace,
    )
    graph_store = registry.knowledge.graph_store
    document_store = registry.knowledge.document_store

    entities_seen: set[str] = set()
    for generated in corpus.traces:
        for entity in generated.entities:
            if entity in entities_seen:
                continue
            entities_seen.add(entity)
            graph_store.upsert_node(
                node_id=entity,
                node_type="entity",
                properties={"name": entity, "domain": generated.domain},
            )
            document_store.put(
                doc_id=f"doc:{entity}",
                content=(
                    f"{entity} ({generated.domain}). Background corpus entity "
                    f"summary for retrieval competition."
                ),
                metadata={
                    "entity_id": entity,
                    "domain": generated.domain,
                    "domains": [generated.domain],
                    "content_type": "entity_summary",
                    "content_tags": {"signal_quality": "standard"},
                },
            )

    manifest: dict[str, Any] = {
        "traces_generated": len(corpus.traces),
        "background_entities": len(entities_seen),
        "stability_queries": [
            {
                "domain": q.domain,
                "intent": q.intent,
                "required_coverage": list(q.required_coverage),
            }
            for q in corpus.queries
        ],
    }
    logger.info(
        "skill_loop.seeded_baseline_corpus",
        traces=manifest["traces_generated"],
        entities=manifest["background_entities"],
    )
    return manifest
