"""CLI contract for the outcome backfill."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from typer.testing import CliRunner

from trellis.feedback.models import PackFeedback
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry
from trellis_cli.exit_codes import EXIT_VALIDATION
from trellis_cli.main import app
from trellis_cli.stores import _reset_registry

if TYPE_CHECKING:
    import pytest

runner = CliRunner()

PACK_ID = "pack-cli"


def _seed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, events: int = 1) -> Path:
    data_dir = tmp_path / "data"
    stores_dir = data_dir / "stores"
    stores_dir.mkdir(parents=True)
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    registry = StoreRegistry(stores_dir=stores_dir)
    event_log = registry.operational.event_log
    event_log.emit(
        EventType.PACK_ASSEMBLED,
        source="test",
        entity_id=PACK_ID,
        entity_type="pack",
        payload={
            "injected_item_ids": ["k1"],
            "injected_items": [
                {"item_id": "k1", "item_type": "document", "strategy_source": "keyword"}
            ],
            # A real domain, so the rendered cell scope is not `*/*/*`.
            # Rich deletes `[trellis-ai/plan/*]` and leaves `[*/*/*]`
            # alone, so an all-wildcard fixture cannot see the markup
            # defect at all.
            "domain": "trellis-ai",
        },
    )
    for index in range(events):
        feedback = PackFeedback(
            run_id=f"run-{index}",
            phase="retrieve",
            intent="fetch context",
            outcome="success",
            items_served=["k1"],
            items_referenced=["k1"],
            intent_family="plan",
        )
        event_log.emit(
            EventType.FEEDBACK_RECORDED,
            source="mcp",
            entity_id=PACK_ID,
            entity_type="pack",
            payload=feedback.to_event_payload(pack_id=PACK_ID),
        )
    registry.close()
    _reset_registry()
    return stores_dir


def _outcome_count(stores_dir: Path) -> int:
    registry = StoreRegistry(stores_dir=stores_dir)
    try:
        return registry.operational.outcome_store.count()
    finally:
        registry.close()


class TestBackfillOutcomesCommand:
    def test_rejects_an_unknown_format(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed(tmp_path, monkeypatch)

        result = runner.invoke(app, ["admin", "backfill-outcomes", "--format", "bogus"])

        assert result.exit_code == EXIT_VALIDATION, result.output
        assert "bogus" in result.output

    def test_dry_run_is_the_default_and_writes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stores_dir = _seed(tmp_path, monkeypatch)

        result = runner.invoke(app, ["admin", "backfill-outcomes", "--format", "json"])

        assert result.exit_code == 0, result.output
        payload: dict[str, Any] = json.loads(result.stdout)
        assert payload["status"] == "ok"
        assert payload["applied"] is False
        assert payload["events_replayable"] == 1
        assert payload["rows_planned"] == 2
        assert payload["rows_pending"] == 2
        assert payload["rows_written"] == 0
        assert _outcome_count(stores_dir) == 0

    def test_apply_writes_and_a_rerun_is_a_no_op(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stores_dir = _seed(tmp_path, monkeypatch)

        first = runner.invoke(
            app, ["admin", "backfill-outcomes", "--apply", "--format", "json"]
        )
        assert first.exit_code == 0, first.output
        assert json.loads(first.stdout)["rows_written"] == 2
        assert _outcome_count(stores_dir) == 2

        _reset_registry()
        second = runner.invoke(
            app, ["admin", "backfill-outcomes", "--apply", "--format", "json"]
        )
        assert second.exit_code == 0, second.output
        rerun = json.loads(second.stdout)
        assert rerun["rows_written"] == 0
        assert rerun["rows_already_present"] == 2
        assert _outcome_count(stores_dir) == 2

    def test_text_arm_names_the_dry_run_and_the_cells(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed(tmp_path, monkeypatch)

        result = runner.invoke(app, ["admin", "backfill-outcomes"])

        assert result.exit_code == 0, result.output
        assert "dry run" in result.output
        assert "--apply" in result.output
        assert "Tuner cells" in result.output
        # The cell's *axes*, not just a line where a cell should be: Rich
        # reads `[...]` as a style tag and renders `[trellis-ai/plan/*]`
        # as the empty string (#492), which leaves a plausible-looking
        # cell line naming no cell.
        assert "[trellis-ai/plan/*]" in result.output

    def test_truncation_exits_the_same_way_in_both_formats(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The parity rule this repo enforces (#437): a run that could not
        # read the whole window must not report success on one surface
        # and failure on the other.
        _seed(tmp_path, monkeypatch, events=3)

        as_json = runner.invoke(
            app,
            ["admin", "backfill-outcomes", "--event-limit", "2", "--format", "json"],
        )
        _reset_registry()
        as_text = runner.invoke(
            app,
            ["admin", "backfill-outcomes", "--event-limit", "2", "--format", "text"],
        )

        assert as_json.exit_code == EXIT_VALIDATION, as_json.output
        assert as_text.exit_code == as_json.exit_code, as_text.output
        payload = json.loads(as_json.stdout)
        assert payload["status"] == "error"
        assert payload["events_truncated"] is True
        assert "--event-limit" in as_text.output
