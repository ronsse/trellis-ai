"""Precedent mining asks the egress gate before it shows traces to a model."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from tests.unit.workers.learning.test_miner import (
    _MOCK_JSON,
    _CountingLLM,
    _make_trace,
)
from trellis.llm.egress import EgressGate
from trellis.schemas.enums import OutcomeStatus
from trellis.stores.sqlite.document import SQLiteDocumentStore
from trellis.stores.sqlite.trace import SQLiteTraceStore
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


def _miner(
    traces: SQLiteTraceStore, llm: _CountingLLM, gate: EgressGate | None
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


async def test_an_evidence_row_that_cannot_be_read_withholds_the_trace(
    traces: SQLiteTraceStore,
) -> None:
    broken = MagicMock()
    broken.get.side_effect = RuntimeError("store down")
    _seed(traces, {"alpha": ["ev-x"], "bravo": [], "charlie": []})
    llm = _CountingLLM(_MOCK_JSON)

    report = await _miner(traces, llm, EgressGate(broken, local=False)).mine(
        min_traces=2
    )

    assert report.egress_withheld == 1
    assert "alpha" not in llm.prompts[0]


async def test_a_declared_local_route_sends_a_labelled_trace_and_reads_nothing(
    traces: SQLiteTraceStore,
) -> None:
    untouched = MagicMock()
    _seed(traces, {"alpha": ["ev-secret"], "bravo": [], "charlie": []})
    gated, plain = _CountingLLM(_MOCK_JSON), _CountingLLM(_MOCK_JSON)

    with_gate = await _miner(traces, gated, EgressGate(untouched, local=True)).mine()
    without = await _miner(traces, plain, None).mine()

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
    without = await _miner(traces, plain, None).mine()

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
