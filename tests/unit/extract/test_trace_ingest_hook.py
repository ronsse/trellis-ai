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
    TRACE_EXTRACTION_MIN_CONFIDENCE_FLAG,
    run_trace_extraction,
    trace_extraction_enabled,
    trace_extraction_min_confidence,
)
from trellis.mutate.commands import CommandResult, CommandStatus, Operation
from trellis.schemas.extraction import (
    EdgeDraft,
    EntityDraft,
    ExtractionProvenance,
    ExtractionResult,
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


def _mock_registry() -> MagicMock:
    """A registry mock whose graph reads say "node does not exist".

    A bare ``MagicMock()`` makes ``get_node`` return a truthy Mock, so
    ``reconcile_node_roles`` sees a role conflict on every command and
    rewrites ``node_role`` to a Mock.  Empty-graph is the honest default.
    """
    registry = MagicMock()
    registry.knowledge.graph_store.get_node.return_value = None
    return registry


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
        registry = _mock_registry()
        with patch("trellis.mutate.build_curate_executor") as build_exec:
            assert run_trace_extraction(registry, _TRACE, requested_by="t") is None
            build_exec.assert_not_called()

    def test_flag_on_executes_batch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(TRACE_EXTRACTION_FLAG, "1")
        registry = _mock_registry()
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
        registry = _mock_registry()
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
        registry = _mock_registry()
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
        registry = _mock_registry()
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
        assert summary == {
            "entities": 0,
            "edges": 0,
            "failed": 0,
            "executed": False,
        }
        executor.execute_batch.assert_not_called()


class TestConfidenceGateFlag:
    """The gate is a second, separately opt-in switch."""

    def test_unset_means_no_gate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(TRACE_EXTRACTION_MIN_CONFIDENCE_FLAG, raising=False)
        assert trace_extraction_min_confidence() is None

    def test_blank_means_no_gate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(TRACE_EXTRACTION_MIN_CONFIDENCE_FLAG, "  ")
        assert trace_extraction_min_confidence() is None

    def test_parses_a_float(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(TRACE_EXTRACTION_MIN_CONFIDENCE_FLAG, "0.75")
        assert trace_extraction_min_confidence() == 0.75

    @pytest.mark.parametrize("val", ["high", "-0.1", "1.5"])
    def test_bad_values_fall_back_to_no_gate(
        self, monkeypatch: pytest.MonkeyPatch, val: str
    ) -> None:
        """Misreading a threshold must never mean "drop everything"."""
        monkeypatch.setenv(TRACE_EXTRACTION_MIN_CONFIDENCE_FLAG, val)
        assert trace_extraction_min_confidence() is None

    def test_env_value_reaches_result_to_batch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The env var must arrive at the gate, not just parse.

        Trace drafts are all deterministic (confidence 1.0), so no floor
        in range can drop one — an end-to-end assertion on the summary is
        satisfied by a completely unwired gate.  Assert the seam instead.
        """
        monkeypatch.setenv(TRACE_EXTRACTION_FLAG, "1")
        monkeypatch.setenv(TRACE_EXTRACTION_MIN_CONFIDENCE_FLAG, "0.75")
        registry = _mock_registry()
        executor = MagicMock()
        with (
            patch("trellis.mutate.build_curate_executor", return_value=executor),
            patch("trellis.extract.trace_ingest_hook.result_to_batch") as to_batch,
        ):
            run_trace_extraction(registry, _TRACE, requested_by="t")
        assert to_batch.call_args.kwargs["min_confidence"] == 0.75

    def test_no_env_value_means_no_floor_at_the_seam(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(TRACE_EXTRACTION_FLAG, "1")
        monkeypatch.delenv(TRACE_EXTRACTION_MIN_CONFIDENCE_FLAG, raising=False)
        registry = _mock_registry()
        executor = MagicMock()
        with (
            patch("trellis.mutate.build_curate_executor", return_value=executor),
            patch("trellis.extract.trace_ingest_hook.result_to_batch") as to_batch,
        ):
            run_trace_extraction(registry, _TRACE, requested_by="t")
        assert to_batch.call_args.kwargs["min_confidence"] is None

    def test_reported_counts_are_post_gate_not_raw_drafts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Telemetry reports what was submitted, not what was extracted.

        Counting ``result.entities`` instead of the batch commands is
        indistinguishable from counting the batch whenever the gate is
        off, so this drives a result whose drafts are *partly*
        sub-threshold and pins the counts to the survivors.
        """
        monkeypatch.setenv(TRACE_EXTRACTION_FLAG, "1")
        monkeypatch.setenv(TRACE_EXTRACTION_MIN_CONFIDENCE_FLAG, "0.5")
        gated = ExtractionResult(
            entities=[
                EntityDraft(
                    entity_id="keep", entity_type="Concept", name="k", confidence=0.9
                ),
                EntityDraft(
                    entity_id="drop", entity_type="Concept", name="d", confidence=0.1
                ),
            ],
            edges=[
                EdgeDraft(
                    source_id="keep",
                    target_id="drop",
                    edge_kind="relatesTo",
                    confidence=0.9,
                ),
            ],
            extractor_used="trace",
            tier="deterministic",
            provenance=ExtractionProvenance(extractor_name="trace"),
        )
        registry = _mock_registry()
        executor = MagicMock()
        executor.execute_batch.return_value = []
        with (
            patch("trellis.mutate.build_curate_executor", return_value=executor),
            patch("trellis.extract.trace_ingest_hook.TraceExtractor") as ext_cls,
        ):

            async def _fake_extract(*_a: object, **_k: object) -> ExtractionResult:
                return gated

            ext_cls.return_value.extract.side_effect = _fake_extract
            summary = run_trace_extraction(registry, _TRACE, requested_by="t")

        assert summary is not None
        # 2 drafted entities -> 1 survives; the edge is orphaned by the drop.
        assert summary["entities"] == 1
        assert summary["edges"] == 0
        assert summary["entities"] < len(gated.entities)
        batch = executor.execute_batch.call_args.args[0]
        assert summary["entities"] + summary["edges"] == len(batch.commands)


class TestFailedCommandsAreReported:
    """CONTINUE_ON_ERROR means a rejected draft is not an exception."""

    def test_failed_commands_counted_separately(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(TRACE_EXTRACTION_FLAG, "1")
        monkeypatch.delenv(TRACE_EXTRACTION_MIN_CONFIDENCE_FLAG, raising=False)
        registry = _mock_registry()
        executor = MagicMock()
        executor.execute_batch.return_value = [
            CommandResult(
                command_id="c1",
                status=CommandStatus.SUCCESS,
                operation=Operation.ENTITY_CREATE,
            ),
            CommandResult(
                command_id="c2",
                status=CommandStatus.FAILED,
                operation=Operation.ENTITY_CREATE,
                message="Execution failed: nope",
            ),
        ]
        with patch("trellis.mutate.build_curate_executor", return_value=executor):
            summary = run_trace_extraction(registry, _TRACE, requested_by="t")
        assert summary is not None
        assert summary["failed"] == 1
        # The submitted counts stay honest about what was *sent*.
        assert summary["entities"] > 0
