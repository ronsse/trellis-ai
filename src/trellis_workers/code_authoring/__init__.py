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
from trellis_workers.code_authoring.generator import (
    DEFAULT_WINDOW,
    PROPOSAL_GENERATOR_AGENT_ID,
    PROPOSAL_GENERATOR_ANALYZER_NAME,
    ProposalGenerator,
)
from trellis_workers.code_authoring.proposal import (
    MARKDOWN_PREVIEW_CHARS,
    MAX_SAMPLE_EVENT_IDS_IN_MARKDOWN,
    Proposal,
    compute_proposal_id,
    render_markdown,
)

__all__ = [
    "DEFAULT_WINDOW",
    "MARKDOWN_PREVIEW_CHARS",
    "MAX_SAMPLE_EVENT_IDS_IN_MARKDOWN",
    "PROPOSAL_GENERATOR_AGENT_ID",
    "PROPOSAL_GENERATOR_ANALYZER_NAME",
    "Cluster",
    "Proposal",
    "ProposalGenerator",
    "cluster_failures",
    "compute_cluster_signature",
    "compute_proposal_id",
    "render_markdown",
]
