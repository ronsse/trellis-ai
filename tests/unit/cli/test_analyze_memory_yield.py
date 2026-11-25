"""``trellis analyze memory-yield``: per creation path, created / served / graded.

Fixture documents are created through the real governed write path
(``build_evidence_ingest_command`` -> ``MutationExecutor``), so the classifier
is tested against the ``MUTATION_EXECUTED`` audit row that path stamps. Packs
are emitted as ``PACK_ASSEMBLED`` rows in the shape the builder writes, and
item-attributed feedback goes through ``record_feedback``; feedback without
item ids goes through the CLI family's ``feedback.record`` command.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from tests.cli_output import plain
from tests.unit.analyze.test_memory_yield_capture import seed_captures
from trellis.analyze.memory_yield import (
    BASIS_EVENT,
    BASIS_NONE,
    BASIS_SOURCE_SYSTEM,
    DUP_NEAR_DUPLICATE,
    DUP_SINGLETON,
    DUP_UNCHECKED,
    KIND_SUBAGENT,
    KIND_TOP_LEVEL,
    KIND_UNKNOWN,
    SESSION_CAPTURE_PATH,
    UNKNOWN_PATH,
    classify_creation_path,
    summarize_memory_yield,
)
from trellis.core.pack_holdout import HOLDOUT_KEY
from trellis.feedback.models import PackFeedback
from trellis.feedback.recording import record_feedback
from trellis.mutate import build_curate_executor
from trellis.mutate.commands import Command, CommandStatus, Operation
from trellis.mutate.evidence_ingest import build_evidence_ingest_command
from trellis.schemas.enums import OutcomeStatus, TraceSource
from trellis.schemas.trace import Outcome, Trace, TraceContext
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry
from trellis_cli.main import app
from trellis_cli.stores import _reset_registry

runner = CliRunner()

SURFACE_A = "test:surface-a"
SURFACE_B = "test:surface-b"


@pytest.fixture
def registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> StoreRegistry:
    data_dir = tmp_path / "data"
    stores_dir = data_dir / "stores"
    stores_dir.mkdir(parents=True)
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))
    _reset_registry()
    return StoreRegistry(stores_dir=stores_dir)


def _create(registry: StoreRegistry, doc_id: str, surface: str) -> None:
    executor = build_curate_executor(
        registry, evidence_embed="none", clearance="restricted"
    )
    command = build_evidence_ingest_command(
        content=f"invented note {doc_id}",
        requested_by=surface,
        doc_id=doc_id,
        embed_mode="none",
    )
    result = executor.execute(command)
    assert result.status == CommandStatus.SUCCESS, result.message


def _pack(
    registry: StoreRegistry,
    pack_id: str,
    item_ids: list[str],
    *,
    holdout: bool = False,
    sectioned: bool = False,
) -> None:
    payload: dict[str, Any] = {
        "injected_items": [
            {
                "item_id": item_id,
                "item_type": "document",
                "estimated_tokens": 10,
                "rank": rank,
                **({"section": "main"} if sectioned else {}),
            }
            for rank, item_id in enumerate(item_ids)
        ]
    }
    if sectioned:
        payload["sections"] = [{"name": "main"}]
    if holdout:
        payload[HOLDOUT_KEY] = True
    registry.operational.event_log.emit(
        EventType.PACK_ASSEMBLED,
        source="test",
        entity_id=pack_id,
        entity_type="pack",
        payload=payload,
    )


def _grade(
    registry: StoreRegistry,
    tmp_path: Path,
    pack_id: str,
    *,
    helpful: tuple[str, ...] = (),
    unhelpful: tuple[str, ...] = (),
) -> None:
    feedback = PackFeedback.from_agent_signal(
        run_id=f"run-{pack_id}",
        rating=0.9 if helpful else 0.1,
        helpful_item_ids=helpful,
        unhelpful_item_ids=unhelpful,
        pack_id=pack_id,
    )
    record_feedback(
        feedback,
        log_dir=tmp_path / "feedback",
        event_log=registry.operational.event_log,
        pack_id=pack_id,
    )


def _grade_without_items(registry: StoreRegistry, pack_id: str) -> None:
    executor = build_curate_executor(registry, clearance="restricted")
    result = executor.execute(
        Command(
            operation=Operation.FEEDBACK_RECORD,
            args={"target_id": pack_id, "rating": 0.8, "pack_id": pack_id},
            target_id=pack_id,
            target_type="pack",
            requested_by="cli:curate-feedback",
        )
    )
    assert result.status == CommandStatus.SUCCESS, result.message


def _seed(registry: StoreRegistry, tmp_path: Path) -> None:
    _create(registry, "doc-helpful", SURFACE_A)
    _create(registry, "doc-unhelpful", SURFACE_A)
    _create(registry, "doc-ungraded", SURFACE_A)
    _create(registry, "doc-never", SURFACE_A)
    _create(registry, "doc-parent", SURFACE_B)
    _create(registry, "doc-sectioned", SURFACE_B)
    _create(registry, "doc-held", SURFACE_B)
    # Written with no creation event and no source_system: unclassifiable.
    registry.knowledge.document_store.put("doc-orphan", "invented orphan", {})

    _pack(registry, "p1", ["doc-helpful", "doc-unhelpful", "doc-ungraded"])
    _grade(registry, tmp_path, "p1", helpful=("doc-helpful",))
    _grade(registry, tmp_path, "p1", unhelpful=("doc-unhelpful",))
    _pack(registry, "p2", ["doc-parent#chunk-0", "doc-orphan"])
    _grade(registry, tmp_path, "p2", helpful=("doc-parent#chunk-0",))
    _pack(registry, "p3", ["doc-sectioned"], sectioned=True)
    _grade(registry, tmp_path, "p3", helpful=("doc-sectioned",))
    _pack(registry, "p-held", ["doc-held"], holdout=True)
    _grade(registry, tmp_path, "p-held", helpful=("doc-held",))
    _grade_without_items(registry, "p1")


def _by_path(registry: StoreRegistry) -> dict[str, Any]:
    report = summarize_memory_yield(
        registry.knowledge.document_store, registry.operational.event_log, days=30
    )
    return {row.path: row for row in report.paths} | {"(all)": report}


class TestClassifier:
    def test_event_surface_wins(self) -> None:
        assert classify_creation_path("mcp:x", {"source_system": "y"}) == (
            "mcp:x",
            BASIS_EVENT,
        )

    def test_source_system_is_labelled_not_a_surface(self) -> None:
        assert classify_creation_path(None, {"source_system": "y"}) == (
            "source_system:y",
            BASIS_SOURCE_SYSTEM,
        )

    @pytest.mark.parametrize("metadata", [None, {}, {"source_system": "  "}])
    def test_nothing_recorded_is_unknown(self, metadata: Any) -> None:
        assert classify_creation_path("", metadata) == (UNKNOWN_PATH, BASIS_NONE)


class TestReport:
    def test_paths_partition_the_outcomes(
        self, registry: StoreRegistry, tmp_path: Path
    ) -> None:
        _seed(registry, tmp_path)
        rows = _by_path(registry)
        a = rows[SURFACE_A]
        assert a.basis == BASIS_EVENT
        assert (a.created, a.served, a.servings) == (4, 3, 3)
        assert (a.graded_helpful, a.graded_unhelpful, a.mixed) == (1, 1, 0)
        assert (a.served_ungraded, a.never_served) == (1, 1)
        assert (a.tokens_served, a.tokens_helpful, a.tokens_unhelpful) == (30, 10, 10)

    def test_a_chunk_is_credited_to_its_parents_path(
        self, registry: StoreRegistry, tmp_path: Path
    ) -> None:
        _seed(registry, tmp_path)
        b = _by_path(registry)[SURFACE_B]
        # doc-parent (via its chunk) and doc-sectioned, both helpful.
        assert b.graded_helpful == 2

    def test_a_sectioned_pack_is_counted(
        self, registry: StoreRegistry, tmp_path: Path
    ) -> None:
        _seed(registry, tmp_path)
        report = _by_path(registry)["(all)"]
        assert report.limits.sectioned_packs == 1
        assert _by_path(registry)[SURFACE_B].served == 2

    def test_a_held_out_pack_is_dropped_and_counted(
        self, registry: StoreRegistry, tmp_path: Path
    ) -> None:
        _seed(registry, tmp_path)
        rows = _by_path(registry)
        b = rows[SURFACE_B]
        # doc-held was served only in the withheld pack: never served here.
        assert (b.created, b.served, b.never_served) == (3, 2, 1)
        limits = rows["(all)"].limits
        assert limits.holdout_packs_dropped == 1
        assert limits.holdout_feedback_dropped == 1

    def test_an_unclassifiable_item_lands_in_unknown(
        self, registry: StoreRegistry, tmp_path: Path
    ) -> None:
        _seed(registry, tmp_path)
        rows = _by_path(registry)
        unknown = rows[UNKNOWN_PATH]
        assert (unknown.created, unknown.served, unknown.served_ungraded) == (1, 1, 1)
        assert rows["(all)"].totals.created == 8

    def test_feedback_without_item_ids_is_counted_in_limits(
        self, registry: StoreRegistry, tmp_path: Path
    ) -> None:
        _seed(registry, tmp_path)
        limits = _by_path(registry)["(all)"].limits
        assert limits.feedback_without_item_ids == 1
        assert limits.feedback_events == 5


class TestCli:
    def test_json_shape(self, registry: StoreRegistry, tmp_path: Path) -> None:
        _seed(registry, tmp_path)
        result = runner.invoke(
            app, ["analyze", "memory-yield", "--format", "json", "--no-meta-trace"]
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout.strip())
        keys = {"window_days", "since", "paths", "totals", "limits", "scan", "traces"}
        assert set(data) == keys
        assert data["totals"]["created"] == 8
        assert data["limits"]["holdout_packs_dropped"] == 1
        # The CLI reads the trace store; this fixture records no trace.
        assert data["traces"]["totals"]["recorded"] == 0

    def test_text_names_each_path(
        self, registry: StoreRegistry, tmp_path: Path
    ) -> None:
        _seed(registry, tmp_path)
        result = runner.invoke(app, ["analyze", "memory-yield", "--no-meta-trace"])
        assert result.exit_code == 0, result.output
        rendered = plain(result.stdout)
        assert f"{SURFACE_A} ({BASIS_EVENT}): created 4, served 3" in rendered
        assert "unknown (none): created 1" in rendered

    def test_exit_code_does_not_depend_on_format(
        self, registry: StoreRegistry, tmp_path: Path
    ) -> None:
        _seed(registry, tmp_path)
        text = runner.invoke(app, ["analyze", "memory-yield", "--no-meta-trace"])
        machine = runner.invoke(
            app, ["analyze", "memory-yield", "--format", "json", "--no-meta-trace"]
        )
        assert text.exit_code == machine.exit_code == 0

    def test_no_meta_trace_run_writes_nothing(
        self, registry: StoreRegistry, tmp_path: Path
    ) -> None:
        _seed(registry, tmp_path)

        def _footprint() -> tuple[int, int, int]:
            return (
                len(registry.operational.event_log.get_events(limit=100_000)),
                registry.knowledge.document_store.count(),
                len(registry.operational.trace_store.query(limit=100_000)),
            )

        before = _footprint()
        _reset_registry()
        for fmt in ("text", "json"):
            result = runner.invoke(
                app, ["analyze", "memory-yield", "--format", fmt, "--no-meta-trace"]
            )
            assert result.exit_code == 0, result.output
        assert _footprint() == before


def _seed_traces(registry: StoreRegistry, tmp_path: Path) -> None:
    """Two invented traces: one served as its Activity node and graded, one not."""
    for trace_id, status in (("t-cli-served", "failure"), ("t-cli-never", "success")):
        registry.operational.trace_store.append(
            Trace(
                trace_id=trace_id,
                source=TraceSource.AGENT,
                intent=f"invented secret intent {trace_id}",
                outcome=Outcome(status=OutcomeStatus(status)),
                context=TraceContext(agent_id="test-agent"),
            )
        )
    _pack(registry, "p-trace", ["trace:t-cli-served"])
    _grade(registry, tmp_path, "p-trace", helpful=("trace:t-cli-served",))


class TestCliTraces:
    def test_json_carries_the_trace_block(
        self, registry: StoreRegistry, tmp_path: Path
    ) -> None:
        _seed_traces(registry, tmp_path)
        result = runner.invoke(
            app, ["analyze", "memory-yield", "--format", "json", "--no-meta-trace"]
        )
        assert result.exit_code == 0, result.output
        traces = json.loads(result.stdout.strip())["traces"]
        assert traces["totals"]["recorded"] == 2
        assert traces["totals"]["served"] == 1
        assert traces["totals"]["graded_helpful"] == 1
        by_outcome = {row["outcome"]: row for row in traces["by_outcome"]}
        assert by_outcome["failure"]["served"] == 1
        assert by_outcome["success"]["never_served"] == 1
        assert traces["servings_by_form"] == {"graph_activity": 1}

    def test_text_prints_counts_not_ids_or_intents(
        self, registry: StoreRegistry, tmp_path: Path
    ) -> None:
        _seed_traces(registry, tmp_path)
        result = runner.invoke(app, ["analyze", "memory-yield", "--no-meta-trace"])
        assert result.exit_code == 0, result.output
        rendered = plain(result.stdout)
        assert "Traces recorded in the window" in rendered
        assert "(all): recorded 2, served 1 (1 servings), helpful 1" in rendered
        assert "graph_activity 1" in rendered
        assert "t-cli-" not in rendered
        assert "invented secret intent" not in rendered

    def test_exit_code_does_not_depend_on_format(
        self, registry: StoreRegistry, tmp_path: Path
    ) -> None:
        _seed_traces(registry, tmp_path)
        text = runner.invoke(app, ["analyze", "memory-yield", "--no-meta-trace"])
        machine = runner.invoke(
            app, ["analyze", "memory-yield", "--format", "json", "--no-meta-trace"]
        )
        assert text.exit_code == machine.exit_code == 0


CAPTURE_FLAG = "--capture-breakdown"


def _invoke(*extra: str) -> Any:
    result = runner.invoke(app, ["analyze", "memory-yield", "--no-meta-trace", *extra])
    assert result.exit_code == 0, result.output
    return result


class TestCliCaptureBreakdown:
    def test_flag_off_output_is_unchanged(
        self, registry: StoreRegistry, tmp_path: Path
    ) -> None:
        seed_captures(registry, tmp_path)
        off = json.loads(_invoke("--format", "json").stdout.strip())
        on = json.loads(_invoke("--format", "json", CAPTURE_FLAG).stdout.strip())
        old_keys = {"window_days", "since", "paths", "totals", "limits", "scan"}
        assert set(off) == old_keys | {"traces"}
        assert set(on) == set(off) | {"capture_breakdown"}
        # The window end is "now" per run; every other block is identical.
        for key in set(off) - {"since", "scan"}:
            assert on[key] == off[key], key
        off_text = plain(_invoke().stdout)
        on_text = plain(_invoke(CAPTURE_FLAG).stdout)
        assert "Session-capture breakdown" not in off_text
        assert on_text.startswith(off_text)

    def test_json_carries_the_block(
        self, registry: StoreRegistry, tmp_path: Path
    ) -> None:
        seed_captures(registry, tmp_path)
        data = json.loads(_invoke("--format", "json", CAPTURE_FLAG).stdout.strip())
        block = data["capture_breakdown"]
        assert block["path"] == SESSION_CAPTURE_PATH
        assert block["totals"]["created"] == 6
        kinds = {row["group"]: row["created"] for row in block["by_session_kind"]}
        assert kinds == {KIND_SUBAGENT: 3, KIND_TOP_LEVEL: 2, KIND_UNKNOWN: 1}
        dups = {row["group"]: row["created"] for row in block["by_duplication"]}
        assert dups == {DUP_NEAR_DUPLICATE: 4, DUP_SINGLETON: 1, DUP_UNCHECKED: 1}
        assert block["duplication"]["clusters"] == 2
        assert len(block["cross"]) == 9

    def test_bound_flag_reaches_the_predicate(
        self, registry: StoreRegistry, tmp_path: Path
    ) -> None:
        seed_captures(registry, tmp_path)
        data = json.loads(
            _invoke(
                "--format", "json", CAPTURE_FLAG, "--capture-max-docs", "2"
            ).stdout.strip()
        )
        dup = data["capture_breakdown"]["duplication"]
        assert (dup["max_docs"], dup["probed"], dup["over_bound"]) == (2, 2, 4)

    def test_text_prints_counts_not_ids_or_sessions(
        self, registry: StoreRegistry, tmp_path: Path
    ) -> None:
        seed_captures(registry, tmp_path)
        rendered = plain(_invoke(CAPTURE_FLAG).stdout)
        assert "Session-capture breakdown" in rendered
        assert f"{KIND_SUBAGENT}: created 3, served 2" in rendered
        assert f"{KIND_SUBAGENT} x {DUP_NEAR_DUPLICATE}: created 2" in rendered
        for leaked in ("c-sub-", "c-top-", "agent-a", "invented capture"):
            assert leaked not in rendered

    def test_exit_code_does_not_depend_on_format(
        self, registry: StoreRegistry, tmp_path: Path
    ) -> None:
        seed_captures(registry, tmp_path)
        text = runner.invoke(
            app, ["analyze", "memory-yield", "--no-meta-trace", CAPTURE_FLAG]
        )
        machine = runner.invoke(
            app,
            [
                "analyze",
                "memory-yield",
                "--no-meta-trace",
                "--format",
                "json",
                CAPTURE_FLAG,
            ],
        )
        assert text.exit_code == machine.exit_code == 0

    def test_run_writes_nothing(self, registry: StoreRegistry, tmp_path: Path) -> None:
        seed_captures(registry, tmp_path)

        def _footprint() -> tuple[int, int, int]:
            return (
                len(registry.operational.event_log.get_events(limit=100_000)),
                registry.knowledge.document_store.count(),
                registry.knowledge.vector_store.count(),
            )

        before = _footprint()
        _reset_registry()
        for fmt in ("text", "json"):
            _invoke("--format", fmt, CAPTURE_FLAG)
        assert _footprint() == before
