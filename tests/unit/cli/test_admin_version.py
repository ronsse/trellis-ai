"""Tests for the ``trellis admin version`` CLI command."""

from __future__ import annotations

import json
from datetime import date

from typer.testing import CliRunner

from trellis.api_version import API_MAJOR, API_MINOR, WIRE_SCHEMA
from trellis_api import deprecation
from trellis_api.deprecation import DeprecationEntry
from trellis_cli.main import app

runner = CliRunner()


class TestAdminVersion:
    def test_text_format_prints_api_version(self):
        result = runner.invoke(app, ["admin", "version"])
        assert result.exit_code == 0
        assert f"{API_MAJOR}.{API_MINOR}" in result.stdout

    def test_json_format_matches_constants(self):
        result = runner.invoke(app, ["admin", "version", "--format", "json"])
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["api_major"] == API_MAJOR
        assert payload["api_minor"] == API_MINOR
        assert payload["wire_schema"] == WIRE_SCHEMA
        assert payload["deprecations"] == []

    def test_surfaces_registered_deprecations(self, monkeypatch):
        monkeypatch.setitem(
            deprecation.ROUTE_DEPRECATIONS,
            "/api/v1/old",
            DeprecationEntry(
                deprecated_since=date(2026, 4, 17),
                sunset_on=date(2026, 10, 17),
                replacement="/api/v1/new",
                reason="renamed",
            ),
        )
        result = runner.invoke(app, ["admin", "version", "--format", "json"])
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert len(payload["deprecations"]) == 1
        assert payload["deprecations"][0]["path"] == "/api/v1/old"
        assert payload["deprecations"][0]["replacement"] == "/api/v1/new"
