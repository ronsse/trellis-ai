"""Synthetic-traces end-to-end scenario.

Mechanism summary (full prose in the README):
1. Build deterministic trace corpus + ground-truth follow-up queries.
2. Append every trace to the operational ``TraceStore``.
3. Mirror entity extraction by upserting one graph node + one document
   per entity name. (Real extraction would happen in a worker — for the
   eval scenario we just want a populated graph + document store with
   known ground truth.)
4. Build a pack per follow-up query via a keyword-only ``PackBuilder``.
5. Score each pack with ``evaluate_pack`` and aggregate.

Single backend per run — multi-backend equivalence is scenario 5.1's
job. This scenario uses the runner-supplied ``registry`` if given, else
spins up a tmp SQLite registry.
"""

from __future__ import annotations

import statistics

from eval.generators.trace_generator import (
    EvalQuery,
    GeneratedCorpus,
    GeneratedTrace,
    generate_corpus,
)
from eval.runner import Finding, ScenarioReport, ScenarioStatus
from trellis.retrieve.evaluate import (
    BUILTIN_PROFILES,
    EvaluationScenario,
    evaluate_pack,
)
from trellis.retrieve.pack_builder import PackBuilder
from trellis.retrieve.strategies import KeywordSearch
from trellis.schemas.pack import Pack, PackBudget
from trellis.stores.registry import StoreRegistry

DEFAULT_TRACES_PER_DOMAIN = 10
DEFAULT_ENTITIES_PER_TRACE = 3
DEFAULT_PACK_MAX_ITEMS = 8
DEFAULT_PACK_MAX_TOKENS = 1_500
WEIGHTED_REGRESS_THRESHOLD = 0.5
DEFAULT_PROFILE_NAME = "domain_context"


def _ingest_traces(registry: StoreRegistry, corpus: GeneratedCorpus) -> int:
    """Append every generated trace to the trace store; return count."""
    trace_store = registry.operational.trace_store
    count = 0
    for gt in corpus.traces:
        trace_store.append(gt.trace)
        count += 1
    return count


def _populate_graph_and_documents(
    registry: StoreRegistry, corpus: GeneratedCorpus
) -> int:
    """Mirror minimal entity extraction.

    For each unique entity name across the corpus:
      * upsert a graph node ``(node_id=<entity>, node_type=entity)``
      * put a document whose content names the entity + its domain
        peers, so the keyword search strategy can score it against an
        intent that mentions the entity.

    Real extraction would do more (edges between co-occurring entities,
    importance scoring, etc.) — keep the eval scenario focused on the
    *retrieval scoring* surface, not on rebuilding extraction.
    """
    knowledge = registry.knowledge
    graph_store = knowledge.graph_store
    document_store = knowledge.document_store

    # Build a per-entity domain → "trace contexts that mention it" so
    # the document content is plausibly relevant to the follow-up
    # query.
    by_entity: dict[str, list[GeneratedTrace]] = {}
    for gt in corpus.traces:
        for entity in gt.entities:
            by_entity.setdefault(entity, []).append(gt)

    upserted = 0
    for entity, traces in by_entity.items():
        domain = traces[0].domain  # entities don't cross domains in this corpus
        graph_store.upsert_node(
            node_id=entity,
            node_type="entity",
            properties={"name": entity, "domain": domain},
        )
        intents = sorted({t.trace.intent for t in traces})
        content = (
            f"{entity} ({domain}). Referenced by {len(traces)} traces. "
            f"Sample intents: {'; '.join(intents[:5])}."
        )
        document_store.put(
            doc_id=f"doc:{entity}",
            content=content,
            metadata={
                "entity_id": entity,
                "domain": domain,
                "content_type": "entity_summary",
                "domains": [domain],
            },
        )
        upserted += 1
    return upserted


def _build_pack_for_query(builder: PackBuilder, query: EvalQuery) -> Pack:
    return builder.build(
        intent=query.intent,
        domain=query.domain,
        budget=PackBudget(
            max_items=DEFAULT_PACK_MAX_ITEMS,
            max_tokens=DEFAULT_PACK_MAX_TOKENS,
        ),
    )


def _score_pack(pack: Pack, query: EvalQuery) -> dict[str, float]:
    eval_scenario = EvaluationScenario(
        name=f"synthetic_{query.domain}",
        intent=query.intent,
        domain=query.domain,
        required_coverage=query.required_coverage,
        expected_categories=["entity_summary"],
    )
    report = evaluate_pack(
        pack,
        eval_scenario,
        profile=BUILTIN_PROFILES.get(DEFAULT_PROFILE_NAME),
    )
    return {
        **report.dimensions,
        "weighted_score": report.weighted_score,
        "missing_coverage_count": float(len(report.missing_coverage)),
    }


def run(
    registry: StoreRegistry,
    *,
    seed: int = 0,
    traces_per_domain: int = DEFAULT_TRACES_PER_DOMAIN,
    entities_per_trace: int = DEFAULT_ENTITIES_PER_TRACE,
) -> ScenarioReport:
    """Execute the synthetic-traces scenario against the supplied registry.

    The runner always supplies a registry. Tests may construct their
    own SQLite registry inline.
    """
    corpus = generate_corpus(
        seed=seed,
        traces_per_domain=traces_per_domain,
        entities_per_trace=entities_per_trace,
    )

    findings: list[Finding] = []
    metrics: dict[str, float] = {
        "trace_count": float(len(corpus.traces)),
        "query_count": float(len(corpus.queries)),
        "entity_count": float(len(corpus.all_entities)),
    }

    ingested = _ingest_traces(registry, corpus)
    upserted = _populate_graph_and_documents(registry, corpus)
    metrics["traces_ingested"] = float(ingested)
    metrics["entities_upserted"] = float(upserted)

    builder = PackBuilder(strategies=[KeywordSearch(registry.knowledge.document_store)])

    per_query_weighted: list[float] = []
    per_dimension: dict[str, list[float]] = {}
    for query in corpus.queries:
        pack = _build_pack_for_query(builder, query)
        scores = _score_pack(pack, query)

        for k, v in scores.items():
            metrics[f"{query.domain}.{k}"] = round(v, 4)
            if k != "missing_coverage_count":
                per_dimension.setdefault(k, []).append(v)

        per_query_weighted.append(scores["weighted_score"])

        if scores.get("missing_coverage_count", 0) > 0:
            findings.append(
                Finding(
                    severity="info",
                    message=(
                        f"{query.domain}: pack missing "
                        f"{int(scores['missing_coverage_count'])} of "
                        f"{len(query.required_coverage)} required entities"
                    ),
                    detail={"required_coverage": query.required_coverage},
                )
            )

    if per_query_weighted:
        metrics["aggregate.weighted_score_mean"] = round(
            statistics.fmean(per_query_weighted), 4
        )
        metrics["aggregate.weighted_score_min"] = round(min(per_query_weighted), 4)
    for dim, values in per_dimension.items():
        metrics[f"aggregate.{dim}_mean"] = round(statistics.fmean(values), 4)

    aggregate_mean = metrics.get("aggregate.weighted_score_mean", 0.0)
    status: ScenarioStatus
    if aggregate_mean < WEIGHTED_REGRESS_THRESHOLD:
        findings.append(
            Finding(
                severity="warn",
                message=(
                    f"aggregate weighted score {aggregate_mean:.3f} below "
                    f"{WEIGHTED_REGRESS_THRESHOLD} threshold — pin a baseline "
                    "and investigate"
                ),
            )
        )
        status = "regress"
    else:
        status = "pass"

    decision = (
        "Per-dimension and aggregate weighted scores are now produced for the "
        "domain-context profile. Pin these as a baseline; subsequent runs "
        "diff against it. Plan §5.2 deferred items unblocked: retrieval "
        "regression detection (this scenario *is* the detector once a "
        "baseline is committed). Follow-up work tracked in the scenario "
        "README — get_objective_context / get_task_context coverage and "
        "vector-strategy comparison."
    )

    return ScenarioReport(
        name="synthetic_traces",
        status=status,
        metrics=metrics,
        findings=findings,
        decision=decision,
    )
