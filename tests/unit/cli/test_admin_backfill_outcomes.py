"""CLI contract for the outcome backfill."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from typer.testing import CliRunner

from tests.cli_output import plain
from trellis.core.base import SCHEMA_VERSION
from trellis.feedback.models import PackFeedback
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry
from trellis_cli.exit_codes import EXIT_INTERNAL, EXIT_VALIDATION
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


def _seed_legacy_nan_row(stores_dir: Path) -> None:
    """Write a FEEDBACK_RECORDED row carrying a non-finite relevance score.

    ``SQLiteEventLog.append`` dumps with ``allow_nan=False`` (#831), so no
    call through the normal API can seed this. A raw INSERT simulates a row
    a pre-#831 build already wrote: ``json.dumps`` here defaults to
    ``allow_nan=True``, landing the non-standard ``NaN`` token on disk, and
    the event log's own read path (plain, lenient ``json.loads``) parses it
    straight back into a live Python float for the backfill to replay.
    """
    feedback = PackFeedback(
        run_id="run-legacy",
        phase="retrieve",
        intent="fetch context",
        outcome="success",
        items_served=["k1"],
        items_referenced=["k1"],
        intent_family="plan",
        relevance_scores={"k1": float("nan")},
    )
    payload = feedback.to_event_payload(pack_id=PACK_ID)
    now = datetime.now(UTC) - timedelta(days=1)
    conn = sqlite3.connect(
        str(stores_dir / "events.db"), timeout=0, isolation_level=None
    )
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO events ("
            " event_id, event_type, source, entity_id, entity_type,"
            " occurred_at, recorded_at, payload_json, metadata_json, schema_version"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "event-legacy-nan",
                EventType.FEEDBACK_RECORDED.value,
                "mcp",
                PACK_ID,
                "pack",
                now.isoformat(),
                now.isoformat(),
                json.dumps(payload),
                json.dumps({}),
                SCHEMA_VERSION,
            ),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()


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
        out = plain(result.output)
        assert "dry run" in out
        assert "--apply" in out
        assert "Tuner cells" in out
        # The cell's *axes*, not just a line where a cell should be: Rich
        # reads `[...]` as a style tag and renders `[trellis-ai/plan/*]`
        # as the empty string (#492), which leaves a plausible-looking
        # cell line naming no cell.
        assert "[trellis-ai/plan/*]" in out

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


class TestALegacyNonFiniteRowIsADescribedFailure:
    """Replaying a pre-#831 NaN row must not surface a bare traceback (#840).

    ``backfill_outcomes``'s ``--apply`` path writes through the real
    ``OutcomeStore``, whose ``append_many`` dumps with ``allow_nan=False``
    (#835) and raises an untyped ``ValueError`` on the first non-finite
    score it meets, chunk-atomically rolling back every row alongside it.
    Before this fix that ``ValueError`` was uncaught at the CLI boundary
    (the global catch in ``trellis_cli.main`` only catches ``TrellisError``
    and ``PackAssemblyError``), so it reached the operator as a bare
    traceback instead of a described failure.
    """

    def test_apply_reports_a_described_failure_instead_of_a_traceback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stores_dir = _seed(tmp_path, monkeypatch, events=0)
        _seed_legacy_nan_row(stores_dir)

        result = runner.invoke(
            app, ["admin", "backfill-outcomes", "--apply", "--format", "json"]
        )

        assert result.exit_code == EXIT_INTERNAL, result.output
        payload: dict[str, Any] = json.loads(result.stdout)
        assert payload["status"] == "error"
        assert payload["error_type"] == "ValueError"
        assert _outcome_count(stores_dir) == 0

    def test_apply_exits_the_same_way_in_both_formats(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stores_dir = _seed(tmp_path, monkeypatch, events=0)
        _seed_legacy_nan_row(stores_dir)

        as_json = runner.invoke(
            app, ["admin", "backfill-outcomes", "--apply", "--format", "json"]
        )
        _reset_registry()
        as_text = runner.invoke(app, ["admin", "backfill-outcomes", "--apply"])

        assert as_json.exit_code == EXIT_INTERNAL, as_json.output
        assert as_text.exit_code == as_json.exit_code, as_text.output
        # Exited through typer.Exit, not an escaped exception, and the
        # message surfaces.
        assert isinstance(as_text.exception, SystemExit), as_text.exception
        assert "not JSON compliant" in plain(as_text.output)
