"""Tests for the ``trellis admin write-config`` operator surface."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from trellis.core.write_config import ENV_VAR_BY_FIELD
from trellis_cli.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ENV_VAR_BY_FIELD.values():
        monkeypatch.delenv(name, raising=False)


class TestJsonOutput:
    def test_reports_build_and_every_knob(self) -> None:
        result = runner.invoke(app, ["admin", "write-config", "--format", "json"])
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["version"]
        assert payload["version_source"]
        assert payload["flags_digest"]
        assert sorted(row["env_var"] for row in payload["knobs"]) == sorted(
            ENV_VAR_BY_FIELD.values()
        )

    def test_defaults_report_nothing_overridden(self) -> None:
        result = runner.invoke(app, ["admin", "write-config", "--format", "json"])
        payload = json.loads(result.stdout)
        assert not any(row["overridden"] for row in payload["knobs"])

    def test_reflects_the_invoking_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Not the cached stamp — the CLI reports the process it runs in."""
        monkeypatch.setenv(ENV_VAR_BY_FIELD["classify_on_ingest"], "1")
        result = runner.invoke(app, ["admin", "write-config", "--format", "json"])
        payload = json.loads(result.stdout)
        assert payload["flags"]["classify_on_ingest"] is True
        overridden = {row["name"] for row in payload["knobs"] if row["overridden"]}
        assert overridden == {"classify_on_ingest"}


class TestTextOutput:
    def test_names_every_environment_variable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Rich folds long cells across lines at narrow widths, which is the
        # right behaviour for a human but unassertable; render wide instead.
        monkeypatch.setenv("COLUMNS", "200")
        result = runner.invoke(app, ["admin", "write-config"])
        assert result.exit_code == 0
        for env_var in ENV_VAR_BY_FIELD.values():
            assert env_var in result.stdout

    def test_reports_the_build(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("COLUMNS", "200")
        result = runner.invoke(app, ["admin", "write-config"])
        assert "version_source" in result.stdout
        assert "flags_digest" in result.stdout
