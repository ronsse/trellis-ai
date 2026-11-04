"""Tests for ``trellis analyze trend``.

The exit code must not depend on ``--format``. That is pinned statically by
``tests/unit/test_format_exit_parity_rule.py`` and again here, behaviourally:
one store state is run through all three format arms, for a clean series, a
truncated one and a refused request, and the exit codes are compared.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.cli_output import plain
from tests.unit.analyze import _trend_seed as seed_mod
from trellis.stores.registry import StoreRegistry
from trellis_cli.exit_codes import EXIT_OK, EXIT_STORE, EXIT_VALIDATION
from trellis_cli.main import app
from trellis_cli.stores import _reset_registry

runner = CliRunner()

ARGS = ["analyze", "trend", "--weeks", "3", "--until", "2026-11-04T12:00:00"]
FORMATS = ("text", "json", "markdown")


@pytest.fixture(autouse=True)
def _temp_stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> StoreRegistry:
    data_dir = tmp_path / "data"
    stores_dir = data_dir / "stores"
    stores_dir.mkdir(parents=True)
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))
    _reset_registry()
    return StoreRegistry(stores_dir=stores_dir)


@pytest.fixture
def seeded(_temp_stores: StoreRegistry) -> StoreRegistry:
    seed_mod.seed(_temp_stores.operational.event_log)
    return _temp_stores


class TestJson:
    def test_report_shape(self, seeded: StoreRegistry) -> None:
        result = runner.invoke(app, [*ARGS, "--format", "json"])

        assert result.exit_code == EXIT_OK
        data = json.loads(result.output)
        assert set(data) >= {
            "weeks",
            "builds",
            "graph_shape",
            "generated_at",
            "window",
            "status",
            "omitted",
        }
        assert [week["week"] for week in data["weeks"]] == [
            "2026-W43",
            "2026-W44",
            "2026-W45",
        ]
        assert data["weeks"][2]["partial"] is True
        assert [build["commit"] for build in data["builds"]] == [
            "aaaaaaa",
            "bbbbbbb",
            None,
        ]
        assert data["graph_shape"]["nodes"] == 0
        assert data["window"]["weeks"] == 3

    def test_a_fully_empty_store_is_a_series_not_an_error(self) -> None:
        result = runner.invoke(app, [*ARGS, "--format", "json"])

        assert result.exit_code == EXIT_OK
        assert len(json.loads(result.output)["weeks"]) == 3


class TestExitParity:
    @pytest.mark.parametrize("output_format", FORMATS)
    def test_a_clean_series_exits_ok_on_every_format(
        self, seeded: StoreRegistry, output_format: str
    ) -> None:
        result = runner.invoke(app, [*ARGS, "--format", output_format])

        assert result.exit_code == EXIT_OK

    @pytest.mark.parametrize("output_format", FORMATS)
    def test_a_truncated_series_exits_store_on_every_format(
        self, seeded: StoreRegistry, output_format: str
    ) -> None:
        result = runner.invoke(app, [*ARGS, "--limit", "7", "--format", output_format])

        assert result.exit_code == EXIT_STORE

    @pytest.mark.parametrize("output_format", FORMATS)
    @pytest.mark.parametrize(
        "bad", [["--weeks", "0"], ["--weeks", "27"], ["--until", "last tuesday"]]
    )
    def test_a_refused_request_exits_validation_on_every_format(
        self, seeded: StoreRegistry, output_format: str, bad: list[str]
    ) -> None:
        argv = ["analyze", "trend", "--until", "2026-11-04", *bad]
        # Later options win, so the bad value replaces the good one.
        result = runner.invoke(app, [*argv, "--format", output_format])

        assert result.exit_code == EXIT_VALIDATION

    def test_a_refusal_is_machine_readable_in_json(self, seeded: StoreRegistry) -> None:
        result = runner.invoke(
            app, ["analyze", "trend", "--weeks", "0", "--format", "json"]
        )

        assert json.loads(result.output)["status"] == "error"


class TestHumanOutput:
    def test_table_marks_the_partial_week_and_lists_commits_and_builds(
        self, seeded: StoreRegistry
    ) -> None:
        result = runner.invoke(app, ARGS)
        out = plain(result.output)

        assert result.exit_code == EXIT_OK
        assert "2026-W43" in out
        assert "2026-W45*" in out
        assert "partial week" in out
        assert "2026-W44 commits: aaaaaaa, bbbbbbb" in out
        assert "unidentified" in out
        assert "sealed" in out
        # No rate where the reader refused one.
        assert "-" in out

    def test_a_commit_that_looks_like_markup_prints_verbatim(
        self, seeded: StoreRegistry
    ) -> None:
        seed_mod.pack(
            seeded.operational.event_log,
            "odd",
            seed_mod.W45 + timedelta(hours=10),
            {"version": "9.9.9", "commit": "[red]abc:zap:"},
            [("doc:odd", 10)],
        )

        out = plain(runner.invoke(app, ARGS).output)

        assert "[red]abc:zap:" in out


class TestMarkdownOutput:
    def test_page_is_plain_gfm_with_frontmatter(self, seeded: StoreRegistry) -> None:
        result = runner.invoke(app, [*ARGS, "--format", "markdown"])
        text = result.output

        assert result.exit_code == EXIT_OK
        assert text.startswith("---\ntitle: ")
        assert "| --- |" in text
        assert "sealed" in text
        assert "<" not in text
        assert "http" not in text
