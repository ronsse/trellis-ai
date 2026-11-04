"""``trellis analyze value``: the implicit-signal and tier blocks, both formats.

The two output formats must agree on the exit code (CLAUDE.md, "A command's
exit code must not depend on ``--format``"), and the new blocks must reach
both: ``implicit`` and ``by_tier`` in the JSON, a section each in the text.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.cli_output import plain
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry
from trellis_cli.main import app
from trellis_cli.stores import _reset_registry

runner = CliRunner()


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
def temp_stores(_temp_stores: StoreRegistry) -> StoreRegistry:
    return _temp_stores


def _seed(registry: StoreRegistry, packs: int) -> None:
    """``packs`` graduated packs (one body, one pointer), two of them fetched."""
    log = registry.operational.event_log
    for index in range(packs):
        pack_id = f"ipack_{index}"
        log.emit(
            EventType.PACK_ASSEMBLED,
            source="test",
            entity_id=pack_id,
            entity_type="pack",
            payload={
                "intent_family": "general_context",
                "session_id": "cli-session",
                "index_mode": False,
                "disclosure": {"mode": "graduated", "pointer_count": 1},
                "injected_items": [
                    {
                        "item_id": f"{pack_id}_a",
                        "item_type": "document",
                        "strategy_source": "semantic",
                        "estimated_tokens": 100,
                        "rank": 0,
                    },
                    {
                        "item_id": f"{pack_id}_b",
                        "item_type": "document",
                        "strategy_source": "keyword",
                        "estimated_tokens": 20,
                        "rank": 1,
                        "selection_reason": "disclosure_pointer",
                    },
                ],
            },
        )
        log.emit(
            EventType.FEEDBACK_RECORDED,
            source="mcp",
            entity_id=pack_id,
            payload={
                "pack_id": pack_id,
                "helpful_item_ids": [f"{pack_id}_a"],
                "unhelpful_item_ids": [],
                "intent_family": "general_context",
                "rating": 0.5,
                "success": True,
            },
        )
    for index in range(min(2, packs)):
        log.emit(
            EventType.PACK_ITEMS_FETCHED,
            source="mcp",
            entity_id=f"ipack_{index}",
            entity_type="pack",
            payload={
                "pack_id": f"ipack_{index}",
                "requested_item_ids": [f"ipack_{index}_b"],
                "served_item_ids": [f"ipack_{index}_b"],
            },
        )


@pytest.mark.parametrize("packs", [0, 2, 6])
def test_exit_code_does_not_depend_on_format(
    temp_stores: StoreRegistry, packs: int
) -> None:
    _seed(temp_stores, packs)
    as_json = runner.invoke(app, ["analyze", "value", "--format", "json"])
    as_text = runner.invoke(app, ["analyze", "value", "--format", "text"])
    assert as_json.exit_code == as_text.exit_code == 0


def test_json_carries_both_blocks_beside_the_unchanged_headline(
    temp_stores: StoreRegistry,
) -> None:
    _seed(temp_stores, 6)
    result = runner.invoke(app, ["analyze", "value", "--format", "json"])
    data = json.loads(result.stdout.strip())

    assert data["useful_token_fraction"] == pytest.approx(100 / 120, abs=1e-4)
    implicit = data["implicit"]
    assert implicit["fetched_items"] == 2
    assert implicit["fetched_tokens"] == 40
    assert implicit["fetched_unjudged_tokens"] == 40
    assert implicit["agreement_suppressed"] is True
    assert implicit["re_retrieved_packs"] == 5  # six packs, one session, seconds apart
    assert {cell["tier"] for cell in data["by_tier"]} == {"body", "pointer", "unknown"}
    tiers = {cell["tier"]: cell for cell in data["by_tier"]}
    assert (tiers["body"]["items"], tiers["pointer"]["items"]) == (6, 6)
    assert tiers["unknown"]["items"] == 0


def test_text_prints_both_blocks_and_the_refusal(temp_stores: StoreRegistry) -> None:
    _seed(temp_stores, 6)
    result = runner.invoke(app, ["analyze", "value", "--format", "text"])
    out = plain(result.stdout)

    assert "Served tokens by disclosure tier" in out
    assert "Implicit signals" in out
    assert "never merged into the useful-token fraction" in out
    assert "fetched tokens: 40 of 720 injected" in out
    assert "agreement refused" in out
    assert "weak signal" in out
