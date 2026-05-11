"""Tests for ``trellis extract refresh`` CLI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from trellis_cli.extract_refresh import _property_diff
from trellis_cli.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _temp_stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point CLI stores at a temp directory."""
    data_dir = tmp_path / "data"
    (data_dir / "stores").mkdir(parents=True)
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))


_SAMPLE_MANIFEST_V1: dict = {
    "metadata": {"adapter_type": "snowflake"},
    "nodes": {
        "model.p.fct_orders": {
            "unique_id": "model.p.fct_orders",
            "resource_type": "model",
            "name": "fct_orders",
            "schema": "marts",
            "database": "analytics",
            "description": "V1 description",
            "config": {"materialized": "table"},
        },
    },
}

_SAMPLE_MANIFEST_V2: dict = {
    "metadata": {"adapter_type": "snowflake"},
    "nodes": {
        "model.p.fct_orders": {
            "unique_id": "model.p.fct_orders",
            "resource_type": "model",
            "name": "fct_orders",
            "schema": "marts",
            "database": "analytics",
            "description": "V2 description — drift",  # ← changed
            "config": {"materialized": "table"},
        },
        "model.p.new_model": {  # ← new entity
            "unique_id": "model.p.new_model",
            "resource_type": "model",
            "name": "new_model",
            "schema": "staging",
            "database": "analytics",
            "config": {"materialized": "view"},
        },
    },
}


# ---------------------------------------------------------------------------
# _property_diff unit tests
# ---------------------------------------------------------------------------


class TestPropertyDiff:
    def test_unchanged_returns_empty(self) -> None:
        before = {"properties": {"a": 1, "b": "x"}}
        after = {"properties": {"a": 1, "b": "x"}}
        assert _property_diff(before, after) == {}

    def test_new_entity_signals_new(self) -> None:
        diff = _property_diff(None, {"properties": {"a": 1}})
        assert diff["new_entity"] is True
        assert diff["added"] == {"a": 1}

    def test_changed_value_in_changed_field(self) -> None:
        before = {"properties": {"a": 1, "b": "x"}}
        after = {"properties": {"a": 1, "b": "y"}}
        diff = _property_diff(before, after)
        assert diff == {"changed": {"b": ["x", "y"]}}

    def test_added_and_removed_properties(self) -> None:
        before = {"properties": {"a": 1, "old": "gone"}}
        after = {"properties": {"a": 1, "new": "present"}}
        diff = _property_diff(before, after)
        assert diff == {
            "added": {"new": "present"},
            "removed": {"old": "gone"},
        }

    def test_deleted_entity_signals_deleted(self) -> None:
        before = {"properties": {"a": 1}}
        diff = _property_diff(before, None)
        assert diff["deleted_entity"] is True
        assert diff["removed"] == {"a": 1}

    def test_both_none_is_empty(self) -> None:
        assert _property_diff(None, None) == {}


# ---------------------------------------------------------------------------
# CLI argument validation
# ---------------------------------------------------------------------------


class TestRefreshCliValidation:
    def test_neither_source_nor_type_errors(self) -> None:
        runner.invoke(app, ["admin", "init"])
        result = runner.invoke(app, ["extract", "refresh"])
        assert result.exit_code == 1
        assert "exactly one of" in result.stdout.lower()

    def test_both_source_and_type_errors(self) -> None:
        runner.invoke(app, ["admin", "init"])
        result = runner.invoke(
            app,
            [
                "extract",
                "refresh",
                "--source",
                "x",
                "--type",
                "dbt-manifest",
                "--path",
                "/tmp/x",
            ],
        )
        assert result.exit_code == 1
        assert "exactly one of" in result.stdout.lower()

    def test_type_without_path_errors(self) -> None:
        runner.invoke(app, ["admin", "init"])
        result = runner.invoke(
            app, ["extract", "refresh", "--type", "dbt-manifest"]
        )
        assert result.exit_code == 1
        assert "--path" in result.stdout

    def test_source_not_in_yaml_errors(self, tmp_path: Path) -> None:
        runner.invoke(app, ["admin", "init"])
        sources_yaml = tmp_path / "sources.yaml"
        sources_yaml.write_text("sources: []\n")
        result = runner.invoke(
            app,
            [
                "extract",
                "refresh",
                "--source",
                "missing",
                "--sources-file",
                str(sources_yaml),
            ],
        )
        assert result.exit_code == 1
        assert "missing" in result.stdout.lower()

    def test_sources_file_not_found_errors(self, tmp_path: Path) -> None:
        runner.invoke(app, ["admin", "init"])
        result = runner.invoke(
            app,
            [
                "extract",
                "refresh",
                "--source",
                "any",
                "--sources-file",
                str(tmp_path / "missing.yaml"),
            ],
        )
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# End-to-end refresh paths
# ---------------------------------------------------------------------------


class TestRefreshEndToEnd:
    def test_type_path_refresh_new_entity(self, tmp_path: Path) -> None:
        runner.invoke(app, ["admin", "init"])
        manifest = tmp_path / "manifest.json"
        manifest.write_text(json.dumps(_SAMPLE_MANIFEST_V1))
        result = runner.invoke(
            app,
            [
                "extract",
                "refresh",
                "--type",
                "dbt-manifest",
                "--path",
                str(manifest),
                "--format",
                "json",
            ],
        )
        assert result.exit_code == 0, result.stdout
        data = json.loads(result.stdout.strip())
        assert data["status"] == "refreshed"
        assert data["entities_scanned"] == 1
        # All 1 entity is new since the graph was empty.
        assert data["new_entities"] == 1
        assert data["changed_entities"] == 0
        assert data["unchanged_entities"] == 0
        # The single diff should mark new_entity.
        assert len(data["diffs"]) == 1
        assert data["diffs"][0]["diff"]["new_entity"] is True

    def test_second_refresh_with_drift_detects_change(self, tmp_path: Path) -> None:
        runner.invoke(app, ["admin", "init"])
        manifest = tmp_path / "manifest.json"
        # Initial ingest.
        manifest.write_text(json.dumps(_SAMPLE_MANIFEST_V1))
        first = runner.invoke(
            app,
            [
                "extract",
                "refresh",
                "--type",
                "dbt-manifest",
                "--path",
                str(manifest),
                "--format",
                "json",
            ],
        )
        assert first.exit_code == 0, first.stdout
        # Edit the manifest: changed description + new model added.
        manifest.write_text(json.dumps(_SAMPLE_MANIFEST_V2))
        second = runner.invoke(
            app,
            [
                "extract",
                "refresh",
                "--type",
                "dbt-manifest",
                "--path",
                str(manifest),
                "--format",
                "json",
            ],
        )
        assert second.exit_code == 0, second.stdout
        data = json.loads(second.stdout.strip())
        assert data["entities_scanned"] == 2
        assert data["new_entities"] == 1  # new_model
        # fct_orders changed its description; expect at least one change.
        assert data["changed_entities"] >= 1
        # Pull the changed-entity diff and check the description swap.
        fct_diff = next(
            d for d in data["diffs"] if d["entity_id"] == "model.p.fct_orders"
        )
        assert "changed" in fct_diff["diff"]
        assert "description" in fct_diff["diff"]["changed"]
        before, after = fct_diff["diff"]["changed"]["description"]
        assert before == "V1 description"
        assert after == "V2 description — drift"

    def test_idempotent_refresh_no_changes(self, tmp_path: Path) -> None:
        runner.invoke(app, ["admin", "init"])
        manifest = tmp_path / "manifest.json"
        manifest.write_text(json.dumps(_SAMPLE_MANIFEST_V1))
        # Two consecutive refreshes against an unchanged input.
        runner.invoke(
            app,
            [
                "extract",
                "refresh",
                "--type",
                "dbt-manifest",
                "--path",
                str(manifest),
                "--format",
                "json",
            ],
        )
        result = runner.invoke(
            app,
            [
                "extract",
                "refresh",
                "--type",
                "dbt-manifest",
                "--path",
                str(manifest),
                "--format",
                "json",
            ],
        )
        assert result.exit_code == 0, result.stdout
        data = json.loads(result.stdout.strip())
        # Second run should produce no diffs.
        assert data["new_entities"] == 0
        assert data["changed_entities"] == 0
        assert data["unchanged_entities"] == 1
        assert data["diffs"] == []

    def test_sources_yaml_path_refresh(self, tmp_path: Path) -> None:
        runner.invoke(app, ["admin", "init"])
        manifest = tmp_path / "manifest.json"
        manifest.write_text(json.dumps(_SAMPLE_MANIFEST_V1))
        sources_yaml = tmp_path / "sources.yaml"
        sources_yaml.write_text(
            "sources:\n"
            "  - name: jaffle\n"
            "    type: dbt-manifest\n"
            f"    path: {manifest}\n"
        )
        result = runner.invoke(
            app,
            [
                "extract",
                "refresh",
                "--source",
                "jaffle",
                "--sources-file",
                str(sources_yaml),
                "--format",
                "json",
            ],
        )
        assert result.exit_code == 0, result.stdout
        data = json.loads(result.stdout.strip())
        assert data["source"] == "jaffle"
        assert data["new_entities"] == 1

    def test_disabled_source_refuses(self, tmp_path: Path) -> None:
        runner.invoke(app, ["admin", "init"])
        manifest = tmp_path / "manifest.json"
        manifest.write_text(json.dumps(_SAMPLE_MANIFEST_V1))
        sources_yaml = tmp_path / "sources.yaml"
        sources_yaml.write_text(
            "sources:\n"
            "  - name: jaffle\n"
            "    type: dbt-manifest\n"
            f"    path: {manifest}\n"
            "    enabled: false\n"
        )
        result = runner.invoke(
            app,
            [
                "extract",
                "refresh",
                "--source",
                "jaffle",
                "--sources-file",
                str(sources_yaml),
            ],
        )
        assert result.exit_code == 1
        assert "disabled" in result.stdout.lower()

    def test_endpoint_source_refuses_with_helpful_message(
        self, tmp_path: Path
    ) -> None:
        runner.invoke(app, ["admin", "init"])
        sources_yaml = tmp_path / "sources.yaml"
        sources_yaml.write_text(
            "sources:\n"
            "  - name: streaming\n"
            "    type: openlineage\n"
            "    endpoint: https://example.com/api\n"
        )
        result = runner.invoke(
            app,
            [
                "extract",
                "refresh",
                "--source",
                "streaming",
                "--sources-file",
                str(sources_yaml),
            ],
        )
        assert result.exit_code == 1
        assert "endpoint" in result.stdout.lower()
        assert "post" in result.stdout.lower()
