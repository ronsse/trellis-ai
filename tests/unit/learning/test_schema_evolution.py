"""Tests for the well-known promotion loop analyzer.

See ``docs/design/plan-well-known-promotion-loop.md`` §3 Phase 1 for the
10-test slate this file implements.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from trellis.learning.schema_evolution import (
    META_EXTRACTOR_PREFIX,
    PARAM_COMPONENT_ID,
    RECOMMENDED_SEED_VALUES,
    REQUIRED_PARAM_KEYS,
    WellKnownCandidate,
    _compute_candidate_id,
    _detect_naming_collision,
    _summarize_tags,
    analyze_well_known_candidates,
    suggest_canonical_name,
)
from trellis.ops import ParameterRegistry
from trellis.schemas.parameters import ParameterScope, ParameterSet
from trellis.stores.base.event_log import EventType
from trellis.stores.sqlite.event_log import SQLiteEventLog
from trellis.stores.sqlite.graph import SQLiteGraphStore
from trellis.stores.sqlite.parameter import SQLiteParameterStore

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def graph_store(tmp_path: Path):
    s = SQLiteGraphStore(tmp_path / "graph.db")
    yield s
    s.close()


@pytest.fixture
def event_log(tmp_path: Path):
    s = SQLiteEventLog(tmp_path / "events.db")
    yield s
    s.close()


@pytest.fixture
def param_store(tmp_path: Path):
    s = SQLiteParameterStore(tmp_path / "params.db")
    yield s
    s.close()


def _seed_registry(
    param_store: SQLiteParameterStore,
    *,
    overrides: dict[str, float | int | str | bool] | None = None,
) -> ParameterRegistry:
    """Build a registry with the recommended seed values plus overrides.

    By default, ``well_known_window_days`` is dropped to ``0`` so the
    evidence-span filter never gates inside unit tests (data inserted
    in a single test obviously spans 0 days). Tests that exercise the
    span filter explicitly override this back.
    """
    values: dict[str, float | int | str | bool] = dict(RECOMMENDED_SEED_VALUES)
    # Default to 0-day window for unit tests; the eval scenario covers
    # the real ≥7-day path with a synthetic timeline.
    values["well_known_window_days"] = 0
    if overrides:
        values.update(overrides)
    param_store.put(
        ParameterSet(
            scope=ParameterScope(component_id=PARAM_COMPONENT_ID),
            values=values,
            source="test:test_schema_evolution",
        )
    )
    return ParameterRegistry(param_store)


def _insert_nodes(
    graph_store: SQLiteGraphStore,
    *,
    node_type: str,
    count: int,
    extractor_id: str,
    event_log: SQLiteEventLog,
    domains: tuple[str, ...] = ("domain_a", "domain_b"),
    signal_quality: str = "standard",
    valid_from: datetime | None = None,
) -> list[str]:
    """Upsert ``count`` nodes of ``node_type`` and emit matching
    ``MUTATION_EXECUTED`` events. Returns the list of node_ids.

    Each node carries ContentTags-shaped properties (``content_tags``
    dict) so the analyzer's domain + signal_quality summarisation has
    real input.
    """
    node_ids: list[str] = []
    for i in range(count):
        nid = graph_store.upsert_node(
            node_id=f"{node_type}_{i}",
            node_type=node_type,
            properties={
                "content_tags": {
                    "domain": list(domains),
                    "signal_quality": signal_quality,
                },
            },
        )
        node_ids.append(nid)
        event_log.emit(
            EventType.MUTATION_EXECUTED,
            source="mutation_executor",
            entity_id=nid,
            entity_type=node_type,
            payload={
                "command_id": f"cmd_{nid}",
                "operation": "entity.create",
                "status": "SUCCESS",
                "requested_by": extractor_id,
            },
        )
    return node_ids


# ---------------------------------------------------------------------------
# Test 1 — empty graph → empty candidate list
# ---------------------------------------------------------------------------


def test_empty_graph_returns_empty_candidate_list(
    graph_store: SQLiteGraphStore,
    event_log: SQLiteEventLog,
    param_store: SQLiteParameterStore,
):
    registry = _seed_registry(param_store)
    candidates = analyze_well_known_candidates(
        graph_store=graph_store,
        event_log=event_log,
        registry=registry,
    )
    assert candidates == []


# ---------------------------------------------------------------------------
# Test 2 — count below threshold → not surfaced
# ---------------------------------------------------------------------------


def test_below_threshold_count_not_surfaced(
    graph_store: SQLiteGraphStore,
    event_log: SQLiteEventLog,
    param_store: SQLiteParameterStore,
):
    # Lower the count threshold to keep the test fast; everything else
    # still requires 2 extractors + 2 domains so we'd surface only with
    # a real signal.
    registry = _seed_registry(param_store, overrides={"well_known_count_threshold": 10})
    _insert_nodes(
        graph_store,
        node_type="metric",
        count=5,
        extractor_id="worker:dbt",
        event_log=event_log,
    )
    candidates = analyze_well_known_candidates(
        graph_store=graph_store,
        event_log=event_log,
        registry=registry,
    )
    assert candidates == []


# ---------------------------------------------------------------------------
# Test 3 — eligible candidate is surfaced and event is emitted
# ---------------------------------------------------------------------------


def _insert_meeting_thresholds(
    graph_store: SQLiteGraphStore,
    event_log: SQLiteEventLog,
    *,
    node_type: str = "metric",
    count: int = 30,
) -> None:
    """Insert nodes from two extractors and two domains."""
    half = count // 2
    _insert_nodes(
        graph_store,
        node_type=node_type,
        count=half,
        extractor_id="worker:dbt",
        event_log=event_log,
        domains=("analytics",),
    )
    # Second batch with different extractor + different domain. Use a
    # name-suffix offset so node_ids don't collide.
    for i in range(half, count):
        nid = graph_store.upsert_node(
            node_id=f"{node_type}_{i}",
            node_type=node_type,
            properties={
                "content_tags": {
                    "domain": ["finance"],
                    "signal_quality": "standard",
                },
            },
        )
        event_log.emit(
            EventType.MUTATION_EXECUTED,
            source="mutation_executor",
            entity_id=nid,
            entity_type=node_type,
            payload={
                "command_id": f"cmd_{nid}",
                "operation": "entity.create",
                "status": "SUCCESS",
                "requested_by": "worker:lineage",
            },
        )


def test_eligible_candidate_surfaces_and_emits_event(
    graph_store: SQLiteGraphStore,
    event_log: SQLiteEventLog,
    param_store: SQLiteParameterStore,
):
    registry = _seed_registry(
        param_store, overrides={"well_known_count_threshold": 20}
    )
    _insert_meeting_thresholds(graph_store, event_log, count=30)

    candidates = analyze_well_known_candidates(
        graph_store=graph_store,
        event_log=event_log,
        registry=registry,
    )
    assert len(candidates) == 1
    cand = candidates[0]
    assert cand.candidate_kind == "entity_type"
    assert cand.open_string_value == "metric"
    assert cand.count == 30
    assert set(cand.distinct_extractors) == {"worker:dbt", "worker:lineage"}
    assert set(cand.distinct_domains) == {"analytics", "finance"}
    assert cand.suggested_canonical_name == "Metric"
    assert cand.naming_collision is False
    assert cand.recurrence_count == 0

    # Event was emitted.
    events = event_log.get_events(
        event_type=EventType.WELL_KNOWN_CANDIDATE,
        limit=10,
    )
    assert len(events) == 1
    assert events[0].payload["candidate_id"] == cand.candidate_id
    assert events[0].payload["open_string_value"] == "metric"


# ---------------------------------------------------------------------------
# Test 4 — cooldown blocks immediate re-emission
# ---------------------------------------------------------------------------


def test_cooldown_suppresses_immediate_re_emission(
    graph_store: SQLiteGraphStore,
    event_log: SQLiteEventLog,
    param_store: SQLiteParameterStore,
):
    registry = _seed_registry(
        param_store, overrides={"well_known_count_threshold": 20}
    )
    _insert_meeting_thresholds(graph_store, event_log, count=30)

    first = analyze_well_known_candidates(
        graph_store=graph_store,
        event_log=event_log,
        registry=registry,
    )
    assert len(first) == 1

    # Run immediately — same evidence — cooldown should suppress.
    second = analyze_well_known_candidates(
        graph_store=graph_store,
        event_log=event_log,
        registry=registry,
    )
    assert second == []

    # Exactly one WELL_KNOWN_CANDIDATE event remains.
    events = event_log.get_events(
        event_type=EventType.WELL_KNOWN_CANDIDATE, limit=10
    )
    assert len(events) == 1


# ---------------------------------------------------------------------------
# Test 5 — past cooldown re-emits with recurrence_count incremented
# ---------------------------------------------------------------------------


def test_past_cooldown_re_emits_with_recurrence_increment(
    graph_store: SQLiteGraphStore,
    event_log: SQLiteEventLog,
    param_store: SQLiteParameterStore,
):
    registry = _seed_registry(
        param_store,
        overrides={
            "well_known_count_threshold": 20,
            "well_known_cooldown_days": 7,
        },
    )
    _insert_meeting_thresholds(graph_store, event_log, count=30)

    base_now = datetime.now(tz=UTC)
    first = analyze_well_known_candidates(
        graph_store=graph_store,
        event_log=event_log,
        registry=registry,
        now=base_now,
    )
    assert len(first) == 1
    assert first[0].recurrence_count == 0

    # Advance the clock past the cooldown.
    later = base_now + timedelta(days=8)
    second = analyze_well_known_candidates(
        graph_store=graph_store,
        event_log=event_log,
        registry=registry,
        now=later,
    )
    assert len(second) == 1
    assert second[0].recurrence_count == 1


# ---------------------------------------------------------------------------
# Test 6 — count growth ≥ 20% triggers re-emission inside cooldown
# ---------------------------------------------------------------------------


def test_growth_trigger_re_emits_inside_cooldown(
    graph_store: SQLiteGraphStore,
    event_log: SQLiteEventLog,
    param_store: SQLiteParameterStore,
):
    registry = _seed_registry(
        param_store, overrides={"well_known_count_threshold": 20}
    )
    _insert_meeting_thresholds(graph_store, event_log, count=30)

    base_now = datetime.now(tz=UTC)
    first = analyze_well_known_candidates(
        graph_store=graph_store,
        event_log=event_log,
        registry=registry,
        now=base_now,
    )
    assert len(first) == 1

    # Add another 8 nodes — 30 → 38 → growth ratio ~27%.
    for i in range(30, 38):
        nid = graph_store.upsert_node(
            node_id=f"metric_{i}",
            node_type="metric",
            properties={
                "content_tags": {
                    "domain": ["finance"],
                    "signal_quality": "standard",
                },
            },
        )
        event_log.emit(
            EventType.MUTATION_EXECUTED,
            source="mutation_executor",
            entity_id=nid,
            entity_type="metric",
            payload={
                "command_id": f"cmd_{nid}",
                "operation": "entity.create",
                "status": "SUCCESS",
                "requested_by": "worker:lineage",
            },
        )

    # Immediate re-run — still inside cooldown, but growth ≥ 20% so
    # the candidate re-surfaces.
    later = base_now + timedelta(minutes=5)
    second = analyze_well_known_candidates(
        graph_store=graph_store,
        event_log=event_log,
        registry=registry,
        now=later,
    )
    assert len(second) == 1
    assert second[0].count == 38
    assert second[0].recurrence_count == 1


# ---------------------------------------------------------------------------
# Test 7 — already-canonical types do not surface
# ---------------------------------------------------------------------------


def test_canonical_type_does_not_surface(
    graph_store: SQLiteGraphStore,
    event_log: SQLiteEventLog,
    param_store: SQLiteParameterStore,
):
    registry = _seed_registry(
        param_store, overrides={"well_known_count_threshold": 5}
    )
    # ``"Person"`` is canonical — should never appear in candidates
    # even at very high counts.
    _insert_nodes(
        graph_store,
        node_type="Person",
        count=50,
        extractor_id="worker:hr",
        event_log=event_log,
    )
    # Add a second batch with a different extractor + domain to ensure
    # the only thing blocking promotion is canonicality.
    for i in range(50, 100):
        nid = graph_store.upsert_node(
            node_id=f"Person_{i}",
            node_type="Person",
            properties={
                "content_tags": {
                    "domain": ["finance"],
                    "signal_quality": "standard",
                },
            },
        )
        event_log.emit(
            EventType.MUTATION_EXECUTED,
            source="mutation_executor",
            entity_id=nid,
            entity_type="Person",
            payload={
                "command_id": f"cmd_{nid}",
                "operation": "entity.create",
                "status": "SUCCESS",
                "requested_by": "worker:lineage",
            },
        )
    candidates = analyze_well_known_candidates(
        graph_store=graph_store,
        event_log=event_log,
        registry=registry,
    )
    assert candidates == []


# ---------------------------------------------------------------------------
# Test 8 — single extractor doesn't promote
# ---------------------------------------------------------------------------


def test_single_extractor_not_eligible(
    graph_store: SQLiteGraphStore,
    event_log: SQLiteEventLog,
    param_store: SQLiteParameterStore,
):
    registry = _seed_registry(
        param_store, overrides={"well_known_count_threshold": 10}
    )
    _insert_nodes(
        graph_store,
        node_type="metric",
        count=50,
        extractor_id="worker:dbt",
        event_log=event_log,
        domains=("analytics", "finance"),
    )
    candidates = analyze_well_known_candidates(
        graph_store=graph_store,
        event_log=event_log,
        registry=registry,
    )
    assert candidates == []


# ---------------------------------------------------------------------------
# Test 9 — missing parameter key raises KeyError naming the key
# ---------------------------------------------------------------------------


def test_missing_parameter_key_raises_key_error(
    graph_store: SQLiteGraphStore,
    event_log: SQLiteEventLog,
    param_store: SQLiteParameterStore,
):
    # Seed a snapshot with all-but-one of the required keys.
    incomplete_values: dict[str, float | int | str | bool] = dict(
        RECOMMENDED_SEED_VALUES
    )
    incomplete_values.pop("well_known_distinct_domains")
    param_store.put(
        ParameterSet(
            scope=ParameterScope(component_id=PARAM_COMPONENT_ID),
            values=incomplete_values,
            source="test:missing_key",
        )
    )
    registry = ParameterRegistry(param_store)
    with pytest.raises(KeyError, match="well_known_distinct_domains"):
        analyze_well_known_candidates(
            graph_store=graph_store,
            event_log=event_log,
            registry=registry,
        )


# ---------------------------------------------------------------------------
# Test 10 — naming-collision detection on a case-mismatched canonical
# ---------------------------------------------------------------------------


def test_naming_collision_flagged(
    graph_store: SQLiteGraphStore,
    event_log: SQLiteEventLog,
    param_store: SQLiteParameterStore,
):
    registry = _seed_registry(
        param_store, overrides={"well_known_count_threshold": 5}
    )
    # ``"persons"`` is NOT canonical (singular is); but it canonicalizes
    # via the suggestion heuristic to ``"Persons"`` — which is *also*
    # not canonical. To force a collision we use an open string whose
    # PascalCase output equals an existing canonical alias spelling.
    # ``"person"`` is in the alias map, but our analyzer filters known
    # types upfront — so we use a case-mismatch like ``"PERSON"`` which
    # is NOT a known string but whose PascalCase suggestion ``"Person"``
    # collides with the canonical ``PERSON = "Person"``.
    _insert_nodes(
        graph_store,
        node_type="PERSON",
        count=20,
        extractor_id="worker:hr",
        event_log=event_log,
        domains=("hr",),
    )
    for i in range(20, 40):
        nid = graph_store.upsert_node(
            node_id=f"PERSON_{i}",
            node_type="PERSON",
            properties={
                "content_tags": {
                    "domain": ["sales"],
                    "signal_quality": "standard",
                },
            },
        )
        event_log.emit(
            EventType.MUTATION_EXECUTED,
            source="mutation_executor",
            entity_id=nid,
            entity_type="PERSON",
            payload={
                "command_id": f"cmd_{nid}",
                "operation": "entity.create",
                "status": "SUCCESS",
                "requested_by": "worker:crm",
            },
        )
    candidates = analyze_well_known_candidates(
        graph_store=graph_store,
        event_log=event_log,
        registry=registry,
    )
    assert len(candidates) == 1
    cand = candidates[0]
    assert cand.suggested_canonical_name == "Person"
    assert cand.naming_collision is True
    assert any("collides" in n for n in cand.notes)


# ---------------------------------------------------------------------------
# Bonus — filtering trellis_meta_ writes (POC directive)
# ---------------------------------------------------------------------------


def test_meta_extractor_writes_filtered_from_extractor_count(
    graph_store: SQLiteGraphStore,
    event_log: SQLiteEventLog,
    param_store: SQLiteParameterStore,
):
    """Meta-extractor writes (``trellis_meta_*``) don't count toward
    the distinct-extractors threshold.

    Preempts Item 6 dogfooding feedback: if the meta-tracer's
    Activity/Observation nodes also emit MUTATION_EXECUTED rows whose
    ``requested_by`` is ``trellis_meta_<something>``, they must NOT
    inflate the extractor count.
    """
    registry = _seed_registry(
        param_store, overrides={"well_known_count_threshold": 5}
    )
    # Two distinct extractors needed; one is real, one is meta — net
    # one real extractor → below threshold.
    _insert_nodes(
        graph_store,
        node_type="metric",
        count=20,
        extractor_id="worker:dbt",
        event_log=event_log,
        domains=("analytics",),
    )
    for i in range(20, 40):
        nid = graph_store.upsert_node(
            node_id=f"metric_{i}",
            node_type="metric",
            properties={
                "content_tags": {
                    "domain": ["finance"],
                    "signal_quality": "standard",
                },
            },
        )
        event_log.emit(
            EventType.MUTATION_EXECUTED,
            source="mutation_executor",
            entity_id=nid,
            entity_type="metric",
            payload={
                "command_id": f"cmd_{nid}",
                "operation": "entity.create",
                "status": "SUCCESS",
                "requested_by": f"{META_EXTRACTOR_PREFIX}schema_evolution",
            },
        )
    candidates = analyze_well_known_candidates(
        graph_store=graph_store,
        event_log=event_log,
        registry=registry,
    )
    assert candidates == []


# ---------------------------------------------------------------------------
# Bonus — dry-run mode does not emit
# ---------------------------------------------------------------------------


def test_emit_events_false_returns_candidates_without_event(
    graph_store: SQLiteGraphStore,
    event_log: SQLiteEventLog,
    param_store: SQLiteParameterStore,
):
    registry = _seed_registry(
        param_store, overrides={"well_known_count_threshold": 20}
    )
    _insert_meeting_thresholds(graph_store, event_log, count=30)

    candidates = analyze_well_known_candidates(
        graph_store=graph_store,
        event_log=event_log,
        registry=registry,
        emit_events=False,
    )
    assert len(candidates) == 1
    # No WELL_KNOWN_CANDIDATE event was emitted.
    events = event_log.get_events(
        event_type=EventType.WELL_KNOWN_CANDIDATE, limit=5
    )
    assert events == []


# ---------------------------------------------------------------------------
# Unit tests for the naming heuristic + collision detector
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "kind", "expected"),
    [
        ("dbt_model", "entity_type", "DbtModel"),
        ("oncall_shift", "entity_type", "OncallShift"),
        ("metric", "entity_type", "Metric"),
        ("emits_metric", "edge_kind", "emitsMetric"),
        ("escalates_to", "edge_kind", "escalatesTo"),
        ("hyphen-name", "entity_type", "HyphenName"),
        ("with.dots", "entity_type", "WithDots"),
    ],
)
def test_suggest_canonical_name(value: str, kind: str, expected: str) -> None:
    assert suggest_canonical_name(value, kind) == expected  # type: ignore[arg-type]


def test_compute_candidate_id_stable() -> None:
    a = _compute_candidate_id("metric", "entity_type")
    b = _compute_candidate_id("metric", "entity_type")
    assert a == b
    # Differing kind => different id.
    c = _compute_candidate_id("metric", "edge_kind")
    assert a != c


def test_detect_naming_collision_case_insensitive() -> None:
    # ``"Person"`` is canonical; case-insensitive match collides.
    assert _detect_naming_collision("Person", "entity_type") is True
    assert _detect_naming_collision("person", "entity_type") is True
    # Unknown PascalCase passes through cleanly.
    assert _detect_naming_collision("DbtModel", "entity_type") is False
    # Edge kind collision against alias.
    assert _detect_naming_collision("trace_used_evidence", "edge_kind") is True


def test_required_param_keys_match_seed_keys() -> None:
    """Guards against an accidental drift between the required key
    tuple and the recommended seed dict — they must enumerate the same
    surface."""
    assert set(REQUIRED_PARAM_KEYS) == set(RECOMMENDED_SEED_VALUES.keys())


def test_candidate_to_event_payload_round_trip() -> None:
    """Payload keys are stable wire contract — guard against renames."""
    now = datetime.now(tz=UTC)
    candidate = WellKnownCandidate(
        candidate_kind="entity_type",
        open_string_value="metric",
        count=500,
        distinct_extractors=("worker:dbt", "worker:lineage"),
        distinct_domains=("analytics", "finance"),
        avg_signal_quality="standard",
        first_seen=now - timedelta(days=7),
        last_seen=now,
        suggested_canonical_name="Metric",
        suggested_alignment_uri="schema.org/Metric",
        candidate_id="wkc_ent_abcdef0123456789",
        cooldown_until=None,
        naming_collision=False,
        recurrence_count=0,
    )
    payload = candidate.to_event_payload()
    expected_keys = {
        "candidate_id",
        "candidate_kind",
        "open_string_value",
        "count",
        "distinct_extractors",
        "distinct_domains",
        "avg_signal_quality",
        "first_seen",
        "last_seen",
        "suggested_canonical_name",
        "suggested_alignment_uri",
        "naming_collision",
        "recurrence_count",
        "notes",
    }
    assert set(payload.keys()) == expected_keys
    assert payload["candidate_id"] == "wkc_ent_abcdef0123456789"


# ---------------------------------------------------------------------------
# Shape-contract regression — content_tags at top level (Phase 5A footgun)
# ---------------------------------------------------------------------------
#
# The well-known analyzer reads ContentTags via
# ``node["properties"]["content_tags"]`` (per the GraphStore ABC
# row-shape contract). If a backend hypothetically promoted
# ``content_tags`` to a top-level column on the row dict, the analyzer
# would silently see zero domains and the candidate would be filtered
# out by the distinct_domains threshold — axis G of the well-known
# promotion analyzer stays at 0 with no diagnostic. Phase 5A flagged
# this footgun; the POC directive says no silent fallbacks, so
# ``_summarize_tags`` raises TypeError when it detects the wrong shape.


def test_summarize_tags_raises_on_top_level_content_tags() -> None:
    """Loud on shape mismatch — a top-level ``content_tags`` key is
    a backend-shape violation, not a "no tags" case.

    The analyzer's expected shape is
    ``node["properties"]["content_tags"]``. A row that puts the dict
    at the top level instead would silently produce zero domains.
    Per the POC directive (no silent fallbacks), surface the violation
    by raising rather than papering over it.
    """
    bad_node: dict[str, object] = {
        "node_id": "metric_42",
        "node_type": "metric",
        # WRONG: content_tags promoted to a top-level column instead of
        # nested under properties. This is the Phase 5A footgun.
        "content_tags": {
            "domain": ["analytics"],
            "signal_quality": "standard",
        },
        "properties": {},
    }
    with pytest.raises(TypeError, match="top-level 'content_tags'"):
        _summarize_tags([bad_node])


def test_summarize_tags_raises_on_top_level_tags_legacy_alias() -> None:
    """The legacy ``tags`` alias is equally reserved at the top level.

    Both ``content_tags`` and ``tags`` are valid nested-under-properties
    keys (``tags`` is the pre-rename alias). Either one at the top
    level is a shape violation — raise on both.
    """
    bad_node: dict[str, object] = {
        "node_id": "metric_99",
        "node_type": "metric",
        "tags": {"domain": ["finance"], "signal_quality": "high"},
        "properties": {},
    }
    with pytest.raises(TypeError, match="top-level 'tags'"):
        _summarize_tags([bad_node])


def test_summarize_tags_tolerates_missing_content_tags() -> None:
    """Genuinely untagged nodes don't raise — structural nodes
    legitimately ship without a ``ContentTags`` dict.

    The contract is "tags MUST live under properties when present", not
    "tags MUST always be present". Missing tags fall back to the
    "no classification signal" path (avg_signal_quality = "standard",
    no domains).
    """
    untagged: dict[str, object] = {
        "node_id": "scaffold_1",
        "node_type": "scaffold",
        "properties": {},
    }
    domains, avg = _summarize_tags([untagged])
    assert domains == ()
    # ADR §2.1: absence of tags defaults to "standard" (the threshold
    # floor) so it neither helps nor blocks promotion on its own.
    assert avg == "standard"


def test_analyze_raises_when_backend_promotes_content_tags_to_top_level(
    graph_store: SQLiteGraphStore,
    event_log: SQLiteEventLog,
    param_store: SQLiteParameterStore,
    monkeypatch: pytest.MonkeyPatch,
):
    """End-to-end regression: a hypothetical backend that returns
    ``content_tags`` as a top-level column makes the analyzer raise.

    Trips every other gate so the only thing left to test is the
    shape-mismatch detection in ``_summarize_tags``. We monkeypatch
    the store's ``query`` method to swap ``content_tags`` from
    ``properties`` to the top level — simulating a backend that
    deviated from the row-dict shape contract.
    """
    registry = _seed_registry(
        param_store, overrides={"well_known_count_threshold": 20}
    )
    _insert_meeting_thresholds(graph_store, event_log, count=30)

    real_query = graph_store.query

    def _shape_violating_query(
        node_type: str | None = None,
        properties: dict[str, object] | None = None,
        limit: int = 50,
        as_of: datetime | None = None,
    ) -> list[dict[str, object]]:
        rows = real_query(
            node_type=node_type,
            properties=properties,
            limit=limit,
            as_of=as_of,
        )
        # Promote content_tags out of `properties` and onto the top
        # level — the exact footgun the contract pin guards against.
        for row in rows:
            props = row.get("properties") or {}
            if isinstance(props, dict) and "content_tags" in props:
                row["content_tags"] = props.pop("content_tags")
        return rows

    monkeypatch.setattr(graph_store, "query", _shape_violating_query)

    with pytest.raises(TypeError, match="top-level 'content_tags'"):
        analyze_well_known_candidates(
            graph_store=graph_store,
            event_log=event_log,
            registry=registry,
        )
