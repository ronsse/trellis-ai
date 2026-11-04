"""``trellis analyze replay --entity-requires-description / --min-distinct-words``.

The arms read the graph and document stores as they are now (nothing in
``PACK_ASSEMBLED`` records a description flag or the served text), and the
report says so. Counts below are computed by hand from ``_seed``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.cli_output import plain
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry
from trellis_cli.exit_codes import EXIT_VALIDATION
from trellis_cli.main import app
from trellis_cli.stores import _reset_registry

runner = CliRunner()

_RUNBOOK_BODY = "Confidential runbook wording that must never be echoed"


@pytest.fixture(autouse=True)
def _temp_stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> StoreRegistry:
    data_dir = tmp_path / "data"
    stores_dir = data_dir / "stores"
    stores_dir.mkdir(parents=True)
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))
    _reset_registry()
    return StoreRegistry(stores_dir=stores_dir)


@pytest.fixture
def seeded(_temp_stores: StoreRegistry) -> StoreRegistry:
    """One pack: a described entity, a stub, an entity the graph lost, a document.

    ``e_full`` helpful 100 tok, ``e_stub`` unhelpful 40, ``e_gone`` unjudged 10,
    ``d_short`` unhelpful 20 (two words).
    """
    knowledge = _temp_stores.knowledge
    knowledge.graph_store.upsert_node(
        "e_full", "concept", {"name": "Runbook", "description": _RUNBOOK_BODY}
    )
    knowledge.graph_store.upsert_node("e_stub", "concept", {"name": "Stub"})
    knowledge.document_store.put("d_short", "two words", {})
    rows = [
        ("e_full", "entity", 100),
        ("e_stub", "entity", 40),
        ("e_gone", "entity", 10),
        ("d_short", "document", 20),
    ]
    log = _temp_stores.operational.event_log
    log.emit(
        EventType.PACK_ASSEMBLED,
        source="test",
        entity_id="pack_1",
        entity_type="pack",
        payload={
            "pack_id": "pack_1",
            "injected_items": [
                {
                    "item_id": item_id,
                    "item_type": item_type,
                    "rank": rank,
                    "estimated_tokens": tokens,
                    "strategy_source": "graph" if item_type == "entity" else "keyword",
                }
                for rank, (item_id, item_type, tokens) in enumerate(rows, 1)
            ],
        },
    )
    log.emit(
        EventType.FEEDBACK_RECORDED,
        source="mcp",
        entity_id="pack_1",
        payload={
            "pack_id": "pack_1",
            "helpful_item_ids": ["e_full"],
            "unhelpful_item_ids": ["e_stub", "d_short"],
            "rating": 0.5,
            "success": True,
        },
    )
    return _temp_stores


def _json(*args: str) -> dict:
    result = runner.invoke(app, ["analyze", "replay", *args, "--format", "json"])
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout.strip())


def test_entity_arm_reports_hand_computed_counts_and_its_basis(
    seeded: StoreRegistry,
) -> None:
    data = _json("--entity-requires-description")

    assert data["served"]["items"] == 4
    assert data["served"]["tokens"] == 170
    # e_stub has no description: removed, and it was cited unhelpful.
    assert data["removed"]["items"] == 1
    assert data["removed"]["tokens"] == 40
    assert data["removed"]["unhelpful_items"] == 1
    assert data["removed"]["helpful_items"] == 0
    # e_gone is no longer in the graph: neither removed nor kept.
    assert data["unresolved"]["items"] == 1
    assert data["unresolved"]["unjudged_tokens"] == 10
    assert data["unresolved_entity_description"] == 1
    assert data["judged_on"] == {"entity_requires_description": "current_state"}


def test_word_arm_reports_hand_computed_counts_and_its_basis(
    seeded: StoreRegistry,
) -> None:
    data = _json("--min-distinct-words", "3")

    # e_full has many words, e_stub serves "Stub" (1), d_short has 2, e_gone
    # cannot be read.
    assert data["removed"]["items"] == 2
    assert data["removed"]["tokens"] == 60
    assert data["removed"]["unhelpful_items"] == 2
    assert data["unresolved"]["items"] == 1
    assert data["judged_on"] == {"min_distinct_words": "current_state"}


def test_the_arms_combine_and_a_serving_both_remove_counts_once(
    seeded: StoreRegistry,
) -> None:
    data = _json("--entity-requires-description", "--min-distinct-words", "3")

    assert data["removed"]["items"] == 2  # e_stub (both), d_short (words)
    assert data["removed_by_several_rules"] == 1
    assert data["removed_by_entity_description"] == 1
    assert data["removed_by_min_distinct_words"] == 2


def test_the_class_prior_flag_combines_and_records_its_mode(
    seeded: StoreRegistry,
) -> None:
    data = _json("--min-distinct-words", "3", "--class-prior")

    # No class-prior knob is set in a fresh config, so the prior is off and
    # the words arm alone removes.
    assert data["removed"]["items"] == 2
    assert data["removed_by_class_prior"] == 0
    assert "off" in data["class_prior"]


def test_text_output_names_the_basis_and_never_echoes_item_text(
    seeded: StoreRegistry,
) -> None:
    result = runner.invoke(app, ["analyze", "replay", "--entity-requires-description"])

    assert result.exit_code == 0, result.output
    out = plain(result.stdout)
    assert "Substance Replay" in out
    assert "judged on" in out
    assert "current_state" in out
    assert "removed" in out
    assert "unresolved" in out
    assert _RUNBOOK_BODY not in out
    assert _RUNBOOK_BODY not in json.dumps(_json("--entity-requires-description"))


def test_a_replay_without_the_arms_is_still_the_walk_report(
    seeded: StoreRegistry,
) -> None:
    data = _json()

    assert "baseline" in data
    assert "judged_on" not in data


@pytest.mark.parametrize(
    ("args", "phrase"),
    [
        (["--min-distinct-words", "0"], "min_distinct_words must be >= 1"),
        (
            ["--entity-requires-description", "--max-items", "5"],
            "removal-only",
        ),
        (["--min-distinct-words", "3", "--no-refill"], "removal-only"),
        (
            ["--min-distinct-words", "3", "--body-items", "4"],
            "removal-only",
        ),
        (
            ["--entity-requires-description", "--graph-selection", "seeded"],
            "--graph-selection cannot be combined with",
        ),
    ],
)
def test_refusals_exit_the_same_whatever_the_format(
    args: list[str], phrase: str
) -> None:
    as_json = runner.invoke(app, ["analyze", "replay", *args, "--format", "json"])
    as_text = runner.invoke(app, ["analyze", "replay", *args, "--format", "text"])

    assert as_json.exit_code == as_text.exit_code == EXIT_VALIDATION
    envelope = json.loads(as_json.stdout.strip())
    assert envelope["status"] == "error"
    assert phrase in envelope["message"]
    assert phrase in plain(as_text.output).replace("\n", " ")


def test_success_exits_the_same_whatever_the_format(seeded: StoreRegistry) -> None:
    args = ["analyze", "replay", "--entity-requires-description"]
    as_json = runner.invoke(app, [*args, "--format", "json"])
    as_text = runner.invoke(app, [*args, "--format", "text"])

    assert as_json.exit_code == as_text.exit_code == 0
