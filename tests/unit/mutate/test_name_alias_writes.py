"""Governed entity writes maintain the normalized name-alias index."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs

from trellis.extract.entity_resolution import (
    NAME_ALIAS_SOURCE_SYSTEM,
    build_name_alias_resolver,
)
from trellis.mutate import build_curate_executor
from trellis.mutate.commands import Command, CommandStatus, Operation
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry


@pytest.fixture
def registry(tmp_path: Path):
    stores = tmp_path / "stores"
    stores.mkdir()
    value = StoreRegistry(stores_dir=stores)
    yield value
    value.close()


def _execute(
    registry: StoreRegistry,
    *,
    operation: Operation = Operation.ENTITY_CREATE,
    entity_id: str,
    name: str | None = None,
    properties: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> None:
    args: dict[str, Any] = {"entity_id": entity_id}
    if name is not None:
        args["name"] = name
    if properties is not None:
        args["properties"] = properties
    if operation == Operation.ENTITY_CREATE:
        args["entity_type"] = "Concept"
    result = build_curate_executor(registry).execute(
        Command(
            operation=operation,
            args=args,
            target_id=entity_id,
            requested_by="test:name-alias",
            idempotency_key=idempotency_key,
        )
    )
    assert result.status == CommandStatus.SUCCESS


class TestGovernedNameAliasWrites:
    def test_oldest_name_resolves_beyond_scan_cap_without_scan(
        self, registry: StoreRegistry, monkeypatch
    ) -> None:
        _execute(registry, entity_id="target", name="  Alpha   Target ")
        for i, name in enumerate(("Beta", "GAMMA", "Delta", "epsilon"), start=1):
            _execute(registry, entity_id=f"filler-{i}", name=name)

        graph = registry.knowledge.graph_store

        def _scan_must_not_run(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            msg = "indexed write-time alias must avoid the bounded scan"
            raise AssertionError(msg)

        monkeypatch.setattr(graph, "query", _scan_must_not_run)
        resolve = build_name_alias_resolver(graph, scan_limit=3)

        assert resolve("ALPHA TARGET") == ["target"]

    def test_update_and_create_upsert_bind_the_new_name(
        self, registry: StoreRegistry
    ) -> None:
        _execute(registry, entity_id="target", name="Old Name")
        for i in range(4):
            _execute(registry, entity_id=f"filler-{i}", name=f"Filler {i}")

        _execute(
            registry,
            operation=Operation.ENTITY_UPDATE,
            entity_id="target",
            name="New Name",
        )
        resolve = build_name_alias_resolver(
            registry.knowledge.graph_store,
            scan_limit=1,
        )
        assert resolve("NEW NAME") == ["target"]
        assert resolve("Old Name") == []

        _execute(registry, entity_id="target", name="Newest Name")
        assert resolve("newest name") == ["target"]

    def test_properties_name_rename_binds_final_merged_name(
        self, registry: StoreRegistry
    ) -> None:
        _execute(registry, entity_id="target", name="Old Name")

        _execute(
            registry,
            operation=Operation.ENTITY_UPDATE,
            entity_id="target",
            properties={"name": "Properties Name"},
        )

        row = registry.knowledge.graph_store.resolve_alias(
            NAME_ALIAS_SOURCE_SYSTEM,
            "properties name",
        )
        assert row is not None
        assert row["entity_id"] == "target"

    def test_top_level_name_wins_over_properties_name_for_binding(
        self, registry: StoreRegistry
    ) -> None:
        _execute(registry, entity_id="target", name="Old Name")

        _execute(
            registry,
            operation=Operation.ENTITY_UPDATE,
            entity_id="target",
            name="Top Level Name",
            properties={"name": "Nested Name"},
        )

        graph = registry.knowledge.graph_store
        winner = graph.resolve_alias(NAME_ALIAS_SOURCE_SYSTEM, "top level name")
        assert winner is not None
        assert winner["entity_id"] == "target"
        assert graph.resolve_alias(NAME_ALIAS_SOURCE_SYSTEM, "nested name") is None

    @pytest.mark.parametrize(
        "supplied_name",
        [
            {"name": "Stable Name"},
            {"properties": {"name": "Stable Name"}},
        ],
    )
    def test_same_value_retry_repairs_failed_alias_bind(
        self,
        registry: StoreRegistry,
        monkeypatch,
        supplied_name: dict[str, Any],
    ) -> None:
        graph = registry.knowledge.graph_store
        graph.upsert_node("target", "Concept", {"name": "Stable Name"})
        calls = 0
        original = graph.bind_alias_if_absent

        def _fail_once(*args: Any, **kwargs: Any):
            nonlocal calls
            calls += 1
            if calls == 1:
                msg = "transient alias outage"
                raise RuntimeError(msg)
            return original(*args, **kwargs)

        monkeypatch.setattr(graph, "bind_alias_if_absent", _fail_once)
        _execute(
            registry,
            operation=Operation.ENTITY_UPDATE,
            entity_id="target",
            **supplied_name,
        )
        assert graph.resolve_alias(NAME_ALIAS_SOURCE_SYSTEM, "stable name") is None

        _execute(
            registry,
            operation=Operation.ENTITY_UPDATE,
            entity_id="target",
            **supplied_name,
        )

        assert calls == 2
        winner = graph.resolve_alias(NAME_ALIAS_SOURCE_SYSTEM, "stable name")
        assert winner is not None
        assert winner["entity_id"] == "target"
        assert len(graph.get_aliases("target", source_system="name")) == 1
        assert len(graph.get_node_history("target")) == 3
        updates = registry.operational.event_log.get_events(
            event_type=EventType.ENTITY_UPDATED,
            entity_id="target",
        )
        assert len(updates) == 2

    def test_renamed_away_alias_rebinds_beyond_scan_cap(
        self, registry: StoreRegistry, monkeypatch
    ) -> None:
        _execute(registry, entity_id="former", name="Old Name")
        _execute(
            registry,
            operation=Operation.ENTITY_UPDATE,
            entity_id="former",
            name="New Name",
        )
        for index in range(4):
            _execute(registry, entity_id=f"filler-{index}", name=f"Filler {index}")
        _execute(registry, entity_id="successor", name="Old Name")
        graph = registry.knowledge.graph_store

        def _scan_must_not_run(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            msg = "repaired alias must avoid the bounded scan"
            raise AssertionError(msg)

        monkeypatch.setattr(graph, "query", _scan_must_not_run)
        resolve = build_name_alias_resolver(graph, scan_limit=1)

        assert resolve("old name") == ["successor"]

    def test_same_named_creates_keep_the_first_binding(
        self, registry: StoreRegistry
    ) -> None:
        _execute(registry, entity_id="first", name="Hermes")
        _execute(registry, entity_id="second", name="Hermes")

        row = registry.knowledge.graph_store.resolve_alias(
            NAME_ALIAS_SOURCE_SYSTEM,
            "hermes",
        )
        assert row is not None
        assert row["entity_id"] == "first"

    def test_existing_unbound_twins_make_new_write_contested(
        self, registry: StoreRegistry
    ) -> None:
        graph = registry.knowledge.graph_store
        graph.upsert_node("twin-a", "Concept", {"name": "Hermes"})
        graph.upsert_node("twin-b", "Concept", {"name": "Hermes"})

        _execute(registry, entity_id="third", name="Hermes")

        assert graph.resolve_alias(NAME_ALIAS_SOURCE_SYSTEM, "hermes") is None
        assert sorted(build_name_alias_resolver(graph, scan_limit=10)("Hermes")) == [
            "third",
            "twin-a",
            "twin-b",
        ]

    @pytest.mark.parametrize(
        ("method", "event"),
        [
            ("query", "entity_resolution_name_alias_twin_check_failed"),
            ("bind_alias_if_absent", "entity_resolution_alias_bind_failed"),
        ],
    )
    def test_alias_maintenance_failure_does_not_fail_entity_write(
        self,
        registry: StoreRegistry,
        monkeypatch,
        method: str,
        event: str,
    ) -> None:
        graph = registry.knowledge.graph_store

        def _fail(*args: Any, **kwargs: Any) -> None:
            msg = "alias index unavailable"
            raise RuntimeError(msg)

        monkeypatch.setattr(graph, method, _fail)
        with capture_logs() as logs:
            _execute(registry, entity_id="survives", name="Survives")

        assert graph.get_node("survives") is not None
        assert any(row["event"] == event for row in logs)
        assert registry.operational.event_log.get_events(
            event_type=EventType.ENTITY_CREATED,
            entity_id="survives",
        )
        assert registry.operational.event_log.get_events(
            event_type=EventType.MUTATION_EXECUTED,
            entity_id="survives",
        )
