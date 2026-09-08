"""Governed normalized-name alias binding and backfill contracts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from trellis.mutate.commands import Command, CommandStatus, Operation
from trellis.mutate.executor import MutationExecutor
from trellis.mutate.handlers import create_curate_handlers
from trellis.mutate.name_aliases import (
    NAME_ALIAS_SOURCE_SYSTEM,
    backfill_name_aliases,
)
from trellis.mutate.policy_gate import DefaultPolicyGate
from trellis.schemas.enums import PolicyType
from trellis.schemas.policy import Policy, PolicyRule, PolicyScope
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry


@pytest.fixture
def registry(tmp_path: Path):
    stores = tmp_path / "stores"
    stores.mkdir()
    value = StoreRegistry(stores_dir=stores)
    yield value
    value.close()


def _executor(
    registry: StoreRegistry,
    *,
    policy_gate: DefaultPolicyGate | None = None,
) -> MutationExecutor:
    return MutationExecutor(
        event_log=registry.operational.event_log,
        handlers=create_curate_handlers(registry),
        policy_gate=policy_gate or DefaultPolicyGate(),
    )


def _add(registry: StoreRegistry, node_id: str, name: Any) -> None:
    registry.knowledge.graph_store.upsert_node(
        node_id=node_id,
        node_type="Concept",
        properties={"name": name},
    )


def _alias_command(*, entity_id: str = "alpha", key: str = "alpha") -> Command:
    return Command(
        operation=Operation.ALIAS_UPSERT,
        args={
            "entity_id": entity_id,
            "source_system": NAME_ALIAS_SOURCE_SYSTEM,
            "raw_id": key,
            "raw_name": "Alpha",
            "if_absent": True,
        },
        target_id=entity_id,
        target_type="alias",
        requested_by="test:name-alias",
        idempotency_key=f"test:{entity_id}:{key}",
    )


class TestGovernedAliasOperation:
    def test_success_is_audited_and_idempotent(self, registry: StoreRegistry) -> None:
        executor = _executor(registry)

        first = executor.execute(_alias_command())
        second = executor.execute(_alias_command())

        assert first.status is CommandStatus.SUCCESS
        assert first.message == "Alias bind outcome: bound"
        assert second.status is CommandStatus.DUPLICATE
        row = registry.knowledge.graph_store.resolve_alias(
            NAME_ALIAS_SOURCE_SYSTEM,
            "alpha",
        )
        assert row is not None
        assert row["entity_id"] == "alpha"
        executed = registry.operational.event_log.get_events(
            event_type=EventType.MUTATION_EXECUTED,
            entity_id="alpha",
        )
        rejected = registry.operational.event_log.get_events(
            event_type=EventType.MUTATION_REJECTED,
            entity_id="alpha",
        )
        assert [event.payload["operation"] for event in executed] == ["alias.upsert"]
        assert [event.payload["reason"] for event in rejected] == ["idempotency_replay"]

    def test_policy_denial_binds_nothing(self, registry: StoreRegistry) -> None:
        policy = Policy(
            policy_type=PolicyType.MUTATION,
            scope=PolicyScope(level="global"),
            rules=[PolicyRule(operation="alias.upsert", action="deny")],
        )
        executor = _executor(
            registry,
            policy_gate=DefaultPolicyGate(policies=[policy]),
        )

        result = executor.execute(_alias_command())

        assert result.status is CommandStatus.REJECTED
        assert (
            registry.knowledge.graph_store.resolve_alias(
                NAME_ALIAS_SOURCE_SYSTEM,
                "alpha",
            )
            is None
        )


class TestGovernedNameAliasBackfill:
    def _seed_mixed_population(self, registry: StoreRegistry) -> None:
        _add(registry, "alpha", "Alpha")
        _add(registry, "beta", "  Beta   Name ")
        _add(registry, "gamma", "GAMMA")
        _add(registry, "twin-a", "Twin")
        _add(registry, "twin-b", "Twin")
        _add(registry, "blank", "   ")

    def test_binds_unique_names_in_one_bounded_batch_and_reruns_cleanly(
        self, registry: StoreRegistry
    ) -> None:
        self._seed_mixed_population(registry)
        executor = _executor(registry)
        graph = registry.knowledge.graph_store

        first = backfill_name_aliases(graph, executor, max_nodes=6)
        second = backfill_name_aliases(graph, executor, max_nodes=6)

        assert first.bound == 3
        assert first.already_bound == 0
        assert first.contested == 1
        assert first.skipped == 1
        assert first.failed == 0
        assert first.commands_submitted == 3
        assert first.truncated is False
        assert second.bound == 0
        assert second.already_bound == 3
        assert second.failed == 0
        events = registry.operational.event_log.get_events(
            event_type=EventType.MUTATION_EXECUTED,
        )
        alias_events = [
            event
            for event in events
            if event.payload.get("operation") == "alias.upsert"
        ]
        assert len(alias_events) == 3

    def test_truncated_population_binds_nothing(self, registry: StoreRegistry) -> None:
        self._seed_mixed_population(registry)
        graph = registry.knowledge.graph_store

        report = backfill_name_aliases(graph, _executor(registry), max_nodes=2)

        assert report.truncated is True
        assert report.commands_submitted == 0
        assert report.bound == 0
        for key in ("alpha", "beta name", "gamma"):
            assert graph.resolve_alias(NAME_ALIAS_SOURCE_SYSTEM, key) is None

    def test_repairs_alias_whose_owner_renamed_away(
        self, registry: StoreRegistry
    ) -> None:
        graph = registry.knowledge.graph_store
        _add(registry, "former", "Old Name")
        graph.bind_alias_if_absent("former", NAME_ALIAS_SOURCE_SYSTEM, "old name")
        _add(registry, "former", "New Name")
        _add(registry, "successor", "Old Name")

        report = backfill_name_aliases(graph, _executor(registry), max_nodes=2)

        assert report.rebound == 1
        assert report.contested == 0
        assert report.failed == 0
        winner = graph.resolve_alias(NAME_ALIAS_SOURCE_SYSTEM, "old name")
        assert winner is not None
        assert winner["entity_id"] == "successor"

    def test_alias_write_outage_is_failed_not_skipped(
        self, registry: StoreRegistry, monkeypatch
    ) -> None:
        _add(registry, "alpha", "Alpha")
        graph = registry.knowledge.graph_store

        def _fail(*args: Any, **kwargs: Any) -> None:
            msg = "alias storage unavailable for secret Alpha"
            raise RuntimeError(msg)

        monkeypatch.setattr(graph, "bind_alias_if_absent", _fail)

        report = backfill_name_aliases(graph, _executor(registry), max_nodes=1)

        assert report.skipped == 0
        assert report.failed == 1
        assert report.failures[0].stage == "bind"
        assert report.failures[0].entity_id == "alpha"
        assert "Alpha" not in repr(report.failures)

    def test_snapshot_query_outage_is_reported_without_rows(
        self, registry: StoreRegistry, monkeypatch
    ) -> None:
        graph = registry.knowledge.graph_store

        def _fail(*args: Any, **kwargs: Any) -> None:
            msg = "query unavailable"
            raise RuntimeError(msg)

        monkeypatch.setattr(graph, "query", _fail)

        report = backfill_name_aliases(graph, _executor(registry), max_nodes=10)

        assert report.failed == 1
        assert report.failures[0].stage == "snapshot"
        assert report.failures[0].entity_id is None
        assert report.commands_submitted == 0

    def test_duplicate_lookup_outage_is_failed_not_already_bound(
        self, registry: StoreRegistry, monkeypatch
    ) -> None:
        _add(registry, "alpha", "Alpha")
        graph = registry.knowledge.graph_store
        executor = _executor(registry)
        assert backfill_name_aliases(graph, executor, max_nodes=1).bound == 1

        def _fail(*args: Any, **kwargs: Any) -> None:
            msg = "alias lookup unavailable"
            raise RuntimeError(msg)

        monkeypatch.setattr(graph, "resolve_alias", _fail)

        report = backfill_name_aliases(graph, executor, max_nodes=1)

        assert report.already_bound == 0
        assert report.failed == 1
        assert report.failures[0].stage == "verify"
        assert report.failures[0].reason == "store"

    def test_policy_denial_is_reported_as_failure(
        self, registry: StoreRegistry
    ) -> None:
        _add(registry, "alpha", "Alpha")
        policy = Policy(
            policy_type=PolicyType.MUTATION,
            scope=PolicyScope(level="global"),
            rules=[PolicyRule(operation="alias.upsert", action="deny")],
        )

        report = backfill_name_aliases(
            registry.knowledge.graph_store,
            _executor(
                registry,
                policy_gate=DefaultPolicyGate(policies=[policy]),
            ),
            max_nodes=1,
        )

        assert report.failed == 1
        assert report.failures[0].reason == "policy"
        assert (
            registry.knowledge.graph_store.resolve_alias(
                NAME_ALIAS_SOURCE_SYSTEM,
                "alpha",
            )
            is None
        )
