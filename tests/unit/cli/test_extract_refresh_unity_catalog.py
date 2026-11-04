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
_CATALOGS = {
    "catalogs": [{"name": "main", "owner": "data-eng", "comment": "Primary catalog"}]
}
_SCHEMAS = {
    "schemas": [
        {
            "name": "sales",
            "catalog_name": "main",
            "full_name": "main.sales",
            "owner": "sales-eng",
            "comment": "Sales marts",
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
        # catalog, schema, table and one column, plus the placeholders for
        # `main.raw.events`, which only lineage names, and its schema `main.raw`.
        assert data["entities_scanned"] == data["new_entities"] == 6

        second = json.loads(_refresh(export).stdout.strip())
        assert second["new_entities"] == 0
        assert second["changed_entities"] == 0
        assert second["unchanged_entities"] == 6
        assert second["diffs"] == []

    def test_a_lineage_only_refresh_changes_no_entity(self, tmp_path: Path) -> None:
        metadata = tmp_path / "tables.json"
        metadata.write_text(json.dumps(_TABLES))
        lineage = tmp_path / "lineage.json"
        lineage.write_text(json.dumps(_LINEAGE))
        assert _refresh(metadata).exit_code == 0
        after = json.loads(_refresh(lineage).stdout.strip())
        assert after["status"] == "refreshed"
        # The run names five nodes: the two lineage endpoints' placeholders
        # (`main.raw.events` and its schema `main.raw` are new) and the three
        # the metadata run already wrote (catalog, schema, table), left alone.
        assert after["entities_scanned"] == 5
        assert after["new_entities"] == 2
        assert after["changed_entities"] == 0
        assert after["unchanged_entities"] == 3
        assert after["edges_emitted"] == 5  # four contains and one depends_on
        # The metadata run still reads back unchanged: nothing was overwritten.
        again = json.loads(_refresh(metadata).stdout.strip())
        assert again["changed_entities"] == again["new_entities"] == 0
        assert again["unchanged_entities"] == 4

    def test_a_tables_only_refresh_after_a_full_export_changes_nothing(
        self, tmp_path: Path
    ) -> None:
        # The catalog and schema a tables-only export re-names are placeholders,
        # so they do not replace the owner and description a full export wrote.
        full = tmp_path / "full"
        full.mkdir()
        (full / "catalogs.json").write_text(json.dumps(_CATALOGS))
        (full / "schemas.json").write_text(json.dumps(_SCHEMAS))
        (full / "tables.json").write_text(json.dumps(_TABLES))
        tables = tmp_path / "tables.json"
        tables.write_text(json.dumps(_TABLES))

        first = json.loads(_refresh(full).stdout.strip())
        assert first["new_entities"] == 4
        after = json.loads(_refresh(tables).stdout.strip())
        assert after["new_entities"] == after["changed_entities"] == 0
        assert after["unchanged_entities"] == 4
        assert after["diffs"] == []

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

    @pytest.mark.parametrize("layout", ["document", "line"])
    def test_input_nested_too_deep(self, tmp_path: Path, fmt: str, layout: str) -> None:
        # Past the parser's recursion limit; the refusal names the file, not content.
        nested = "[" * 5000 + "]" * 5000
        bad = tmp_path / "deep.json"
        bad.write_text(nested if layout == "document" else f"{{}}\n{nested}\n")
        result = _refresh(bad, fmt)
        self._assert_refused(result, fmt)
        out = result.stdout + result.stderr
        assert "nested too deeply" in out
        assert "RecursionError" not in out

    def test_a_column_record_is_not_an_export(self, tmp_path: Path, fmt: str) -> None:
        bad = tmp_path / "column.json"
        bad.write_text(json.dumps({"name": "do-not-echo", "type_text": "int"}))
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
