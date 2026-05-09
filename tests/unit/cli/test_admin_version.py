"""Tests for the ``trellis admin version`` CLI command."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from trellis.api_version import (
    API_MAJOR,
    API_MINOR,
    MCP_TOOLS_VERSION,
    WIRE_SCHEMA,
)
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
        assert payload["mcp_tools_version"] == MCP_TOOLS_VERSION
