"""``trellis worker distill-precedents-local``: dry-run first, local routes only."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from tests.precedent_candidates import SENTINEL, recovering_trace
from trellis.llm.types import LLMResponse
from trellis.mutate.handlers.precedent import LOCAL_MODEL_CANDIDATE_REQUESTERS
from trellis.schemas.enums import OutcomeStatus, TraceSource
from trellis.schemas.trace import Outcome, Trace, TraceContext, TraceStep
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry
from trellis_cli import worker
from trellis_cli.main import app
from trellis_cli.stores import _reset_registry
from trellis_cli.worker_distill_precedents_local import REQUESTED_BY

if TYPE_CHECKING:
    from pathlib import Path

    from click.testing import Result

runner = CliRunner()

LESSON = json.dumps(
    {
        "lessons": [
            {
                "kind": "avoid",
                "action": f"do not retry the bulk loader without a lock {SENTINEL}",
                "trigger": "a bulk load stalls on a held lock",
                "because": f"the loader waits forever {SENTINEL}",
                "confidence": 0.9,
            }
        ]
    }
)
SAME = json.dumps({"verdict": "same"})


class FakeClient:
    """Answers each ``generate`` with the next reply; counts the calls."""

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.calls = 0

    async def generate(self, **_kwargs) -> LLMResponse:
        self.calls += 1
        reply = self.replies.pop(0) if self.replies else json.dumps({"lessons": []})
        return LLMResponse(content=reply, model="fake-reported")


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


def _smooth(trace_id: str) -> Trace:
    """A trace the deterministic tier finds nothing in."""
    return Trace(
        trace_id=trace_id,
        source=TraceSource.AGENT,
        intent="bulk load the nightly ledger",
        steps=[TraceStep(step_type="tool_call", name="load", args={"k": 1})],
        outcome=Outcome(status=OutcomeStatus.SUCCESS, summary=f"done {SENTINEL}"),
        context=TraceContext(domain="data"),
    )


def _seed(stores: StoreRegistry) -> None:
    """Two traces the model is asked about, one the deterministic tier covers."""
    for trace in (_smooth("t-one"), _smooth("t-two"), recovering_trace()):
        stores.operational.trace_store.append(trace)


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    client: FakeClient,
    *,
    local: bool = True,
    model: str | None = "placeholder-model",
) -> FakeClient:
    monkeypatch.setattr(
        worker, "_require_llm_client_or_exit", lambda _consumer, *, command: client
    )
    monkeypatch.setattr(
        StoreRegistry,
        "llm_route",
        lambda _self, _consumer=None: SimpleNamespace(model=model, local=local),
    )
    return client


def _run(*args: str) -> Result:
    return runner.invoke(app, ["worker", "distill-precedents-local", *args])


def _count(stores: StoreRegistry, event_type: EventType) -> int:
    return stores.operational.event_log.count(event_type=event_type)


def test_a_dry_run_writes_no_candidate_but_logs_every_call(
    stores: StoreRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(stores)
    client = _wire(monkeypatch, FakeClient(LESSON, LESSON, SAME))

    result = _run("--format", "json")

    payload = json.loads(result.stdout)
    assert result.exit_code == 0, result.output
    assert payload["mode"] == "dry-run"
    assert payload["traces_read"] == 3
    assert payload["model_calls"] == {"distill": 2, "merge": 1}
    assert payload["skipped"] == {"deterministic_covered": 1}
    assert payload["outcomes"] == {"distill_lesson": 2, "merge_same": 1}
    assert payload["candidates"] == {"distill": 2, "merge": 1}
    assert payload["would_record"] == {"distill": 2, "merge": 1}
    assert payload["prompt_versions"]["distill"] == "precedent-distill/v1"
    # Events by type: no candidate, one judged event per model call.
    assert client.calls == 3
    assert _count(stores, EventType.PRECEDENT_CANDIDATE_MINED) == 0
    assert _count(stores, EventType.MEMORY_OP_JUDGED) == 3
    assert payload["judged_events"] == {
        "distillation": 2,
        "reconciliation": 1,
        "total": 3,
    }


def test_apply_records_the_candidates_and_a_rerun_records_none(
    stores: StoreRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(stores)
    _wire(monkeypatch, FakeClient(LESSON, LESSON, SAME))

    first = json.loads(_run("--apply", "--format", "json").stdout)

    assert first["mode"] == "apply"
    assert first["recorded"] == {"distill": 2, "merge": 1}
    assert len(first["candidate_ids"]) == 3
    assert _count(stores, EventType.PRECEDENT_CANDIDATE_MINED) == 3
    assert _count(stores, EventType.MEMORY_OP_JUDGED) == 3
    recorded = stores.operational.event_log.get_events(
        event_type=EventType.PRECEDENT_CANDIDATE_MINED, limit=10
    )
    assert {e.payload["record"]["provenance_tier"] for e in recorded} == {"local_model"}
    merged = [
        e.payload["record"]
        for e in recorded
        if e.payload["record"]["evidence"]["merged_from"]
    ]
    assert len(merged) == 1
    assert len(merged[0]["evidence"]["merged_from"]) == 2

    _wire(monkeypatch, FakeClient(LESSON, LESSON))
    second = json.loads(
        _run("--apply", "--merge-limit", "0", "--format", "json").stdout
    )
    assert second["recorded"] == {"distill": 0, "merge": 0}
    assert second["duplicates"] == 2
    assert _count(stores, EventType.PRECEDENT_CANDIDATE_MINED) == 3
    # The rerun asked the model again: those calls are judged too.
    assert _count(stores, EventType.MEMORY_OP_JUDGED) == 5


def test_a_cloud_route_is_refused_with_no_call(
    stores: StoreRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(stores)
    client = _wire(monkeypatch, FakeClient(LESSON), local=False)

    result = _run("--apply", "--format", "json")

    payload = json.loads(result.stdout)
    assert result.exit_code == 3
    assert payload["status"] == "error"
    assert payload["refusals"] == {"route_not_local": 1}
    assert payload["model_calls"] == {}
    assert client.calls == 0
    assert _count(stores, EventType.PRECEDENT_CANDIDATE_MINED) == 0
    assert _count(stores, EventType.MEMORY_OP_JUDGED) == 0


def test_unparsed_replies_are_counted_logged_and_never_recorded(
    stores: StoreRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(stores)
    _wire(monkeypatch, FakeClient("not json", '{"lessons": [], "extra": 1}'))

    payload = json.loads(_run("--apply", "--format", "json").stdout)

    assert payload["outcomes"] == {"distill_unparsed": 2}
    assert payload["recorded"] == {"distill": 0, "merge": 0}
    assert payload["judged_events"]["total"] == 2
    assert _count(stores, EventType.PRECEDENT_CANDIDATE_MINED) == 0


def test_the_exit_code_does_not_depend_on_the_format(
    stores: StoreRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(stores)
    for local in (True, False):
        codes = []
        for fmt in ("json", "text"):
            _wire(monkeypatch, FakeClient(LESSON, LESSON, SAME), local=local)
            codes.append(_run("--format", fmt).exit_code)
        assert codes[0] == codes[1]
        assert codes[0] == (0 if local else 3)


def test_the_output_carries_counts_and_ids_only(
    stores: StoreRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(stores)

    for args in (
        ["--format", "json"],
        [],
        ["--apply", "--format", "json"],
        ["--apply"],
    ):
        _wire(monkeypatch, FakeClient(LESSON, LESSON, SAME))
        result = _run(*args)
        assert result.exit_code == 0, result.output
        assert SENTINEL not in result.stdout
        assert "bulk loader" not in result.stdout
        assert "stalls on a held lock" not in result.stdout


def test_a_route_with_no_model_and_no_label_is_a_validation_error(
    stores: StoreRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(stores)
    client = _wire(monkeypatch, FakeClient(LESSON), model=None)

    result = _run("--format", "json")

    assert result.exit_code == 2
    assert client.calls == 0
    labelled = _run("--model-id", "placeholder-label", "--merge-limit", "0")
    assert labelled.exit_code == 0, labelled.output


def test_the_sanctioned_requester_is_the_one_the_handler_accepts() -> None:
    assert REQUESTED_BY in LOCAL_MODEL_CANDIDATE_REQUESTERS
