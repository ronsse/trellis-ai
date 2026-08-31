"""Meta-Activities are minted ``structural`` (#375, plan option 2).

One node per analyzer invocation is a *rate*, not a corpus. On the reference
deployment those rows were a fifth of the whole graph, cited zero times in 190
graded graph servings, and — because ``PackBuilder``'s meta filter runs *after*
``GraphSearch`` has sliced ``nodes[:limit]`` — they were spending candidate
slots before being discarded. ``node_role`` is read *before* that slice.

Four properties are pinned here, and the third is the one that could break a
nightly cron rather than merely a pack:

1. the meta recorder mints ``structural``;
2. a trace-extraction ``Activity`` still mints ``semantic`` — the scope is the
   recorder, not ``Activity`` as a type (``extract/trace.py`` says so
   explicitly: the Activity *is* the trace);
3. the merge-within-window dedup path does **not** re-upsert the Activity, so
   it cannot trip ``check_node_role_immutable``, which raises on any role
   change across SCD-2 versions;
4. ``GraphSearch`` drops it by default and surfaces it under
   ``include_structural=True``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from trellis.meta import (
    DEFAULT_MERGE_WINDOW_SECONDS,
    DEFAULT_META_AGENT_ID,
    META_TRACES_ENV_VAR,
    record_meta_analysis,
)
from trellis.retrieve.strategies import GraphSearch
from trellis.schemas import well_known as wk
from trellis.schemas.enums import NodeRole
from trellis.stores.registry import StoreRegistry


@pytest.fixture(autouse=True)
def _clear_meta_traces_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(META_TRACES_ENV_VAR, raising=False)


@pytest.fixture
def registry(tmp_path: Path) -> StoreRegistry:
    stores_dir = tmp_path / "stores"
    stores_dir.mkdir()
    return StoreRegistry(stores_dir=stores_dir)


def _record(
    registry: StoreRegistry,
    *,
    analyzer: str = "curate.learning",
    merge_window_seconds: int = DEFAULT_MERGE_WINDOW_SECONDS,
) -> str:
    with record_meta_analysis(
        analyzer_name=analyzer,
        agent_id=DEFAULT_META_AGENT_ID,
        registry=registry,
        merge_window_seconds=merge_window_seconds,
    ) as rec:
        assert rec.activity_id is not None
        return rec.activity_id


class TestMintedRole:
    def test_meta_activity_is_structural(self, registry: StoreRegistry) -> None:
        activity_id = _record(registry)
        node = registry.knowledge.graph_store.get_node(activity_id)
        assert node is not None
        assert node["node_role"] == NodeRole.STRUCTURAL.value

    def test_a_trace_extraction_activity_stays_semantic(
        self, registry: StoreRegistry
    ) -> None:
        """Scope check: the stamp is the recorder's, not ``Activity``'s.

        ``extract/trace.py`` mints its Activity ``SEMANTIC`` deliberately —
        the Activity *is* the trace, and demoting it would hide trace memory
        from packs entirely. Only the meta recorder's own rows move.
        """
        registry.knowledge.graph_store.upsert_node(
            node_id="trace-activity-1",
            node_type=wk.ACTIVITY,
            properties={"name": "a real unit of agent work"},
        )
        node = registry.knowledge.graph_store.get_node("trace-activity-1")
        assert node is not None
        assert node["node_role"] == NodeRole.SEMANTIC.value


class TestImmutabilityIsNotTripped:
    """``check_node_role_immutable`` raises on any role change; nothing here
    re-upserts the Activity, so the nightly crons cannot start raising."""

    def test_merge_within_window_does_not_re_upsert_the_activity(
        self, registry: StoreRegistry
    ) -> None:
        graph = registry.knowledge.graph_store
        first = _record(registry, merge_window_seconds=300)
        second = _record(registry, merge_window_seconds=300)
        assert second == first, "merge window should have reused the Activity"

        # One version, still structural: a second upsert would have closed the
        # first version and opened a new one.
        history = graph.get_node_history(first)
        assert len(history) == 1
        node = graph.get_node(first)
        assert node is not None
        assert node["node_role"] == NodeRole.STRUCTURAL.value

    def test_merge_still_appends_edges(self, registry: StoreRegistry) -> None:
        """The merge path's actual job keeps working under the new role."""
        graph = registry.knowledge.graph_store
        graph.upsert_node(node_id="evt-1", node_type="Placeholder", properties={})
        graph.upsert_node(node_id="evt-2", node_type="Placeholder", properties={})

        with record_meta_analysis(
            analyzer_name="curate.learning",
            agent_id=DEFAULT_META_AGENT_ID,
            registry=registry,
            merge_window_seconds=300,
        ) as rec:
            rec.consumed_event("evt-1")
            activity_id = rec.activity_id
        with record_meta_analysis(
            analyzer_name="curate.learning",
            agent_id=DEFAULT_META_AGENT_ID,
            registry=registry,
            merge_window_seconds=300,
        ) as rec:
            rec.consumed_event("evt-2")
            assert rec.activity_id == activity_id

        assert activity_id is not None
        informed = graph.get_edges(
            activity_id, direction="outgoing", edge_type=wk.WAS_INFORMED_BY
        )
        assert {e["target_id"] for e in informed} == {"evt-1", "evt-2"}

    def test_the_merge_lookup_still_finds_a_structural_activity(
        self, registry: StoreRegistry
    ) -> None:
        """``_find_recent_activity`` reads ``GraphStore.query``, which does not
        filter by role — if it ever did, every invocation would mint a fresh
        Activity and the churn this change removes would come straight back."""
        first = _record(registry, merge_window_seconds=300)
        candidates = registry.knowledge.graph_store.query(
            node_type=wk.ACTIVITY,
            properties={
                "agent_id": DEFAULT_META_AGENT_ID,
                "analyzer_name": "curate.learning",
            },
            limit=50,
        )
        assert first in {c["node_id"] for c in candidates}

    def test_a_materialised_endpoint_stub_does_not_raise(
        self, registry: StoreRegistry
    ) -> None:
        """``_materialise_node_if_absent`` mints consumed/produced endpoints at
        the default role. It create-if-absents, so it never re-roles a row —
        including one that already exists as structural."""
        graph = registry.knowledge.graph_store
        graph.upsert_node(
            node_id="finding-structural",
            node_type=wk.OBSERVATION,
            properties={"name": "pre-existing structural endpoint"},
            node_role=NodeRole.STRUCTURAL.value,
        )
        with record_meta_analysis(
            analyzer_name="curate.learning",
            agent_id=DEFAULT_META_AGENT_ID,
            registry=registry,
        ) as rec:
            rec.produced_finding("finding-structural", finding_type=wk.OBSERVATION)
        node = graph.get_node("finding-structural")
        assert node is not None
        assert node["node_role"] == NodeRole.STRUCTURAL.value


class TestGraphSearchSuppression:
    """The point of the stamp: the row leaves the candidate window."""

    def test_excluded_by_default(self, registry: StoreRegistry) -> None:
        activity_id = _record(registry)
        items = GraphSearch(registry.knowledge.graph_store).search("anything")
        assert activity_id not in {i.item_id for i in items}

    def test_surfaced_under_include_structural(self, registry: StoreRegistry) -> None:
        activity_id = _record(registry)
        items = GraphSearch(registry.knowledge.graph_store).search(
            "anything", filters={"include_structural": True}
        )
        served = {i.item_id: i for i in items}
        assert activity_id in served
        assert served[activity_id].metadata["node_role"] == NodeRole.STRUCTURAL.value

    def test_a_semantic_neighbour_keeps_its_slot(self, registry: StoreRegistry) -> None:
        """Suppression frees the slot rather than shrinking the window.

        The meta-Activity used to occupy a place in ``nodes[:limit]`` and be
        discarded downstream by ``PackBuilder._is_meta_activity``; a real
        memory ranked behind it never became a candidate at all.

        The window here is the oldest-first ``gotcha``, then the synthetic
        ``Agent`` node ``ensure_meta_agent`` creates, then the Activity. At
        ``limit=2`` the Activity used to evict the gotcha; now it does not.
        (The ``Agent`` node stays ``semantic`` — it is written once per
        ``agent_id``, not once per invocation, so it is not churn and is out
        of this change's scope.)
        """
        graph = registry.knowledge.graph_store
        graph.upsert_node(
            node_id="gotcha-1",
            node_type="gotcha",
            properties={
                "name": "make lint needs the venv on PATH",
                "description": "make lint/format/test need the venv on PATH",
            },
        )
        activity_id = _record(registry)
        served = {i.item_id for i in GraphSearch(graph).search("anything", limit=2)}
        assert "gotcha-1" in served
        assert activity_id not in served
