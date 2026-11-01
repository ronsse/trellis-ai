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
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry
from trellis_cli import worker
from trellis_cli.main import app
from trellis_cli.stores import _reset_registry
from trellis_workers.learning.miner import MiningReport

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


class _FailingLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, **_kwargs: Any) -> LLMResponse:
        self.calls += 1
        msg = "model unreachable"
        raise RuntimeError(msg)


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


def _seed(
    stores: StoreRegistry,
    statuses: list[OutcomeStatus],
    *,
    domain: str = "mining",
) -> None:
    """Append one trace per status, oldest first."""
    for i, status in enumerate(statuses):
        stores.operational.trace_store.append(
            Trace(
                source=TraceSource.AGENT,
                intent=f"task-{i}",
                outcome=Outcome(status=status, summary=f"outcome {i}"),
                context=TraceContext(domain=domain),
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


class TestScopeAndLimitReachTheMiner:
    def test_a_real_run_is_scoped_to_its_domain(
        self, stores: StoreRegistry, llm: _CountingLLM
    ) -> None:
        _seed(stores, [F] * 4, domain="mining")

        elsewhere = _json(_run("--domain", "other", "--format", "json"))
        here = _json(_run("--domain", "mining", "--format", "json"))

        assert elsewhere["outcome"] == "no_failures"
        assert elsewhere["domain"] == "other"
        assert here["outcome"] == "mined"
        assert llm.calls == 1

    def test_a_dry_run_is_scoped_to_its_domain(
        self, stores: StoreRegistry, llm: _CountingLLM
    ) -> None:
        _seed(stores, [F] * 4, domain="mining")

        elsewhere = _json(_run("--dry-run", "--domain", "other", "--format", "json"))
        here = _json(_run("--dry-run", "--domain", "mining", "--format", "json"))

        assert elsewhere["failure_traces_in_scope"] == 0
        assert elsewhere["would_mine"] is False
        assert here["failure_traces_in_scope"] == 4
        assert here["would_mine"] is True

    def test_a_real_run_counts_failures_against_its_limit(
        self, stores: StoreRegistry, llm: _CountingLLM
    ) -> None:
        _seed(stores, [F] * 10)

        data = _json(_run("--limit", "4", "--min-traces", "3", "--format", "json"))

        assert data["failure_traces_in_scope"] == 4

    def test_a_dry_run_counts_failures_against_its_limit(
        self, stores: StoreRegistry, llm: _CountingLLM
    ) -> None:
        _seed(stores, [F] * 10)

        data = _json(
            _run("--dry-run", "--limit", "4", "--min-traces", "3", "--format", "json")
        )

        assert data["failure_traces_in_scope"] == 4


class TestArgumentsThatCannotMine:
    @pytest.mark.parametrize("fmt", ["text", "json"])
    def test_a_limit_below_min_traces_is_refused_in_both_formats(
        self, stores: StoreRegistry, llm: _CountingLLM, fmt: str
    ) -> None:
        _seed(stores, [F] * 10)

        result = _run("--limit", "2", "--min-traces", "5", "--format", fmt)

        assert result.exit_code == 2
        assert llm.calls == 0
        if fmt == "json":
            data = json.loads(result.stdout.strip())
            assert data["status"] == "error"
            assert "--limit (2)" in data["message"]
        else:
            assert "--limit (2) is below --min-traces (5)" in _flat(result)

    def test_a_dry_run_refuses_it_too(
        self, stores: StoreRegistry, llm: _CountingLLM
    ) -> None:
        _seed(stores, [F] * 10)

        result = _run("--dry-run", "--limit", "2", "--min-traces", "5")

        assert result.exit_code == 2

    @pytest.mark.parametrize(
        "args", [["--limit", "0"], ["--min-traces", "0"], ["--scan-cap", "0"]]
    )
    def test_a_zero_that_can_read_or_mine_nothing_is_a_usage_error(
        self, stores: StoreRegistry, llm: _CountingLLM, args: list[str]
    ) -> None:
        _seed(stores, [F] * 4)

        result = _run(*args)

        assert result.exit_code == 2
        assert llm.calls == 0
        # Refused by the option's own range, not by the --limit/--min-traces
        # cross-check, which would otherwise answer a zero limit.
        assert "is below --min-traces" not in result.output


class TestAFailedModelCall:
    def test_json_says_partial_and_names_the_outage(
        self, stores: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        failing = _FailingLLM()
        monkeypatch.setattr(
            worker, "_require_llm_client_or_exit", lambda _c, *, command: failing
        )
        _seed(stores, [F] * 4)

        result = _run("--format", "json")

        data = json.loads(result.stdout.strip())
        assert result.exit_code == 0
        assert data["status"] == "partial"
        assert data["outcome"] == "llm_error"
        assert data["candidate_count"] == 0
        assert failing.calls == 1

    @pytest.mark.parametrize("fmt", ["text", "json"])
    def test_the_exit_code_is_the_same_in_both_formats(
        self, stores: StoreRegistry, monkeypatch: pytest.MonkeyPatch, fmt: str
    ) -> None:
        monkeypatch.setattr(
            worker,
            "_require_llm_client_or_exit",
            lambda _c, *, command: _FailingLLM(),
        )
        _seed(stores, [F] * 4)

        default = _run("--format", fmt)
        strict = _run("--strict", "--format", fmt)

        assert default.exit_code == 0
        assert strict.exit_code == 1

    def test_strict_does_not_fail_a_run_that_worked(
        self, stores: StoreRegistry, llm: _CountingLLM
    ) -> None:
        _seed(stores, [F] * 4)

        result = _run("--strict", "--format", "json")

        assert result.exit_code == 0
        assert json.loads(result.stdout.strip())["status"] == "ok"

    def test_a_model_that_returns_nothing_is_not_a_failed_call(
        self, stores: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            worker,
            "_require_llm_client_or_exit",
            lambda _c, *, command: _CountingLLM("[]"),
        )
        _seed(stores, [F] * 4)

        result = _run("--strict", "--format", "json")

        data = json.loads(result.stdout.strip())
        assert result.exit_code == 0
        assert data["status"] == "ok"
        assert data["outcome"] == "no_candidates"

    def test_a_cut_off_read_stays_ok(
        self, stores: StoreRegistry, llm: _CountingLLM
    ) -> None:
        _seed(stores, [F] * 4 + [S] * 12)

        result = _run("--scan-cap", "8", "--strict", "--format", "json")

        data = json.loads(result.stdout.strip())
        assert result.exit_code == 0
        assert data["status"] == "ok"
        assert data["outcome"] == "read_bound_hit"


class TestTextNamesEveryOutcome:
    def test_a_model_that_returns_nothing_says_so_in_text(
        self, stores: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            worker,
            "_require_llm_client_or_exit",
            lambda _c, *, command: _CountingLLM("[]"),
        )
        _seed(stores, [F] * 4)

        out = _flat(_run())

        assert "no candidates:" in out
        assert "the next run asks again" in out
        assert "llm error" not in out

    def test_an_outage_says_so_in_text(
        self, stores: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            worker,
            "_require_llm_client_or_exit",
            lambda _c, *, command: _FailingLLM(),
        )
        _seed(stores, [F] * 4)

        out = _flat(_run())

        assert "llm error:" in out
        assert "the model call failed" in out
        assert "no candidates" not in out

    def test_a_real_run_says_it_mined(
        self, stores: StoreRegistry, llm: _CountingLLM
    ) -> None:
        _seed(stores, [F] * 4)

        out = _flat(_run())

        assert "mined: 1 candidate(s) from 4 failure/partial trace(s)" in out

    def test_a_dry_run_says_what_a_run_would_do(
        self, stores: StoreRegistry, llm: _CountingLLM
    ) -> None:
        _seed(stores, [F] * 4)

        out = _flat(_run("--dry-run"))

        assert "would mine: a real run would show the model 4" in out

    def test_a_dry_run_says_what_it_found_when_it_would_skip(
        self, stores: StoreRegistry, llm: _CountingLLM
    ) -> None:
        _seed(stores, [F] * 4)
        _run("--format", "json")

        out = _flat(_run("--dry-run"))

        assert "skipped:" in out
        assert "already mined" in out

    def test_a_read_to_its_end_below_the_minimum_says_so(
        self, stores: StoreRegistry, llm: _CountingLLM
    ) -> None:
        _seed(stores, [F, F, S, S])

        out = _flat(_run())

        assert "Below --min-traces (3): the store was read to its end." in out

    def test_the_cut_off_sentence_rides_on_a_run_that_mined(
        self, stores: StoreRegistry, llm: _CountingLLM
    ) -> None:
        _seed(stores, [F] * 2 + [S] * 20 + [F] * 3)

        out = _flat(_run("--scan-cap", "22", "--min-traces", "3", "--limit", "10"))

        assert "mined: 1 candidate(s)" in out
        assert "read stopped at the 22-trace cap" in out

    def test_the_cut_off_sentence_is_not_said_of_an_uncut_read(
        self, stores: StoreRegistry, llm: _CountingLLM
    ) -> None:
        _seed(stores, [F] * 4)

        assert "trace cap" not in _flat(_run())

    @pytest.mark.parametrize(
        ("outcome", "fragment"),
        [
            ("mined", "mined: 1 candidate(s) from 0"),
            ("would_mine", "would mine: a real run would show the model 0"),
            ("skipped_unchanged", "were already mined into 0 candidate(s)"),
            ("no_failures", "No failure/partial traces: read 9 trace(s)"),
            ("below_min_traces", "only 2 were kept, so nothing was mined"),
            ("no_candidates", "returned none; nothing was written"),
            ("llm_error", "the model call failed; nothing was written"),
            ("no_llm", "No model is configured"),
            ("read_bound_hit", "read stopped at the 50-trace cap"),
        ],
    )
    def test_each_outcome_has_its_own_sentence(
        self, outcome: Any, fragment: str
    ) -> None:
        # ``read_to_end`` is False here, so a below-minimum run caused by a
        # small ``--limit`` (reachable only through the API) is not said to
        # have read the store to its end.
        report = MiningReport(
            outcome=outcome,
            failures_in_scope=2,
            traces_examined=9,
            scan_cap=50,
            read_bound_hit=outcome == "read_bound_hit",
            candidates=(_one_candidate(),) if outcome == "mined" else (),
        )

        with worker.console.capture() as captured:
            worker._print_mining_outcome(report, min_traces=3)

        assert fragment in " ".join(plain(captured.get()).split())


def _one_candidate() -> Any:
    from trellis.schemas.precedent import Precedent

    return Precedent(
        precedent_id="p1",
        source_trace_ids=[],
        title="t",
        description="d",
        promoted_by="precedent_miner",
    )


class TestSkipOutputNamesWhatWasAnalysed:
    def test_the_skip_counts_the_traces_the_model_was_shown(
        self, stores: StoreRegistry, llm: _CountingLLM
    ) -> None:
        _seed(stores, [F] * 25)
        first = _json(_run("--format", "json"))

        again = _run()

        out = _flat(again)
        assert first["failure_traces_in_scope"] == 25
        assert "these 20 failure/partial trace(s) were already mined" in out
        assert "these 25" not in out

    def test_the_skip_ids_survive_markup_characters(
        self, stores: StoreRegistry, llm: _CountingLLM
    ) -> None:
        _seed(stores, [F] * 4)
        digest = _json(_run("--dry-run", "--format", "json"))["source_digest"]
        stores.operational.event_log.emit(
            EventType.PRECEDENT_CANDIDATE_MINED,
            source="precedent_miner",
            entity_id="cand-[x] :fire:",
            entity_type="precedent",
            payload={
                "source_digest": digest,
                "mining_run_id": "run-1",
                "run_candidate_count": 1,
            },
        )

        out = _flat(_run())

        assert "cand-[x] :fire:" in out

    def test_a_candidate_title_survives_markup_characters(
        self, stores: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reply = json.dumps(
            [{"title": "Retry [config] :fire: loop", "description": "d"}]
        )
        monkeypatch.setattr(
            worker,
            "_require_llm_client_or_exit",
            lambda _c, *, command: _CountingLLM(reply),
        )
        _seed(stores, [F] * 4)

        out = _flat(_run())

        assert "Retry [config] :fire: loop" in out
