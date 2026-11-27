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
from trellis.mutate import (
    Command,
    CommandResult,
    CommandStatus,
    Operation,
    build_correction_executor,
)
from trellis.retrieve.lifecycle import declared_successor
from trellis.schemas.classification import LIFECYCLE_KEY, Lifecycle
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry
from trellis_cli.exit_codes import (
    EXIT_IDEMPOTENCY,
    EXIT_OK,
    EXIT_STORE,
    EXIT_VALIDATION,
)
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


def _propose(reg: StoreRegistry, text: str = X_TEXT) -> str:
    pid = proposal_id_for(TARGET, content_hash(text))
    payload = proposal_payload(
        proposal_id=pid,
        target_id=TARGET,
        target_metadata={},
        corrected_content=text,
        reason="rig replaced",
        proposed_by="local",
        pack_id=None,
        evidence_uri=None,
    )
    result = build_correction_executor(reg, clearance="restricted").execute(
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


def _applied_events(reg: StoreRegistry) -> list[Any]:
    return reg.operational.event_log.get_events(
        event_type=EventType.CORRECTION_APPLIED, limit=10
    )


def _status(pid: str) -> str:
    return str(
        json.loads(_run("show", pid, "--format", "json").output)["proposal"]["status"]
    )


class TestAnExistingDocumentWithTheText:
    """Only this proposal's own earlier successor is reused; others refuse."""

    def _assert_refused(
        self, registry: StoreRegistry, pid: str, existing_id: str
    ) -> None:
        docs = registry.knowledge.document_store
        before = {d: docs.get(d) for d in (TARGET, existing_id)}
        text, js = _both("apply", pid)
        assert text.exit_code == js.exit_code == EXIT_VALIDATION
        body = json.loads(js.output)
        assert body["refusal"] == "content_exists"
        assert body["existing_id"] == existing_id
        assert existing_id in body["message"]
        assert {d: docs.get(d) for d in (TARGET, existing_id)} == before
        assert declared_successor(docs.get(TARGET)["metadata"]) is None
        assert _applied_events(registry) == []
        assert _status(pid) == "pending"

    def test_a_revert_does_not_close_a_cycle(self, registry: StoreRegistry) -> None:
        stamp = Lifecycle(state="superseded", superseded_by=TARGET)
        registry.knowledge.document_store.put(
            "doc-old", X_TEXT, {LIFECYCLE_KEY: stamp.model_dump(mode="json")}
        )
        self._assert_refused(registry, _propose(registry), "doc-old")

    def test_an_archived_row_is_not_adopted(self, registry: StoreRegistry) -> None:
        archived = Lifecycle(state="archived").model_dump(mode="json")
        registry.knowledge.document_store.put(
            "doc-arch", X_TEXT, {LIFECYCLE_KEY: archived}
        )
        self._assert_refused(registry, _propose(registry), "doc-arch")

    def test_an_unrelated_lower_labelled_row_is_not_adopted(
        self, registry: StoreRegistry
    ) -> None:
        docs = registry.knowledge.document_store
        docs.put(TARGET, T_TEXT, {"data_classification": {"sensitivity": "restricted"}})
        docs.put("doc-pub", X_TEXT, {"data_classification": {"sensitivity": "public"}})
        self._assert_refused(registry, _propose(registry), "doc-pub")

    def test_its_own_earlier_successor_is_reused(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import trellis.mcp.supersession as supersession_mod

        pid = _propose(registry)

        real = supersession_mod.supersede_document
        monkeypatch.setattr(
            supersession_mod, "supersede_document", lambda *a, **k: "store down"
        )
        first = _run("apply", pid, "--format", "json")
        assert first.exit_code == EXIT_STORE
        successor = json.loads(first.output)["successor_id"]
        docs = registry.knowledge.document_store
        assert declared_successor(docs.get(TARGET)["metadata"]) is None
        monkeypatch.setattr(supersession_mod, "supersede_document", real)
        again = _run("apply", pid, "--format", "json")
        assert again.exit_code == EXIT_OK, again.output
        assert json.loads(again.output)["successor_id"] == successor
        assert docs.count() == 2
        assert declared_successor(docs.get(TARGET)["metadata"]) == successor
        assert _status(pid) == "applied"


class TestTheSuccessorInheritsTheLabel:
    def test_a_confidential_target_gives_a_confidential_successor(
        self, registry: StoreRegistry
    ) -> None:
        registry.knowledge.document_store.put(
            TARGET, T_TEXT, {"data_classification": {"sensitivity": "confidential"}}
        )
        pid = _propose(registry)
        result = _run("apply", pid, "--format", "json")
        assert result.exit_code == EXIT_OK, result.output
        successor = registry.knowledge.document_store.get(
            json.loads(result.output)["successor_id"]
        )
        assert successor["metadata"]["data_classification"]["sensitivity"] == (
            "confidential"
        )


class TestAnInterruptedApplyResumes:
    def test_a_lost_verdict_is_recorded_on_retry(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pid = _propose(registry)
        real = corrections_mod._resolve

        def lost(*args: Any, **kwargs: Any) -> CommandResult:
            return CommandResult(
                command_id="cmd-lost",
                status=CommandStatus.FAILED,
                operation=Operation.CORRECTION_RESOLVE,
                message="log down",
            )

        monkeypatch.setattr(corrections_mod, "_resolve", lost)
        first = _run("apply", pid, "--format", "json")
        assert first.exit_code == EXIT_STORE
        body = json.loads(first.output)
        successor = body["successor_id"]
        assert "re-running apply completes" in body["message"]
        docs = registry.knowledge.document_store
        assert declared_successor(docs.get(TARGET)["metadata"]) == successor
        assert _status(pid) == "pending"
        monkeypatch.setattr(corrections_mod, "_resolve", real)
        again = _run("apply", pid, "--format", "json")
        assert again.exit_code == EXIT_OK, again.output
        assert json.loads(again.output)["successor_id"] == successor
        assert docs.count() == 2

    def test_a_retry_after_the_stamp_records_the_verdict(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pid = _propose(registry)

        def boom(*args: Any, **kwargs: Any) -> Any:
            msg = "event log down"
            raise RuntimeError(msg)

        real = corrections_mod._resolve
        monkeypatch.setattr(corrections_mod, "_resolve", boom)
        with pytest.raises(RuntimeError):
            corrections_mod.apply_correction(registry, pid, reason="ok")
        monkeypatch.setattr(corrections_mod, "_resolve", real)
        docs = registry.knowledge.document_store
        successor = declared_successor(docs.get(TARGET)["metadata"])
        assert successor is not None
        again = _run("apply", pid, "--format", "json")
        assert again.exit_code == EXIT_OK, again.output
        assert json.loads(again.output)["successor_id"] == successor
        assert docs.count() == 2
        (event,) = _applied_events(registry)
        assert event.payload["successor_id"] == successor
        assert _status(pid) == "applied"

    def test_another_successor_still_refuses(self, registry: StoreRegistry) -> None:
        pid = _propose(registry)
        stamp = Lifecycle(state="superseded", superseded_by="doc-other")
        registry.knowledge.document_store.put(
            TARGET, T_TEXT, {LIFECYCLE_KEY: stamp.model_dump(mode="json")}
        )
        registry.knowledge.document_store.put("doc-other", "something else", {})
        text, js = _both("apply", pid)
        assert text.exit_code == js.exit_code == EXIT_VALIDATION
        assert json.loads(js.output)["refusal"] == "target_superseded"


class TestTheGateRunsAtTheCliClearance:
    def test_every_executor_gets_restricted(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from trellis.mutate import policy_source

        pid = _propose(registry)
        seen: list[str] = []
        real = policy_source.build_policy_gate

        def spy(reg: StoreRegistry, *, clearance: Any = "unset") -> Any:
            seen.append(clearance)
            return real(reg) if clearance == "unset" else real(reg, clearance=clearance)

        monkeypatch.setattr(policy_source, "build_policy_gate", spy)
        assert _run("apply", pid).exit_code == EXIT_OK
        assert seen
        assert set(seen) == {"restricted"}
