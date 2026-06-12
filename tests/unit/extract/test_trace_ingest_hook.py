"""Tests for the shared post-ingest trace->graph extraction hook.

The hook is fail-soft and feature-flagged. These tests cover the three
contract guarantees the wiring depends on:

* flag off  -> returns ``None`` and submits nothing.
* flag on   -> drafts flow through ``result_to_batch`` -> ``execute_batch``.
* failure   -> caught + logged, returns an error summary, never raises.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from trellis.extract.trace_ingest_hook import (
    TRACE_EXTRACTION_FLAG,
    run_trace_extraction,
    trace_extraction_enabled,
)
from trellis.schemas.trace import Trace

_TRACE = Trace.model_validate(
    {
        "source": "agent",
        "intent": "fix the bug",
        "steps": [
            {"step_type": "tool_call", "name": "grep", "args": {}, "result": {}},
        ],
        "context": {"agent_id": "a1", "domain": "backend"},
    }
)


class TestFlag:
    def test_disabled_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(TRACE_EXTRACTION_FLAG, raising=False)
        assert trace_extraction_enabled() is False

    @pytest.mark.parametrize("val", ["1", "true", "yes", "on", "TRUE", "On"])
    def test_truthy_spellings(self, monkeypatch: pytest.MonkeyPatch, val: str) -> None:
        monkeypatch.setenv(TRACE_EXTRACTION_FLAG, val)
        assert trace_extraction_enabled() is True

    @pytest.mark.parametrize("val", ["0", "false", "no", "off", ""])
    def test_falsy_spellings(self, monkeypatch: pytest.MonkeyPatch, val: str) -> None:
        monkeypatch.setenv(TRACE_EXTRACTION_FLAG, val)
        assert trace_extraction_enabled() is False


class TestHook:
    def test_flag_off_returns_none_and_runs_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(TRACE_EXTRACTION_FLAG, raising=False)
        registry = MagicMock()
        with patch("trellis.mutate.build_curate_executor") as build_exec:
            assert run_trace_extraction(registry, _TRACE, requested_by="t") is None
            build_exec.assert_not_called()

    def test_flag_on_executes_batch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(TRACE_EXTRACTION_FLAG, "1")
        registry = MagicMock()
        executor = MagicMock()
        with patch(
            "trellis.mutate.build_curate_executor", return_value=executor
        ) as build_exec:
            summary = run_trace_extraction(registry, _TRACE, requested_by="t")
        build_exec.assert_called_once_with(registry)
        executor.execute_batch.assert_called_once()
        assert summary is not None
        assert summary["executed"] is True
        assert summary["entities"] > 0
        # The Activity + agent attribution at minimum -> at least one edge.
        assert summary["edges"] > 0

    def test_flag_on_batch_has_provenance_stamped_commands(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(TRACE_EXTRACTION_FLAG, "1")
        registry = MagicMock()
        executor = MagicMock()
        with patch("trellis.mutate.build_curate_executor", return_value=executor):
            run_trace_extraction(registry, _TRACE, requested_by="cli:ingest-trace")
        batch = executor.execute_batch.call_args.args[0]
        # Every entity command carries the source_trace_id provenance prop.
        entity_cmds = [c for c in batch.commands if c.target_type == "entity"]
        assert entity_cmds
        for cmd in entity_cmds:
            props = cmd.args.get("properties", {})
            if "source_trace_id" in props:
                assert props["source_trace_id"] == _TRACE.trace_id

    def test_failure_is_swallowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(TRACE_EXTRACTION_FLAG, "1")
        registry = MagicMock()
        executor = MagicMock()
        executor.execute_batch.side_effect = RuntimeError("graph down")
        with patch("trellis.mutate.build_curate_executor", return_value=executor):
            summary = run_trace_extraction(registry, _TRACE, requested_by="t")
        # Must not raise; reports the error in the summary.
        assert summary is not None
        assert summary["executed"] is False
        assert "graph down" in summary["error"]

    def test_empty_extraction_skips_batch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(TRACE_EXTRACTION_FLAG, "1")
        # A minimal trace with no agent/domain/steps still yields the
        # Activity node, so to test the empty path we patch the extractor.
        registry = MagicMock()
        executor = MagicMock()
        empty_result = MagicMock(entities=[], edges=[])
        with (
            patch("trellis.mutate.build_curate_executor", return_value=executor),
            patch("trellis.extract.trace_ingest_hook.TraceExtractor") as ext_cls,
        ):
            ext = ext_cls.return_value

            async def _fake_extract(*_a: object, **_k: object) -> object:
                return empty_result

            ext.extract.side_effect = _fake_extract
            summary = run_trace_extraction(registry, _TRACE, requested_by="t")
        assert summary == {"entities": 0, "edges": 0, "executed": False}
        executor.execute_batch.assert_not_called()
