"""``trellis ingest evidence`` stores the evidence's ``uri`` in metadata (#1250)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from trellis_cli.main import app
from trellis_cli.stores import get_document_store

runner = CliRunner()

URI = "https://docs.example.test/kiln/handbook"


@pytest.fixture(autouse=True)
def _temp_stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = tmp_path / "data"
    (data_dir / "stores").mkdir(parents=True)
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))


def _write(tmp_path: Path, **extra: str) -> str:
    path = tmp_path / "evidence.json"
    path.write_text(
        json.dumps(
            {
                "evidence_id": "ev-uri-1",
                "evidence_type": "snippet",
                "content": "an invented snippet about kilns",
                "source_origin": "trace",
                **extra,
            }
        )
    )
    return str(path)


def test_the_uri_reaches_the_stored_metadata(tmp_path: Path) -> None:
    result = runner.invoke(app, ["ingest", "evidence", _write(tmp_path, uri=URI)])
    assert result.exit_code == 0
    stored = get_document_store().get("ev-uri-1")
    assert stored is not None
    assert stored["metadata"]["uri"] == URI


def test_no_uri_means_no_key(tmp_path: Path) -> None:
    result = runner.invoke(app, ["ingest", "evidence", _write(tmp_path)])
    assert result.exit_code == 0
    stored = get_document_store().get("ev-uri-1")
    assert stored is not None
    assert "uri" not in stored["metadata"]
