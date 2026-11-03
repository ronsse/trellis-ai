"""Precedent mining asks the egress gate before it shows traces to a model."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from tests.cli_output import plain
from tests.unit.cli.test_worker import runner
from tests.unit.workers.learning.test_miner import (
    _MOCK_JSON,
    _CountingLLM,
    _make_trace,
)
from trellis.ingest_corpus.models import chunk_doc_id
from trellis.llm.egress import EgressGate
from trellis.llm.routing import LLMConsumer
from trellis.schemas.enums import OutcomeStatus
from trellis.stores.sqlite.document import SQLiteDocumentStore
from trellis.stores.sqlite.trace import SQLiteTraceStore
from trellis_cli import worker
from trellis_cli.main import app
from trellis_cli.stores import _reset_registry, get_document_store, get_trace_store
from trellis_cli.worker import _mining_scan_fields
from trellis_workers.learning.miner import PrecedentMiner

_SECRET = {"data_classification": {"sensitivity": "confidential"}}


@pytest.fixture
def docs(tmp_path: Path):
    store = SQLiteDocumentStore(tmp_path / "docs.db")
    yield store
    store.close()


@pytest.fixture
def traces(tmp_path: Path):
    store = SQLiteTraceStore(tmp_path / "traces.db")
    yield store
    store.close()


def _seed(traces: SQLiteTraceStore, evidence: dict[str, list[str]]) -> None:
    """One failure trace per intent word, citing the given evidence ids."""
    for word, ids in evidence.items():
        traces.append(
            _make_trace(
                intent=f"{word} intent", status=OutcomeStatus.FAILURE, evidence_ids=ids
            )
        )


#: A gate over a declared-local route: it reads nothing, so it stands in for "no gate".
_LOCAL = EgressGate(None, local=True)


def _miner(
    traces: SQLiteTraceStore, llm: _CountingLLM, gate: EgressGate
) -> PrecedentMiner:
    return PrecedentMiner(traces, llm=llm, egress=gate)


async def test_a_trace_citing_a_labelled_document_is_not_shown_to_the_model(
    traces: SQLiteTraceStore, docs: SQLiteDocumentStore
) -> None:
    docs.put("ev-secret", "body", _SECRET)
    _seed(traces, {"alpha": [], "bravo": ["ev-secret"], "charlie": []})
    llm = _CountingLLM(_MOCK_JSON)

    report = await _miner(traces, llm, EgressGate(docs, local=False)).mine(min_traces=2)

    assert report.outcome == "mined"
    assert report.egress_withheld == 1
    assert len(llm.prompts) == 1
    assert "bravo" not in llm.prompts[0]
    assert "alpha intent" in llm.prompts[0]
    assert len(report.candidates[0].source_trace_ids) == 2


async def test_withheld_traces_count_against_the_minimum(
    traces: SQLiteTraceStore, docs: SQLiteDocumentStore
) -> None:
    docs.put("ev-secret", "body", _SECRET)
    _seed(traces, {"alpha": [], "bravo": ["ev-secret"], "charlie": []})
    llm = _CountingLLM(_MOCK_JSON)
    miner = _miner(traces, llm, EgressGate(docs, local=False))

    report = await miner.mine(min_traces=3)

    assert report.outcome == "below_min_traces"
    assert llm.prompts == []
    assert _mining_scan_fields(report)["skipped_egress_withheld"] == 1


async def test_a_store_outage_is_unreadable_and_is_not_counted_as_a_label(
    traces: SQLiteTraceStore,
) -> None:
    broken = MagicMock()
    broken.get.side_effect = RuntimeError("store down")
    _seed(traces, {"alpha": ["ev-x"], "bravo": [], "charlie": []})
    llm = _CountingLLM(_MOCK_JSON)

    report = await _miner(traces, llm, EgressGate(broken, local=False)).mine(
        min_traces=2
    )

    assert report.egress_unreadable == 1
    assert report.egress_withheld == 0
    assert "alpha" not in llm.prompts[0]
    fields = _mining_scan_fields(report)
    assert fields["skipped_egress_unreadable"] == 1
    assert fields["skipped_egress_withheld"] == 0


async def test_the_constructor_requires_a_gate(traces: SQLiteTraceStore) -> None:
    with pytest.raises(TypeError, match="egress"):
        PrecedentMiner(traces)  # type: ignore[call-arg]


@pytest.mark.parametrize("order", [("ev-ok", "ev-secret"), ("ev-secret", "ev-ok")])
async def test_one_labelled_citation_withholds_the_trace_in_either_order(
    traces: SQLiteTraceStore, docs: SQLiteDocumentStore, order: tuple[str, ...]
) -> None:
    docs.put("ev-ok", "body", {})
    docs.put("ev-secret", "body", _SECRET)
    _seed(traces, {"alpha": list(order), "bravo": [], "charlie": []})
    llm = _CountingLLM(_MOCK_JSON)

    report = await _miner(traces, llm, EgressGate(docs, local=False)).mine(min_traces=2)

    assert report.egress_withheld == 1
    assert "alpha" not in llm.prompts[0]


async def test_a_labelled_trace_beside_a_withheld_one_is_counted_once_each(
    traces: SQLiteTraceStore, docs: SQLiteDocumentStore
) -> None:
    docs.put("ev-secret", "body", _SECRET)
    broken = MagicMock()
    broken.get.side_effect = lambda doc_id: (
        (_ for _ in ()).throw(RuntimeError("down"))
        if doc_id == "ev-down"
        else docs.get(doc_id)
    )
    _seed(
        traces,
        {"alpha": ["ev-secret"], "bravo": ["ev-down"], "charlie": [], "delta": []},
    )
    llm = _CountingLLM(_MOCK_JSON)

    report = await _miner(traces, llm, EgressGate(broken, local=False)).mine(
        min_traces=2
    )

    assert (report.egress_withheld, report.egress_unreadable) == (1, 1)
    assert report.failures_in_scope == 2


async def test_a_chunk_id_is_judged_by_its_parent_and_an_orphan_is_withheld(
    traces: SQLiteTraceStore, docs: SQLiteDocumentStore
) -> None:
    docs.put("secret-parent", "body", _SECRET)
    docs.put("plain-parent", "body", {})
    docs.put(chunk_doc_id("secret-parent", 0), "c", {})
    docs.put(chunk_doc_id("plain-parent", 0), "c", {})
    _seed(
        traces,
        {
            "alpha": [chunk_doc_id("secret-parent", 0)],
            "bravo": [chunk_doc_id("plain-parent", 0)],
            "charlie": [chunk_doc_id("gone", 0)],
            "delta": [],
        },
    )
    llm = _CountingLLM(_MOCK_JSON)

    report = await _miner(traces, llm, EgressGate(docs, local=False)).mine(min_traces=2)

    assert (report.egress_withheld, report.egress_unreadable) == (2, 0)
    assert "bravo" in llm.prompts[0]
    assert "delta" in llm.prompts[0]
    assert "alpha" not in llm.prompts[0]
    assert "charlie" not in llm.prompts[0]


@pytest.mark.parametrize("fault", ["label", "outage"])
async def test_every_trace_kept_back_is_all_withheld_not_no_failures(
    traces: SQLiteTraceStore, docs: SQLiteDocumentStore, fault: str
) -> None:
    docs.put("ev-secret", "body", _SECRET)
    broken = MagicMock()
    broken.get.side_effect = RuntimeError("store down")
    _seed(traces, {w: ["ev-secret"] for w in ("alpha", "bravo", "charlie")})
    llm = _CountingLLM(_MOCK_JSON)
    gate = EgressGate(docs if fault == "label" else broken, local=False)

    report = await _miner(traces, llm, gate).mine()

    assert report.outcome == "all_withheld"
    assert llm.prompts == []
    assert report.egress_withheld + report.egress_unreadable == 3


async def test_below_min_says_when_withheld_traces_caused_it(
    traces: SQLiteTraceStore, docs: SQLiteDocumentStore
) -> None:
    docs.put("ev-secret", "body", _SECRET)
    _seed(traces, {"alpha": ["ev-secret"], "bravo": [], "charlie": []})
    caused = await _miner(
        traces, _CountingLLM(_MOCK_JSON), EgressGate(docs, local=False)
    ).mine(min_traces=3)
    short = await _miner(
        traces, _CountingLLM(_MOCK_JSON), EgressGate(docs, local=False)
    ).mine(min_traces=4)

    assert caused.outcome == short.outcome == "below_min_traces"
    assert caused.below_min_due_to_egress is True
    assert short.below_min_due_to_egress is False
    assert _mining_scan_fields(caused)["below_min_traces_due_to_egress"] is True
    assert _mining_scan_fields(short)["below_min_traces_due_to_egress"] is False


async def test_labelled_traces_still_take_a_seat_under_the_scan_limit(
    traces: SQLiteTraceStore, docs: SQLiteDocumentStore
) -> None:
    docs.put("ev-secret", "body", _SECRET)
    # Newest first: the two labelled traces are read before the two plain ones.
    _seed(traces, {"charlie": [], "delta": []})
    _seed(traces, {"alpha": ["ev-secret"], "bravo": ["ev-secret"]})
    llm = _CountingLLM(_MOCK_JSON)

    report = await _miner(traces, llm, EgressGate(docs, local=False)).mine(
        min_traces=1, limit=2
    )

    # The filter runs after the scan limit, so labelled traces use up seats:
    # two plain traces exist and none is mined.
    assert report.outcome == "all_withheld"
    assert report.failures_in_scope == 0
    assert llm.prompts == []


async def test_a_declared_local_route_sends_a_labelled_trace_and_reads_nothing(
    traces: SQLiteTraceStore,
) -> None:
    untouched = MagicMock()
    _seed(traces, {"alpha": ["ev-secret"], "bravo": [], "charlie": []})
    gated, plain = _CountingLLM(_MOCK_JSON), _CountingLLM(_MOCK_JSON)

    with_gate = await _miner(traces, gated, EgressGate(untouched, local=True)).mine()
    without = await _miner(traces, plain, _LOCAL).mine()

    untouched.get.assert_not_called()
    assert gated.prompts == plain.prompts
    assert "alpha intent" in gated.prompts[0]
    assert with_gate.egress_withheld == 0
    assert with_gate.source_digest == without.source_digest


async def test_nothing_labelled_is_byte_identical_to_an_ungated_run(
    traces: SQLiteTraceStore, docs: SQLiteDocumentStore
) -> None:
    docs.put("ev-internal", "body", {})
    docs.put("ev-open", "body", {"data_classification": {"sensitivity": "public"}})
    _seed(
        traces,
        {
            "alpha": ["ev-internal"],
            "bravo": ["ev-open", "no-such-row", ""],
            "charlie": [],
        },
    )
    gated, plain = _CountingLLM(_MOCK_JSON), _CountingLLM(_MOCK_JSON)

    with_gate = await _miner(traces, gated, EgressGate(docs, local=False)).mine()
    without = await _miner(traces, plain, _LOCAL).mine()

    assert gated.prompts == plain.prompts
    assert len(gated.prompts) == 1
    assert with_gate.egress_withheld == 0
    assert with_gate.source_digest == without.source_digest
    assert with_gate.failures_in_scope == without.failures_in_scope == 3
    assert [c.title for c in with_gate.candidates] == [
        c.title for c in without.candidates
    ]
    fields: dict[str, Any] = _mining_scan_fields(with_gate)
    assert fields["skipped_egress_withheld"] == 0


# ---------------------------------------------------------------------------
# Through the real ``trellis worker mine-precedents`` command
# ---------------------------------------------------------------------------


@pytest.fixture
def cli_stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the CLI's stores at a synthetic directory."""
    data_dir = tmp_path / "cli-data"
    (data_dir / "stores").mkdir(parents=True)
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "cli-config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))
    _reset_registry()


def _seed_cli(evidence: dict[str, list[str]]) -> None:
    store = get_trace_store()
    for word, ids in evidence.items():
        store.append(
            _make_trace(
                intent=f"{word} intent", status=OutcomeStatus.FAILURE, evidence_ids=ids
            )
        )


def _stub_llm(monkeypatch: pytest.MonkeyPatch, llm: _CountingLLM) -> None:
    monkeypatch.setattr(worker, "_require_llm_client_or_exit", lambda *_a, **_k: llm)


def test_the_command_does_not_show_a_labelled_trace_to_the_model(
    cli_stores: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_document_store().put("ev-secret", "body", _SECRET)
    _seed_cli({"alpha": [], "bravo": ["ev-secret"], "charlie": [], "delta": []})
    llm = _CountingLLM(_MOCK_JSON)
    _stub_llm(monkeypatch, llm)

    result = runner.invoke(app, ["worker", "mine-precedents", "--format", "json"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["skipped_egress_withheld"] == 1
    assert data["skipped_egress_unreadable"] == 0
    assert len(llm.prompts) == 1
    assert "bravo intent" not in llm.prompts[0]
    assert "alpha intent" in llm.prompts[0]


def test_the_command_reports_all_withheld_in_both_formats(
    cli_stores: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_document_store().put("ev-secret", "body", _SECRET)
    _seed_cli({w: ["ev-secret"] for w in ("alpha", "bravo", "charlie")})
    llm = _CountingLLM(_MOCK_JSON)
    _stub_llm(monkeypatch, llm)

    as_json = runner.invoke(app, ["worker", "mine-precedents", "--format", "json"])
    as_text = runner.invoke(app, ["worker", "mine-precedents"])

    data = json.loads(as_json.output)
    assert data["outcome"] == "all_withheld"
    assert data["failure_traces_in_scope"] == 0
    assert llm.prompts == []
    text = plain(as_text.output)
    assert "all withheld" in text
    assert "no failure/partial traces" not in text.lower()
    assert as_json.exit_code == as_text.exit_code == 0


def test_the_command_says_when_withheld_traces_caused_below_min(
    cli_stores: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_document_store().put("ev-secret", "body", _SECRET)
    _seed_cli({"alpha": ["ev-secret"], "bravo": [], "charlie": []})
    _stub_llm(monkeypatch, _CountingLLM(_MOCK_JSON))

    as_json = runner.invoke(app, ["worker", "mine-precedents", "--format", "json"])
    as_text = runner.invoke(app, ["worker", "mine-precedents"])

    assert json.loads(as_json.output)["below_min_traces_due_to_egress"] is True
    assert "because of egress" in plain(as_text.output)


@pytest.mark.parametrize("strict", [False, True])
def test_a_store_outage_is_partial_and_strict_fails_it_in_either_format(
    cli_stores: None,
    monkeypatch: pytest.MonkeyPatch,
    strict: bool,
) -> None:
    docs = get_document_store()
    _seed_cli({w: ["ev-x"] for w in ("alpha", "bravo", "charlie")})
    _stub_llm(monkeypatch, _CountingLLM(_MOCK_JSON))

    def down(_doc_id: str) -> Any:
        msg = "store down"
        raise RuntimeError(msg)

    monkeypatch.setattr(docs, "get", down)
    flag = ["--strict"] if strict else []

    as_json = runner.invoke(
        app, ["worker", "mine-precedents", "--format", "json", *flag]
    )
    as_text = runner.invoke(app, ["worker", "mine-precedents", *flag])

    data = json.loads(as_json.output)
    assert data["status"] == "partial"
    assert data["outcome"] == "all_withheld"
    assert data["skipped_egress_unreadable"] == 3
    assert data["skipped_egress_withheld"] == 0
    assert "egress_unreadable" in plain(as_text.output)
    expected = worker.EXIT_INTERNAL if strict else 0
    assert as_json.exit_code == as_text.exit_code == expected


@pytest.mark.parametrize("output_format", ["json", "text"])
def test_a_run_that_only_withheld_traces_is_ok_and_strict_exits_zero(
    cli_stores: None,
    monkeypatch: pytest.MonkeyPatch,
    output_format: str,
) -> None:
    """A label doing its job is not a failed run, so strict must not fail it."""
    get_document_store().put("ev-secret", "body", _SECRET)
    _seed_cli({"alpha": [], "bravo": ["ev-secret"], "charlie": [], "delta": []})
    _stub_llm(monkeypatch, _CountingLLM(_MOCK_JSON))

    result = runner.invoke(
        app,
        ["worker", "mine-precedents", "--format", output_format, "--strict"],
    )

    assert result.exit_code == 0, result.output
    if output_format == "json":
        data = json.loads(result.output)
        assert data["status"] == "ok"
        assert data["skipped_egress_withheld"] == 1
        assert data["skipped_egress_unreadable"] == 0


def test_the_gate_and_the_client_are_built_from_the_same_route(
    cli_stores: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the miner's own route decides: a registry that answers per consumer."""
    registry = worker._get_registry()
    get_document_store().put("ev-secret", "body", _SECRET)
    _seed_cli({"alpha": ["ev-secret"], "bravo": [], "charlie": [], "delta": []})
    asked: list[LLMConsumer] = []
    built: list[LLMConsumer] = []
    llm = _CountingLLM(_MOCK_JSON)
    local_for = {"consumer": LLMConsumer.PRECEDENT_MINING, "local": False}

    def llm_route(consumer: LLMConsumer) -> Any:
        asked.append(consumer)
        return SimpleNamespace(
            local=(consumer is local_for["consumer"]) == local_for["local"]
        )

    def build_llm_client(*, consumer: LLMConsumer) -> Any:
        built.append(consumer)
        return llm

    monkeypatch.setattr(registry, "llm_route", llm_route)
    monkeypatch.setattr(registry, "build_llm_client", build_llm_client)

    # Only the miner's route is non-local: the labelled trace is withheld.
    runner.invoke(app, ["worker", "mine-precedents", "--format", "json"])
    assert "alpha intent" not in llm.prompts[0]
    assert set(asked) == set(built) == {LLMConsumer.PRECEDENT_MINING}

    # Only the miner's route is local, every other consumer is cloud: it is sent.
    llm.prompts.clear()
    local_for["local"] = True
    runner.invoke(app, ["worker", "mine-precedents", "--format", "json"])
    assert "alpha intent" in llm.prompts[0]


def _dry_run_json(*flags: str) -> tuple[dict[str, Any], int]:
    result = runner.invoke(
        app, ["worker", "mine-precedents", "--dry-run", "--format", "json", *flags]
    )
    return json.loads(result.output), result.exit_code


def test_a_dry_run_with_an_unreadable_store_is_partial_and_exits_zero_even_strict(
    cli_stores: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docs = get_document_store()
    _seed_cli({w: ["ev-x"] for w in ("alpha", "bravo", "charlie")})
    _stub_llm(monkeypatch, _CountingLLM(_MOCK_JSON))

    def down(_doc_id: str) -> Any:
        msg = "store down"
        raise RuntimeError(msg)

    monkeypatch.setattr(docs, "get", down)

    data, exit_code = _dry_run_json("--strict")

    assert data["dry_run"] is True
    assert data["status"] == "partial"
    assert data["skipped_egress_unreadable"] == 3
    assert exit_code == 0


def test_a_dry_run_that_asked_every_question_is_ok(
    cli_stores: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_cli({w: ["ev-x"] for w in ("alpha", "bravo", "charlie")})
    _stub_llm(monkeypatch, _CountingLLM(_MOCK_JSON))

    data, exit_code = _dry_run_json()

    assert data["status"] == "ok"
    assert data["skipped_egress_unreadable"] == 0
    assert exit_code == 0
