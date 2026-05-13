"""Code-authoring workers — proposal generation for the self-improvement loop.

Item 7 of the self-improvement program — the "capstone" that closes the
loop between operational telemetry (extraction failures, well-known
candidates) and a human-reviewable proposal for code changes.

This Phase-0 surface ships the read-only half:

* :func:`cluster_failures` groups ``EXTRACTION_FAILED`` events by
  ``(source_file, failure_class)`` over a rolling window.
* :class:`Cluster` is the dataclass returned by the clusterer.
* :class:`ProposalGenerator` consumes clusters + ``WELL_KNOWN_CANDIDATE``
  events, renders markdown, and emits ``PROPOSAL_DRAFTED`` /
  ``PROPOSAL_UPDATED`` events idempotently keyed on the cluster
  signature.
* :class:`Proposal` is the dataclass returned by the generator.

The write-side half (sandboxed Claude Code spawn, GitHub PR creation,
budget ledger) is deliberately out of scope for this PR — see
``docs/design/plan-coding-agent-loop.md`` Phases 2+3 (a separate release
cycle).
"""

from __future__ import annotations

from trellis_workers.code_authoring.clustering import (
    Cluster,
    cluster_failures,
    compute_cluster_signature,
)

__all__ = [
    "Cluster",
    "cluster_failures",
    "compute_cluster_signature",
]
