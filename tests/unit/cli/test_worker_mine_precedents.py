"""``trellis worker mine-precedents``: what a run reads, skips and says.

The miner's own behaviour is pinned in
``tests/unit/workers/learning/test_miner.py``. These pin the command around it:
the payload keys a script reads, that a skipped rerun and a cut-off read are
visible in both formats, and that the exit code does not depend on ``--format``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from typer.testing import CliRunner

from tests.cli_output import assert_coloured, force_colour, plain
from trellis.llm import LLMResponse, Message
from trellis.schemas.enums import OutcomeStatus, TraceSource
from trellis.schemas.trace import Outcome, Trace, TraceContext
from trellis.stores.registry import StoreRegistry
from trellis_cli import worker
from trellis_cli.main import app
from trellis_cli.stores import _reset_registry

if TYPE_CHECKING:
    from pathlib import Path

    from click.testing import Result

runner = CliRunner()

F = OutcomeStatus.FAILURE
S = OutcomeStatus.SUCCESS
_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)

_CANDIDATE = json.dumps(
    [
        {
            "title": "Failure pattern",
            "description": "Common breakage",
            "pattern": "p",
            "confidence": 0.8,
        }
    ]
)


class _CountingLLM:
    def __init__(self, content: str = _CANDIDATE) -> None:
        self._content = content
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
        return LLMResponse(content=self._content, model=model or "test-model")


@pytest.fixture
def stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> StoreRegistry:
    data_dir = tmp_path / "data"
    stores_dir = data_dir / "stores"
    stores_dir.mkdir(parents=True)
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))
    _reset_registry()
    return StoreRegistry(stores_dir=stores_dir)


@pytest.fixture
def llm(monkeypatch: pytest.MonkeyPatch) -> _CountingLLM:
    stub = _CountingLLM()
    monkeypatch.setattr(
        worker, "_require_llm_client_or_exit", lambda _consumer, *, command: stub
    )
    return stub


def _seed(stores: StoreRegistry, statuses: list[OutcomeStatus]) -> None:
    """Append one trace per status, oldest first."""
    for i, status in enumerate(statuses):
        stores.operational.trace_store.append(
            Trace(
                source=TraceSource.AGENT,
                intent=f"task-{i}",
                outcome=Outcome(status=status, summary=f"outcome {i}"),
                context=TraceContext(domain="mining"),
                created_at=_EPOCH + timedelta(minutes=i),
            )
        )


def _run(*args: str) -> Result:
    return runner.invoke(app, ["worker", "mine-precedents", *args])


def _flat(result: Result) -> str:
    """The sentence a person reads: no colour, and no wrap at the terminal width."""
    return " ".join(plain(result.stdout).split())


def _json(result: Result) -> dict[str, Any]:
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout.strip())  # type: ignore[no-any-return]


class TestLimitCountsFailures:
    def test_dry_run_counts_failures_older_than_the_newest_window(
        self, stores: StoreRegistry, llm: _CountingLLM
    ) -> None:
        _seed(stores, [F] * 5 + [S] * 10)

        data = _json(_run("--dry-run", "--limit", "10", "--format", "json"))

        assert data["failure_traces_in_scope"] == 5
        assert data["would_mine"] is True
        assert data["outcome"] == "would_mine"
        assert llm.calls == 0

    def test_a_run_mines_failures_older_than_the_newest_window(
        self, stores: StoreRegistry, llm: _CountingLLM
    ) -> None:
        _seed(stores, [F] * 5 + [S] * 10)

        data = _json(_run("--limit", "10", "--format", "json"))

        assert data["candidate_count"] == 1
        assert data["outcome"] == "mined"
        assert llm.calls == 1


class TestEmptyResultSaysWhy:
    def test_no_failures_in_the_store(
        self, stores: StoreRegistry, llm: _CountingLLM
    ) -> None:
        _seed(stores, [S] * 6)

        data = _json(_run("--format", "json"))

        assert data["outcome"] == "no_failures"
        assert data["read_bound_hit"] is False
        assert data["traces_examined"] == 6
        assert data["candidate_count"] == 0

    def test_the_read_bound_hit_before_any_failure(
        self, stores: StoreRegistry, llm: _CountingLLM
    ) -> None:
        _seed(stores, [F] * 4 + [S] * 12)

        data = _json(_run("--scan-cap", "8", "--format", "json"))

        assert data["outcome"] == "read_bound_hit"
        assert data["read_bound_hit"] is True
        assert data["scan_cap"] == 8
        assert data["traces_examined"] == 8
        assert data["failure_traces_in_scope"] == 0
        assert llm.calls == 0

    def test_dry_run_says_the_bound_was_hit_too(
        self, stores: StoreRegistry, llm: _CountingLLM
    ) -> None:
        _seed(stores, [F] * 4 + [S] * 12)

        data = _json(_run("--dry-run", "--scan-cap", "8", "--format", "json"))

        assert data["would_mine"] is False
        assert data["outcome"] == "read_bound_hit"
        assert data["read_bound_hit"] is True

    def test_text_says_the_read_was_cut_off(
        self, stores: StoreRegistry, llm: _CountingLLM
    ) -> None:
        _seed(stores, [F] * 4 + [S] * 12)

        cut_off = _flat(_run("--scan-cap", "8"))

        assert "read stopped at the 8-trace cap" in cut_off
        assert "older traces were not read" in cut_off
        assert "holds no more" not in cut_off

    def test_text_says_the_store_was_read_to_its_end(
        self, stores: StoreRegistry, llm: _CountingLLM
    ) -> None:
        _seed(stores, [S] * 6)

        out = _flat(_run())

        assert "holds no more" in out
        assert "trace cap" not in out

    @pytest.mark.parametrize("failures", [0, 2])
    def test_exit_code_does_not_depend_on_format(
        self, stores: StoreRegistry, llm: _CountingLLM, failures: int
    ) -> None:
        _seed(stores, [F] * failures + [S] * 12)

        as_text = _run("--scan-cap", "8")
        as_json = _run("--scan-cap", "8", "--format", "json")

        assert as_text.exit_code == as_json.exit_code == 0


class TestRerun:
    def test_the_second_run_over_unchanged_failures_skips_and_says_so(
        self, stores: StoreRegistry, llm: _CountingLLM
    ) -> None:
        _seed(stores, [F] * 4)
        first = _json(_run("--format", "json"))

        second = _json(_run("--format", "json"))

        assert first["candidate_count"] == 1
        assert second["candidate_count"] == 0
        assert second["outcome"] == "skipped_unchanged"
        assert second["skipped_unchanged"] is True
        assert second["already_mined_candidate_ids"] == [
            first["candidates"][0]["precedent_id"]
        ]
        assert second["source_digest"]
        assert llm.calls == 1

    def test_the_skip_is_in_the_text_output_too(
        self, stores: StoreRegistry, llm: _CountingLLM
    ) -> None:
        _seed(stores, [F] * 4)
        first = _json(_run("--format", "json"))

        result = _run()

        out = _flat(result)
        assert result.exit_code == 0
        assert "skipped" in out
        assert "already mined" in out
        assert first["candidates"][0]["precedent_id"] in out
        assert llm.calls == 1

    def test_skipped_run_and_mined_run_exit_alike_in_both_formats(
        self, stores: StoreRegistry, llm: _CountingLLM
    ) -> None:
        _seed(stores, [F] * 4)
        _run("--format", "json")

        assert _run().exit_code == _run("--format", "json").exit_code == 0

    def test_a_new_failure_mints_again(
        self, stores: StoreRegistry, llm: _CountingLLM
    ) -> None:
        _seed(stores, [F] * 4)
        _run("--format", "json")
        stores.operational.trace_store.append(
            Trace(
                source=TraceSource.AGENT,
                intent="a newer failure",
                outcome=Outcome(status=F, summary="it broke again"),
                context=TraceContext(domain="mining"),
                created_at=_EPOCH + timedelta(days=1),
            )
        )

        data = _json(_run("--format", "json"))

        assert data["outcome"] == "mined"
        assert data["candidate_count"] == 1
        assert llm.calls == 2

    def test_dry_run_reports_that_a_run_would_skip(
        self, stores: StoreRegistry, llm: _CountingLLM
    ) -> None:
        _seed(stores, [F] * 4)
        _run("--format", "json")

        data = _json(_run("--dry-run", "--format", "json"))

        assert data["failure_traces_in_scope"] == 4
        assert data["would_mine"] is False
        assert data["outcome"] == "skipped_unchanged"
        assert data["skipped_unchanged"] is True
        assert llm.calls == 1


class TestColour:
    def test_a_skip_names_a_candidate_id_whole_under_colour(
        self,
        stores: StoreRegistry,
        llm: _CountingLLM,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed(stores, [F] * 4)
        first = _json(_run("--format", "json"))
        force_colour(monkeypatch, worker)

        result = _run()

        out = " ".join(assert_coloured(result.stdout).split())
        assert first["candidates"][0]["precedent_id"] in out
