"""Re-running a promotions file does not re-promote rows that already landed (#983).

A mixed run (one row landed, one named a candidate that is not there) exits
``2`` / ``refused``, and the operator's next move is to fix the row and run the
same file again. ``submit_learning_promotion`` refused re-approval only for a
precedent that was not current, so the second run wrote the landed row a second
time: a new SCD-2 version at the same id and a second ``PRECEDENT_PROMOTED``
event. A row whose precedent is current, complete and unchanged is now reported
``already_promoted`` and written not at all.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from tests.cli_output import plain
from trellis.learning.scoring import precedent_entity_id_for_candidate
from trellis.stores.base.event_log import EventType
from trellis_cli import curate as curate_cli
from trellis_cli.exit_codes import EXIT_OK, EXIT_VALIDATION
from trellis_cli.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _temp_stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = tmp_path / "data"
    (data_dir / "stores").mkdir(parents=True)
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))


def _candidate(candidate_id: str) -> dict[str, Any]:
    return {
        "candidate_id": candidate_id,
        "intent_family": "fam",
        "recommendation_type": "promote_guidance",
        "item_id": f"doc:{candidate_id}",
        "item_type": "document",
        "title": f"title {candidate_id}",
        "precedent_name": f"Learning: {candidate_id}",
        "precedent_properties": {"source_of_truth": "reviewed_promotion"},
        "target_entity_ids": [],
        "supporting_run_ids": ["run-1"],
        "phases": [],
        "domain_systems": [],
    }


def _run(
    tmp_path: Path,
    candidates: list[dict[str, Any]],
    decisions: list[tuple[str, bool]],
    *,
    fmt: str = "json",
    dry_run: bool = False,
    rationale: str = "r",
) -> tuple[int, Any, str]:
    candidates_path = tmp_path / "candidates.json"
    decisions_path = tmp_path / "decisions.json"
    candidates_path.write_text(json.dumps({"candidates": candidates}), encoding="utf-8")
    decisions_path.write_text(
        json.dumps(
            {
                "decisions": [
                    {"candidate_id": cid, "approved": approved, "rationale": rationale}
                    for cid, approved in decisions
                ]
            }
        ),
        encoding="utf-8",
    )
    result = runner.invoke(
        app,
        [
            "curate",
            "promote-learning",
            "--candidates",
            str(candidates_path),
            "--decisions",
            str(decisions_path),
            *(["--dry-run"] if dry_run else []),
            "--format",
            fmt,
        ],
    )
    data = json.loads(result.stdout.strip()) if fmt == "json" else None
    return result.exit_code, data, result.stdout


def _writes(candidate_id: str) -> tuple[int, int]:
    """``(SCD-2 versions, PRECEDENT_PROMOTED events)`` for the candidate's precedent."""
    registry = curate_cli._get_registry()
    try:
        entity_id = precedent_entity_id_for_candidate(candidate_id)
        return (
            len(registry.knowledge.graph_store.get_node_history(entity_id)),
            len(
                registry.operational.event_log.get_events(
                    event_type=EventType.PRECEDENT_PROMOTED, entity_id=entity_id
                )
            ),
        )
    finally:
        registry.close()


def test_rerun_of_a_refused_file_does_not_repromote_the_landed_row(
    tmp_path: Path,
) -> None:
    """The reported shape: one row lands, one is missing, then the file runs again."""
    candidates = [_candidate("landed")]
    decisions = [("landed", True), ("ghost", True)]

    code, first, _ = _run(tmp_path, candidates, decisions)
    assert code == EXIT_VALIDATION
    assert [r["status"] for r in first["results"]] == ["promoted", "missing_candidate"]
    assert _writes("landed") == (1, 1)

    code, second, _ = _run(tmp_path, candidates, decisions)

    # The ghost row still refuses the run; the landed row writes nothing more,
    # and says so in its own row rather than reading as a second promotion.
    assert code == EXIT_VALIDATION
    assert second["status"] == "refused"
    assert [r["status"] for r in second["results"]] == [
        "already_promoted",
        "missing_candidate",
    ]
    assert second["results"][0]["entity_id"] == precedent_entity_id_for_candidate(
        "landed"
    )
    assert _writes("landed") == (1, 1)
    assert second["promoted_count"] == 0
    assert second["already_promoted_count"] == 1


def test_fixing_the_refused_row_and_rerunning_the_file_exits_clean(
    tmp_path: Path,
) -> None:
    """The point of the skip: a corrected file runs to ``ok`` instead of refusing."""
    decisions = [("landed", True), ("late", True)]

    code, first, _ = _run(tmp_path, [_candidate("landed")], decisions)
    assert code == EXIT_VALIDATION
    assert [r["status"] for r in first["results"]] == ["promoted", "missing_candidate"]

    # ``late`` now exists in the candidates artifact: the operator fixed the row.
    code, second, _ = _run(
        tmp_path, [_candidate("landed"), _candidate("late")], decisions
    )

    assert code == EXIT_OK
    assert second["status"] == "ok"
    assert [r["status"] for r in second["results"]] == ["already_promoted", "promoted"]
    assert second["promoted_count"] == 1
    assert second["already_promoted_count"] == 1
    assert _writes("landed") == (1, 1)
    assert _writes("late") == (1, 1)


def test_text_run_names_the_skipped_row(tmp_path: Path) -> None:
    candidates = [_candidate("landed")]
    _run(tmp_path, candidates, [("landed", True)], fmt="text")

    code, _, raw = _run(tmp_path, candidates, [("landed", True)], fmt="text")
    out = plain(raw)  # a coloured run puts escape codes between the words

    assert code == EXIT_OK
    assert "already_promoted" in out
    assert "1 already promoted" in out
    assert _writes("landed") == (1, 1)


def test_dry_run_previews_the_skip(tmp_path: Path) -> None:
    """A dry run and a real run must not disagree about the row."""
    candidates = [_candidate("landed")]
    decisions = [("landed", True)]
    _run(tmp_path, candidates, decisions)

    code, data, _ = _run(tmp_path, candidates, decisions, dry_run=True)

    assert code == EXIT_OK
    assert data["results"][0]["would_status"] == "already_promoted"
    assert data["would_verdict"] == "ok"
    assert _writes("landed") == (1, 1)


def test_a_changed_rationale_is_still_written(tmp_path: Path) -> None:
    """Skipped only when the write would add nothing: new content still lands."""
    candidates = [_candidate("landed")]
    _run(tmp_path, candidates, [("landed", True)], rationale="first reason")

    code, data, _ = _run(
        tmp_path, candidates, [("landed", True)], rationale="second reason"
    )

    assert code == EXIT_OK
    assert [r["status"] for r in data["results"]] == ["promoted"]
    assert _writes("landed") == (2, 2)
