"""``trellis ingest dbt-manifest`` binds ``unique_id`` aliases only behind the knob."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from trellis.core.write_config import KNOWLEDGE_LINK_OR_MINT_ENV
from trellis_cli.main import app
from trellis_cli.stores import _reset_registry, get_graph_store

runner = CliRunner()

_MANIFEST = {
    "nodes": {
        "model.p.stg_orders": {
            "unique_id": "model.p.stg_orders",
            "resource_type": "model",
            "name": "stg_orders",
            "schema": "staging",
            "depends_on": {"nodes": ["source.p.raw.orders"]},
        },
    },
    "sources": {
        "source.p.raw.orders": {
            "unique_id": "source.p.raw.orders",
            "resource_type": "source",
            "name": "orders",
            "source_name": "raw",
            "schema": "public",
        },
    },
}


@pytest.fixture(autouse=True)
def _stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = tmp_path / "data"
    (data_dir / "stores").mkdir(parents=True)
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))
    monkeypatch.delenv(KNOWLEDGE_LINK_OR_MINT_ENV, raising=False)
    _reset_registry()


def _ingest(tmp_path: Path) -> dict:
    runner.invoke(app, ["admin", "init"])
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(_MANIFEST))
    result = runner.invoke(
        app, ["ingest", "dbt-manifest", str(manifest), "--format", "json"]
    )
    assert result.exit_code == 0, result.stdout
    return json.loads(result.stdout.strip())


def test_knob_off_binds_no_dbt_alias(tmp_path: Path) -> None:
    summary = _ingest(tmp_path)
    assert (summary["nodes"], summary["edges"]) == (2, 1)
    assert get_graph_store().resolve_alias("dbt", "model.p.stg_orders") is None


def test_knob_on_binds_the_alias_and_reports_the_same_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(KNOWLEDGE_LINK_OR_MINT_ENV, "1")
    summary = _ingest(tmp_path)
    assert (summary["nodes"], summary["edges"]) == (2, 1)
    row = get_graph_store().resolve_alias("dbt", "model.p.stg_orders")
    assert row is not None
    assert row["entity_id"] == "model.p.stg_orders"
