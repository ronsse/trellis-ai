"""Eval scenario for the well-known promotion loop (Item 5).

Mechanism summary:

1. Seed a ParameterRegistry with the recommended schema-evolution
   thresholds, but with ``well_known_window_days=0`` so the synthetic
   data — which spans the same instant — actually surfaces.
2. Insert 1000 nodes of ``node_type="metric"`` (the canonical example
   from ``plan-self-improvement-program.md`` §5.1) across:
     - 3 distinct extractors (``worker:dbt``, ``worker:lineage``,
       ``worker:metric_layer``)
     - 4 distinct ContentTags.domain values
     - ``signal_quality="standard"`` or above.
   Emit one ``MUTATION_EXECUTED`` event per node so the analyzer can
   compute distinct-extractor counts.
3. Run :func:`analyze_well_known_candidates` against the synthetic
   graph + event log. Assert:
     - One candidate surfaces for ``"metric"`` with count=1000.
     - Suggested canonical name is ``"Metric"``.
     - ``naming_collision`` is False.
4. Run the analyzer again immediately (same evidence). Assert:
     - No candidate re-emitted (cooldown gate).
5. Advance the test clock past the cooldown window and re-run. Assert:
     - The candidate re-emerges with ``recurrence_count == 1``.

The scenario is **single-backend** and uses tmp SQLite stores from the
runner-supplied registry, falling back to a fresh in-process registry
when the runner doesn't supply one (common for direct CLI invocation).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Any

from eval.runner import Finding, ScenarioReport
from trellis.learning import (
    RECOMMENDED_SEED_VALUES,
    WellKnownCandidate,
    analyze_well_known_candidates,
)
from trellis.learning.schema_evolution import PARAM_COMPONENT_ID
from trellis.ops import ParameterRegistry
from trellis.schemas.parameters import ParameterScope, ParameterSet
from trellis.stores.base.event_log import EventType
from trellis.stores.sqlite.event_log import SQLiteEventLog
from trellis.stores.sqlite.graph import SQLiteGraphStore
from trellis.stores.sqlite.parameter import SQLiteParameterStore

if TYPE_CHECKING:
    from trellis.stores.base.event_log import EventLog
    from trellis.stores.base.graph import GraphStore
    from trellis.stores.base.parameter import ParameterStore
    from trellis.stores.registry import StoreRegistry


SCENARIO_NAME = "schema_evolution_candidate_emergence"

#: Total nodes inserted. Per ``plan-self-improvement-program.md`` §5.1
#: ("Schema-evolution candidate emergence — synthetic ingest of 1000
#: nodes with ``node_type="metric"``").
NODE_COUNT = 1000

#: Distinct extractor identifiers writing the same open-string type —
#: must be ≥ the seed threshold of 2.
EXTRACTORS: tuple[str, ...] = (
    "worker:dbt",
    "worker:lineage",
    "worker:metric_layer",
)

#: Distinct domain values across the 1000 nodes — must be ≥ 2.
DOMAINS: tuple[str, ...] = (
    "analytics",
    "finance",
    "operations",
    "marketing",
)


@dataclass(slots=True)
class _Stores:
    graph: GraphStore
    events: EventLog
    params: ParameterStore


def _resolve_stores(registry: StoreRegistry | None) -> tuple[_Stores, TemporaryDirectory]:
    """Return isolated scenario stores in a tmp dir.

    Unlike the other eval scenarios (synthetic_traces, etc.) which
    deliberately exercise the runner-supplied registry, this scenario
    inserts 1000 ``metric`` nodes + emits WELL_KNOWN_CANDIDATE events
    that should not pollute the operator's real data dir. Reasons:

    * The scenario is **deterministic** — it cares about counts that
      are exact, not approximate; any pre-existing nodes inflate the
      candidate counts and break the assertion that the suggested
      canonical_name is exactly "Metric".
    * The WELL_KNOWN_CANDIDATE events emitted as cooldown tracking
      bleed into subsequent CLI invocations of
      ``trellis analyze schema-evolution``.

    The ``registry`` argument is accepted for runner-compat but
    ignored. Future work could honor an explicit "use my registry"
    opt-in flag.
    """
    del registry  # intentionally unused; see docstring
    tmp = TemporaryDirectory()
    base = Path(tmp.name)
    return (
        _Stores(
            graph=SQLiteGraphStore(base / "graph.db"),
            events=SQLiteEventLog(base / "events.db"),
            params=SQLiteParameterStore(base / "params.db"),
        ),
        tmp,
    )


def _seed_registry(params: ParameterStore) -> ParameterRegistry:
    """Persist the threshold snapshot used by the analyzer.

    Override ``well_known_window_days`` to ``0`` so the evidence-span
    filter doesn't gate the synthetic data (which is inserted in a
    single instant). The other defaults (count=500, distinct
    extractors=2, distinct domains=2, signal_quality=standard,
    cooldown=7) are taken from the production seed.
    """
    values: dict[str, float | int | str | bool] = dict(RECOMMENDED_SEED_VALUES)
    values["well_known_window_days"] = 0
    params.put(
        ParameterSet(
            scope=ParameterScope(component_id=PARAM_COMPONENT_ID),
            values=values,
            source="eval:schema_evolution_candidate_emergence",
        )
    )
    return ParameterRegistry(params)


def _insert_synthetic_corpus(stores: _Stores) -> None:
    """Insert 1000 ``metric`` nodes + matching MUTATION_EXECUTED events."""
    for i in range(NODE_COUNT):
        extractor = EXTRACTORS[i % len(EXTRACTORS)]
        domain = DOMAINS[i % len(DOMAINS)]
        nid = stores.graph.upsert_node(
            node_id=f"metric_{i:04d}",
            node_type="metric",
            properties={
                "content_tags": {
                    "domain": [domain],
                    "signal_quality": "standard",
                },
            },
        )
        stores.events.emit(
            EventType.MUTATION_EXECUTED,
            source="mutation_executor",
            entity_id=nid,
            entity_type="metric",
            payload={
                "command_id": f"cmd_{nid}",
                "operation": "entity.create",
                "status": "SUCCESS",
                "requested_by": extractor,
            },
        )


def _assert_candidate(candidate: WellKnownCandidate) -> list[Finding]:
    """Validate the candidate matches expectations from plan §5.1."""
    findings: list[Finding] = []
    expectations: list[tuple[str, Any, Any]] = [
        ("open_string_value", "metric", candidate.open_string_value),
        ("count", NODE_COUNT, candidate.count),
        ("suggested_canonical_name", "Metric", candidate.suggested_canonical_name),
        ("candidate_kind", "entity_type", candidate.candidate_kind),
        ("naming_collision", False, candidate.naming_collision),
    ]
    for name, expected, actual in expectations:
        if expected != actual:
            findings.append(
                Finding(
                    severity="fail",
                    message=f"{name}: expected {expected!r}, got {actual!r}",
                )
            )
    if len(set(candidate.distinct_extractors)) < 2:
        findings.append(
            Finding(
                severity="fail",
                message=(
                    "distinct_extractors should have >=2 entries; got "
                    f"{candidate.distinct_extractors!r}"
                ),
            )
        )
    if len(set(candidate.distinct_domains)) < 2:
        findings.append(
            Finding(
                severity="fail",
                message=(
                    "distinct_domains should have >=2 entries; got "
                    f"{candidate.distinct_domains!r}"
                ),
            )
        )
    return findings


def run(registry: StoreRegistry | None = None) -> ScenarioReport:
    """Run the schema-evolution emergence scenario."""
    stores, tmp_holder = _resolve_stores(registry)
    findings: list[Finding] = []
    metrics: dict[str, float] = {}
    try:  # noqa: PLR1702 — single try/finally for tmp cleanup
        param_registry = _seed_registry(stores.params)
        _insert_synthetic_corpus(stores)
        metrics["nodes_inserted"] = float(NODE_COUNT)

        first_pass_now = datetime.now(tz=UTC)
        first_pass = analyze_well_known_candidates(
            graph_store=stores.graph,
            event_log=stores.events,
            registry=param_registry,
            now=first_pass_now,
        )
        metrics["first_pass_candidates"] = float(len(first_pass))
        if len(first_pass) != 1:
            findings.append(
                Finding(
                    severity="fail",
                    message=(
                        f"expected exactly 1 candidate, got {len(first_pass)}; "
                        f"surfaced={[c.open_string_value for c in first_pass]!r}"
                    ),
                )
            )
        else:
            findings.extend(_assert_candidate(first_pass[0]))
            findings.append(
                Finding(
                    severity="info",
                    message=(
                        f"Candidate emerged: {first_pass[0].open_string_value!r} "
                        f"(count={first_pass[0].count}, "
                        f"extractors={len(set(first_pass[0].distinct_extractors))}, "
                        f"domains={len(set(first_pass[0].distinct_domains))})"
                    ),
                )
            )

        # Re-run immediately — cooldown should suppress emission.
        cooldown_pass = analyze_well_known_candidates(
            graph_store=stores.graph,
            event_log=stores.events,
            registry=param_registry,
            now=first_pass_now + timedelta(minutes=5),
        )
        metrics["cooldown_pass_candidates"] = float(len(cooldown_pass))
        if cooldown_pass:
            findings.append(
                Finding(
                    severity="fail",
                    message=(
                        "cooldown should have suppressed re-emission; got "
                        f"{len(cooldown_pass)} candidate(s)"
                    ),
                )
            )
        else:
            findings.append(
                Finding(severity="info", message="Cooldown suppressed re-emission.")
            )

        # Advance past cooldown — candidate should re-emerge with
        # recurrence_count incremented.
        post_cooldown_pass = analyze_well_known_candidates(
            graph_store=stores.graph,
            event_log=stores.events,
            registry=param_registry,
            now=first_pass_now + timedelta(days=8),
        )
        metrics["post_cooldown_candidates"] = float(len(post_cooldown_pass))
        if len(post_cooldown_pass) != 1:
            findings.append(
                Finding(
                    severity="fail",
                    message=(
                        "post-cooldown should re-emit exactly 1 candidate; got "
                        f"{len(post_cooldown_pass)}"
                    ),
                )
            )
        else:
            recurrence = post_cooldown_pass[0].recurrence_count
            metrics["recurrence_count"] = float(recurrence)
            if recurrence < 1:
                findings.append(
                    Finding(
                        severity="fail",
                        message=(
                            "post-cooldown candidate should have "
                            f"recurrence_count >= 1; got {recurrence}"
                        ),
                    )
                )
            else:
                findings.append(
                    Finding(
                        severity="info",
                        message=(
                            "Post-cooldown re-emission fired; "
                            f"recurrence_count={recurrence}"
                        ),
                    )
                )
    finally:
        stores.graph.close()
        stores.events.close()
        stores.params.close()
        tmp_holder.cleanup()

    has_fail = any(f.severity == "fail" for f in findings)
    return ScenarioReport(
        name=SCENARIO_NAME,
        status="fail" if has_fail else "pass",
        metrics=metrics,
        findings=findings,
        decision=(
            "Item 5 well-known promotion loop closes end-to-end on synthetic "
            "1000-node corpus: surfaces, respects cooldown, and re-emits after "
            "cooldown elapses with recurrence_count incremented."
        ),
    )
