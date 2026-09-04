"""CLI contract for the bounded name-alias backfill."""

from __future__ import annotations

import json
from typing import Any

from typer.testing import CliRunner

from trellis.mutate.name_aliases import NAME_ALIAS_SOURCE_SYSTEM
from trellis.schemas.enums import PolicyType
from trellis.schemas.policy import Policy, PolicyRule, PolicyScope
from trellis.stores.policy_store import PolicyStore
from trellis.stores.registry import StoreRegistry
from trellis.stores.sqlite.graph import SQLiteGraphStore
from trellis_cli.main import app
from trellis_cli.stores import _reset_registry

runner = CliRunner()


def _seed(tmp_path, monkeypatch, names: list[tuple[str, str]]):
    data_dir = tmp_path / "data"
    stores_dir = data_dir / "stores"
    stores_dir.mkdir(parents=True)
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    registry = StoreRegistry(stores_dir=stores_dir)
    graph = registry.knowledge.graph_store
    for node_id, name in names:
        graph.upsert_node(node_id, "Concept", {"name": name})
    registry.close()
    _reset_registry()
    return stores_dir


def _open_graph(stores_dir):
    registry = StoreRegistry(stores_dir=stores_dir)
    return registry, registry.knowledge.graph_store


class TestBackfillNameAliasesCommand:
    def test_json_success_and_idempotent_rerun(self, tmp_path, monkeypatch) -> None:
        stores_dir = _seed(
            tmp_path,
            monkeypatch,
            [
                ("alpha", "Alpha"),
                ("beta", "Beta Name"),
                ("twin-a", "Twin"),
                ("twin-b", "Twin"),
                ("blank", " "),
            ],
        )

        first = runner.invoke(
            app,
            ["admin", "backfill-name-aliases", "--format", "json"],
        )
        assert first.exit_code == 0, first.output
        payload = json.loads(first.stdout)
        assert payload == {
            "status": "ok",
            "max_nodes": 6,
            "bound": 2,
            "already_bound": 0,
            "contested": 1,
            "skipped": 1,
            "failed": 0,
            "failures": [],
            "commands_submitted": 2,
            "truncated": False,
        }

        _reset_registry()
        second = runner.invoke(
            app,
            ["admin", "backfill-name-aliases", "--format", "json"],
        )
        assert second.exit_code == 0, second.output
        rerun = json.loads(second.stdout)
        assert rerun["bound"] == 0
        assert rerun["already_bound"] == 2

        _reset_registry()
        registry, graph = _open_graph(stores_dir)
        try:
            assert graph.resolve_alias(NAME_ALIAS_SOURCE_SYSTEM, "alpha") is not None
            assert (
                graph.resolve_alias(NAME_ALIAS_SOURCE_SYSTEM, "beta name") is not None
            )
            assert graph.resolve_alias(NAME_ALIAS_SOURCE_SYSTEM, "twin") is None
        finally:
            registry.close()

    def test_truncation_binds_nothing_and_formats_share_exit(
        self, tmp_path, monkeypatch
    ) -> None:
        stores_dir = _seed(
            tmp_path,
            monkeypatch,
            [(f"node-{i}", f"Name {i}") for i in range(5)],
        )

        text = runner.invoke(
            app,
            ["admin", "backfill-name-aliases", "--max-nodes", "2"],
        )
        _reset_registry()
        machine = runner.invoke(
            app,
            [
                "admin",
                "backfill-name-aliases",
                "--max-nodes",
                "2",
                "--format",
                "json",
            ],
        )

        assert text.exit_code == machine.exit_code == 2
        payload = json.loads(machine.stdout)
        assert payload["status"] == "error"
        assert payload["bound"] == 0
        assert payload["truncated"] is True

        _reset_registry()
        registry, graph = _open_graph(stores_dir)
        try:
            assert graph.get_aliases("node-0", NAME_ALIAS_SOURCE_SYSTEM) == []
            assert graph.get_aliases("node-4", NAME_ALIAS_SOURCE_SYSTEM) == []
        finally:
            registry.close()

    def test_rejects_unsupported_format_before_opening_stores(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "missing"))

        result = runner.invoke(
            app,
            ["admin", "backfill-name-aliases", "--format", "yaml"],
        )

        assert result.exit_code == 2
        assert "expected one of: text, json" in result.stdout

    def test_alias_outage_is_nonzero_error_in_both_formats(
        self, tmp_path, monkeypatch
    ) -> None:
        _seed(tmp_path, monkeypatch, [("alpha", "Secret Alpha")])

        def _fail(*args: Any, **kwargs: Any) -> None:
            msg = "alias outage contains Secret Alpha"
            raise RuntimeError(msg)

        monkeypatch.setattr(SQLiteGraphStore, "bind_alias_if_absent", _fail)
        text = runner.invoke(app, ["admin", "backfill-name-aliases"])
        _reset_registry()
        machine = runner.invoke(
            app,
            ["admin", "backfill-name-aliases", "--format", "json"],
        )

        assert text.exit_code == machine.exit_code == 5
        payload = json.loads(machine.stdout)
        assert payload["status"] == "error"
        assert payload["failed"] == 1
        assert payload["skipped"] == 0
        assert "Secret Alpha" not in machine.stdout

    def test_mixed_alias_outage_reports_partial(self, tmp_path, monkeypatch) -> None:
        _seed(
            tmp_path,
            monkeypatch,
            [("good", "Good"), ("bad", "Secret Bad")],
        )
        original = SQLiteGraphStore.bind_alias_if_absent

        def _sometimes_fail(
            store: SQLiteGraphStore,
            entity_id: str,
            *args: Any,
            **kwargs: Any,
        ) -> Any:
            if entity_id == "bad":
                msg = "alias outage contains Secret Bad"
                raise RuntimeError(msg)
            return original(store, entity_id, *args, **kwargs)

        monkeypatch.setattr(
            SQLiteGraphStore,
            "bind_alias_if_absent",
            _sometimes_fail,
        )
        result = runner.invoke(
            app,
            ["admin", "backfill-name-aliases", "--format", "json"],
        )

        assert result.exit_code == 5
        payload = json.loads(result.stdout)
        assert payload["status"] == "partial"
        assert payload["bound"] == 1
        assert payload["failed"] == 1
        assert "Secret Bad" not in result.stdout

    def test_policy_denial_uses_policy_exit(self, tmp_path, monkeypatch) -> None:
        stores_dir = _seed(tmp_path, monkeypatch, [("alpha", "Alpha")])
        PolicyStore(stores_dir / "policies.json").add(
            Policy(
                policy_type=PolicyType.MUTATION,
                scope=PolicyScope(level="global"),
                rules=[PolicyRule(operation="alias.upsert", action="deny")],
            )
        )
        _reset_registry()

        result = runner.invoke(
            app,
            ["admin", "backfill-name-aliases", "--format", "json"],
        )

        assert result.exit_code == 3
        payload = json.loads(result.stdout)
        assert payload["status"] == "error"
        assert payload["failed"] == 1
        assert payload["failures"][0]["reason"] == "policy"
