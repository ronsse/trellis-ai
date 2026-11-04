"""``trellis extract refresh --type unity-catalog``: files, directories, refusals."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from trellis_cli.main import app

runner = CliRunner()

_TABLES = {
    "tables": [
        {
            "name": "orders",
            "catalog_name": "main",
            "schema_name": "sales",
            "full_name": "main.sales.orders",
            "table_type": "MANAGED",
            "updated_at": 1700000000000,
            "columns": [{"name": "order_id", "type_text": "bigint", "position": 0}],
        }
    ]
}
_LINEAGE = [
    {
        "source_table_full_name": "main.raw.events",
        "target_table_full_name": "main.sales.orders",
        "event_time": "2024-01-06T10:00:00.000+00:00",
    }
]


@pytest.fixture(autouse=True)
def _temp_stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = tmp_path / "data"
    (data_dir / "stores").mkdir(parents=True)
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))
    runner.invoke(app, ["admin", "init"])


def _refresh(path: Path, fmt: str = "json") -> Any:
    return runner.invoke(
        app,
        [
            *("extract", "refresh", "--type", "unity-catalog", "--path", str(path)),
            *("--format", fmt),
        ],
    )


def _export_dir(tmp_path: Path) -> Path:
    export = tmp_path / "export"
    export.mkdir()
    (export / "tables.json").write_text(json.dumps(_TABLES))
    (export / "lineage.json").write_text(json.dumps(_LINEAGE))
    (export / "notes.txt").write_text("not part of the export")
    return export


class TestRefresh:
    def test_directory_refresh_writes_then_is_idempotent(self, tmp_path: Path) -> None:
        export = _export_dir(tmp_path)
        first = _refresh(export)
        assert first.exit_code == 0, first.stdout
        data = json.loads(first.stdout.strip())
        assert data["status"] == "refreshed"
        # catalog, two schemas, two tables, one column
        assert data["entities_scanned"] == data["new_entities"] == 6

        second = json.loads(_refresh(export).stdout.strip())
        assert second["new_entities"] == 0
        assert second["changed_entities"] == 0
        assert second["unchanged_entities"] == 6
        assert second["diffs"] == []

    def test_single_file_in_either_layout(self, tmp_path: Path) -> None:
        wrapped = tmp_path / "wrapped.json"
        wrapped.write_text(json.dumps(_TABLES))
        lines = tmp_path / "lineage.ndjson"
        lines.write_text("\n".join(json.dumps(row) for row in _LINEAGE) + "\n")
        for path in (wrapped, lines):
            result = _refresh(path)
            assert result.exit_code == 0, result.stdout
            assert json.loads(result.stdout.strip())["status"] == "refreshed"

    def test_sources_yaml_entry(self, tmp_path: Path) -> None:
        export = _export_dir(tmp_path)
        sources = tmp_path / "sources.yaml"
        sources.write_text(
            "sources:\n  - name: lakehouse\n    type: unity-catalog\n"
            f"    path: {export}\n"
        )
        result = runner.invoke(
            app,
            [
                *("extract", "refresh", "--source", "lakehouse"),
                *("--sources-file", str(sources), "--format", "json"),
            ],
        )
        assert result.exit_code == 0, result.stdout
        assert json.loads(result.stdout.strip())["new_entities"] == 6


class TestRefusals:
    @pytest.fixture(params=["json", "text"])
    def fmt(self, request: pytest.FixtureRequest) -> str:
        return str(request.param)

    def _assert_refused(self, result: Any, fmt: str, secret: str = "") -> None:
        assert result.exit_code == 1, result.stdout
        # A typed refusal ends in SystemExit; a crash would leave the exception.
        assert isinstance(result.exception, SystemExit), repr(result.exception)
        out = result.stdout + result.stderr
        assert "Traceback" not in out
        if secret:
            assert secret not in out
        if fmt == "json":
            payload = json.loads(result.stdout.strip())
            assert payload["status"] == "error"
            assert payload["message"]

    def test_malformed_json(self, tmp_path: Path, fmt: str) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text('{"tables": [ {"name": "do-not-echo"')
        self._assert_refused(_refresh(bad, fmt), fmt, "do-not-echo")

    def test_wrong_shape(self, tmp_path: Path, fmt: str) -> None:
        bad = tmp_path / "shape.json"
        bad.write_text(json.dumps({"tables": [{"name": "do-not-echo"}]}))
        self._assert_refused(_refresh(bad, fmt), fmt, "do-not-echo")

    def test_not_an_export_at_all(self, tmp_path: Path, fmt: str) -> None:
        bad = tmp_path / "scalar.json"
        bad.write_text("42")
        self._assert_refused(_refresh(bad, fmt), fmt)

    def test_empty_file(self, tmp_path: Path, fmt: str) -> None:
        empty = tmp_path / "empty.json"
        empty.write_text("")
        self._assert_refused(_refresh(empty, fmt), fmt)

    def test_empty_directory(self, tmp_path: Path, fmt: str) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        self._assert_refused(_refresh(empty, fmt), fmt)

    def test_one_bad_file_in_a_directory_refuses_the_lot(
        self, tmp_path: Path, fmt: str
    ) -> None:
        export = _export_dir(tmp_path)
        (export / "zz.json").write_text("[1, 2")
        self._assert_refused(_refresh(export, fmt), fmt)

    def test_exit_code_does_not_depend_on_format(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text("{")
        assert _refresh(bad, "json").exit_code == _refresh(bad, "text").exit_code
