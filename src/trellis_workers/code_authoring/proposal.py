"""``Proposal`` dataclass + markdown renderer for the coding-agent loop.

The :class:`Proposal` is the human-reviewable artifact that the
:class:`~trellis_workers.code_authoring.generator.ProposalGenerator`
emits per surfaced :class:`~trellis_workers.code_authoring.clustering.Cluster`
or :data:`trellis.stores.base.event_log.EventType.WELL_KNOWN_CANDIDATE`
event. It carries:

* :attr:`Proposal.proposal_id` — stable, hash-derived ID. Same input →
  same ID, so the generator's idempotency check is a one-event log
  lookup.
* :attr:`Proposal.cluster_signature` — the originating cluster's
  signature. Joins the proposal back to the cluster identity without
  carrying the full cluster payload through the event log.
* :attr:`Proposal.markdown` — the human-readable body. Operators read
  this to decide whether to invoke the (cohort-2) Claude Code author.
* :attr:`Proposal.source_event_ids` — tuple of every contributing
  event_id. The meta-Activity wrapper writes one ``wasInformedBy`` edge
  per ID so provenance is preserved.

This module intentionally does **not** depend on stores, the event log,
or the meta recorder. It owns the proposal *shape* and the markdown
*template*; the generator owns the I/O.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from trellis_workers.code_authoring.clustering import Cluster


#: Maximum number of sample event IDs rendered into the markdown body —
#: enough for an operator to spot-check provenance, few enough that a
#: 500-failure cluster doesn't bloat the artefact. Full
#: ``source_event_ids`` is always carried on the dataclass for
#: programmatic consumers.
MAX_SAMPLE_EVENT_IDS_IN_MARKDOWN: int = 5

#: Hard cap on the ``markdown_preview`` slice attached to the
#: ``PROPOSAL_DRAFTED`` / ``PROPOSAL_UPDATED`` event payloads. Keeps the
#: EventLog row size bounded — the full markdown is always available on
#: the in-memory :class:`Proposal` instance and (in cohort G2) the
#: ``agent-proposals/`` filesystem directory.
MARKDOWN_PREVIEW_CHARS: int = 500


@dataclass(frozen=True, slots=True)
class Proposal:
    """A drafted proposal for a single signal cluster.

    Frozen / slotted so the generator can hash by :attr:`proposal_id`
    when deduplicating across runs. Field order matches the natural
    read order — identity first, evidence next, rendered output last.

    Attributes:
        proposal_id: SHA-256 hex digest of the cluster signature. Same
            cluster across runs → same ID. The generator uses this to
            short-circuit when a ``PROPOSAL_DRAFTED`` event already
            exists for the same ID.
        cluster_signature: The originating cluster's signature. Carried
            on the event payload so analysts can rejoin proposals to
            clusters without going through the full ID hash.
        markdown: Rendered proposal body — see :func:`render_markdown`.
        generated_at: Timestamp of the run that produced this proposal.
        source_event_ids: Every event_id that contributed to the
            cluster, in insertion order. The generator writes one
            ``wasInformedBy`` provenance edge per ID.
    """

    proposal_id: str
    cluster_signature: str
    markdown: str
    generated_at: datetime
    source_event_ids: tuple[str, ...] = field(default_factory=tuple)

    def markdown_preview(
        self,
        max_chars: int = MARKDOWN_PREVIEW_CHARS,
    ) -> str:
        """Return a length-bounded slice of :attr:`markdown` for event payloads.

        The full markdown lives on the dataclass; this is for the
        ``PROPOSAL_DRAFTED`` / ``PROPOSAL_UPDATED`` event payloads so the
        EventLog row size stays bounded.
        """
        return self.markdown[:max_chars]


def compute_proposal_id(cluster_signature: str) -> str:
    """Return the stable :attr:`Proposal.proposal_id` for a cluster signature.

    SHA-256 hex digest of the signature. Re-hashing the already-hashed
    cluster signature is intentional — it gives the proposal ID a
    distinct namespace from the cluster signature (so the two never
    collide in any future event-log query) while preserving determinism.
    """
    return hashlib.sha256(cluster_signature.encode()).hexdigest()


def render_markdown(
    cluster: Cluster,
    *,
    sample_event_id_cap: int = MAX_SAMPLE_EVENT_IDS_IN_MARKDOWN,
) -> str:
    """Render the proposal markdown for one cluster.

    The template targets a human reviewer (operator or maintainer) who
    will decide whether the surfaced pattern justifies a code change. It
    deliberately stays prescriptive about *what to look at*, not *what
    to do* — the latter is the Claude Code author's job in cohort 2.

    Sections (in order):

    1. ``# Proposal: address <failure_class> in <source_file>`` — title.
    2. ``## Cluster summary`` — count, time window, signature.
    3. ``## Recommended action`` — failure-class-specific hint.
    4. ``## Sample event IDs`` — first ``sample_event_id_cap`` IDs.
    5. ``## Provenance`` — generator + cluster identity for traceability.

    Args:
        cluster: The clustered failures to surface.
        sample_event_id_cap: Cap on the number of event IDs rendered
            into the "Sample event IDs" section. The full list is
            always carried on :attr:`Proposal.source_event_ids`.
    """
    lines: list[str] = []
    lines.append(
        f"# Proposal: address {cluster.failure_class} in {cluster.source_file}"
    )
    lines.append("")
    lines.append("## Cluster summary")
    lines.append("")
    lines.append(f"- **Source file:** `{cluster.source_file}`")
    lines.append(f"- **Failure class:** `{cluster.failure_class}`")
    lines.append(f"- **Failure count:** {cluster.count}")
    if cluster.earliest_at is not None and cluster.latest_at is not None:
        lines.append(
            f"- **Time window:** {cluster.earliest_at.isoformat()} → "
            f"{cluster.latest_at.isoformat()}"
        )
    lines.append(f"- **Cluster signature:** `{cluster.signature}`")
    lines.append("")
    lines.append("## Recommended action")
    lines.append("")
    lines.append(_recommended_action(cluster))
    lines.append("")
    lines.append("## Sample event IDs")
    lines.append("")
    sample = cluster.events[:sample_event_id_cap]
    if not sample:
        lines.append("_No sample event IDs available._")
    else:
        lines.extend(f"- `{event_id}`" for event_id in sample)
        remainder = len(cluster.events) - len(sample)
        if remainder > 0:
            lines.append(f"- _… and {remainder} more (see provenance edges)._")
    lines.append("")
    lines.append("## Provenance")
    lines.append("")
    lines.append(
        "- Generated by `trellis_workers.code_authoring.ProposalGenerator` "
        "(Item 7 Phase 0)."
    )
    lines.append(
        "- Re-running the generator over the same window will surface the "
        "same proposal_id; no duplicate `PROPOSAL_DRAFTED` event is emitted."
    )
    lines.append("")
    return "\n".join(lines)


# Mapping of well-known failure_kind values to a one-line recommended
# action. Conservative phrasing — we suggest the smallest change that
# would surface the root cause, not the fix itself. The author cohort
# (cohort 2) reads the full markdown and proposes the actual edit.
_FAILURE_KIND_ACTIONS: dict[str, str] = {
    "parse_error": (
        "The extractor is emitting payloads that fail JSON parsing. Narrow "
        "the catch block at the parse site to the specific decoder error and "
        "log the offending input shape before re-raising — silent fallbacks "
        "here are the defect Item 4 was meant to retire."
    ),
    "validation_error": (
        "An extraction validator is flagging malformed drafts. Audit the "
        "validator rule set vs. the extractor's output schema — drift "
        "between the two will keep producing this signal until they "
        "realign."
    ),
    "policy_violation": (
        "Extracted drafts are tripping a policy gate. Confirm the policy "
        "rule still matches the current ingestion contract; if it does, the "
        "extractor needs an upstream tag or redaction pass."
    ),
    "low_confidence": (
        "The extractor's reported confidence is below the gate threshold. "
        "Investigate whether the input has shifted (drift) or the threshold "
        "needs a recalibration via the ParameterRegistry."
    ),
    "tier_fallback": (
        "An extractor is falling back to a lower-priority tier. Inspect the "
        "dispatcher logs for the original failure and decide whether to "
        "graduate the source (LLM → hybrid → deterministic) or retire the "
        "current rule set."
    ),
    "model_error": (
        "The LLM provider returned an error at the extraction call. Check "
        "the provider's status, the request shape, and any timeout/retry "
        "policy on the LLM client."
    ),
    "budget_exhausted": (
        "The extraction budget is exhausted before the work is complete. "
        "Confirm whether to raise the budget or tighten the input set fed "
        "into the LLM-backed extractor."
    ),
}


def _recommended_action(cluster: Cluster) -> str:
    """Return a one-paragraph hint for the cluster's failure class.

    Falls back to a generic message for unknown failure kinds — the
    important signal in that case is "we recognise the cluster but the
    failure_kind is not on our known list", which is itself worth a
    reviewer's attention.
    """
    known = _FAILURE_KIND_ACTIONS.get(cluster.failure_class)
    if known is not None:
        return known
    return (
        f"Failure kind `{cluster.failure_class}` is not in the recommended-action "
        f"table. Inspect the originating extractor at `{cluster.source_file}` and "
        "extend `_FAILURE_KIND_ACTIONS` if this pattern is worth surfacing "
        "again."
    )


__all__ = [
    "MARKDOWN_PREVIEW_CHARS",
    "MAX_SAMPLE_EVENT_IDS_IN_MARKDOWN",
    "Proposal",
    "compute_proposal_id",
    "render_markdown",
]
