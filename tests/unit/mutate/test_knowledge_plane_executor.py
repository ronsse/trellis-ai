"""Knowledge-plane-only governed mutations via build_curate_executor (#196).

A consumer that runs only Knowledge-Plane writes (graph / vector) with no
Operational-Plane persistence wires ``event_log: {backend: null}``. The curate
executor and its handlers — which emit through
``registry.operational.event_log`` — must then run ENTITY_CREATE / LINK_CREATE
end-to-end with mutation-event emission as an intentional no-op, no
``event_log=None`` special-casing, and no monkey patch.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from trellis.mutate import build_curate_executor
from trellis.mutate.commands import Command, CommandStatus, Operation
from trellis.stores.null.event_log import NullEventLog
from trellis.stores.registry import StoreRegistry


@pytest.fixture
def kp_registry(tmp_path: Path) -> StoreRegistry:
    """A knowledge-plane-only registry: sqlite graph, null event log."""
    return StoreRegistry(
        config={"event_log": {"backend": "null"}},
        stores_dir=tmp_path / "stores",
    )


def _create_entity(executor, name: str) -> str:
    result = executor.execute(
        Command(
            operation=Operation.ENTITY_CREATE,
            args={"entity_type": "table", "name": name},
        )
    )
    assert result.status == CommandStatus.SUCCESS
    assert result.created_id is not None
    return result.created_id


class TestKnowledgePlaneOnlyExecutor:
    def test_event_log_is_null(self, kp_registry: StoreRegistry) -> None:
        assert isinstance(kp_registry.operational.event_log, NullEventLog)

    def test_entity_and_link_create_end_to_end(
        self, kp_registry: StoreRegistry
    ) -> None:
        executor = build_curate_executor(kp_registry)

        id_a = _create_entity(executor, "events")
        id_b = _create_entity(executor, "users")

        link = executor.execute(
            Command(
                operation=Operation.LINK_CREATE,
                args={
                    "source_id": id_a,
                    "target_id": id_b,
                    "edge_kind": "references_table",
                },
            )
        )
        assert link.status == CommandStatus.SUCCESS
        assert link.created_id is not None

        # Knowledge-Plane writes really landed.
        graph = kp_registry.knowledge.graph_store
        assert graph.get_node(id_a) is not None
        assert graph.get_node(id_b) is not None

        # ...and emission was a no-op: nothing persisted to the event log,
        # even though the executor + handlers each "emit" per mutation.
        assert kp_registry.operational.event_log.count() == 0
        assert kp_registry.operational.event_log.get_events() == []

    def test_allow_dangling_link_knowledge_plane_only(
        self, kp_registry: StoreRegistry
    ) -> None:
        """#211 + #196: opt-in dangling edge works with no operational plane."""
        executor = build_curate_executor(kp_registry)
        id_a = _create_entity(executor, "src")

        link = executor.execute(
            Command(
                operation=Operation.LINK_CREATE,
                args={
                    "source_id": id_a,
                    "target_id": "tbl-not-materialised",
                    "edge_kind": "references_table",
                    "allow_dangling": True,
                },
            )
        )
        assert link.status == CommandStatus.SUCCESS
        assert kp_registry.operational.event_log.count() == 0
