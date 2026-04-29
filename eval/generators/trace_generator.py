"""Deterministic synthetic trace generator.

Used by scenario 5.2 (synthetic traces e2e). Produces traces in three
coarse domains — software engineering, data pipeline ops, customer
support — each with a known ground-truth set of entity names that
should appear in a follow-up retrieval pack.

Determinism: every randomness source is a seeded ``random.Random``
instance.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from trellis.schemas.enums import OutcomeStatus, TraceSource
from trellis.schemas.trace import Outcome, Trace, TraceContext, TraceStep


@dataclass(frozen=True)
class _DomainTemplate:
    """Per-domain content templates.

    ``intent_template`` interpolates ``{n}`` for the trace ordinal so
    intents differ across traces of the same domain. ``entities``
    enumerates the entity-name pool the generator draws from.
    ``query_intent`` is the follow-up query a future agent might ask;
    paired with ``required_coverage`` it forms the ground-truth eval
    scenario for that domain.
    """

    name: str
    intent_template: str
    entities: list[str]
    query_intent: str
    required_coverage: list[str]


DOMAIN_TEMPLATES: list[_DomainTemplate] = [
    _DomainTemplate(
        name="software_engineering",
        intent_template="Refactor module {n} to extract pure helpers",
        entities=[
            "auth_module",
            "session_token",
            "rate_limiter",
            "request_router",
            "config_loader",
            "test_harness",
            "ci_pipeline",
            "feature_flag",
        ],
        # Query mentions every required entity by name so FTS5 can find
        # them all by token-overlap. Without this row-3 fix, only
        # ``session_token`` matched (via "session" / "token") and the
        # other two were unreachable, driving SE's success_rate to 0.
        query_intent=(
            "How do we structure session_token validation, "
            "auth_module integration, and rate_limiter behavior?"
        ),
        required_coverage=["session_token", "auth_module", "rate_limiter"],
    ),
    _DomainTemplate(
        name="data_pipeline",
        intent_template="Backfill table partition {n}",
        entities=[
            "etl_job",
            "staging_table",
            "fact_table",
            "warehouse_role",
            "schema_registry",
            "lineage_event",
            "quality_check",
            "watermark",
        ],
        query_intent=(
            "What are the upstream dependencies of fact_table backfills "
            "via the etl_job pipeline and the staging_table?"
        ),
        required_coverage=["fact_table", "etl_job", "staging_table"],
    ),
    _DomainTemplate(
        name="customer_support",
        intent_template="Resolve ticket #{n} for billing dispute",
        entities=[
            "ticket_queue",
            "billing_record",
            "refund_policy",
            "agent_macro",
            "escalation_path",
            "customer_account",
            "support_kb",
            "sla_timer",
        ],
        query_intent=(
            "What's our refund_policy for a billing_record dispute in the ticket_queue?"
        ),
        required_coverage=["refund_policy", "billing_record", "ticket_queue"],
    ),
]


@dataclass
class GeneratedTrace:
    """A single synthetic trace plus its ground-truth labels."""

    trace: Trace
    domain: str
    entities: list[str]
    """Entity names cited in this trace's steps. These map to graph node
    ids the scenario will create."""


@dataclass
class EvalQuery:
    """A follow-up query the eval scenario poses + its ground truth."""

    domain: str
    intent: str
    required_coverage: list[str]


@dataclass
class GeneratedCorpus:
    """Container the scenario consumes."""

    traces: list[GeneratedTrace]
    queries: list[EvalQuery]
    """One query per domain. The scenario builds a pack per query and
    scores it against the matching ``required_coverage``."""

    @property
    def all_entities(self) -> list[str]:
        """Sorted unique entity-name list across the corpus.

        The scenario uses this to seed the graph + document store so a
        retrieval strategy can find them by name.
        """
        seen: set[str] = set()
        out: list[str] = []
        for t in self.traces:
            for e in t.entities:
                if e not in seen:
                    seen.add(e)
                    out.append(e)
        return sorted(out)


def _entity_subset(rng: random.Random, entities: list[str], k: int) -> list[str]:
    return rng.sample(entities, k=min(k, len(entities)))


def _entity_subset_with_anchor(
    rng: random.Random, entities: list[str], k: int, anchor: str
) -> list[str]:
    """Sample ``k`` distinct entities, guaranteeing ``anchor`` is included.

    The remaining ``k - 1`` slots are sampled without replacement from
    ``entities`` excluding ``anchor``. Order: anchor first, then the
    random fill — preserving determinism given the seed.
    """
    others = [e for e in entities if e != anchor]
    fill_count = max(0, k - 1)
    fill = rng.sample(others, k=min(fill_count, len(others)))
    return [anchor, *fill]


def generate_corpus(
    *,
    seed: int = 0,
    traces_per_domain: int = 10,
    entities_per_trace: int = 3,
) -> GeneratedCorpus:
    """Build a deterministic synthetic trace corpus.

    Defaults (10 traces/domain x 3 domains = 30 traces) are smaller than
    plan §5.2's 100-1000 target; the scenario kwargs let scheduled runs
    dial them up. The point of the smaller default is dev-machine
    iteration speed — the *pipeline* is the same shape regardless of
    scale.
    """
    if traces_per_domain <= 0:
        msg = "traces_per_domain must be positive"
        raise ValueError(msg)
    if entities_per_trace <= 0:
        msg = "entities_per_trace must be positive"
        raise ValueError(msg)

    rng = random.Random(seed)  # noqa: S311 — synthetic test data, not crypto

    traces: list[GeneratedTrace] = []
    base_time = datetime(2026, 1, 1, tzinfo=UTC)

    for template in DOMAIN_TEMPLATES:
        # Guarantee every ``required_coverage`` entity is sampled into
        # at least one trace per domain. Without this, with small
        # ``traces_per_domain`` and the ``rng.sample`` draw, some
        # required entities never appear — so they have no document
        # afterward and are unfindable by the domain query (the
        # 2026-04-28 scenario 5.4 baseline missed
        # ``customer_support``'s ``billing_record`` + ``refund_policy``
        # this way). Anchor the first ``len(required_coverage)`` traces
        # to one required entity each; later traces sample randomly.
        guaranteed = min(len(template.required_coverage), traces_per_domain)
        for n in range(traces_per_domain):
            if n < guaranteed:
                anchor = template.required_coverage[n]
                entities = _entity_subset_with_anchor(
                    rng, template.entities, entities_per_trace, anchor
                )
            else:
                entities = _entity_subset(rng, template.entities, entities_per_trace)
            steps = [
                TraceStep(
                    step_type="action",
                    name=f"touch_{e}",
                    args={"entity": e},
                    result={"status": "ok"},
                    started_at=base_time + timedelta(minutes=n),
                )
                for e in entities
            ]
            trace = Trace(
                source=TraceSource.AGENT,
                intent=template.intent_template.format(n=n),
                steps=steps,
                outcome=Outcome(
                    status=OutcomeStatus.SUCCESS,
                    summary=(
                        f"{template.name} trace {n} touched {len(entities)} entities"
                    ),
                ),
                context=TraceContext(
                    agent_id="synthetic_agent",
                    domain=template.name,
                    started_at=base_time + timedelta(minutes=n),
                ),
            )
            traces.append(
                GeneratedTrace(
                    trace=trace,
                    domain=template.name,
                    entities=entities,
                )
            )

    queries = [
        EvalQuery(
            domain=t.name,
            intent=t.query_intent,
            required_coverage=t.required_coverage,
        )
        for t in DOMAIN_TEMPLATES
    ]

    return GeneratedCorpus(traces=traces, queries=queries)
