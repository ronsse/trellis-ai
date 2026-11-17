"""``trellis analyze pack-scenario``: a served, graded pack as an eval scenario.

Packs are emitted as ``PACK_ASSEMBLED`` rows in the shape the builder writes
(``intent``, ``domain``, ``injected_items`` with ``item_type`` and
``category``). Feedback of the full family goes through ``record_feedback``;
the governed family through the executor's ``feedback.record`` command.
Every intent below is invented.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from trellis.analyze.pack_scenario import (
    REFUSED_HELD_OUT,
    REFUSED_NO_INTENT,
    REFUSED_SECTIONED,
    REFUSED_UNKNOWN,
    VERDICT_ANSWERED,
    VERDICT_UNANSWERED,
    VERDICT_UNGRADED,
    export_pack_scenarios,
)
from trellis.core.pack_holdout import HOLDOUT_KEY
from trellis.feedback.models import PackFeedback
from trellis.feedback.recording import record_feedback
from trellis.mutate import build_curate_executor
from trellis.mutate.commands import Command, CommandStatus, Operation
from trellis.retrieve.evaluate import EvaluationScenario
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry
from trellis_cli.analyze import _load_scenarios
from trellis_cli.exit_codes import EXIT_VALIDATION
from trellis_cli.main import app
from trellis_cli.stores import _reset_registry

runner = CliRunner()

INTENT_GOOD = "how do I recalibrate the widget frobnicator"
INTENT_BAD = "summarize the teapot humidity notes"

ROWS = [
    ("doc-a", "document", "pattern"),
    ("doc-b", "document", "decision"),
    ("ent-c", "entity", None),
    ("vec-d", "vector", "error-resolution"),
]


@pytest.fixture
def registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> StoreRegistry:
    data_dir = tmp_path / "data"
    stores_dir = data_dir / "stores"
    stores_dir.mkdir(parents=True)
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))
    _reset_registry()
    return StoreRegistry(stores_dir=stores_dir)


def _pack(
    registry: StoreRegistry,
    pack_id: str,
    intent: str | None = INTENT_GOOD,
    rows: list[tuple[str, str, str | None]] | None = None,
    *,
    domain: str | None = "testing",
    holdout: bool = False,
    sectioned: bool = False,
) -> None:
    injected = []
    for rank, (item_id, item_type, category) in enumerate(rows or ROWS, start=1):
        row: dict[str, Any] = {
            "item_id": item_id,
            "item_type": item_type,
            "rank": rank,
            "category": category,
        }
        if sectioned:
            row["section"] = "main"
        injected.append(row)
    payload: dict[str, Any] = {
        "domain": domain,
        "intent_family": "general_context",
        "injected_items": [] if holdout else injected,
    }
    if intent is not None:
        payload["intent"] = intent
    if holdout:
        payload[HOLDOUT_KEY] = True
        payload["holdout_items"] = injected
    if sectioned:
        payload["sections"] = [{"name": "main", "items_count": len(injected)}]
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
    rating: float,
    *,
    helpful: tuple[str, ...] = (),
    unhelpful: tuple[str, ...] = (),
) -> None:
    feedback = PackFeedback.from_agent_signal(
        run_id=f"run-{pack_id}",
        rating=rating,
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


def _grade_governed(registry: StoreRegistry, pack_id: str, rating: float) -> None:
    executor = build_curate_executor(registry, clearance="restricted")
    result = executor.execute(
        Command(
            operation=Operation.FEEDBACK_RECORD,
            args={"target_id": pack_id, "rating": rating},
            target_id=pack_id,
            target_type="pack",
            requested_by="cli:curate-feedback",
        )
    )
    assert result.status == CommandStatus.SUCCESS, result.message


def _export(registry: StoreRegistry, *pack_ids: str) -> Any:
    return export_pack_scenarios(registry.operational.event_log, list(pack_ids))


def _invoke(*args: str) -> Any:
    return runner.invoke(app, ["analyze", "pack-scenario", *args, "--no-meta-trace"])


class TestExport:
    def test_full_family_derives_from_helpful_items_only(
        self, registry: StoreRegistry, tmp_path: Path
    ) -> None:
        _pack(registry, "p-good")
        _grade(
            registry,
            tmp_path,
            "p-good",
            0.9,
            helpful=("doc-a", "ent-c", "vec-d"),
            unhelpful=("doc-b",),
        )
        result = _export(registry, "p-good")
        assert result.refusals == []
        (scenario,) = result.scenarios
        assert scenario.intent == INTENT_GOOD
        assert scenario.domain == "testing"
        assert scenario.required_coverage == []
        # ent-c carried no category: it adds no category, and is counted.
        assert scenario.expected_categories == ["error-resolution", "pattern"]
        assert result.limits.helpful_items_without_category == 1
        assert scenario.expected_shapes is not None
        shapes = {
            k: (v.min_count, v.max_count) for k, v in scenario.expected_shapes.items()
        }
        assert shapes == {
            "document": (1, None),
            "entity": (1, None),
            "vector": (1, None),
        }
        meta = scenario.metadata
        assert meta["source_pack_id"] == "p-good"
        assert meta["verdict"] == VERDICT_ANSWERED
        assert meta["rating"] == 0.9
        assert meta["helpful_item_ids"] == ["doc-a", "ent-c", "vec-d"]
        assert meta["unhelpful_item_ids"] == ["doc-b"]
        assert meta["served_items"] == [
            {"item_id": item_id, "item_type": item_type}
            for item_id, item_type, _ in ROWS
        ]
        assert meta["assembled_at"]

    def test_governed_family_grades_but_names_no_items(
        self, registry: StoreRegistry
    ) -> None:
        _pack(registry, "p-gov", INTENT_BAD)
        _grade_governed(registry, "p-gov", 0.1)
        (scenario,) = _export(registry, "p-gov").scenarios
        assert scenario.metadata["verdict"] == VERDICT_UNANSWERED
        assert scenario.metadata["rating"] == 0.1
        assert scenario.metadata["helpful_item_ids"] == []
        assert scenario.expected_categories == []
        assert scenario.expected_shapes is None

    def test_latest_verdict_decides(
        self, registry: StoreRegistry, tmp_path: Path
    ) -> None:
        _pack(registry, "p-two")
        _grade(registry, tmp_path, "p-two", 0.9, helpful=("doc-a",))
        _grade(registry, tmp_path, "p-two", 0.1)
        result = _export(registry, "p-two")
        assert result.scenarios[0].metadata["verdict"] == VERDICT_UNANSWERED
        assert result.limits.packs_with_conflicting_verdicts == 1

    def test_ungraded_exports_with_empty_expectations(
        self, registry: StoreRegistry
    ) -> None:
        _pack(registry, "p-none")
        result = _export(registry, "p-none")
        (scenario,) = result.scenarios
        assert scenario.metadata["verdict"] == VERDICT_UNGRADED
        assert scenario.metadata["rating"] is None
        assert scenario.expected_categories == []
        assert scenario.expected_shapes is None
        assert result.limits.ungraded_exported == 1

    def test_unserved_citation_is_a_stray(
        self, registry: StoreRegistry, tmp_path: Path
    ) -> None:
        _pack(registry, "p-stray")
        _grade(registry, tmp_path, "p-stray", 0.9, helpful=("doc-a", "not-served"))
        result = _export(registry, "p-stray")
        assert result.limits.stray_citations == 1
        assert result.scenarios[0].expected_shapes is not None
        assert set(result.scenarios[0].expected_shapes) == {"document"}

    def test_held_out_pack_is_refused(
        self, registry: StoreRegistry, tmp_path: Path
    ) -> None:
        _pack(registry, "p-held", holdout=True)
        _grade(registry, tmp_path, "p-held", 0.1)
        result = _export(registry, "p-held")
        assert result.scenarios == []
        assert [r.reason for r in result.refusals] == [REFUSED_HELD_OUT]
        assert result.limits.held_out_refused == 1

    def test_no_intent_is_refused(self, registry: StoreRegistry) -> None:
        _pack(registry, "p-blank", "   ")
        _pack(registry, "p-missing", None)
        result = _export(registry, "p-blank", "p-missing")
        assert result.scenarios == []
        assert [r.reason for r in result.refusals] == [REFUSED_NO_INTENT] * 2
        assert result.limits.no_intent_refused == 2

    def test_sectioned_pack_is_refused(
        self, registry: StoreRegistry, tmp_path: Path
    ) -> None:
        _pack(registry, "p-sec", sectioned=True)
        _grade(registry, tmp_path, "p-sec", 0.9, helpful=("doc-a",))
        result = _export(registry, "p-sec")
        assert result.scenarios == []
        assert [r.reason for r in result.refusals] == [REFUSED_SECTIONED]
        assert result.limits.sectioned_refused == 1

    def test_unknown_pack_is_refused(self, registry: StoreRegistry) -> None:
        result = _export(registry, "p-ghost")
        assert [r.reason for r in result.refusals] == [REFUSED_UNKNOWN]
        assert result.limits.unknown_refused == 1

    def test_names_are_unique_and_deterministic(
        self, registry: StoreRegistry, tmp_path: Path
    ) -> None:
        # "p/x" and "p:x" slug to the same text; repeated ids export once.
        for pack_id in ("p/x", "p:x", "p-y"):
            _pack(registry, pack_id)
        result = _export(registry, "p/x", "p:x", "p-y", "p-y")
        names = [s.name for s in result.scenarios]
        assert len(names) == len(set(names)) == 3
        assert result.limits.duplicate_pack_ids == 1
        again = _export(registry, "p/x", "p:x", "p-y")
        assert [s.name for s in again.scenarios] == names


class TestCli:
    def _seed(self, registry: StoreRegistry, tmp_path: Path) -> None:
        _pack(registry, "p-good")
        _grade(registry, tmp_path, "p-good", 0.9, helpful=("doc-a", "ent-c"))
        _pack(registry, "p-gov", INTENT_BAD)
        _grade_governed(registry, "p-gov", 0.1)
        _pack(registry, "p-none")

    def test_round_trip_through_loader_and_no_assemble(
        self, registry: StoreRegistry, tmp_path: Path
    ) -> None:
        self._seed(registry, tmp_path)
        out = tmp_path / "scenarios.yaml"
        args = ["--pack-id", "p-good", "--pack-id", "p-gov", "--pack-id", "p-none"]
        result = _invoke(*args, "--out", str(out))
        assert result.exit_code == 0, result.output
        loaded = _load_scenarios(out)
        assert [s.metadata["source_pack_id"] for s in loaded] == [
            "p-good",
            "p-gov",
            "p-none",
        ]
        assert all(isinstance(s, EvaluationScenario) for s in loaded)
        check = runner.invoke(
            app,
            [
                "analyze",
                "pack-quality",
                "--scenarios",
                str(out),
                "--no-assemble",
                "--format",
                "json",
                "--no-meta-trace",
            ],
        )
        assert check.exit_code == 0, check.output
        parsed = json.loads(check.stdout.strip())
        assert [s["name"] for s in parsed["scenarios"]] == [s.name for s in loaded]

    def test_stdout_is_loadable_yaml(
        self, registry: StoreRegistry, tmp_path: Path
    ) -> None:
        self._seed(registry, tmp_path)
        result = _invoke("--pack-id", "p-good")
        assert result.exit_code == 0, result.output
        raw = yaml.safe_load(result.stdout)
        (entry,) = raw["scenarios"]
        assert EvaluationScenario(**entry).intent == INTENT_GOOD

    def test_json_carries_scenarios_and_limits(
        self, registry: StoreRegistry, tmp_path: Path
    ) -> None:
        self._seed(registry, tmp_path)
        result = _invoke("--pack-id", "p-good", "--format", "json")
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout.strip())
        assert data["status"] == "ok"
        assert data["scenarios"][0]["metadata"]["source_pack_id"] == "p-good"
        assert "statements" in data["limits"]

    @pytest.mark.parametrize("fmt", ["text", "json"])
    def test_unknown_id_exit_is_format_independent(
        self, registry: StoreRegistry, tmp_path: Path, fmt: str
    ) -> None:
        self._seed(registry, tmp_path)
        out = tmp_path / f"never-{fmt}.yaml"
        result = _invoke(
            "--pack-id",
            "p-good",
            "--pack-id",
            "p-ghost",
            "--out",
            str(out),
            "--format",
            fmt,
        )
        assert result.exit_code == EXIT_VALIDATION, result.output
        # A refusal writes nothing, not a partial file.
        assert not out.exists()
        if fmt == "json":
            data = json.loads(result.stdout.strip())
            assert data["status"] == "refused"
            assert data["refusals"][0]["reason"] == REFUSED_UNKNOWN

    @pytest.mark.parametrize("fmt", ["text", "json"])
    def test_held_out_refused_on_both_surfaces(
        self, registry: StoreRegistry, tmp_path: Path, fmt: str
    ) -> None:
        _pack(registry, "p-held", holdout=True)
        result = _invoke("--pack-id", "p-held", "--format", fmt)
        assert result.exit_code == EXIT_VALIDATION, result.output

    @pytest.mark.parametrize("fmt", ["text", "json"])
    def test_out_refuses_to_overwrite(
        self, registry: StoreRegistry, tmp_path: Path, fmt: str
    ) -> None:
        self._seed(registry, tmp_path)
        out = tmp_path / "taken.yaml"
        out.write_text("keep me\n", encoding="utf-8")
        result = _invoke("--pack-id", "p-good", "--out", str(out), "--format", fmt)
        assert result.exit_code == EXIT_VALIDATION, result.output
        assert out.read_text(encoding="utf-8") == "keep me\n"
        forced = _invoke(
            "--pack-id", "p-good", "--out", str(out), "--force", "--format", fmt
        )
        assert forced.exit_code == 0, forced.output
        assert _load_scenarios(out)[0].metadata["source_pack_id"] == "p-good"

    def test_no_meta_trace_run_writes_nothing(
        self, registry: StoreRegistry, tmp_path: Path
    ) -> None:
        self._seed(registry, tmp_path)

        def _footprint() -> tuple[int, int, int]:
            return (
                len(registry.operational.event_log.get_events(limit=100_000)),
                registry.knowledge.document_store.count(),
                len(registry.operational.trace_store.query(limit=100_000)),
            )

        before = _footprint()
        _reset_registry()
        for fmt in ("text", "json"):
            result = _invoke("--pack-id", "p-good", "--format", fmt)
            assert result.exit_code == 0, result.output
        assert _footprint() == before
