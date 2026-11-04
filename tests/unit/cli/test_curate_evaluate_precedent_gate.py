"""``trellis curate evaluate-precedent-gate``: counts and ids, report only."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from typer.testing import CliRunner

from tests.cli_output import plain
from tests.precedent_candidates import SENTINEL, recovering_trace
from trellis.learning.scoring import PROMOTE_RECOMMENDATIONS
from trellis.stores.base.event_log import Event, EventType
from trellis.stores.registry import StoreRegistry
from trellis_cli.main import app
from trellis_cli.stores import _reset_registry

if TYPE_CHECKING:
    from pathlib import Path

    from click.testing import Result

runner = CliRunner()
TARGET = "doc-target"
PROMOTE = min(PROMOTE_RECOMMENDATIONS)


@pytest.fixture
def stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    data_dir = tmp_path / "data"
    stores_dir = data_dir / "stores"
    stores_dir.mkdir(parents=True)
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))
    _reset_registry()
    reg = StoreRegistry(stores_dir=stores_dir)
    yield reg
    reg.close()


def _emit(stores: StoreRegistry, event: Event) -> None:
    stores.operational.event_log.append(event)


def _serve_and_grade(
    stores: StoreRegistry,
    pack_id: str,
    *,
    session: str,
    holdout: bool = False,
    served: list[str] | None = None,
) -> None:
    items = served or [TARGET, *(f"{pack_id}-x{i}" for i in range(59))]
    payload: dict[str, Any] = {
        "intent": f"find the ledger {SENTINEL}",
        "session_id": session,
        "injected_items": [
            {"item_id": item, "item_type": "document", "rank": n + 1}
            for n, item in enumerate(items)
        ],
    }
    if holdout:
        payload["holdout"] = True
    _emit(
        stores,
        Event(
            event_type=EventType.PACK_ASSEMBLED,
            source="test",
            entity_id=pack_id,
            entity_type="pack",
            payload=payload,
        ),
    )
    _emit(
        stores,
        Event(
            event_type=EventType.FEEDBACK_RECORDED,
            source="test",
            entity_id=f"fb-{pack_id}",
            payload={
                "pack_id": pack_id,
                "rating": 0.9,
                "success": True,
                "helpful_item_ids": [TARGET],
            },
        ),
    )


def _candidates_file(tmp_path: Path, *, helpful: int = 6, sessions: int = 4) -> Path:
    path = tmp_path / "intent_learning_candidates.json"
    path.write_text(
        json.dumps(
            {
                "candidates": [
                    {
                        "candidate_id": "general_context:abc",
                        "intent_family": "general_context",
                        "recommendation_type": PROMOTE,
                        "item_id": TARGET,
                        "title": f"ledger guidance {SENTINEL}",
                        "citation_evidence": {
                            "helpful_count": helpful,
                            "unhelpful_count": 0,
                            "distinct_helpful_sessions": sessions,
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return path


def _run(*args: str) -> Result:
    return runner.invoke(app, ["curate", "evaluate-precedent-gate", *args])


def _events(stores: StoreRegistry) -> int:
    return stores.operational.event_log.count()


def _seed_four_packs(stores: StoreRegistry) -> None:
    for i in range(4):
        _serve_and_grade(stores, f"pack-{i}", session=f"s{i % 2}")


def test_it_counts_both_gates_and_writes_nothing(
    stores: StoreRegistry, tmp_path: Path
) -> None:
    _seed_four_packs(stores)
    stores.operational.trace_store.append(recovering_trace())
    path = _candidates_file(tmp_path)
    before = _events(stores)

    result = _run("--candidates", str(path), "--dry-run", "--format", "json")

    payload = json.loads(result.stdout)
    assert result.exit_code == 0, result.output
    assert payload["mode"] == "dry-run"
    assert payload["stage2"]["evaluated"] == 1
    assert payload["stage2"]["current_gate_pass"] == 1
    assert payload["stage2"]["new_gate_pass"] == 1
    assert payload["stage2"]["both"] == 1
    assert [t["candidate_id"] for t in payload["stage2"]["targets"]] == [
        "general_context:abc"
    ]
    assert payload["stage1"]["traces_read"] == 1
    assert payload["stage1"]["would_mint"] == payload["stage1"]["candidates"] > 0
    assert payload["packs"]["graded"] == 4
    assert _events(stores) == before


def test_json_and_text_exit_alike_on_success_and_on_a_bad_file(
    stores: StoreRegistry, tmp_path: Path
) -> None:
    path = _candidates_file(tmp_path)
    missing = str(tmp_path / "nope.json")

    ok = [
        _run("--candidates", str(path), "--format", f).exit_code
        for f in ("json", "text")
    ]
    bad = [
        _run("--candidates", missing, "--format", f).exit_code for f in ("json", "text")
    ]

    assert ok == [0, 0]
    assert bad[0] == bad[1] != 0


def test_an_empty_window_reports_zeros_and_says_why(
    stores: StoreRegistry, tmp_path: Path
) -> None:
    result = _run("--candidates", str(_candidates_file(tmp_path)), "--format", "json")

    payload = json.loads(result.stdout)
    assert result.exit_code == 0
    assert payload["stage1"]["traces_read"] == 0
    assert payload["stage2"]["new_gate_pass"] == 0
    assert payload["stage2"]["failed_at"]["helpful"] == 1
    assert payload["stage2"]["without_graded_servings"] == 1
    assert payload["candidates_recorded"] == 0
    assert payload["candidates_with_servings"] == 0
    assert payload["scan"]["truncated"] is False
    assert any("never served" in note for note in payload["notes"])


def test_held_out_packs_and_their_feedback_are_dropped_and_counted(
    stores: StoreRegistry, tmp_path: Path
) -> None:
    _seed_four_packs(stores)
    # Three more helpful citations in withheld packs must not move any count.
    for i in range(3):
        _serve_and_grade(stores, f"held-{i}", session=f"h{i}", holdout=True)

    payload = json.loads(
        _run("--candidates", str(_candidates_file(tmp_path)), "--format", "json").stdout
    )

    assert payload["packs"]["held_out_dropped"] == 3
    assert payload["packs"]["feedback_held_out_dropped"] == 3
    assert payload["packs"]["graded"] == 4
    row = payload["stage2"]["targets"][0]
    assert (row["servings"], row["helpful"]) == (4, 4)


def test_truncation_is_stated_in_both_formats(
    stores: StoreRegistry, tmp_path: Path
) -> None:
    _seed_four_packs(stores)
    path = str(_candidates_file(tmp_path))

    as_json = json.loads(
        _run("--candidates", path, "--scan-limit", "2", "--format", "json").stdout
    )
    as_text = plain(_run("--candidates", path, "--scan-limit", "2").output)

    assert as_json["scan"]["truncated"] is True
    assert as_json["scan"]["pack_assembled"]["truncated"] is True
    assert any("truncated" in note for note in as_json["notes"])
    assert "truncated" in as_text


def test_recorded_candidates_have_no_servings(
    stores: StoreRegistry, tmp_path: Path
) -> None:
    stores.operational.trace_store.append(recovering_trace())
    applied = runner.invoke(
        app, ["worker", "distill-precedents", "--apply", "--format", "json"]
    )
    assert applied.exit_code == 0, applied.output
    minted = json.loads(applied.stdout)["recorded"]

    payload = json.loads(
        _run("--candidates", str(_candidates_file(tmp_path)), "--format", "json").stdout
    )

    assert payload["candidates_recorded"] == minted > 0
    assert payload["candidates_with_servings"] == 0
    assert payload["stage1"]["would_mint"] == 0
    assert payload["stage1"]["duplicates"] == minted


def test_a_served_recorded_candidate_is_counted(
    stores: StoreRegistry, tmp_path: Path
) -> None:
    stores.operational.trace_store.append(recovering_trace())
    runner.invoke(app, ["worker", "distill-precedents", "--apply", "--format", "json"])
    (recorded,) = stores.operational.event_log.get_events(
        event_type=EventType.PRECEDENT_CANDIDATE_MINED, limit=1
    )
    _serve_and_grade(
        stores, "p-served", session="s1", served=[recorded.entity_id, TARGET]
    )

    payload = json.loads(
        _run("--candidates", str(_candidates_file(tmp_path)), "--format", "json").stdout
    )

    assert payload["candidates_with_servings"] == 1


@pytest.mark.parametrize("fmt", ["json", "text"])
def test_no_item_intent_or_trace_text_reaches_the_output(
    stores: StoreRegistry, tmp_path: Path, fmt: str
) -> None:
    _seed_four_packs(stores)
    stores.operational.trace_store.append(recovering_trace())

    result = _run("--candidates", str(_candidates_file(tmp_path)), "--format", fmt)

    assert result.exit_code == 0
    assert SENTINEL not in result.output
    assert "find the ledger" not in result.output
    assert TARGET not in result.output
    assert "general_context:abc" in plain(result.output) or fmt == "text"


def test_the_text_report_names_each_stage(
    stores: StoreRegistry, tmp_path: Path
) -> None:
    _seed_four_packs(stores)

    text = plain(_run("--candidates", str(_candidates_file(tmp_path))).output)

    assert "stage 1 (mint)" in text
    assert "stage 2 (activate)" in text
    assert "new_gate_pass 1" in text
    assert "held out" in text
