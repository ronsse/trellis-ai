"""C2 Phase 6 — explicit CLI exit codes (`docs/design/adr-cli-exit-codes.md`).

Covers:
- The five canonical exit-code constants are present and stable.
- CLI swallow sites cited in the silent-fallback audit now surface
  structured failures (log signal or typed exit) rather than degrading
  to empty results.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import structlog

from trellis_cli import exit_codes
from trellis_cli.claude_integration import read_claude_settings
from trellis_cli.extract_refresh import _emit_refresh_event, _snapshot_entities


class TestExitCodeMap:
    """The five canonical codes must stay stable — operators script
    against them. Changing any of these is a breaking change."""

    def test_codes_have_documented_values(self) -> None:
        assert exit_codes.EXIT_OK == 0
        assert exit_codes.EXIT_INTERNAL == 1
        assert exit_codes.EXIT_VALIDATION == 2
        assert exit_codes.EXIT_POLICY == 3
        assert exit_codes.EXIT_IDEMPOTENCY == 4
        assert exit_codes.EXIT_STORE == 5

    def test_codes_are_unique(self) -> None:
        values = {
            exit_codes.EXIT_OK,
            exit_codes.EXIT_INTERNAL,
            exit_codes.EXIT_VALIDATION,
            exit_codes.EXIT_POLICY,
            exit_codes.EXIT_IDEMPOTENCY,
            exit_codes.EXIT_STORE,
        }
        assert len(values) == 6


class TestReadClaudeSettings:
    """The missing-file branch is documented graceful degradation,
    but it must log so the create-from-empty path is recoverable
    from structured logs (no longer a silent swallow)."""

    def test_missing_file_returns_empty_dict_and_logs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from trellis_cli import claude_integration as ci

        captured: list[tuple[str, dict]] = []

        def _debug(event: str, **kw: object) -> None:
            captured.append((event, dict(kw)))

        monkeypatch.setattr(ci, "_logger", structlog.get_logger().bind())
        monkeypatch.setattr(ci._logger, "debug", _debug)
        target = tmp_path / "no-such-file.json"
        result = read_claude_settings(target)
        assert result == {}
        assert ("claude_settings_not_found", {"path": str(target)}) in captured


class TestExtractRefreshSnapshotErrors:
    """Per-entity snapshot errors no longer hit a bare ``Exception``
    swallow — the catch is narrowed and each failure logs."""

    def test_snapshot_failure_logs_and_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from trellis_cli import extract_refresh as er

        captured: list[tuple[str, dict]] = []

        def _debug(event: str, **kw: object) -> None:
            captured.append((event, dict(kw)))

        monkeypatch.setattr(er._logger, "debug", _debug)

        graph = MagicMock()
        graph.get_node.side_effect = RuntimeError("backend down")
        registry = MagicMock()
        registry.knowledge.graph_store = graph

        out = _snapshot_entities(registry, ["ent_1", "ent_2"])
        assert out == {"ent_1": None, "ent_2": None}
        events = [e for e, _ in captured]
        assert events.count("extract_refresh_snapshot_get_node_failed") == 2

    def test_snapshot_unexpected_type_still_propagates(self) -> None:
        """A SystemExit or other non-listed exception must propagate so
        truly unexpected failures aren't masked by the narrowed catch."""
        graph = MagicMock()
        graph.get_node.side_effect = SystemExit("boom")
        registry = MagicMock()
        registry.knowledge.graph_store = graph

        with pytest.raises(SystemExit):
            _snapshot_entities(registry, ["ent_1"])


class TestExtractRefreshEmitErrors:
    """The TAGS_REFRESHED emit no longer catches bare ``Exception``."""

    def test_emit_failure_logs_with_error_type(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from trellis_cli import extract_refresh as er

        captured: list[tuple[str, dict]] = []

        def _exception(event: str, **kw: object) -> None:
            captured.append((event, dict(kw)))

        monkeypatch.setattr(er._logger, "exception", _exception)

        registry = MagicMock()
        registry.operational.event_log.emit.side_effect = OSError("disk full")
        _emit_refresh_event(
            registry,
            "ent_1",
            "service",
            {"changed": {"description": ["old", "new"]}},
            source_name="dbt",
            extractor_used="dbt-manifest",
        )
        events = [(e, kw.get("error_type")) for e, kw in captured]
        assert ("extract_refresh_emit_failed", "OSError") in events
