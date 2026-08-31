"""What the withholding report can and cannot see, pinned as a decision.

#404 says a pack must state what it did not serve. #375/#436, merged six
minutes earlier, moved every newly-written meta-Activity to
``node_role="structural"`` — which ``GraphSearch`` drops *inside the
strategy*, before a ``PackItem`` exists. No ``PackItem``, no
``RejectedItem``, nothing for ``summarize_withheld`` to count.

Neither change is wrong. The pair leaves an asymmetry that is worth pinning
rather than rediscovering:

* ``noise`` and ``archived`` run at ``PackBuilder``'s collect seam and **are**
  reported, even though ``candidates_found`` is incremented after them too;
* the graph axis's ``structural`` filter runs one layer down and is **not**.

So "a strategy-level filter never produces a candidate, therefore there is
nothing to withhold" does not distinguish the two cases. What separates them
is where the code lives. Kept as-is because the caller-facing consequence is
an *improvement* (the note stopped claiming Trellis's own per-cron plumbing
"matched this intent"), while the operator-facing consequence — a
``meta_filtered_count`` of ``0`` on a corpus where suppression happens every
pack — is documented in ``trellis.retrieve.withholding`` and in
``docs/agent-guide/operations.md``.

Every existing ``structural_filter`` / ``meta_activity_filter`` test drives a
**fake** strategy returning pre-built ``PackItem``s, so it exercises only the
collect-seam backstop and cannot observe any of this. These go through the
real ``GraphSearch`` against a real store.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from trellis.meta import (
    DEFAULT_META_AGENT_ID,
    META_TRACES_ENV_VAR,
    record_meta_analysis,
)
from trellis.retrieve.pack_builder import PackBuilder
from trellis.retrieve.strategies import GraphSearch
from trellis.schemas import well_known as wk
from trellis.schemas.enums import NodeRole
from trellis.schemas.pack import PackBudget
from trellis.stores.registry import StoreRegistry

if TYPE_CHECKING:
    from pathlib import Path

_BUDGET = PackBudget(max_items=50, max_tokens=100_000)


@pytest.fixture(autouse=True)
def _clear_meta_traces_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(META_TRACES_ENV_VAR, raising=False)


@pytest.fixture
def registry(tmp_path: Path) -> StoreRegistry:
    stores_dir = tmp_path / "stores"
    stores_dir.mkdir()
    reg = StoreRegistry(stores_dir=stores_dir)
    reg.knowledge.graph_store.upsert_node(
        node_id="memory-1",
        node_type=wk.OBSERVATION,
        properties={"name": "keeper", "description": "a durable memory about caching"},
    )
    return reg


def _seed_legacy_meta(registry: StoreRegistry, count: int = 3) -> list[str]:
    """Pre-#436 rows: same type and ``agent_id``, ``node_role="semantic"``."""
    graph = registry.knowledge.graph_store
    ids = []
    for i in range(count):
        node_id = f"cli.worker.curate.learning@{i}"
        graph.upsert_node(
            node_id=node_id,
            node_type=wk.ACTIVITY,
            properties={
                "name": node_id,
                "agent_id": DEFAULT_META_AGENT_ID,
                "analyzer_name": "curate.learning",
            },
            node_role=NodeRole.SEMANTIC.value,
        )
        ids.append(node_id)
    return ids


def _seed_stamped_meta(registry: StoreRegistry, count: int = 3) -> list[str]:
    """Post-#436 rows, via the real recorder."""
    ids = []
    for i in range(count):
        with record_meta_analysis(
            analyzer_name=f"curate.learning.{i}",
            agent_id=DEFAULT_META_AGENT_ID,
            registry=registry,
            merge_window_seconds=0,
        ) as rec:
            ids.append(rec.activity_id)
    return ids


def _build(registry: StoreRegistry) -> object:
    builder = PackBuilder(strategies=[GraphSearch(registry.knowledge.graph_store)])
    return builder.build("caching", budget=_BUDGET)


class TestNeitherRoleReachesThePack:
    """Whatever the reporting says, the serving behaviour is identical."""

    @pytest.mark.parametrize("seed", [_seed_legacy_meta, _seed_stamped_meta])
    def test_meta_activities_are_never_served(
        self, registry: StoreRegistry, seed: object
    ) -> None:
        meta_ids = seed(registry)  # type: ignore[operator]
        pack = _build(registry)
        assert set(meta_ids).isdisjoint({i.item_id for i in pack.items})  # type: ignore[attr-defined]


class TestTheReportingAsymmetry:
    """The part that changed, stated so a future change has to argue with it."""

    def test_a_legacy_row_is_counted_as_withheld(self, registry: StoreRegistry) -> None:
        _seed_legacy_meta(registry)
        pack = _build(registry)
        withholding = pack.metadata["withholding"]  # type: ignore[attr-defined]
        assert withholding["by_reason"] == {"meta_activity_filter": 3}
        assert withholding["total"] == 3

    def test_a_stamped_row_is_not_counted_at_all(self, registry: StoreRegistry) -> None:
        """Not a bug to fix by re-counting — see the module docstring. If
        this ever needs to change, it is a decision about the *caller's*
        note, not a repair of a broken count."""
        meta_ids = _seed_stamped_meta(registry)
        pack = _build(registry)
        withholding = pack.metadata["withholding"]  # type: ignore[attr-defined]
        assert withholding["by_reason"] == {}
        assert withholding["total"] == 0
        assert withholding["withheld_item_ids"] == []
        # And the rows really were removed — this is under-reporting, not a
        # pack that happened to serve everything. (The recorder's synthetic
        # ``Agent`` node is ``semantic`` and is served; only the Activities
        # are structural, so assert on the Activity ids specifically.)
        served = {i.item_id for i in pack.items}  # type: ignore[attr-defined]
        assert "memory-1" in served
        assert served.isdisjoint(set(meta_ids))

    def test_the_collect_seam_gates_are_still_reported_on_the_same_axis(
        self, registry: StoreRegistry
    ) -> None:
        """Noise and archived are removed no later than structural in the
        ``candidates_found`` accounting, and are reported. That is the
        asymmetry, on one build, on one store."""
        graph = registry.knowledge.graph_store
        graph.upsert_node(
            node_id="noisy-1",
            node_type=wk.OBSERVATION,
            properties={
                "name": "noisy",
                "description": "a demoted memory about caching",
                "content_tags": {"signal_quality": "noise"},
            },
        )
        graph.upsert_node(
            node_id="archived-1",
            node_type=wk.OBSERVATION,
            properties={
                "name": "archived",
                "description": "an archived memory about caching",
                "lifecycle": {"state": "archived"},
            },
        )
        graph.upsert_node(
            node_id="structural-1",
            node_type=wk.OBSERVATION,
            properties={"name": "plumbing", "description": "a structural row"},
            node_role=NodeRole.STRUCTURAL.value,
        )
        pack = _build(registry)
        assert [i.item_id for i in pack.items] == ["memory-1"]  # type: ignore[attr-defined]
        withholding = pack.metadata["withholding"]  # type: ignore[attr-defined]
        assert withholding["by_reason"] == {"archived": 1, "noise": 1}
        assert "structural-1" not in withholding["withheld_item_ids"]


class TestTheOnlyObservableTheDropHas:
    def test_the_structural_filter_logs_what_it_removed(
        self, registry: StoreRegistry
    ) -> None:
        """Parity with the ``include_unconfirmed`` filter beside it. A debug
        line is not enough on its own — #404 said so — but it is strictly
        more than the nothing this drop had."""
        import structlog

        registry.knowledge.graph_store.upsert_node(
            node_id="structural-1",
            node_type=wk.OBSERVATION,
            properties={"name": "plumbing", "description": "a structural row"},
            node_role=NodeRole.STRUCTURAL.value,
        )
        with structlog.testing.capture_logs() as logs:
            GraphSearch(registry.knowledge.graph_store).search("caching")
        excluded = [
            entry
            for entry in logs
            if entry.get("event") == "graph_search_structural_excluded"
        ]
        assert len(excluded) == 1
        assert excluded[0]["excluded"] == 1

    def test_nothing_is_logged_when_nothing_is_structural(
        self, registry: StoreRegistry
    ) -> None:
        import structlog

        with structlog.testing.capture_logs() as logs:
            GraphSearch(registry.knowledge.graph_store).search("caching")
        assert not [
            e for e in logs if e.get("event") == "graph_search_structural_excluded"
        ]
