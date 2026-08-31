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
from trellis.meta.agents import META_AGENT_PREFIX
from trellis.retrieve.pack_builder import PackBuilder
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


class TestCounterWrapUpTrap:
    """The next planned change to this module re-upserts the Activity.

    ``adr-dogfooding-meta-traces.md`` §2.4 describes a merge that "extends
    ``ended_at``, increments ``events_consumed``", and ``_create_activity``'s
    docstring says Phase 1 populates the counters "at exit time once the
    wrap-up phase exists". That write is a node re-upsert — and
    ``upsert_node`` defaults ``node_role`` to ``semantic`` while
    ``check_node_role_immutable`` raises on any change across versions. So
    the naive wrap-up raises for *every* meta-Activity in every nightly
    cron. Nothing re-upserts the Activity today, which is why this is a trap
    for the next change rather than a live defect — and why it is pinned
    here rather than left to be rediscovered from a cron backtrace.
    """

    def test_naive_wrap_up_raises(self, registry: StoreRegistry) -> None:
        graph = registry.knowledge.graph_store
        activity_id = _record(registry)
        node = graph.get_node(activity_id)
        assert node is not None
        props = dict(node["properties"])
        props["events_consumed"] = 12

        with pytest.raises(ValueError, match="node_role is immutable"):
            graph.upsert_node(
                node_id=activity_id,
                node_type=node["node_type"],
                properties=props,
            )

    def test_wrap_up_carrying_the_role_forward_succeeds(
        self, registry: StoreRegistry
    ) -> None:
        """The shape a wrap-up implementation has to use."""
        graph = registry.knowledge.graph_store
        activity_id = _record(registry)
        node = graph.get_node(activity_id)
        assert node is not None
        props = dict(node["properties"])
        props["events_consumed"] = 12

        graph.upsert_node(
            node_id=activity_id,
            node_type=node["node_type"],
            properties=props,
            node_role=node["node_role"],
        )
        updated = graph.get_node(activity_id)
        assert updated is not None
        assert updated["node_role"] == NodeRole.STRUCTURAL.value
        assert updated["properties"]["events_consumed"] == 12
        assert len(graph.get_node_history(activity_id)) == 2


class TestTheStampIsWhatFreesTheSlot:
    """End-to-end: ``_is_meta_activity`` alone empties the pack.

    The distinction the whole change rests on. Both worlds keep the
    meta-Activity out of the served pack — so "absent from the pack" proves
    nothing. What separates them is whether a *real memory* got a candidate
    slot: pre-stamp the meta rows fill ``nodes[:limit]``, are rejected
    downstream as ``meta_activity_filter``, and the pack comes back empty.
    """

    @staticmethod
    def _seed(registry: StoreRegistry, *, structural: bool) -> tuple[str, list[str]]:
        graph = registry.knowledge.graph_store
        graph.upsert_node(
            node_id="memory-1",
            node_type="gotcha",
            properties={
                "name": "deploy gotcha",
                "description": "the build produces differently-named images",
            },
        )
        agent_id = f"{META_AGENT_PREFIX}cli_worker"
        graph.upsert_node(
            node_id=agent_id,
            node_type=wk.AGENT,
            properties={"name": agent_id, "synthetic": True},
        )
        role = {"node_role": NodeRole.STRUCTURAL.value} if structural else {}
        metas = []
        for i in range(6):
            node_id = f"meta-activity-{i}"
            graph.upsert_node(
                node_id=node_id,
                node_type=wk.ACTIVITY,
                properties={
                    "name": f"cli.worker.curate.learning@{i}",
                    "analyzer_name": "cli.worker.curate.learning",
                    "agent_id": agent_id,
                },
                **role,
            )
            metas.append(node_id)
        return agent_id, metas

    def test_unstamped_meta_rows_starve_the_pack(self, registry: StoreRegistry) -> None:
        self._seed(registry, structural=False)
        builder = PackBuilder(
            strategies=[GraphSearch(registry.knowledge.graph_store)],
            event_log=registry.operational.event_log,
        )
        pack = builder.build("deploy gotcha", limit_per_strategy=3)
        # All three candidate slots went to meta rows, which the pack-side
        # filter then dropped. The memory never became a candidate.
        assert [item.item_id for item in pack.items] == []

    def test_stamped_meta_rows_leave_the_slot_for_the_memory(
        self, registry: StoreRegistry
    ) -> None:
        self._seed(registry, structural=True)
        builder = PackBuilder(
            strategies=[GraphSearch(registry.knowledge.graph_store)],
            event_log=registry.operational.event_log,
        )
        pack = builder.build("deploy gotcha", limit_per_strategy=3)
        served = {item.item_id for item in pack.items}
        assert "memory-1" in served
        assert not served & {f"meta-activity-{i}" for i in range(6)}


class TestIncludeMetaEscapeHatch:
    """``include_meta=True`` alone no longer reaches a *new* meta-Activity.

    The stamp moves the drop one step earlier, to the graph axis, where
    ``include_structural`` is the gate. Documented on ``PackBuilder.build``
    and in the recorder module docstring; pinned here so the two do not
    drift.
    """

    def test_include_meta_alone_is_not_enough(self, registry: StoreRegistry) -> None:
        activity_id = _record(registry)
        builder = PackBuilder(strategies=[GraphSearch(registry.knowledge.graph_store)])
        pack = builder.build("anything", include_meta=True)
        assert activity_id not in {item.item_id for item in pack.items}

    def test_both_flags_surface_it(self, registry: StoreRegistry) -> None:
        activity_id = _record(registry)
        builder = PackBuilder(strategies=[GraphSearch(registry.knowledge.graph_store)])
        pack = builder.build("anything", include_meta=True, include_structural=True)
        assert activity_id in {item.item_id for item in pack.items}

    def test_a_legacy_semantic_row_still_needs_only_include_meta(
        self, registry: StoreRegistry
    ) -> None:
        """Rows written before #375 kept ``semantic`` and are unaffected."""
        registry.knowledge.graph_store.upsert_node(
            node_id="legacy-meta-activity",
            node_type=wk.ACTIVITY,
            properties={
                "name": "cli.worker.curate.learning@old",
                "analyzer_name": "cli.worker.curate.learning",
                "agent_id": f"{META_AGENT_PREFIX}cli_worker",
            },
        )
        builder = PackBuilder(strategies=[GraphSearch(registry.knowledge.graph_store)])
        assert "legacy-meta-activity" not in {
            item.item_id for item in builder.build("anything").items
        }
        assert "legacy-meta-activity" in {
            item.item_id for item in builder.build("anything", include_meta=True).items
        }
