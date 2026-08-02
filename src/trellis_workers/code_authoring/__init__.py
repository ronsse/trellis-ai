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

Cohort 2 lands guardrails-first. Present:

* :mod:`~trellis_workers.code_authoring.safety` — the diff-level
  allowlist and secret scrub (amendment §2.5 / §2.6). Pure decisions,
  no I/O.
* :mod:`~trellis_workers.code_authoring.issue_selector` — the second
  proposal source, reading the curated GitHub issue queue instead of
  operational telemetry.

Still absent, and still gated: the sandboxed Claude Code spawn, the
``gh`` PR proposer, and the budget ledger. Controls before capability is
deliberate — there is no code path here that can write to a repo, so
these modules cannot author anything on their own. See
``docs/design/adr-coding-agent-loop-cohort2-amendment.md``.
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
from trellis_workers.code_authoring.issue_selector import (
    DISQUALIFYING_LABELS,
    REQUIRED_LABELS,
    IssueCandidate,
    evaluate_issue,
    parse_files_allowed,
    select_candidate,
)
from trellis_workers.code_authoring.proposal import (
    MARKDOWN_PREVIEW_CHARS,
    MAX_SAMPLE_EVENT_IDS_IN_MARKDOWN,
    Proposal,
    compute_proposal_id,
    render_markdown,
)
from trellis_workers.code_authoring.safety import (
    HARD_EXCLUDED_GLOBS,
    SECRET_PATTERNS,
    AllowlistError,
    AllowlistViolation,
    SecretMatch,
    is_hard_excluded,
    scan_secrets,
    validate_allowlist,
    verify_diff_allowlist,
)

__all__ = [
    "DEFAULT_WINDOW",
    "DISQUALIFYING_LABELS",
    "HARD_EXCLUDED_GLOBS",
    "MARKDOWN_PREVIEW_CHARS",
    "MAX_SAMPLE_EVENT_IDS_IN_MARKDOWN",
    "PROPOSAL_GENERATOR_AGENT_ID",
    "PROPOSAL_GENERATOR_ANALYZER_NAME",
    "REQUIRED_LABELS",
    "SECRET_PATTERNS",
    "AllowlistError",
    "AllowlistViolation",
    "Cluster",
    "IssueCandidate",
    "Proposal",
    "ProposalGenerator",
    "SecretMatch",
    "cluster_failures",
    "compute_cluster_signature",
    "compute_proposal_id",
    "evaluate_issue",
    "is_hard_excluded",
    "parse_files_allowed",
    "render_markdown",
    "scan_secrets",
    "select_candidate",
    "validate_allowlist",
    "verify_diff_allowlist",
]
