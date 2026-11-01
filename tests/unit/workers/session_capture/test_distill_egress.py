"""Distillation asks the egress gate before a transcript digest goes to a model."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from tests.unit.workers.session_capture.test_capture import (
    A,
    B,
    _pack_session,
    _registry,
    _sweep,
)
from trellis.llm.types import LLMResponse
from trellis.stores.base.event_log import EventType

if TYPE_CHECKING:
    import pytest

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


def test_a_pack_that_cannot_be_read_withholds_the_session(
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
    assert report.sessions_skipped_egress_withheld == 1


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
        _serve(registry, A, "doc-internal", "entity:x", "no-such-row")
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
