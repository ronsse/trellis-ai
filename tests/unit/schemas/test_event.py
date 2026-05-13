"""Event schema tests — verify newly-added EventType members exist with
their canonical wire values.

EventType lives at ``trellis.stores.base.event_log`` (the EventLog ABC
co-locates the enum with the table it discriminates over), but the
schema-level contract — "what string lands on the wire / on disk" — is
schema-tier concern, so the tests live here.
"""

from __future__ import annotations

from trellis.stores.base.event_log import EventType


def test_well_known_candidate_event_type_registered() -> None:
    """The well-known promotion loop's event type is on the enum.

    Phase 0 of ``plan-well-known-promotion-loop.md``. The wire value
    follows the dotted-namespace convention used by every other member
    (``feedback.recorded``, ``extraction.rejected``, ...). Hard-coding
    the literal here rather than asserting against the enum value
    catches accidental renames that would corrupt on-disk event logs.
    """
    assert EventType.WELL_KNOWN_CANDIDATE.value == "well_known.candidate"
    # Round-trip the string through StrEnum so consumers reading raw
    # payloads (e.g., ad-hoc SQL `WHERE event_type = 'well_known.candidate'`)
    # land on the same member.
    assert EventType("well_known.candidate") is EventType.WELL_KNOWN_CANDIDATE


def test_proposal_event_types_registered() -> None:
    """The coding-agent loop's proposal-lifecycle event types are on the enum.

    Phase 0 of ``plan-coding-agent-loop.md`` (Item 7 of the
    self-improvement program). Hard-coding the literals catches
    accidental renames that would corrupt on-disk event logs read by the
    proposal-generator's idempotency check.
    """
    assert EventType.PROPOSAL_DRAFTED.value == "proposal.drafted"
    assert EventType.PROPOSAL_UPDATED.value == "proposal.updated"
    # Round-trip both wire strings — the generator joins on these to
    # find prior emissions when checking whether to skip or emit
    # ``PROPOSAL_UPDATED``.
    assert EventType("proposal.drafted") is EventType.PROPOSAL_DRAFTED
    assert EventType("proposal.updated") is EventType.PROPOSAL_UPDATED
