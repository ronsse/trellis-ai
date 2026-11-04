"""``trellis worker distill-precedents``: dry-run first, counts and ids only."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from tests.cli_output import plain
from tests.precedent_candidates import SENTINEL, recovering_trace
from trellis.mutate.policy_source import resolve_policy_path
from trellis.schemas.enums import OutcomeStatus, PolicyType, TraceSource
from trellis.schemas.policy import Policy, PolicyRule, PolicyScope
from trellis.schemas.precedent_record import MAX_DOMAIN_CHARS, MAX_ID_CHARS
from trellis.schemas.trace import (
    EvidenceRef,
    Outcome,
    Trace,
    TraceContext,
    TraceStep,
)
from trellis.stores.base.event_log import EventType
from trellis.stores.policy_store import PolicyStore
from trellis.stores.registry import StoreRegistry
from trellis_cli.main import app
from trellis_cli.stores import _reset_registry

if TYPE_CHECKING:
    from pathlib import Path

    from click.testing import Result

runner = CliRunner()


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


def _failed_trace() -> Trace:
    return Trace(
        trace_id="trace-failed",
        source=TraceSource.AGENT,
        intent=f"migrate the ledger {SENTINEL}",
        steps=[TraceStep(step_type="tool_call", name="migrate", args={"k": 1})],
        outcome=Outcome(status=OutcomeStatus.FAILURE, summary=f"lock held {SENTINEL}"),
        context=TraceContext(domain="data"),
    )


def _seed(stores: StoreRegistry) -> None:
    for trace in (recovering_trace(), _failed_trace()):
        stores.operational.trace_store.append(trace)


def _run(*args: str) -> Result:
    return runner.invoke(app, ["worker", "distill-precedents", *args])


def _total_events(stores: StoreRegistry) -> int:
    return stores.operational.event_log.count()


def _candidates(stores: StoreRegistry) -> int:
    return stores.operational.event_log.count(
        event_type=EventType.PRECEDENT_CANDIDATE_MINED
    )


def test_a_dry_run_reports_counts_and_writes_nothing(stores: StoreRegistry) -> None:
    _seed(stores)
    before = _total_events(stores)

    result = _run("--format", "json")

    payload = json.loads(result.stdout)
    assert result.exit_code == 0
    assert payload["mode"] == "dry-run"
    assert payload["traces_read"] == 2
    assert payload["candidates"] == payload["would_record"] == 3
    assert payload["by_rule"] == {
        "failed_outcome": 1,
        "failed_step_avoid": 1,
        "failed_step_do": 1,
    }
    assert payload["duplicates"] == 0
    assert payload["truncated"] is False
    assert _total_events(stores) == before


def test_apply_records_each_candidate_once_and_a_rerun_records_none(
    stores: StoreRegistry,
) -> None:
    _seed(stores)

    first = json.loads(_run("--apply", "--format", "json").stdout)
    second = json.loads(_run("--apply", "--format", "json").stdout)
    dry = json.loads(_run("--format", "json").stdout)

    assert first["recorded"] == 3
    assert _candidates(stores) == 3
    assert second["recorded"] == 0
    assert second["duplicates"] == 3
    assert dry["would_record"] == 0
    assert dry["duplicates"] == 3
    assert _candidates(stores) == 3


def test_the_output_carries_no_trace_text(stores: StoreRegistry) -> None:
    _seed(stores)

    for args in (
        ["--format", "json"],
        [],
        ["--apply", "--format", "json"],
        ["--apply"],
    ):
        result = _run(*args)
        assert result.exit_code == 0
        assert SENTINEL not in result.stdout
        assert "schema mismatch" not in result.stdout


def test_a_window_cut_off_by_limit_says_so_in_both_formats(
    stores: StoreRegistry,
) -> None:
    _seed(stores)

    as_json = json.loads(_run("--limit", "1", "--format", "json").stdout)
    as_text = " ".join(plain(_run("--limit", "1").stdout).split())

    assert as_json["truncated"] is True
    assert as_json["traces_read"] == 1
    assert "older ones were not read" in as_text


def test_the_exit_code_does_not_depend_on_the_format(stores: StoreRegistry) -> None:
    _seed(stores)

    ok = [_run("--format", f).exit_code for f in ("json", "text")]
    bad = [
        _run("--since", "not-a-date", "--format", f).exit_code for f in ("json", "text")
    ]

    assert ok == [0, 0]
    assert bad[0] == bad[1] != 0


def test_a_policy_refusal_exits_the_same_in_both_formats(
    stores: StoreRegistry, tmp_path: Path
) -> None:
    _seed(stores)
    deny = Policy(
        policy_type=PolicyType.MUTATION,
        scope=PolicyScope(level="global", value=None),
        rules=[PolicyRule(operation="precedent.candidate", action="deny")],
    )
    PolicyStore(resolve_policy_path(tmp_path / "data" / "stores")).add(deny)

    codes = [_run("--apply", "--format", f).exit_code for f in ("json", "text")]

    assert codes[0] == codes[1] != 0
    assert _candidates(stores) == 0


def _seed_one_good_and_one(stores: StoreRegistry, bad: Trace) -> None:
    for trace in (recovering_trace(), bad):
        stores.operational.trace_store.append(trace)


@pytest.mark.parametrize("fmt", ["json", "text"])
def test_a_long_domain_is_clamped_and_does_not_stop_the_run(
    stores: StoreRegistry, fmt: str
) -> None:
    long_domain = recovering_trace(trace_id="trace-long", domain="d" * 130)
    assert len(long_domain.context.domain or "") > MAX_DOMAIN_CHARS
    _seed_one_good_and_one(stores, long_domain)

    result = _run("--apply", "--format", fmt)

    assert result.exit_code == 0
    assert _candidates(stores) == 4
    if fmt == "json":
        assert json.loads(result.stdout)["recorded"] == 4
    domains = {
        e.payload["record"]["situation"]["domain"]
        for e in stores.operational.event_log.get_events(
            event_type=EventType.PRECEDENT_CANDIDATE_MINED, limit=10
        )
    }
    assert "data" in domains
    assert len(domains) == 2
    assert all(len(d) <= MAX_DOMAIN_CHARS for d in domains)


@pytest.mark.parametrize("fmt", ["json", "text"])
def test_a_trace_that_cannot_make_a_valid_record_is_counted_and_the_run_goes_on(
    stores: StoreRegistry, fmt: str
) -> None:
    bad = recovering_trace(trace_id="t" * (MAX_ID_CHARS + 1))
    _seed_one_good_and_one(stores, bad)

    result = _run("--apply", "--format", fmt)

    assert result.exit_code == 0
    assert _candidates(stores) == 2  # the good trace's pair
    assert "t" * (MAX_ID_CHARS + 1) not in result.stdout
    if fmt == "json":
        payload = json.loads(result.stdout)
        assert payload["status"] == "ok"
        assert payload["traces_read"] == 2
        assert payload["recorded"] == 2
        assert payload["skipped"] == {"invalid_record": 1}
    else:
        assert "skipped invalid_record: 1" in plain(result.stdout)


@pytest.mark.parametrize("fmt", ["json", "text"])
def test_what_the_schema_bounds_cut_is_counted_not_silent(
    stores: StoreRegistry, fmt: str
) -> None:
    long_id = "e" * (MAX_ID_CHARS + 1)
    trace = _failed_trace().model_copy(
        update={"evidence_used": [EvidenceRef(evidence_id=long_id)]}
    )
    stores.operational.trace_store.append(trace)

    result = _run("--format", fmt)

    assert result.exit_code == 0
    assert long_id not in result.stdout
    if fmt == "json":
        assert json.loads(result.stdout)["dropped"] == {"entity_id_overlong": 1}
    else:
        assert "dropped entity_id_overlong: 1" in plain(result.stdout)


def test_a_run_that_cuts_nothing_reports_an_empty_dropped(
    stores: StoreRegistry,
) -> None:
    _seed(stores)

    assert json.loads(_run("--format", "json").stdout)["dropped"] == {}
