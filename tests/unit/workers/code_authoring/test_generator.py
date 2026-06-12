"""Tests for :mod:`trellis_workers.code_authoring.generator`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from trellis.meta import META_TRACES_ENV_VAR
from trellis.schemas import well_known as wk
from trellis.stores.base.event_log import Event, EventType
from trellis.stores.registry import StoreRegistry
from trellis_workers.code_authoring import (
    PROPOSAL_GENERATOR_AGENT_ID,
    PROPOSAL_GENERATOR_ANALYZER_NAME,
    ProposalGenerator,
    compute_cluster_signature,
    compute_proposal_id,
)


@pytest.fixture(autouse=True)
def _clear_meta_traces_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test starts with the meta-traces env var unset (default = on)."""
    monkeypatch.delenv(META_TRACES_ENV_VAR, raising=False)


@pytest.fixture
def registry(tmp_path: Path) -> StoreRegistry:
    """Fresh SQLite-backed registry per test."""
    stores_dir = tmp_path / "stores"
    stores_dir.mkdir()
    return StoreRegistry(stores_dir=stores_dir)


def _seed_failure_event(
    registry: StoreRegistry,
    *,
    source_file: str = "src/trellis/extract/llm.py",
    failure_class: str = "parse_error",
    occurred_at: datetime | None = None,
    extractor_id: str = "LLMExtractor",
) -> Event:
    """Append a synthetic ``EXTRACTION_FAILED`` event and return it."""
    occurred = occurred_at or datetime.now(tz=UTC)
    event = Event(
        event_type=EventType.EXTRACTION_FAILED,
        source="extraction_failure_helper",
        occurred_at=occurred,
        recorded_at=occurred,
        payload={
            "extractor_id": extractor_id,
            "extractor_tier": "llm",
            "failure_kind": failure_class,
            "source_hint": source_file,
            "prompt_hash": "h" * 16,
            "error_class": "ValueError",
            "error_excerpt": "bad json",
        },
    )
    registry.operational.event_log.append(event)
    return event


def _seed_well_known_candidate(
    registry: StoreRegistry,
    *,
    candidate_id: str = "wkc_nod_abcdef1234567890",
    candidate_kind: str = "node_type",
    open_string_value: str = "MyType",
) -> Event:
    """Append a synthetic ``WELL_KNOWN_CANDIDATE`` event."""
    occurred = datetime.now(tz=UTC)
    event = Event(
        event_type=EventType.WELL_KNOWN_CANDIDATE,
        source="schema_evolution",
        occurred_at=occurred,
        recorded_at=occurred,
        payload={
            "candidate_id": candidate_id,
            "candidate_kind": candidate_kind,
            "open_string_value": open_string_value,
            "count": 510,
        },
    )
    registry.operational.event_log.append(event)
    return event


def _drafted_events(registry: StoreRegistry) -> list[Event]:
    return registry.operational.event_log.get_events(
        event_type=EventType.PROPOSAL_DRAFTED,
        limit=100,
    )


def _updated_events(registry: StoreRegistry) -> list[Event]:
    return registry.operational.event_log.get_events(
        event_type=EventType.PROPOSAL_UPDATED,
        limit=100,
    )


# ---------------------------------------------------------------------------
# Empty / no-op paths
# ---------------------------------------------------------------------------


def test_empty_window_returns_empty_list_and_emits_no_events(
    registry: StoreRegistry,
) -> None:
    """No signal events → no proposals, no events, no Activity."""
    generator = ProposalGenerator(registry)
    proposals = generator.run()
    assert proposals == []
    assert _drafted_events(registry) == []
    assert _updated_events(registry) == []
    # The empty-run short-circuit avoids materialising an Activity.
    activities = registry.knowledge.graph_store.query(node_type=wk.ACTIVITY, limit=10)
    assert activities == []


# ---------------------------------------------------------------------------
# Happy paths — round trips
# ---------------------------------------------------------------------------


def test_failures_produce_one_proposal_per_cluster(
    registry: StoreRegistry,
) -> None:
    """N failures across two clusters → two proposals + two DRAFTED events."""
    _seed_failure_event(registry, source_file="src/a.py")
    _seed_failure_event(registry, source_file="src/a.py")
    _seed_failure_event(registry, source_file="src/b.py")

    proposals = ProposalGenerator(registry).run()
    assert len(proposals) == 2

    drafted = _drafted_events(registry)
    assert len(drafted) == 2
    drafted_ids = {e.payload["proposal_id"] for e in drafted}
    assert drafted_ids == {p.proposal_id for p in proposals}

    # No UPDATED events on the first run.
    assert _updated_events(registry) == []


def test_proposal_id_matches_hash_of_cluster_signature(
    registry: StoreRegistry,
) -> None:
    """Each proposal's ID is the deterministic hash of its cluster signature."""
    _seed_failure_event(
        registry,
        source_file="src/trellis/extract/llm.py",
        failure_class="parse_error",
    )
    proposals = ProposalGenerator(registry).run()
    assert len(proposals) == 1
    expected_sig = compute_cluster_signature(
        "src/trellis/extract/llm.py", "parse_error"
    )
    assert proposals[0].cluster_signature == expected_sig
    assert proposals[0].proposal_id == compute_proposal_id(expected_sig)


def test_proposal_markdown_carries_expected_sections(
    registry: StoreRegistry,
) -> None:
    """Markdown round-trip — source_file, failure_class, count, IDs all present."""
    e1 = _seed_failure_event(registry, source_file="src/trellis/extract/llm.py")
    e2 = _seed_failure_event(registry, source_file="src/trellis/extract/llm.py")
    proposals = ProposalGenerator(registry).run()
    md = proposals[0].markdown
    assert "src/trellis/extract/llm.py" in md
    assert "parse_error" in md
    assert "**Failure count:** 2" in md
    # The sample IDs section renders our seeded events.
    assert e1.event_id in md
    assert e2.event_id in md


def test_event_payload_carries_markdown_preview_and_count(
    registry: StoreRegistry,
) -> None:
    """``PROPOSAL_DRAFTED`` payload contains markdown_preview + source_event_count."""
    _seed_failure_event(registry)
    _seed_failure_event(registry)
    _seed_failure_event(registry)
    ProposalGenerator(registry).run()
    drafted = _drafted_events(registry)
    assert len(drafted) == 1
    payload = drafted[0].payload
    assert payload["source_event_count"] == 3
    assert "markdown_preview" in payload
    assert payload["markdown_preview"].startswith("# Proposal:")
    assert payload["cluster_signature"] == compute_cluster_signature(
        "src/trellis/extract/llm.py", "parse_error"
    )


def test_well_known_candidate_event_produces_proposal(
    registry: StoreRegistry,
) -> None:
    """Each ``WELL_KNOWN_CANDIDATE`` event becomes its own proposal."""
    _seed_well_known_candidate(
        registry, candidate_id="wkc_nod_abc", candidate_kind="node_type"
    )
    _seed_well_known_candidate(
        registry, candidate_id="wkc_edg_xyz", candidate_kind="edge_kind"
    )
    proposals = ProposalGenerator(registry).run()
    assert len(proposals) == 2
    # Both proposals are distinct (different candidate IDs).
    assert {p.proposal_id for p in proposals} == {
        compute_proposal_id(compute_cluster_signature("wkc_nod_abc", "node_type")),
        compute_proposal_id(compute_cluster_signature("wkc_edg_xyz", "edge_kind")),
    }


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_second_run_emits_no_new_drafted_events(
    registry: StoreRegistry,
) -> None:
    """Idempotency contract: re-running over the same window does not double-draft."""
    _seed_failure_event(registry)
    _seed_failure_event(registry)

    first_run = ProposalGenerator(registry).run()
    assert len(first_run) == 1
    assert len(_drafted_events(registry)) == 1

    second_run = ProposalGenerator(registry).run()
    assert len(second_run) == 1
    # Stable proposal_id across runs.
    assert second_run[0].proposal_id == first_run[0].proposal_id
    # No new DRAFTED event — the second run hit the idempotency check.
    assert len(_drafted_events(registry)) == 1
    # An UPDATED event landed instead.
    updated = _updated_events(registry)
    assert len(updated) == 1
    assert updated[0].payload["proposal_id"] == first_run[0].proposal_id


def test_proposal_id_stable_across_independent_generator_instances(
    registry: StoreRegistry,
) -> None:
    """A fresh generator instance computes the same proposal_id as the prior run."""
    _seed_failure_event(registry, source_file="src/foo.py")
    first = ProposalGenerator(registry).run()
    # Different instance — same registry / same events → same ID.
    second = ProposalGenerator(registry, window=timedelta(hours=24)).run()
    assert first[0].proposal_id == second[0].proposal_id


# ---------------------------------------------------------------------------
# Window filtering on the generator path
# ---------------------------------------------------------------------------


def test_events_outside_window_are_excluded(registry: StoreRegistry) -> None:
    """An old failure outside the window does not produce a proposal."""
    long_ago = datetime.now(tz=UTC) - timedelta(days=30)
    _seed_failure_event(registry, occurred_at=long_ago)
    proposals = ProposalGenerator(registry, window=timedelta(hours=1)).run()
    assert proposals == []
    assert _drafted_events(registry) == []


# ---------------------------------------------------------------------------
# Meta-Activity wiring
# ---------------------------------------------------------------------------


def test_run_records_meta_activity_under_proposal_generator_agent(
    registry: StoreRegistry,
) -> None:
    """The run wraps itself in ``record_meta_analysis`` — Activity lands."""
    _seed_failure_event(registry)
    ProposalGenerator(registry).run()

    activities = registry.knowledge.graph_store.query(
        node_type=wk.ACTIVITY,
        properties={
            "agent_id": PROPOSAL_GENERATOR_AGENT_ID,
            "analyzer_name": PROPOSAL_GENERATOR_ANALYZER_NAME,
        },
        limit=10,
    )
    assert len(activities) == 1
    props = activities[0]["properties"]
    assert props["agent_id"] == PROPOSAL_GENERATOR_AGENT_ID
    assert props["analyzer_name"] == PROPOSAL_GENERATOR_ANALYZER_NAME


def test_run_writes_was_informed_by_edges_per_consumed_event(
    registry: StoreRegistry,
) -> None:
    """One ``wasInformedBy`` edge lands per consumed failure event."""
    e1 = _seed_failure_event(registry)
    e2 = _seed_failure_event(registry)
    ProposalGenerator(registry).run()

    activities = registry.knowledge.graph_store.query(
        node_type=wk.ACTIVITY,
        properties={
            "agent_id": PROPOSAL_GENERATOR_AGENT_ID,
            "analyzer_name": PROPOSAL_GENERATOR_ANALYZER_NAME,
        },
        limit=10,
    )
    activity_id = activities[0]["node_id"]
    edges = registry.knowledge.graph_store.get_edges(activity_id, direction="outgoing")
    informed = [e for e in edges if e["edge_type"] == wk.WAS_INFORMED_BY]
    targets = {e["target_id"] for e in informed}
    assert targets == {e1.event_id, e2.event_id}


def test_meta_traces_off_short_circuits_activity_writes(
    registry: StoreRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``TRELLIS_META_TRACES=off`` keeps the generator side-effect-free in the graph."""
    monkeypatch.setenv(META_TRACES_ENV_VAR, "off")
    _seed_failure_event(registry)
    proposals = ProposalGenerator(registry).run()
    # Proposal is still produced (the event-log side is authoritative).
    assert len(proposals) == 1
    # But no Activity / Agent landed in the graph.
    activities = registry.knowledge.graph_store.query(node_type=wk.ACTIVITY, limit=10)
    assert activities == []
    assert registry.knowledge.graph_store.get_node(PROPOSAL_GENERATOR_AGENT_ID) is None
