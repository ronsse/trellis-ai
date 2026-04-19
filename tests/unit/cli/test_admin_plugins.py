"""Tests for ``trellis admin check-plugins``."""

from __future__ import annotations

import json
from dataclasses import dataclass
from unittest.mock import patch

from typer.testing import CliRunner

from trellis_cli.main import app

runner = CliRunner()


@dataclass
class _FakeDist:
    name: str | None = None
    version: str | None = None


@dataclass
class _FakeEntryPoint:
    name: str
    value: str
    dist: _FakeDist | None = None


class TestCheckPluginsCLI:
    def test_no_plugins_exit_zero_text(self):
        with patch(
            "trellis.plugins.loader.entry_points",
            side_effect=lambda *, group: [],
        ):
            result = runner.invoke(app, ["admin", "check-plugins"])
        assert result.exit_code == 0
        assert "Trellis Plugins" in result.stdout

    def test_no_plugins_exit_zero_json(self):
        with patch(
            "trellis.plugins.loader.entry_points",
            side_effect=lambda *, group: [],
        ):
            result = runner.invoke(app, ["admin", "check-plugins", "--format", "json"])
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["loaded"] == 0
        assert payload["blocked"] == 0
        assert payload["shadowed"] == 0
        assert payload["exit_code"] == 0
        assert len(payload["groups_checked"]) > 0
        assert payload["plugins"] == []

    def test_blocked_plugin_exits_two(self):
        eps = {
            "trellis.rerankers": [
                _FakeEntryPoint(
                    name="broken",
                    value="pkg.not.real:NotAThing",
                ),
            ],
        }
        with patch(
            "trellis.plugins.loader.entry_points",
            side_effect=lambda *, group: eps.get(group, []),
        ):
            result = runner.invoke(app, ["admin", "check-plugins", "--format", "json"])
        assert result.exit_code == 2
        payload = json.loads(result.stdout)
        assert payload["blocked"] == 1

    def test_shadowed_plugin_exits_one(self):
        eps = {
            "trellis.stores.graph": [
                _FakeEntryPoint(
                    name="sqlite",
                    value="evil.pkg:EvilGraphStore",
                ),
            ],
        }
        with patch(
            "trellis.plugins.loader.entry_points",
            side_effect=lambda *, group: eps.get(group, []),
        ):
            result = runner.invoke(app, ["admin", "check-plugins", "--format", "json"])
        assert result.exit_code == 1
        payload = json.loads(result.stdout)
        assert payload["shadowed"] == 1

    def test_loaded_plugin_exits_zero(self):
        eps = {
            "trellis.classifiers": [
                _FakeEntryPoint(
                    name="probe",
                    value="trellis.core.base:TrellisModel",
                    dist=_FakeDist(name="test", version="0.1"),
                ),
            ],
        }
        with patch(
            "trellis.plugins.loader.entry_points",
            side_effect=lambda *, group: eps.get(group, []),
        ):
            result = runner.invoke(app, ["admin", "check-plugins", "--format", "json"])
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["loaded"] == 1
