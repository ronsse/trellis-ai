"""Tests for :mod:`trellis_workers.code_authoring.proposal`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from trellis_workers.code_authoring.clustering import (
    Cluster,
    compute_cluster_signature,
)
from trellis_workers.code_authoring.proposal import (
    MARKDOWN_PREVIEW_CHARS,
    MAX_SAMPLE_EVENT_IDS_IN_MARKDOWN,
    Proposal,
    compute_proposal_id,
    render_markdown,
)


def _make_cluster(
    *,
    source_file: str = "src/trellis/extract/llm.py",
    failure_class: str = "parse_error",
    event_count: int = 3,
) -> Cluster:
    signature = compute_cluster_signature(source_file, failure_class)
    base = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)
    events = tuple(f"evt-{i:03d}" for i in range(event_count))
    return Cluster(
        signature=signature,
        source_file=source_file,
        failure_class=failure_class,
        events=events,
        earliest_at=base,
        latest_at=base + timedelta(minutes=event_count),
        count=event_count,
    )


# ---------------------------------------------------------------------------
# compute_proposal_id — determinism + uniqueness
# ---------------------------------------------------------------------------


def test_proposal_id_is_stable_for_same_signature() -> None:
    """Same cluster signature → same proposal_id across calls."""
    sig = compute_cluster_signature("src/foo.py", "parse_error")
    assert compute_proposal_id(sig) == compute_proposal_id(sig)
    # Hex digest length sanity check.
    assert len(compute_proposal_id(sig)) == 64


def test_proposal_id_differs_for_different_signatures() -> None:
    """Different cluster signatures → different proposal_ids."""
    sig_a = compute_cluster_signature("src/a.py", "parse_error")
    sig_b = compute_cluster_signature("src/b.py", "parse_error")
    assert compute_proposal_id(sig_a) != compute_proposal_id(sig_b)


def test_proposal_id_differs_from_its_input_signature() -> None:
    """The proposal_id sits in a different namespace from the cluster sig.

    Phase 0 chose to re-hash the cluster signature so the two IDs never
    collide in any future event-log query that filters on either field.
    """
    sig = compute_cluster_signature("src/foo.py", "parse_error")
    assert compute_proposal_id(sig) != sig


# ---------------------------------------------------------------------------
# Proposal dataclass shape
# ---------------------------------------------------------------------------


def test_proposal_dataclass_fields() -> None:
    """Frozen-dataclass round-trip — fields are immutable + carry expected types."""
    cluster = _make_cluster()
    proposal = Proposal(
        proposal_id=compute_proposal_id(cluster.signature),
        cluster_signature=cluster.signature,
        markdown=render_markdown(cluster),
        generated_at=datetime(2026, 5, 13, 9, 0, 0, tzinfo=UTC),
        source_event_ids=cluster.events,
    )
    assert proposal.proposal_id == compute_proposal_id(cluster.signature)
    assert proposal.cluster_signature == cluster.signature
    assert proposal.markdown.startswith("# Proposal:")
    assert proposal.generated_at.tzinfo is UTC
    assert proposal.source_event_ids == cluster.events

    # Frozen — mutation raises.
    with pytest.raises(AttributeError):
        proposal.proposal_id = "different"  # type: ignore[misc]


def test_markdown_preview_caps_at_max_chars() -> None:
    """The event-payload preview is bounded by ``MARKDOWN_PREVIEW_CHARS``."""
    huge = "x" * (MARKDOWN_PREVIEW_CHARS * 3)
    proposal = Proposal(
        proposal_id="pid",
        cluster_signature="sig",
        markdown=huge,
        generated_at=datetime(2026, 5, 13, tzinfo=UTC),
        source_event_ids=(),
    )
    assert len(proposal.markdown_preview()) == MARKDOWN_PREVIEW_CHARS
    # Custom cap takes precedence.
    assert len(proposal.markdown_preview(max_chars=10)) == 10


# ---------------------------------------------------------------------------
# render_markdown — template structure
# ---------------------------------------------------------------------------


def test_render_markdown_contains_all_required_sections() -> None:
    """The template renders every named section in the documented order."""
    cluster = _make_cluster()
    md = render_markdown(cluster)
    # Title first.
    assert md.startswith(
        f"# Proposal: address {cluster.failure_class} in {cluster.source_file}"
    )
    # Required headings in template order.
    title_idx = md.index("# Proposal:")
    summary_idx = md.index("## Cluster summary")
    action_idx = md.index("## Recommended action")
    samples_idx = md.index("## Sample event IDs")
    provenance_idx = md.index("## Provenance")
    assert title_idx < summary_idx < action_idx < samples_idx < provenance_idx


def test_render_markdown_carries_cluster_identity_fields() -> None:
    """Source file, failure class, count, signature all surface in the body."""
    cluster = _make_cluster(
        source_file="src/trellis/extract/llm.py",
        failure_class="parse_error",
        event_count=7,
    )
    md = render_markdown(cluster)
    assert "src/trellis/extract/llm.py" in md
    assert "parse_error" in md
    assert "**Failure count:** 7" in md
    assert cluster.signature in md


def test_render_markdown_renders_sample_event_ids_bounded_by_cap() -> None:
    """Sample event IDs render at most ``MAX_SAMPLE_EVENT_IDS_IN_MARKDOWN``."""
    cluster = _make_cluster(event_count=MAX_SAMPLE_EVENT_IDS_IN_MARKDOWN + 3)
    md = render_markdown(cluster)
    # All event IDs that fit in the cap should appear.
    for event_id in cluster.events[:MAX_SAMPLE_EVENT_IDS_IN_MARKDOWN]:
        assert event_id in md
    # The overflow notice mentions the remaining count.
    assert "and 3 more" in md


def test_render_markdown_emits_known_failure_class_action() -> None:
    """A known failure_kind gets a specific recommended-action paragraph."""
    cluster = _make_cluster(failure_class="parse_error")
    md = render_markdown(cluster)
    # The parse_error action is the only one that mentions JSON parsing.
    assert "JSON parsing" in md


def test_render_markdown_falls_back_for_unknown_failure_class() -> None:
    """An unknown failure_kind triggers the fallback recommended-action body."""
    cluster = _make_cluster(failure_class="brand_new_failure_kind")
    md = render_markdown(cluster)
    # Fallback paragraph identifies that the failure_kind is not on the
    # recommended-action table.
    assert "not in the recommended-action table" in md


def test_render_markdown_with_zero_events_emits_no_sample_placeholder() -> None:
    """A degenerate empty cluster still produces a valid markdown body."""
    cluster = Cluster(
        signature="empty-sig",
        source_file="src/foo.py",
        failure_class="parse_error",
        events=(),
        earliest_at=None,
        latest_at=None,
        count=0,
    )
    md = render_markdown(cluster)
    assert "No sample event IDs available" in md
    # Time window is conditionally rendered — when both endpoints are
    # missing the section header is still present but the bullet is
    # omitted.
    assert "## Cluster summary" in md
    assert "Time window:" not in md
