"""Tests for the ``trellis admin write-config`` operator surface."""

from __future__ import annotations

import json

import pytest
from click.utils import strip_ansi
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
        stamp = payload["write_provenance"]
        assert stamp["version"]
        assert stamp["version_source"]
        assert stamp["env_flags_digest"]
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
        assert payload["write_provenance"]["env_flags"]["classify_on_ingest"] is True
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
        assert "env_flags_digest" in result.stdout


LIVE_SHA = "def5678" + "1" * 33


class TestStampStaleness:
    """#348 — the operator surface says whether ``commit`` still holds."""

    def test_json_names_the_state_even_when_nothing_is_wrong(
        self, pin_source_tree
    ) -> None:
        """The stamp is silent when fresh; this surface must not be.

        "Checked, fine" and "never checked" are different operator facts
        and a missing key renders as neither.
        """
        pin_source_tree(commit="abc1234", head="abc1234" + "0" * 33)
        result = runner.invoke(app, ["admin", "write-config", "--format", "json"])
        payload = json.loads(result.stdout)
        assert payload["stamp_staleness"]["state"] == "fresh"
        assert "stamp_stale" not in payload["write_provenance"]

    def test_json_distinguishes_never_checked_from_fine(self, pin_source_tree) -> None:
        pin_source_tree(commit="abc1234", head=LIVE_SHA, tree=None)
        result = runner.invoke(app, ["admin", "write-config", "--format", "json"])
        payload = json.loads(result.stdout)
        assert payload["stamp_staleness"]["state"] == "not-checked"
        assert payload["stamp_staleness"]["source_tree_commit"] is None

    def test_json_reports_a_stale_install_in_both_places(self, pin_source_tree) -> None:
        pin_source_tree(commit="abc1234", head=LIVE_SHA)
        result = runner.invoke(app, ["admin", "write-config", "--format", "json"])
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["stamp_staleness"] == {
            "state": "stale",
            "source_tree_commit": LIVE_SHA,
        }
        assert payload["write_provenance"]["stamp_stale"] is True
        assert payload["write_provenance"]["commit"] == "abc1234"

    def test_text_names_the_live_sha_when_stale(
        self, pin_source_tree, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("COLUMNS", "200")
        pin_source_tree(commit="abc1234", head=LIVE_SHA)
        result = runner.invoke(app, ["admin", "write-config"])
        out = strip_ansi(result.stdout)
        assert "stamp_stale" in out
        assert LIVE_SHA in out

    def test_text_says_no_rather_than_nothing_when_fresh(
        self, pin_source_tree, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("COLUMNS", "200")
        pin_source_tree(commit="abc1234", head="abc1234" + "0" * 33)
        out = strip_ansi(runner.invoke(app, ["admin", "write-config"]).stdout)
        assert "no — source tree HEAD matches" in out

    def test_text_distinguishes_unreadable_from_fresh(
        self, pin_source_tree, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("COLUMNS", "200")
        pin_source_tree(commit="abc1234", head=None)
        out = strip_ansi(runner.invoke(app, ["admin", "write-config"]).stdout)
        assert "unknown" in out
        assert "no — source tree HEAD matches" not in out

    def test_text_says_not_applicable_when_there_is_no_source_tree(
        self, pin_source_tree, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A container's "nothing to check" must not read as "checked, fine"."""
        monkeypatch.setenv("COLUMNS", "200")
        pin_source_tree(commit="abc1234", head=LIVE_SHA, tree=None)
        out = strip_ansi(runner.invoke(app, ["admin", "write-config"]).stdout)
        assert "n/a" in out
        assert "no — source tree HEAD matches" not in out

    def test_text_labels_the_install_time_dirty_flag(
        self, pin_source_tree, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two clocks on one table — the older one has to say which it is."""
        monkeypatch.setenv("COLUMNS", "200")
        pin_source_tree(commit="abc1234", head=LIVE_SHA)
        out = strip_ansi(runner.invoke(app, ["admin", "write-config"]).stdout)
        assert "dirty (at install time)" in out
