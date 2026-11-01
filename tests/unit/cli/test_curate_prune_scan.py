"""``trellis curate prune`` says when the entity scan hit its cap.

The capped scan is a property of the pass, not a failure of it: the command
still exits ``0`` in both output formats and names the cap in its message,
which is the only place the CLI carries the resolver's ``scan_truncated``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.cli_output import plain
from trellis.mutate import handlers as handlers_module
from trellis.mutate import retention as retention_module
from trellis.schemas.extraction import (
    EXTRACTION_STATUS_PROPERTY,
    EXTRACTION_STATUS_UNCONFIRMED,
)
from trellis.stores.registry import StoreRegistry
from trellis_cli.main import app

runner = CliRunner()

_ARGV = ["curate", "prune", "--reason", "r", "--unconfirmed-mints"]


@pytest.fixture
def stores_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the CLI at a temp data dir holding three young mints."""
    data_dir = tmp_path / "data"
    stores = data_dir / "stores"
    stores.mkdir(parents=True)
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))
    registry = StoreRegistry(stores_dir=stores)
    for i in range(3):
        registry.knowledge.graph_store.upsert_node(
            node_id=f"m{i}",
            node_type="concept",
            properties={
                "name": f"m{i}",
                EXTRACTION_STATUS_PROPERTY: EXTRACTION_STATUS_UNCONFIRMED,
            },
        )
    registry.close()
    return stores


def _cap(monkeypatch: pytest.MonkeyPatch, cap: int) -> None:
    monkeypatch.setattr(retention_module, "MAX_ENTITIES_SCANNED", cap)
    monkeypatch.setattr(handlers_module, "MAX_ENTITIES_SCANNED", cap)
    monkeypatch.setattr(retention_module, "_SCAN_PAGE", 2)


def test_json_says_the_scan_stopped_early_and_exits_zero(
    stores_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cap(monkeypatch, 2)
    result = runner.invoke(app, [*_ARGV, "--format", "json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output.strip())
    assert data["status"] == "success"
    assert "entity scan stopped early (the 2-entity cap," in data["message"]


def test_text_says_the_scan_stopped_early_and_exits_zero(
    stores_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cap(monkeypatch, 2)
    result = runner.invoke(app, _ARGV)
    assert result.exit_code == 0, result.output
    text = " ".join(plain(result.output).split())
    assert "entity scan stopped early (the 2-entity cap," in text


@pytest.mark.parametrize("fmt", ["json", "text"])
def test_a_complete_scan_says_nothing_about_stopping_early(
    stores_dir: Path, monkeypatch: pytest.MonkeyPatch, fmt: str
) -> None:
    _cap(monkeypatch, 10)
    result = runner.invoke(app, [*_ARGV, "--format", fmt])
    assert result.exit_code == 0, result.output
    assert "stopped early" not in " ".join(plain(result.output).split())
