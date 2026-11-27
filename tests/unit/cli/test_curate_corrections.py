"""``trellis curate corrections``: format-independent exits, one verdict each."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import trellis_cli.curate_corrections as corrections_mod
from tests.cli_output import plain
from trellis.core.hashing import content_hash
from trellis.feedback.corrections import (
    PROPOSE_CORRECTION_SURFACE,
    proposal_id_for,
    proposal_payload,
)
from trellis.mutate import Command, CommandStatus, Operation, build_correction_executor
from trellis.retrieve.lifecycle import declared_successor
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry
from trellis_cli.exit_codes import EXIT_IDEMPOTENCY, EXIT_OK, EXIT_VALIDATION
from trellis_cli.main import app

runner = CliRunner()

TARGET = "doc-target"
T_TEXT = "the settle window on the tank rig is forty milliseconds"
X_TEXT = "the settle window on the replacement rig is twenty-five milliseconds"


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> StoreRegistry:
    stores = tmp_path / "stores"
    stores.mkdir()
    reg = StoreRegistry(stores_dir=stores)
    reg.knowledge.document_store.put(TARGET, T_TEXT, {"title": "Calibration"})
    monkeypatch.setattr(corrections_mod, "_get_registry", lambda: reg)
    return reg


def _propose(reg: StoreRegistry) -> str:
    pid = proposal_id_for(TARGET, content_hash(X_TEXT))
    payload = proposal_payload(
        proposal_id=pid,
        target_id=TARGET,
        target_metadata={},
        corrected_content=X_TEXT,
        reason="rig replaced",
        proposed_by="local",
        pack_id=None,
        evidence_uri=None,
    )
    result = build_correction_executor(reg).execute(
        Command(
            operation=Operation.CORRECTION_PROPOSE,
            target_id=pid,
            target_type="correction",
            args={"proposal": payload},
            requested_by=PROPOSE_CORRECTION_SURFACE,
        )
    )
    assert result.status is CommandStatus.SUCCESS
    return pid


def _run(*argv: str) -> Any:
    return runner.invoke(app, ["curate", "corrections", *argv])


def _both(*argv: str) -> tuple[Any, Any]:
    return _run(*argv), _run(*argv, "--format", "json")


class TestExitParity:
    def test_list_and_show(self, registry: StoreRegistry) -> None:
        pid = _propose(registry)
        text, js = _both("list")
        assert text.exit_code == js.exit_code == EXIT_OK
        assert pid in plain(text.output)
        body = json.loads(js.output)
        assert body["status"] == "ok"
        assert body["count"] == 1
        assert "corrected_content" not in body["proposals"][0]
        text, js = _both("show", pid)
        assert text.exit_code == js.exit_code == EXIT_OK
        assert X_TEXT in plain(text.output)
        assert json.loads(js.output)["proposal"]["corrected_content"] == X_TEXT

    @pytest.mark.parametrize(
        "argv",
        [
            ("show", "corr-missing"),
            ("apply", "corr-missing"),
            ("reject", "corr-missing", "--reason", "no"),
            ("list", "--status", "bogus"),
        ],
    )
    def test_refusals_exit_alike(
        self, registry: StoreRegistry, argv: tuple[str, ...]
    ) -> None:
        text, js = _both(*argv)
        assert text.exit_code == js.exit_code == EXIT_VALIDATION
        assert json.loads(js.output)["status"] == "error"

    def test_status_filter(self, registry: StoreRegistry) -> None:
        _propose(registry)
        js = _run("list", "--status", "applied", "--format", "json")
        assert json.loads(js.output)["count"] == 0
        js = _run("list", "--status", "pending", "--format", "json")
        assert json.loads(js.output)["count"] == 1


class TestVerdicts:
    def test_reject_writes_only_the_verdict(self, registry: StoreRegistry) -> None:
        pid = _propose(registry)
        docs = registry.knowledge.document_store
        before = (docs.count(), docs.get(TARGET))
        result = _run(
            "reject", pid, "--reason", "vendor sheet is wrong", "--format", "json"
        )
        assert result.exit_code == EXIT_OK
        assert json.loads(result.output)["proposal_status"] == "rejected"
        assert (docs.count(), docs.get(TARGET)) == before
        again = _both("reject", pid, "--reason", "twice")
        assert again[0].exit_code == again[1].exit_code == EXIT_IDEMPOTENCY
        applied = _both("apply", pid)
        assert applied[0].exit_code == applied[1].exit_code == EXIT_IDEMPOTENCY
        assert (docs.count(), docs.get(TARGET)) == before

    def test_apply_supersedes_exactly_once(self, registry: StoreRegistry) -> None:
        pid = _propose(registry)
        result = _run("apply", pid, "--format", "json")
        assert result.exit_code == EXIT_OK, result.output
        body = json.loads(result.output)
        successor = body["successor_id"]
        docs = registry.knowledge.document_store
        assert docs.get(successor)["content"] == X_TEXT
        target = docs.get(TARGET)
        assert target["content"] == T_TEXT
        assert declared_successor(target["metadata"]) == successor
        again = _both("apply", pid)
        assert again[0].exit_code == again[1].exit_code == EXIT_IDEMPOTENCY
        assert docs.count() == 2
        events = registry.operational.event_log
        assert (
            len(events.get_events(event_type=EventType.CORRECTION_APPLIED, limit=10))
            == 1
        )
        listed = json.loads(_run("show", pid, "--format", "json").output)
        assert listed["proposal"]["status"] == "applied"
        assert listed["proposal"]["resolution"]["successor_id"] == successor
