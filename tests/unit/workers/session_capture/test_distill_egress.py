"""Distillation asks the egress gate before a transcript digest goes to a model."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.unit.cli.test_worker import runner
from tests.unit.workers.session_capture.test_capture import (
    A,
    B,
    _pack_session,
    _registry,
    _sweep,
)
from trellis.ingest_corpus.models import chunk_doc_id
from trellis.llm.routing import LLMConsumer
from trellis.llm.types import LLMResponse
from trellis.stores.base.event_log import EventType
from trellis_cli import worker
from trellis_cli.main import app
from trellis_workers.session_capture import sweep as capture_sweep
from trellis_workers.session_capture.models import CaptureReport

_SECRET = {"data_classification": {"sensitivity": "confidential"}}


class _Recorder:
    """A judge that records every prompt it is sent and distils nothing."""

    def __init__(self) -> None:
        self.prompts: list[Any] = []

    async def generate(self, *, messages: list[Any], **_: Any) -> LLMResponse:
        self.prompts.append(messages)
        return LLMResponse(content="[]", model="test-model")


def _serve(registry: Any, pack_id: str, *item_ids: str) -> None:
    registry.operational.event_log.emit(
        EventType.PACK_ASSEMBLED,
        source="test",
        entity_id=pack_id,
        entity_type="pack",
        payload={"injected_item_ids": list(item_ids)},
    )


def _emit_pack(registry: Any, pack_id: str, payload: dict[str, Any]) -> None:
    registry.operational.event_log.emit(
        EventType.PACK_ASSEMBLED,
        source="test",
        entity_id=pack_id,
        entity_type="pack",
        payload=payload,
    )


def _session(root: Path, pack_id: str) -> None:
    _pack_session(
        root / "proj" / "sess-egress-0001.jsonl",
        "sess-egress-0001",
        ("get_context", pack_id),
        error=True,
    )


def _declare_local(registry: Any) -> None:
    registry.llm_route.return_value = SimpleNamespace(local=True)


def test_a_session_served_a_labelled_document_is_not_distilled_and_is_retried(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    registry.knowledge.document_store.put("doc-secret", "body", _SECRET)
    _serve(registry, A, "doc-secret")
    root, wm, judge = tmp_path / "projects", tmp_path / "wm.json", _Recorder()
    _session(root, A)

    report = _sweep(registry, root, wm, judge)

    assert judge.prompts == []
    assert report.sessions_skipped_egress_withheld == 1
    assert report.sessions_judge_unavailable == 0
    assert report.to_payload()["sessions_skipped_egress_withheld"] == 1

    _declare_local(registry)
    retried = _sweep(registry, root, wm, judge)

    assert len(judge.prompts) == 1
    assert retried.sessions_skipped_egress_withheld == 0


def _two_pack_session(root: Path) -> None:
    _pack_session(
        root / "proj" / "sess-egress-0001.jsonl",
        "sess-egress-0001",
        ("get_context", A),
        ("search", B),
        error=True,
    )


def test_a_pack_that_cannot_be_read_is_unreadable_not_withheld(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = _registry(tmp_path)
    log = registry.operational.event_log
    real = log.get_events

    def get_events(*args: Any, **kwargs: Any) -> Any:
        if kwargs.get("event_type") is EventType.PACK_ASSEMBLED:
            msg = "log down"
            raise RuntimeError(msg)
        return real(*args, **kwargs)

    monkeypatch.setattr(log, "get_events", get_events)
    root, judge = tmp_path / "projects", _Recorder()
    _session(root, A)

    report = _sweep(registry, root, tmp_path / "wm.json", judge)

    assert judge.prompts == []
    assert report.sessions_skipped_egress_unreadable == 1
    assert report.sessions_skipped_egress_withheld == 0
    assert [w["kind"] for w in report.warnings] == ["egress_unreadable"]
    assert report.to_payload()["sessions_skipped_egress_unreadable"] == 1


def test_a_document_store_outage_is_unreadable_not_withheld(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = _registry(tmp_path)
    _serve(registry, A, "doc-x")

    def down(_doc_id: str) -> Any:
        msg = "store down"
        raise RuntimeError(msg)

    monkeypatch.setattr(registry.knowledge.document_store, "get", down)
    root, judge = tmp_path / "projects", _Recorder()
    _session(root, A)

    report = _sweep(registry, root, tmp_path / "wm.json", judge)

    assert judge.prompts == []
    assert (
        report.sessions_skipped_egress_unreadable,
        report.sessions_skipped_egress_withheld,
    ) == (1, 0)


@pytest.mark.parametrize("ids", [("doc-ok", "doc-secret"), ("doc-secret", "doc-ok")])
def test_one_labelled_item_withholds_the_pack_in_either_order(
    tmp_path: Path, ids: tuple[str, ...]
) -> None:
    registry = _registry(tmp_path)
    registry.knowledge.document_store.put("doc-ok", "body", {})
    registry.knowledge.document_store.put("doc-secret", "body", _SECRET)
    _serve(registry, A, *ids)
    root, judge = tmp_path / "projects", _Recorder()
    _session(root, A)

    report = _sweep(registry, root, tmp_path / "wm.json", judge)

    assert judge.prompts == []
    assert report.sessions_skipped_egress_withheld == 1


@pytest.mark.parametrize("labelled", [A, B])
def test_a_labelled_item_in_either_pack_of_a_session_withholds_it(
    tmp_path: Path, labelled: str
) -> None:
    registry = _registry(tmp_path)
    registry.knowledge.document_store.put("doc-ok", "body", {})
    registry.knowledge.document_store.put("doc-secret", "body", _SECRET)
    _serve(registry, A, "doc-secret" if labelled == A else "doc-ok")
    _serve(registry, B, "doc-secret" if labelled == B else "doc-ok")
    root, judge = tmp_path / "projects", _Recorder()
    _two_pack_session(root)

    report = _sweep(registry, root, tmp_path / "wm.json", judge)

    assert judge.prompts == []
    assert report.sessions_skipped_egress_withheld == 1


def test_a_sectioned_pack_is_judged_by_its_item_rows(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    registry.knowledge.document_store.put("doc-secret", "body", _SECRET)
    _emit_pack(
        registry,
        A,
        {
            "sections": [{"name": "background", "items": 1}],
            "injected_items": [
                {"item_id": "doc-secret", "section": "background", "rank": 1}
            ],
        },
    )
    root, judge = tmp_path / "projects", _Recorder()
    _session(root, A)

    report = _sweep(registry, root, tmp_path / "wm.json", judge)

    assert judge.prompts == []
    assert report.sessions_skipped_egress_withheld == 1


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({}, id="no-item-record"),
        pytest.param({"injected_item_ids": "doc-secret"}, id="ids-not-a-list"),
        pytest.param({"injected_item_ids": [None]}, id="id-not-a-string"),
        pytest.param({"injected_items": [{"rank": 1}]}, id="row-without-an-id"),
        pytest.param({"injected_items": "doc-secret"}, id="rows-not-a-list"),
        pytest.param({"sections": "background"}, id="sections-not-a-list"),
        pytest.param({"sections": [{"name": "a"}]}, id="section-without-ids"),
        pytest.param(
            {"sections": [{"item_ids": [None]}]}, id="section-id-not-a-string"
        ),
        pytest.param({"sections": ["a"]}, id="section-not-a-mapping"),
    ],
)
def test_a_pack_with_no_readable_item_record_is_withheld_and_never_sent(
    tmp_path: Path, payload: dict[str, Any]
) -> None:
    """Events are immutable, so this is a permanent fact: withheld, not a fault."""
    registry = _registry(tmp_path)
    _emit_pack(registry, A, payload)
    root, judge = tmp_path / "projects", _Recorder()
    _session(root, A)

    report = _sweep(registry, root, tmp_path / "wm.json", judge)

    assert judge.prompts == []
    assert report.sessions_skipped_egress_withheld == 1
    assert report.sessions_skipped_egress_unreadable == 0
    assert report.warnings == []


# The shape every sectioned pack carried from the first release until
# ``injected_items[]`` was added to it (git history: ``_section_rows``): no
# ``injected_item_ids``, no ``injected_items``, one ``item_ids`` list a section.
def _legacy_sectioned(*item_ids: str) -> dict[str, Any]:
    return {
        "section_count": 1,
        "total_items": len(item_ids),
        "sections": [
            {
                "name": "background",
                "items_count": len(item_ids),
                "item_ids": list(item_ids),
                "injected_advisory_ids": [[] for _ in item_ids],
            }
        ],
    }


def test_a_legacy_sectioned_pack_is_judged_by_its_section_item_ids(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    registry.knowledge.document_store.put("doc-secret", "body", _SECRET)
    _emit_pack(registry, A, _legacy_sectioned("doc-secret"))
    root, judge = tmp_path / "projects", _Recorder()
    _session(root, A)

    report = _sweep(registry, root, tmp_path / "wm.json", judge)

    assert judge.prompts == []
    assert report.sessions_skipped_egress_withheld == 1
    assert report.sessions_skipped_egress_unreadable == 0


def test_a_legacy_sectioned_pack_of_unlabelled_documents_is_sent(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    registry.knowledge.document_store.put("doc-ok", "body", {})
    _emit_pack(registry, A, _legacy_sectioned("doc-ok"))
    root, judge = tmp_path / "projects", _Recorder()
    _session(root, A)

    report = _sweep(registry, root, tmp_path / "wm.json", judge)

    assert len(judge.prompts) == 1
    assert report.sessions_skipped_egress_withheld == 0
    assert report.sessions_skipped_egress_unreadable == 0


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(
            {
                "injected_item_ids": ["doc-ok"],
                "injected_items": [{"item_id": "doc-secret"}],
            },
            id="ids-then-rows",
        ),
        pytest.param(
            {
                "injected_item_ids": ["doc-secret"],
                "injected_items": [{"item_id": "doc-ok"}],
            },
            id="rows-then-ids",
        ),
        pytest.param(
            {
                "injected_item_ids": ["doc-ok"],
                "sections": [{"item_ids": ["doc-secret"]}],
            },
            id="ids-then-sections",
        ),
        pytest.param(
            {
                "injected_items": [{"item_id": "doc-ok"}],
                "sections": [{"item_ids": ["doc-secret"]}],
            },
            id="rows-then-sections",
        ),
    ],
)
def test_every_item_record_a_pack_carries_is_read_not_the_first_found(
    tmp_path: Path, payload: dict[str, Any]
) -> None:
    registry = _registry(tmp_path)
    registry.knowledge.document_store.put("doc-ok", "body", {})
    registry.knowledge.document_store.put("doc-secret", "body", _SECRET)
    _emit_pack(registry, A, payload)
    root, judge = tmp_path / "projects", _Recorder()
    _session(root, A)

    report = _sweep(registry, root, tmp_path / "wm.json", judge)

    assert judge.prompts == []
    assert report.sessions_skipped_egress_withheld == 1


def test_a_pack_that_truly_served_nothing_is_permitted(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    _emit_pack(registry, A, {"injected_item_ids": [], "injected_items": []})
    root, judge = tmp_path / "projects", _Recorder()
    _session(root, A)

    report = _sweep(registry, root, tmp_path / "wm.json", judge)

    assert len(judge.prompts) == 1
    assert report.sessions_skipped_egress_unreadable == 0


def test_a_chunk_with_no_parent_row_is_withheld_not_unreadable(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    _serve(registry, A, chunk_doc_id("gone", 0))
    root, judge = tmp_path / "projects", _Recorder()
    _session(root, A)

    report = _sweep(registry, root, tmp_path / "wm.json", judge)

    assert judge.prompts == []
    assert report.sessions_skipped_egress_withheld == 1
    assert report.sessions_skipped_egress_unreadable == 0


def test_the_gate_is_built_from_the_session_capture_route(tmp_path: Path) -> None:
    """A registry that answers per consumer: only this sender's route decides."""
    sent: dict[str, int] = {}
    for name, target_local in (("cloud", False), ("local", True)):
        (tmp_path / name).mkdir()
        registry = _registry(tmp_path / name)
        registry.knowledge.document_store.put("doc-secret", "body", _SECRET)
        _serve(registry, A, "doc-secret")
        registry.llm_route.side_effect = lambda consumer, want=target_local: (
            SimpleNamespace(local=(consumer is LLMConsumer.SESSION_CAPTURE) == want)
        )
        root, judge = tmp_path / name / "projects", _Recorder()
        _session(root, A)

        _sweep(registry, root, tmp_path / name / "wm.json", judge)

        sent[name] = len(judge.prompts)

    assert sent == {"cloud": 0, "local": 1}


def test_the_sweep_builds_its_client_and_its_gate_from_the_same_consumer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = _registry(tmp_path)
    asked: list[LLMConsumer] = []
    built: list[LLMConsumer] = []
    registry.llm_route.side_effect = lambda consumer: (
        asked.append(consumer) or SimpleNamespace(local=False)
    )
    registry.build_llm_client.side_effect = lambda *, consumer: (
        built.append(consumer) or _Recorder()
    )
    root = tmp_path / "projects"
    _session(root, A)
    monkeypatch.setenv(capture_sweep.ENV_ROOT, str(root))
    monkeypatch.setenv(capture_sweep.ENV_WATERMARK, str(tmp_path / "wm.json"))
    _serve(registry, A, "doc-x")

    capture_sweep.run_sweep(registry=registry)

    assert asked, "the gate never asked for a route"
    assert set(asked) == set(built) == {LLMConsumer.SESSION_CAPTURE}


@pytest.mark.parametrize(
    ("counts", "failed"),
    [
        ({"sessions_skipped_egress_unreadable": 2}, True),
        ({"sessions_skipped_egress_withheld": 2}, False),
    ],
)
def test_the_command_fails_a_sweep_with_unreadable_sessions_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    counts: dict[str, int],
    failed: bool,
) -> None:
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("TRELLIS_CAPTURE_STRICT", raising=False)
    monkeypatch.setattr(
        worker, "_get_registry", lambda: SimpleNamespace(stores_dir=tmp_path)
    )
    monkeypatch.setattr(
        capture_sweep,
        "run_sweep",
        lambda **_k: CaptureReport(transcripts_root="x", **counts),
    )

    as_json = runner.invoke(app, ["worker", "capture-sessions", "--format", "json"])
    as_text = runner.invoke(app, ["worker", "capture-sessions"])

    assert json.loads(as_json.output)["status"] == ("partial" if failed else "ok")
    expected = worker.EXIT_INTERNAL if failed else 0
    assert as_json.exit_code == as_text.exit_code == expected


def test_nothing_labelled_sends_the_same_prompt_on_either_route(
    tmp_path: Path,
) -> None:
    sent: dict[str, list[Any]] = {}
    for name, local in (("cloud", False), ("local", True)):
        (tmp_path / name).mkdir()
        registry = _registry(tmp_path / name)
        docs = registry.knowledge.document_store
        docs.put("doc-internal", "body", {})
        docs.put("doc-open", "body", {"data_classification": {"sensitivity": "public"}})
        docs.put("doc-parent", "body", {})
        docs.put(chunk_doc_id("doc-parent", 0), "chunk", {})
        _serve(
            registry,
            A,
            "doc-internal",
            "entity:x",
            "no-such-row",
            chunk_doc_id("doc-parent", 0),
        )
        _serve(registry, B, "doc-open")
        if local:
            _declare_local(registry)
        root, judge = tmp_path / name / "projects", _Recorder()
        _pack_session(
            root / "proj" / "sess-egress-0001.jsonl",
            "sess-egress-0001",
            ("get_context", A),
            ("search", B),
            error=True,
        )

        report = _sweep(registry, root, tmp_path / name / "wm.json", judge)

        assert report.sessions_skipped_egress_withheld == 0
        sent[name] = judge.prompts

    assert len(sent["cloud"]) == 1
    assert sent["cloud"] == sent["local"]
