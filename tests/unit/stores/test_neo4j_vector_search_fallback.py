"""``Neo4jVectorStore.query`` falls back to the procedure when SEARCH is refused.

Backend-free: the store gets a mock driver (as in
``test_neo4j_vector_query_cypher.py``) and its ``_run_read_list`` is replaced
by a recorder that raises the error shapes a live ``neo4j:2025.12`` returns.
Those shapes were read off that image: the statement as sent fails with
``42001`` whose GQL cause chain is ``42I06`` (Invalid input 'SEARCH') then
``42I67`` (valid CYPHER 25, run in CYPHER 5); with a ``CYPHER 25`` prefix it
fails with ``51N26`` (SEARCH not available). The live suites in
``test_neo4j_vector.py`` and the vector contract exercise the real thing.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from structlog.testing import capture_logs

pytest.importorskip("neo4j")

from trellis.stores.neo4j.vector import (
    Neo4jVectorStore,
    search_refusal_status,
)

_DIMS = 3


class _GqlError(Exception):
    """Stands in for a driver error: a ``gql_status`` and a cause chain."""

    def __init__(self, gql_status: str, cause: BaseException | None = None) -> None:
        super().__init__(gql_status)
        self.gql_status = gql_status
        self.__cause__ = cause


def _parse_time_refusal() -> _GqlError:
    return _GqlError("42001", _GqlError("42I06", _GqlError("42I67")))


def _not_available_refusal() -> _GqlError:
    return _GqlError("51N26")


_ROWS = [
    {"item_id": "a", "score": 0.9, "metadata_json": '{"kind": "x"}'},
    {"item_id": "b", "score": 0.5, "metadata_json": '{"kind": "y"}'},
    {"item_id": "c", "score": 0.4, "metadata_json": '{"kind": "x"}'},
]


class _Recorder:
    """Records each statement; SEARCH raises ``search_error`` if one is set."""

    def __init__(self, search_error: BaseException | None) -> None:
        self.search_error = search_error
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, cypher: str, **params: Any) -> list[dict[str, Any]]:
        self.calls.append((cypher, params))
        if "SEARCH" in cypher and self.search_error is not None:
            raise self.search_error
        return list(_ROWS)

    def statements(self) -> list[str]:
        return [
            "search" if "SEARCH" in c else "procedure" if "queryNodes" in c else c
            for c, _ in self.calls
        ]


def _store(
    monkeypatch: pytest.MonkeyPatch, search_error: BaseException | None
) -> tuple[Neo4jVectorStore, _Recorder]:
    monkeypatch.setattr(
        "trellis.stores.neo4j.vector.wait_for_vector_index_online",
        lambda *_, **__: None,
    )
    store = Neo4jVectorStore(
        "bolt://x", user="u", driver=MagicMock(name="driver"), dimensions=_DIMS
    )
    recorder = _Recorder(search_error)
    monkeypatch.setattr(store, "_run_read_list", recorder)
    return store, recorder


_REFUSALS = pytest.mark.parametrize(
    ("make_error", "code"),
    [(_parse_time_refusal, "42I67"), (_not_available_refusal, "51N26")],
    ids=["parse_time_42I67", "not_available_51N26"],
)


@_REFUSALS
def test_refusal_falls_back_to_procedure_with_same_result(
    monkeypatch: pytest.MonkeyPatch, make_error: Any, code: str
) -> None:
    baseline, _ = _store(monkeypatch, None)
    expected = baseline.query([1.0, 0.0, 0.0], top_k=2, filters={"kind": "x"})

    store, rec = _store(monkeypatch, make_error())
    got = store.query([1.0, 0.0, 0.0], top_k=2, filters={"kind": "x"})

    assert rec.statements() == ["search", "procedure"]
    assert got == expected
    assert [r["item_id"] for r in got] == ["a", "c"]
    search_params = rec.calls[0][1]
    proc_cypher, proc_params = rec.calls[1]
    # Same k and vector as SEARCH; the procedure takes the index by parameter.
    assert proc_params["k"] == search_params["k"]
    assert proc_params["vector"] == search_params["vector"]
    assert proc_params["index"] == "trellis_node_embeddings"
    assert "WHERE node.valid_to IS NULL" in proc_cypher


@_REFUSALS
def test_second_query_sends_no_search_and_warns_once(
    monkeypatch: pytest.MonkeyPatch, make_error: Any, code: str
) -> None:
    store, rec = _store(monkeypatch, make_error())
    with capture_logs() as logs:
        store.query([1.0, 0.0, 0.0], top_k=1)
        store.query([0.0, 1.0, 0.0], top_k=1)
        store.query([0.0, 0.0, 1.0], top_k=1)

    assert rec.statements() == ["search", "procedure", "procedure", "procedure"]
    warnings = [
        e for e in logs if e["event"] == "neo4j_vector_search_refused_using_procedure"
    ]
    assert len(warnings) == 1
    assert warnings[0]["log_level"] == "warning"
    assert warnings[0]["reason_code"] == code


def test_unrelated_error_propagates_and_does_not_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An ordinary syntax error on the SEARCH statement, without the
    # 42I67 that marks the keyword itself as refused. It must surface
    # unchanged, and the instance must keep trying SEARCH.
    unrelated = _GqlError("42001", _GqlError("42I06"))
    store, rec = _store(monkeypatch, unrelated)
    with capture_logs() as logs, pytest.raises(_GqlError) as raised:
        store.query([1.0, 0.0, 0.0], top_k=1)
    assert raised.value is unrelated

    rec.search_error = None
    store.query([1.0, 0.0, 0.0], top_k=1)
    assert rec.statements() == ["search", "search"]
    assert not [e for e in logs if e["log_level"] == "warning"]


def test_plain_exception_is_not_a_refusal() -> None:
    assert search_refusal_status(RuntimeError("SEARCH 51N26 42I67")) is None
    assert search_refusal_status(_parse_time_refusal()) == "42I67"
    assert search_refusal_status(_not_available_refusal()) == "51N26"
