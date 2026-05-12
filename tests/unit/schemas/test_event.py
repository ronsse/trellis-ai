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
