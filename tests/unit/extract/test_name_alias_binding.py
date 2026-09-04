"""Write-time name-alias binding and bounded backfill contracts."""

from __future__ import annotations

from pathlib import Path

import pytest

from trellis.extract import entity_resolution as resolution
from trellis.schemas.well_known import normalize_entity_name
from trellis.stores.sqlite.graph import SQLiteGraphStore


@pytest.fixture
def store(tmp_path: Path):
    graph = SQLiteGraphStore(tmp_path / "graph.db")
    yield graph
    graph.close()


def _add(store: SQLiteGraphStore, node_id: str, name: str) -> None:
    store.upsert_node(
        node_id=node_id,
        node_type="Concept",
        properties={"name": name},
    )


class TestBindNameAlias:
    def test_reports_each_sequential_first_wins_branch(self, store) -> None:
        _add(store, "first", "Hermes")
        _add(store, "second", "Hermes")

        assert (
            resolution.bind_name_alias(store, entity_id="first", name="Hermes")
            == "contested"
        )

        store.upsert_node("second", "Concept", {"name": "Other"})
        assert (
            resolution.bind_name_alias(store, entity_id="first", name="Hermes")
            == "bound"
        )
        assert (
            resolution.bind_name_alias(store, entity_id="first", name=" HERMES ")
            == "already_bound"
        )

        store.upsert_node("second", "Concept", {"name": "Hermes"})
        assert (
            resolution.bind_name_alias(store, entity_id="second", name="Hermes")
            == "kept_existing"
        )
        assert (
            resolution.bind_name_alias(store, entity_id="second", name=" ") == "skipped"
        )

        row = store.resolve_alias(resolution.NAME_ALIAS_SOURCE_SYSTEM, "hermes")
        assert row is not None
        assert row["entity_id"] == "first"

    def test_stale_binding_can_be_replaced(self, store) -> None:
        _add(store, "old", "Old Name")
        assert (
            resolution.bind_name_alias(store, entity_id="old", name="Old Name")
            == "bound"
        )
        store.upsert_node("old", "Concept", {"name": "Renamed"})
        _add(store, "new", "Old Name")

        assert (
            resolution.bind_name_alias(store, entity_id="new", name="Old Name")
            == "bound"
        )
        row = store.resolve_alias(resolution.NAME_ALIAS_SOURCE_SYSTEM, "old name")
        assert row is not None
        assert row["entity_id"] == "new"


class TestBackfillNameAliases:
    def _seed_mixed_population(self, store) -> None:
        _add(store, "alpha", "Alpha")
        _add(store, "beta", "  Beta   Name ")
        _add(store, "gamma", "GAMMA")
        _add(store, "twin-a", "Twin")
        _add(store, "twin-b", "Twin")
        _add(store, "blank", "   ")

    def test_binds_only_unique_normalized_names_and_is_idempotent(self, store) -> None:
        self._seed_mixed_population(store)

        first = resolution.backfill_name_aliases(store, max_nodes=6)

        assert first.bound == 3
        assert first.already_bound == 0
        assert first.contested_keys == ["twin"]
        assert first.skipped == 1
        assert first.truncated is False

        resolve = resolution.build_name_alias_resolver(store, scan_limit=1)
        assert resolve("ALPHA") == ["alpha"]
        assert resolve("beta name") == ["beta"]
        assert resolve("gamma") == ["gamma"]
        assert sorted(
            resolution.build_name_alias_resolver(store, scan_limit=10)("Twin")
        ) == ["twin-a", "twin-b"]

        second = resolution.backfill_name_aliases(store, max_nodes=6)
        assert second.bound == 0
        assert second.already_bound == 3
        assert second.contested_keys == ["twin"]
        assert second.skipped == 1
        assert second.truncated is False

    def test_truncated_population_binds_nothing(self, store) -> None:
        self._seed_mixed_population(store)

        report = resolution.backfill_name_aliases(store, max_nodes=2)

        assert report.bound == 0
        assert report.already_bound == 0
        assert report.truncated is True
        for name in ("Alpha", "Beta Name", "Gamma"):
            assert (
                store.resolve_alias(
                    resolution.NAME_ALIAS_SOURCE_SYSTEM,
                    normalize_entity_name(name),
                )
                is None
            )

        resumed = resolution.backfill_name_aliases(store, max_nodes=6)
        assert resumed.bound == 3
        assert resumed.truncated is False
