"""Tests for ``trellis analyze intent-families``.

The exit code must not depend on ``--format``. That is pinned statically by
``tests/unit/test_format_exit_parity_rule.py`` and again here, behaviourally:
one store state is run through all three format arms for a clean report, a
truncated scan, a refused request, an empty store and a malformed config, and
the exit codes are compared.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.cli_output import plain
from tests.unit.learning import _intent_family_seed as seed_mod
from trellis.stores.registry import StoreRegistry
from trellis_cli.exit_codes import EXIT_OK, EXIT_STORE, EXIT_VALIDATION
from trellis_cli.main import app
from trellis_cli.stores import _reset_registry

runner = CliRunner()

ARGS = [
    "analyze",
    "intent-families",
    "--days",
    "30",
    "--until",
    "2026-11-04T12:00:00",
]
FORMATS = ("text", "json", "markdown")
CUSTOM_CONFIG = """\
retrieval:
  intent_families:
    fallback: misc
    rules:
      - id: x.fix
        family: repairs
        keywords: [fix, flaky]
"""
MALFORMED_CONFIG = "retrieval:\n  intent_families: []\n"


@pytest.fixture(autouse=True)
def _temp_stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> StoreRegistry:
    data_dir = tmp_path / "data"
    stores_dir = data_dir / "stores"
    stores_dir.mkdir(parents=True)
    (tmp_path / "config").mkdir()
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))
    _reset_registry()
    return StoreRegistry(stores_dir=stores_dir)


@pytest.fixture
def seeded(_temp_stores: StoreRegistry) -> StoreRegistry:
    seed_mod.seed(_temp_stores.operational.event_log)
    return _temp_stores


def _write_config(tmp_path: Path, text: str) -> None:
    (tmp_path / "config" / "config.yaml").write_text(text, encoding="utf-8")
    _reset_registry()


class TestJson:
    def test_report_shape_and_headline_counts(self, seeded: StoreRegistry) -> None:
        result = runner.invoke(app, [*ARGS, "--format", "json"])

        assert result.exit_code == EXIT_OK
        data = json.loads(result.output)
        assert set(data) >= {
            "status",
            "empty_reason",
            "rule_set",
            "packs_served",
            "packs_analyzed",
            "fallback_share",
            "families",
            "crosstab",
            "scan",
            "notes",
        }
        assert data["status"] == "ok"
        assert data["packs_analyzed"] == 38
        assert data["fallback_packs"] == 5
        by_family = {row["family"]: row for row in data["families"]}
        assert by_family["bugfix"]["packs"] == 20
        assert by_family["bugfix"]["helpful_rate"] == pytest.approx(0.6667, abs=1e-4)
        assert by_family["review"]["helpful_rate"] is None
        assert "below the floor of 20" in by_family["review"]["withheld_reason"]

    def test_an_empty_store_is_a_report_not_an_error(self) -> None:
        result = runner.invoke(app, [*ARGS, "--format", "json"])

        assert result.exit_code == EXIT_OK
        data = json.loads(result.output)
        assert data["status"] == "ok"
        assert data["empty_reason"] == "no served packs in the window"

    def test_a_configured_set_replaces_the_shipped_one(
        self, seeded: StoreRegistry, tmp_path: Path
    ) -> None:
        _write_config(tmp_path, CUSTOM_CONFIG)

        result = runner.invoke(app, [*ARGS, "--format", "json"])

        assert result.exit_code == EXIT_OK
        data = json.loads(result.output)
        assert data["rule_set"]["source"] == "config"
        assert data["rule_set"]["families"] == ["repairs", "misc"]
        assert {row["family"]: row["packs"] for row in data["families"]} == {
            "repairs": 20,
            "misc": 18,
        }


class TestExitParity:
    @pytest.mark.parametrize("output_format", FORMATS)
    def test_a_clean_report_exits_ok_on_every_format(
        self, seeded: StoreRegistry, output_format: str
    ) -> None:
        assert (
            runner.invoke(app, [*ARGS, "--format", output_format]).exit_code == EXIT_OK
        )

    @pytest.mark.parametrize("output_format", FORMATS)
    def test_an_empty_store_exits_ok_on_every_format(self, output_format: str) -> None:
        assert (
            runner.invoke(app, [*ARGS, "--format", output_format]).exit_code == EXIT_OK
        )

    @pytest.mark.parametrize("output_format", FORMATS)
    def test_a_truncated_scan_exits_store_on_every_format(
        self, seeded: StoreRegistry, output_format: str
    ) -> None:
        result = runner.invoke(app, [*ARGS, "--limit", "7", "--format", output_format])

        assert result.exit_code == EXIT_STORE

    @pytest.mark.parametrize("output_format", FORMATS)
    @pytest.mark.parametrize(
        "bad",
        [
            ["--days", "0"],
            ["--since", "last tuesday"],
            ["--until", "not a date"],
        ],
    )
    def test_a_refused_request_exits_validation_on_every_format(
        self, seeded: StoreRegistry, output_format: str, bad: list[str]
    ) -> None:
        # Later options win, so the bad value replaces the good one.
        result = runner.invoke(app, [*ARGS, *bad, "--format", output_format])

        assert result.exit_code == EXIT_VALIDATION

    @pytest.mark.parametrize("output_format", FORMATS)
    def test_a_malformed_config_exits_store_on_every_format(
        self, seeded: StoreRegistry, tmp_path: Path, output_format: str
    ) -> None:
        _write_config(tmp_path, MALFORMED_CONFIG)

        result = runner.invoke(app, [*ARGS, "--format", output_format])

        assert result.exit_code == EXIT_STORE

    def test_a_malformed_config_never_falls_back_to_the_shipped_rules(
        self, seeded: StoreRegistry, tmp_path: Path
    ) -> None:
        _write_config(tmp_path, MALFORMED_CONFIG)

        result = runner.invoke(app, [*ARGS, "--format", "json"])

        data = json.loads(result.output)
        assert data["status"] == "error"
        assert data["setting"] == "retrieval.intent_families"
        assert "families" not in data

    def test_a_refusal_is_machine_readable_in_json(self, seeded: StoreRegistry) -> None:
        result = runner.invoke(app, [*ARGS, "--since", "nope", "--format", "json"])

        assert json.loads(result.output)["status"] == "error"


class TestHumanOutput:
    def test_table_lists_families_and_states_why_a_rate_is_withheld(
        self, seeded: StoreRegistry
    ) -> None:
        result = runner.invoke(app, ARGS)
        out = plain(result.output)

        assert result.exit_code == EXIT_OK
        assert "bugfix" in out
        assert "general" in out
        assert "66.7%" in out
        assert "below the floor of 20" in out
        assert "legacy" in out.lower()

    def test_a_truncated_scan_is_stated_in_the_table(
        self, seeded: StoreRegistry
    ) -> None:
        out = plain(runner.invoke(app, [*ARGS, "--limit", "7"]).output)

        assert "truncated" in out


class TestMarkdownOutput:
    def test_page_is_plain_gfm(self, seeded: StoreRegistry) -> None:
        result = runner.invoke(app, [*ARGS, "--format", "markdown"])
        text = result.output

        assert result.exit_code == EXIT_OK
        assert "| --- |" in text
        assert "Legacy by new" in text
        assert "<" not in text


class TestNoIntentText:
    @pytest.mark.parametrize("output_format", FORMATS)
    def test_an_intent_string_never_reaches_the_output(
        self, seeded: StoreRegistry, output_format: str
    ) -> None:
        seed_mod.seed_sentinel(seeded.operational.event_log)

        result = runner.invoke(app, [*ARGS, "--format", output_format])

        assert result.exit_code == EXIT_OK
        # The sentinel pack is in the population (bugfix 21), so this is not vacuous.
        assert "21" in result.output
        assert seed_mod.SENTINEL not in result.output.lower()
