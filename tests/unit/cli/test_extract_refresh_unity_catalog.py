"""``trellis extract refresh --type unity-catalog``: files, directories, refusals."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from tests.cli_output import plain
from trellis_cli.main import app
from trellis_workers.extract.unity_catalog import DEFAULT_MAX_COLUMNS_PER_TABLE

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


def _wide(width: int) -> dict[str, Any]:
    return {
        "tables": [
            {
                "name": "wide",
                "catalog_name": "main",
                "schema_name": "sales",
                "full_name": "main.sales.wide",
                "columns": [
                    {"name": f"c{i}", "type_text": "int", "position": i}
                    for i in range(width)
                ],
            }
        ]
    }


class TestSkippedRows:
    @pytest.fixture(params=["json", "text"])
    def fmt(self, request: pytest.FixtureRequest) -> str:
        return str(request.param)

    def _skipping_export(self, tmp_path: Path) -> Path:
        # Two lineage rows that name a path, not a table, on one side.
        export = tmp_path / "skips.json"
        export.write_text(
            json.dumps(
                [
                    _TABLES,
                    {"source_table_full_name": "main.sales.orders"},
                    {"target_table_full_name": "main.sales.orders"},
                ]
            )
        )
        return export

    def test_skipped_rows_are_counted_by_reason(self, tmp_path: Path, fmt: str) -> None:
        result = _refresh(self._skipping_export(tmp_path), fmt)
        assert result.exit_code == 0, result.stdout
        if fmt == "json":
            data = json.loads(result.stdout.strip())
            assert data["skipped_rows"] == {"lineage_without_two_tables": 2}
        else:
            text = plain(result.stdout)
            assert "Skipped rows:" in text
            assert "lineage_without_two_tables=2" in text
            # No total: a uc_storage_* reason counts written rows, not skips.
            assert "2 (lineage_without_two_tables=2)" not in text

    def test_a_skip_does_not_change_the_exit_code(self, tmp_path: Path) -> None:
        export = self._skipping_export(tmp_path)
        assert _refresh(export, "json").exit_code == _refresh(export, "text").exit_code

    def test_a_clean_export_reports_none(self, tmp_path: Path, fmt: str) -> None:
        export = tmp_path / "tables.json"
        export.write_text(json.dumps(_TABLES))
        result = _refresh(export, fmt)
        assert result.exit_code == 0, result.stdout
        if fmt == "json":
            assert json.loads(result.stdout.strip())["skipped_rows"] == {}
        else:
            assert "Skipped rows" not in plain(result.stdout)


class TestColumnCap:
    @pytest.fixture(params=["json", "text"])
    def fmt(self, request: pytest.FixtureRequest) -> str:
        return str(request.param)

    def _export(self, tmp_path: Path, width: int) -> Path:
        export = tmp_path / "wide.json"
        export.write_text(json.dumps(_wide(width)))
        return export

    def _run(self, path: Path, *extra: str, fmt: str = "json") -> Any:
        return runner.invoke(
            app,
            [
                *("extract", "refresh", "--type", "unity-catalog", "--path", str(path)),
                *extra,
                *("--format", fmt),
            ],
        )

    def test_the_option_bounds_the_column_nodes_and_counts_the_rest(
        self, tmp_path: Path, fmt: str
    ) -> None:
        result = self._run(
            self._export(tmp_path, 5), "--max-columns-per-table", "3", fmt=fmt
        )
        assert result.exit_code == 0, result.stdout
        if fmt == "json":
            data = json.loads(result.stdout.strip())
            # catalog, schema, table and three of the five columns
            assert data["entities_scanned"] == 6
            assert data["skipped_rows"] == {"columns_over_cap": 2}
        else:
            assert "columns_over_cap=2" in plain(result.stdout)

    def test_a_small_table_keeps_every_column_by_default(self, tmp_path: Path) -> None:
        result = self._run(self._export(tmp_path, 5))
        data = json.loads(result.stdout.strip())
        assert data["entities_scanned"] == 8
        assert data["skipped_rows"] == {}

    def test_the_default_is_stated_in_the_help(self) -> None:
        result = runner.invoke(app, ["extract", "refresh", "--help"])
        assert f"(default {DEFAULT_MAX_COLUMNS_PER_TABLE})" in plain(result.stdout)

    def test_sources_yaml_entry_takes_the_option(self, tmp_path: Path) -> None:
        export = self._export(tmp_path, 5)
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
                *("--max-columns-per-table", "2"),
            ],
        )
        assert result.exit_code == 0, result.stdout
        assert json.loads(result.stdout.strip())["skipped_rows"] == {
            "columns_over_cap": 3
        }

    @pytest.mark.parametrize("value", ["0", "-3"])
    def test_a_cap_below_one_is_refused_on_both_formats(
        self, tmp_path: Path, fmt: str, value: str
    ) -> None:
        result = self._run(
            self._export(tmp_path, 2), "--max-columns-per-table", value, fmt=fmt
        )
        assert result.exit_code == 1, result.stdout
        assert "at least 1" in plain(result.stdout)
        if fmt == "json":
            assert json.loads(result.stdout.strip())["status"] == "error"

    def test_another_extractor_refuses_the_option(
        self, tmp_path: Path, fmt: str
    ) -> None:
        path = tmp_path / "manifest.json"
        path.write_text("{}")
        result = runner.invoke(
            app,
            [
                *("extract", "refresh", "--type", "dbt-manifest", "--path", str(path)),
                *("--max-columns-per-table", "5", "--format", fmt),
            ],
        )
        assert result.exit_code == 1, result.stdout
        assert "applies only to the unity-catalog type" in plain(result.stdout)

    def test_exit_code_does_not_depend_on_format(self, tmp_path: Path) -> None:
        path = self._export(tmp_path, 2)
        codes = {
            fmt: self._run(path, "--max-columns-per-table", "0", fmt=fmt).exit_code
            for fmt in ("json", "text")
        }
        assert codes["json"] == codes["text"]


_OL_EVENTS = [
    {
        "job": {"namespace": "databricks", "name": "build_revenue"},
        "inputs": [
            {
                "namespace": "s3://example-bucket",
                "name": "/warehouse/orders",
                "facets": {
                    "symlinks": {
                        "identifiers": [
                            {
                                "namespace": "unity-catalog",
                                "name": "main.sales.orders",
                                "type": "TABLE",
                            }
                        ]
                    }
                },
            },
            {"namespace": "s3://example-bucket", "name": "/warehouse/orders"},
        ],
        "outputs": [],
    }
]


class TestOpenLineageScope:
    """``--unity-catalog-scope`` reaches ``OpenLineageExtractor`` and nothing else."""

    @pytest.fixture(params=["json", "text"])
    def fmt(self, request: pytest.FixtureRequest) -> str:
        return str(request.param)

    def _events(self, tmp_path: Path) -> Path:
        path = tmp_path / "events.json"
        path.write_text(json.dumps(_OL_EVENTS))
        return path

    def _run(self, path: Path, *extra: str, fmt: str = "json") -> Any:
        return runner.invoke(
            app,
            [
                *("extract", "refresh", "--type", "openlineage", "--path", str(path)),
                *extra,
                *("--format", fmt),
            ],
        )

    def test_the_scope_is_applied_and_reported(self, tmp_path: Path) -> None:
        result = self._run(self._events(tmp_path), "--unity-catalog-scope", "custom")
        assert result.exit_code == 0, result.stdout
        data = json.loads(result.stdout.strip())
        assert data["unity_catalog_scope"] == "custom"
        ids = {d["entity_id"] for d in data["diffs"]}
        assert "dataset:custom:main.sales.orders" in ids
        assert "dataset:unity_catalog:main.sales.orders" not in ids

    def test_the_scope_is_normalised_as_the_catalog_side_does(
        self, tmp_path: Path
    ) -> None:
        result = self._run(
            self._events(tmp_path),
            "--unity-catalog-scope",
            " UnityCatalog://Host:443/ ",
        )
        data = json.loads(result.stdout.strip())
        assert data["unity_catalog_scope"] == "unitycatalog://host"
        assert "dataset:unitycatalog://host:main.sales.orders" in {
            d["entity_id"] for d in data["diffs"]
        }

    def test_the_default_scope_is_reported_when_none_is_given(
        self, tmp_path: Path
    ) -> None:
        data = json.loads(self._run(self._events(tmp_path)).stdout.strip())
        assert data["unity_catalog_scope"] == "unity_catalog"
        # And the redirect is reported by `extract refresh`, beside the scope.
        assert data["skipped_rows"] == {"uc_storage_redirected": 1}

    def test_text_output_names_the_scope(self, tmp_path: Path) -> None:
        result = self._run(
            self._events(tmp_path), "--unity-catalog-scope", "custom", fmt="text"
        )
        assert result.exit_code == 0, result.stdout
        out = plain(result.stdout)
        assert "UC symlink scope:  custom" in out
        assert "uc_storage_redirected=1" in out

    def test_another_extractor_reports_no_scope(self, tmp_path: Path) -> None:
        result = _refresh(_export_dir(tmp_path))
        assert "unity_catalog_scope" not in json.loads(result.stdout.strip())

    def test_sources_yaml_entry_takes_the_option(self, tmp_path: Path) -> None:
        sources = tmp_path / "sources.yaml"
        sources.write_text(
            "sources:\n  - name: lineage\n    type: openlineage\n"
            f"    path: {self._events(tmp_path)}\n"
        )
        result = runner.invoke(
            app,
            [
                *("extract", "refresh", "--source", "lineage"),
                *("--sources-file", str(sources), "--format", "json"),
                *("--unity-catalog-scope", "custom"),
            ],
        )
        assert result.exit_code == 0, result.stdout
        assert json.loads(result.stdout.strip())["unity_catalog_scope"] == "custom"

    @pytest.mark.parametrize("extractor_type", ["unity-catalog", "dbt-manifest"])
    def test_another_extractor_refuses_the_option(
        self, tmp_path: Path, fmt: str, extractor_type: str
    ) -> None:
        path = tmp_path / "input.json"
        path.write_text("{}")
        result = runner.invoke(
            app,
            [
                *("extract", "refresh", "--type", extractor_type, "--path", str(path)),
                *("--unity-catalog-scope", "custom", "--format", fmt),
            ],
        )
        assert result.exit_code == 1, result.stdout
        assert "applies only to the openlineage type" in plain(result.stdout)
        if fmt == "json":
            assert json.loads(result.stdout.strip())["status"] == "error"

    def test_a_sources_entry_of_another_type_refuses_it(self, tmp_path: Path) -> None:
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
                *("--unity-catalog-scope", "custom"),
            ],
        )
        assert result.exit_code == 1, result.stdout
        assert "applies only to the openlineage type" in plain(result.stdout)

    @pytest.mark.parametrize("value", ["", "   ", "unitycatalog://", "/"])
    def test_a_scope_that_names_nothing_is_refused_on_both_formats(
        self, tmp_path: Path, fmt: str, value: str
    ) -> None:
        result = self._run(
            self._events(tmp_path), "--unity-catalog-scope", value, fmt=fmt
        )
        assert result.exit_code == 1, result.stdout
        assert "must name a scope" in plain(result.stdout)
        if fmt == "json":
            assert json.loads(result.stdout.strip())["status"] == "error"

    def test_exit_code_does_not_depend_on_format(self, tmp_path: Path) -> None:
        events = self._events(tmp_path)
        for args in (("--unity-catalog-scope", ""), ("--unity-catalog-scope", "x")):
            codes = {
                fmt: self._run(events, *args, fmt=fmt).exit_code
                for fmt in ("json", "text")
            }
            assert codes["json"] == codes["text"], args

    def test_the_option_is_documented_in_the_help(self) -> None:
        result = runner.invoke(app, ["extract", "refresh", "--help"])
        assert "--unity-catalog-scope" in plain(result.stdout)
