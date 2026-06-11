"""Seed helpers — populate stores with the corpus the loop will curate.

Skeleton only. Each helper raises :class:`NotImplementedError` naming
the F-phase that fills it in. The signatures are stable so scenario.py
can wire them up without further refactoring.

The three helpers carve the corpus into the slices the inner loop
operates on:

- :func:`seed_under_populated_nodes` — graph nodes with sparse
  evidence/observation coverage. The curator skill's job is to enrich
  these.
- :func:`seed_documents_for_nodes` — the document store rows the
  curator reads to propose enrichments.
- :func:`seed_baseline_corpus` — the broader trace/document corpus
  the retrieval lift metric measures against (so we can tell whether
  enrichment improved pack quality on queries unrelated to the
  enrichment work itself).
"""

from __future__ import annotations

from typing import Any

import structlog

from trellis.stores.registry import StoreRegistry

logger = structlog.get_logger(__name__)


def seed_under_populated_nodes(
    registry: StoreRegistry,
    *,
    seed: int,
    node_count: int,
) -> list[str]:
    """Upsert ``node_count`` under-populated graph nodes; return their ids.

    F1 fills this in (graph-skill harness — defines what an
    "under-populated" node looks like and which fields the curator
    needs to enrich). Stub: raises :class:`NotImplementedError`.
    """
    msg = "F1 (graph-skill harness) fills this in"
    raise NotImplementedError(msg)


def seed_documents_for_nodes(
    registry: StoreRegistry,
    node_ids: list[str],
    *,
    seed: int,
    docs_per_node: int,
) -> int:
    """Populate the document store with source material for ``node_ids``.

    F2 fills this in (curator skill — declares the document shape it
    expects to read). Returns the number of documents written. Stub:
    raises :class:`NotImplementedError`.
    """
    msg = "F2 (curator skill) fills this in"
    raise NotImplementedError(msg)


def seed_baseline_corpus(
    registry: StoreRegistry,
    *,
    seed: int,
    traces_per_domain: int,
    entities_per_trace: int,
) -> dict[str, Any]:
    """Seed the baseline trace + document corpus used by the lift metric.

    F6 fills this in (this scenario — wraps
    :func:`eval.generators.trace_generator.generate_corpus` with the
    knobs ``retrieval_lift_curve`` needs). Returns a manifest dict
    summarising what was seeded. Stub: raises
    :class:`NotImplementedError`.
    """
    msg = "F6 (this scenario) fills this in"
    raise NotImplementedError(msg)
