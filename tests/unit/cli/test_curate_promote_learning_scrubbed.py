"""The ``promote-learning`` dry run does not echo a refused row's name (#1025).

A candidate captured before its source was redacted holds a name copied from
that source. The refusal keeps it out of the stores; the ``--dry-run`` JSON
echoed the planned entity payload, name included, so the text still reached the
terminal and any log the output was piped to. A refused row's payload is echoed
without its name.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from trellis.mutate import Command, CommandStatus, Operation, build_curate_executor
from trellis_cli import curate as curate_cli
from trellis_cli.main import app

runner = CliRunner()

SOURCE_ID = "doc-source"
NAME_TEXT = "Quartz heron opening line of the source"
COPIED_TEXT = "Amber ledger sentence copied from the source body"


@pytest.fixture(autouse=True)
def _temp_stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = tmp_path / "data"
    (data_dir / "stores").mkdir(parents=True)
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))


def _candidate(name: str, name_source: str) -> dict[str, Any]:
    return {
        "candidate_id": "cand:one",
        "intent_family": "fam",
        "recommendation_type": "promote_guidance",
        "item_id": SOURCE_ID,
        "item_type": "document",
        "title": None,
        "precedent_name": name,
        "precedent_name_source": name_source,
        "precedent_properties": {"intent_family": "fam", "source_item_id": SOURCE_ID},
        "target_entity_ids": [],
        "supporting_run_ids": ["run-1"],
        "phases": [],
        "domain_systems": [],
    }


def _invoke(tmp_path: Path, candidate: dict[str, Any], *args: str) -> Any:
    candidates_path = tmp_path / "candidates.json"
    decisions_path = tmp_path / "decisions.json"
    candidates_path.write_text(json.dumps({"candidates": [candidate]}))
    decisions_path.write_text(
        json.dumps(
            {
                "decisions": [
                    {"candidate_id": "cand:one", "approved": True, "rationale": "r"}
                ]
            }
        )
    )
    return runner.invoke(
        app,
        [
            "curate",
            "promote-learning",
            "--candidates",
            str(candidates_path),
            "--decisions",
            str(decisions_path),
            *args,
            "--format",
            "json",
        ],
    )


def _redact_source() -> None:
    registry = curate_cli._get_registry()
    try:
        result = build_curate_executor(registry, clearance="restricted").execute(
            Command(
                operation=Operation.REDACTION_APPLY,
                args={"target_id": SOURCE_ID, "reason": "owner request"},
                target_id=SOURCE_ID,
                target_type="entity",
                requested_by="test",
            )
        )
        assert result.status == CommandStatus.SUCCESS, result.message
    finally:
        registry.close()


def _seed_source() -> None:
    registry = curate_cli._get_registry()
    try:
        registry.knowledge.document_store.put(
            SOURCE_ID, f"{NAME_TEXT}\n\n{COPIED_TEXT}", {}
        )
        registry.knowledge.graph_store.upsert_node(
            SOURCE_ID, "CreativeWork", {"name": SOURCE_ID}
        )
    finally:
        registry.close()


def test_a_refused_dry_run_row_does_not_echo_the_name(tmp_path: Path) -> None:
    _seed_source()
    stale = _candidate(NAME_TEXT, "source_text")
    assert _invoke(tmp_path, stale).exit_code == 0
    _redact_source()

    result = _invoke(tmp_path, stale, "--dry-run")

    assert result.exit_code == 0
    row = json.loads(result.stdout.strip())["results"][0]
    assert row["would_status"] == "refused_redacted"
    assert NAME_TEXT not in result.stdout
    assert COPIED_TEXT not in result.stdout
    # The rest of the payload is still there for the reviewer to read.
    assert "name" not in row["entity_payload"]
    assert row["entity_payload"]["entity_id"] == row["entity_id"]


def test_a_row_that_would_promote_still_echoes_its_name(tmp_path: Path) -> None:
    _seed_source()

    result = _invoke(tmp_path, _candidate(NAME_TEXT, "source_text"), "--dry-run")

    assert result.exit_code == 0
    row = json.loads(result.stdout.strip())["results"][0]
    assert row["would_status"] == "promoted"
    assert row["entity_payload"]["name"] == NAME_TEXT
