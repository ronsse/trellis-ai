"""``trellis analyze pack-judge``: the two phases, their exit codes, their output.

CliRunner against real SQLite stores in a tmp config/data dir, with a scripted
fake client swapped in for the one the ``llm:`` block would build. No test
reaches a model or a network.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from tests.cli_output import force_colour, plain
from trellis.llm.types import LLMResponse, Message
from trellis.stores.base.event_log import Event, EventType
from trellis_cli import analyze_pack_judge as judge_cli
from trellis_cli import worker
from trellis_cli.admin import admin_app
from trellis_cli.exit_codes import EXIT_OK, EXIT_STORE, EXIT_VALIDATION
from trellis_cli.main import app
from trellis_cli.stores import _get_registry, _reset_registry

runner = CliRunner()

INTENT = "roll out the billing service to staging"
ANSWERS = {"a1": "helpful", "a2": "unhelpful", "a3": "unhelpful", "a4": "helpful"}


class _Client:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    async def generate(
        self,
        *,
        messages: list[Message],
        temperature: float = 0.3,
        max_tokens: int = 500,
        model: str | None = None,
    ) -> LLMResponse:
        self.calls += 1
        if self.fail:
            message = "endpoint down"
            raise RuntimeError(message)
        match = re.search(r"DOC::(\w+)", messages[-1].content)
        assert match
        return LLMResponse(content=ANSWERS[match.group(1)])


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("COLUMNS", "500")
    init = runner.invoke(admin_app, ["init"])
    assert init.exit_code == 0, init.output
    _reset_registry()


def _emit(event_type: EventType, entity_id: str, payload: dict[str, Any]) -> None:
    at = datetime.now(tz=UTC) - timedelta(hours=1)
    _get_registry().operational.event_log.append(
        Event(
            event_type=event_type,
            source="test",
            entity_id=entity_id,
            occurred_at=at,
            recorded_at=at,
            payload=payload,
        )
    )


@pytest.fixture
def seeded(cli_env: None) -> None:
    _emit(
        EventType.PACK_ASSEMBLED,
        "pack-1",
        {
            "intent": INTENT,
            "injected_items": [
                {"item_id": item, "rank": rank, "estimated_tokens": 10}
                for rank, item in enumerate(ANSWERS, start=1)
            ],
        },
    )
    _emit(
        EventType.FEEDBACK_RECORDED,
        "pack-1",
        {
            "pack_id": "pack-1",
            "feedback_id": "fb-1",
            "rating": 0.5,
            "success": True,
            "helpful_item_ids": ["a1", "a2"],
            "unhelpful_item_ids": ["a3"],
        },
    )
    store = _get_registry().knowledge.document_store
    for item in ANSWERS:
        store.put(item, f"DOC::{item} body for {item}")


def _client(monkeypatch: pytest.MonkeyPatch, client: _Client) -> _Client:
    monkeypatch.setattr(
        worker, "_require_llm_client_or_exit", lambda _consumer, *, command: client
    )
    return client


def _run(*args: str, fmt: str = "json") -> Any:
    return runner.invoke(app, ["analyze", "pack-judge", *args, "--format", fmt])


def test_the_command_is_mounted_under_analyze(cli_env: None) -> None:
    result = runner.invoke(app, ["analyze", "--help"])

    assert result.exit_code == 0
    assert "pack-judge" in plain(result.output)


def test_judge_then_calibrate_without_touching_any_store(
    seeded: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(monkeypatch, _Client())
    log = _get_registry().operational.event_log
    before = log.count()
    out = tmp_path / "judged.jsonl"

    judged = _run("--out", str(out), "--model-id", "local-m", "--since", "2020-01-01")
    assert judged.exit_code == EXIT_OK, judged.output
    summary = json.loads(judged.stdout)
    assert summary["status"] == "ok"
    assert summary["model"] == "local-m"
    assert summary["rows_written"] == 4
    assert client.calls == 4
    assert log.count() == before  # no event, and no meta-trace, was written

    calibrated = _run(
        "--judged", str(out), "--min-graded", "2", "--since", "2020-01-01"
    )
    assert calibrated.exit_code == EXIT_OK, calibrated.output
    report = json.loads(calibrated.stdout)
    assert report["status"] == "ok"
    assert report["graded_overlap"] == 3
    assert report["agreement"] == pytest.approx(2 / 3)
    assert report["matrix"]["agent_unhelpful"]["unhelpful"] == 1
    assert log.count() == before


def test_the_model_label_defaults_when_no_route_names_one(
    seeded: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _client(monkeypatch, _Client())
    result = _run("--out", str(tmp_path / "j.jsonl"), "--since", "2020-01-01")

    assert result.exit_code == EXIT_OK, result.output
    assert json.loads(result.stdout)["model"] == "unknown"


@pytest.mark.parametrize("fmt", ["text", "json"])
def test_a_refused_calibration_exits_the_same_whatever_the_format(
    seeded: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fmt: str
) -> None:
    _client(monkeypatch, _Client())
    out = tmp_path / "judged.jsonl"
    assert _run("--out", str(out), "--since", "2020-01-01").exit_code == EXIT_OK

    result = _run("--judged", str(out), "--since", "2020-01-01", fmt=fmt)

    assert result.exit_code == EXIT_VALIDATION
    if fmt == "json":
        payload = json.loads(result.stdout)
        assert payload["status"] == "refused"
        assert payload["refusal"] == "insufficient_graded_overlap"
        assert payload["kappa"] is None
    else:
        assert "refused" in plain(result.output)


@pytest.mark.parametrize("fmt", ["text", "json"])
def test_a_failing_endpoint_exits_non_zero_whatever_the_format(
    seeded: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fmt: str
) -> None:
    _client(monkeypatch, _Client(fail=True))
    out = tmp_path / f"{fmt}.jsonl"

    result = _run("--out", str(out), "--since", "2020-01-01", fmt=fmt)

    assert result.exit_code == EXIT_STORE
    if fmt == "json":
        payload = json.loads(result.stdout)
        assert payload["status"] == "partial"
        assert payload["llm_errors"] == 4
        assert payload["rows_written"] == 0


@pytest.mark.parametrize("fmt", ["text", "json"])
def test_naming_both_phases_or_neither_is_a_validation_error(
    seeded: None, tmp_path: Path, fmt: str
) -> None:
    both = _run("--out", str(tmp_path / "a"), "--judged", str(tmp_path / "b"), fmt=fmt)
    neither = _run(fmt=fmt)

    assert both.exit_code == EXIT_VALIDATION
    assert neither.exit_code == EXIT_VALIDATION
    assert "--out" in plain(both.output)


@pytest.mark.parametrize("fmt", ["text", "json"])
def test_a_bad_window_is_a_validation_error(
    seeded: None, tmp_path: Path, fmt: str
) -> None:
    result = _run("--out", str(tmp_path / "a"), "--since", "last tuesday", fmt=fmt)

    assert result.exit_code == EXIT_VALIDATION
    assert "--since" in plain(result.output)


def test_the_judge_without_a_configured_model_says_so(
    seeded: None, tmp_path: Path
) -> None:
    result = _run("--out", str(tmp_path / "a"), "--since", "2020-01-01")

    assert result.exit_code != EXIT_OK
    assert "llm" in result.output.lower()


@pytest.mark.parametrize("colour", [False, True], ids=["plain", "colour"])
def test_a_path_that_looks_like_markup_prints_verbatim(
    seeded: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    colour: bool,
) -> None:
    _client(monkeypatch, _Client())
    if colour:
        force_colour(monkeypatch, judge_cli)
    out = tmp_path / "out[dev].jsonl"

    result = _run("--out", str(out), "--since", "2020-01-01", fmt="text")

    assert result.exit_code == EXIT_OK, result.output
    assert str(out) in plain(result.output)
    assert "helpful" in plain(result.output)
