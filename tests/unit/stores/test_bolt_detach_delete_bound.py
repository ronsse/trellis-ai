"""The Bolt purge's repeat-until-none delete ends whatever the server answers.

``_detach_delete_until_none`` repeats a ``DETACH DELETE`` until it removes
nothing, so that a version written by a transaction it waited on is removed
too. On ``neo4j:2026.10.2``, after ``delete_node``'s lock statement, a
``MATCH`` on the indexed ``node_id`` keeps returning rows this transaction
already deleted, so ``RETURN count(n)`` never reaches 0 (#1272). The loop
reads the summary's ``nodes_deleted`` instead, and a hard bound on the
rounds turns any server that keeps reporting removals into a
``StoreError`` naming the node rather than a call that never returns.

No database is needed. The scripted transactions below pin the normal path
(rounds until the counter reads 0, returning the total) and that the count
is the summary counter rather than the returned row; the always-one fakes
report one removed row on every statement, in both the row and the counter.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("neo4j")

from tests.fake_bolt_driver import FakeBoltDriver, FakeResult, FakeTransaction
from trellis.errors import StoreError
from trellis.stores.bolt_opencypher import graph as bolt_graph
from trellis.stores.bolt_opencypher.graph import BoltOpenCypherGraphStore

NODE = "never-gone"


# A loop with no bound would run forever; the fakes fail it here instead, so
# a defective subject fails this file rather than hanging the suite.
_SPIN_LIMIT = 10_000


class _AlwaysOneResult:
    def single(self, strict: bool = False) -> dict[str, int]:
        return {"deleted": 1}

    def consume(self) -> Any:
        return SimpleNamespace(counters=SimpleNamespace(nodes_deleted=1))


class _AlwaysOneTx:
    """Every statement reports one removed row, forever."""

    def __init__(self) -> None:
        self.runs = 0

    def run(self, cypher: str, **_params: Any) -> _AlwaysOneResult:
        self.runs += 1
        if self.runs > _SPIN_LIMIT:
            msg = "the delete loop did not stop"
            raise AssertionError(msg)
        return _AlwaysOneResult()


def test_a_delete_that_always_reports_a_row_raises_instead_of_spinning() -> None:
    tx = _AlwaysOneTx()

    with pytest.raises(StoreError) as caught:
        bolt_graph._detach_delete_until_none(
            tx,  # type: ignore[arg-type]
            "MATCH (n:Node {node_id: $nid}) DETACH DELETE n",
            NODE,
        )

    assert NODE in caught.value.message
    assert caught.value.store == "graph"
    assert tx.runs == bolt_graph._DETACH_DELETE_MAX_ROUNDS


class _ScriptedTx:
    """Answer each statement with the next scripted result, then fail."""

    def __init__(self, *results: FakeResult) -> None:
        self._results = list(results)
        self.runs = 0

    def run(self, cypher: str, **_params: Any) -> FakeResult:
        self.runs += 1
        if not self._results:
            msg = "the delete loop ran past its scripted rounds"
            raise AssertionError(msg)
        return self._results.pop(0)


_CYPHER = "MATCH (n:Node {node_id: $nid}) DETACH DELETE n"


def test_a_delete_runs_until_the_counter_reads_zero_and_returns_the_total() -> None:
    tx = _ScriptedTx(FakeResult({"deleted": 2}), FakeResult({"deleted": 0}))

    removed = bolt_graph._detach_delete_until_none(
        tx,  # type: ignore[arg-type]
        _CYPHER,
        NODE,
    )

    assert removed == 2
    assert tx.runs == 2


def test_the_count_is_the_summary_counter_not_the_returned_row() -> None:
    # The returned row disagrees with the counter on every round, as a
    # ``RETURN count(n)`` did on ``neo4j:2026.10.2`` (#1272). Reading the row
    # would return 0 at once; reading the counter removes 3 + 2.
    tx = _ScriptedTx(
        FakeResult({"deleted": 0}, nodes_deleted=3),
        FakeResult({"deleted": 0}, nodes_deleted=2),
        FakeResult({"deleted": 0}, nodes_deleted=0),
    )

    removed = bolt_graph._detach_delete_until_none(
        tx,  # type: ignore[arg-type]
        _CYPHER,
        NODE,
    )

    assert removed == 5
    assert tx.runs == 3


class _AlwaysOneFakeTransaction(FakeTransaction):
    def __init__(self, outcome: Any, driver: Any = None) -> None:
        super().__init__(outcome, driver=driver)
        self._deletes = _AlwaysOneTx()

    def run(self, cypher: str, **params: Any) -> Any:
        if cypher.startswith("MATCH (n:Node") and "DETACH DELETE" in cypher:
            return self._deletes.run(cypher, **params)
        return super().run(cypher, **params)


def test_delete_node_raises_a_store_error_and_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "tests.fake_bolt_driver.FakeTransaction", _AlwaysOneFakeTransaction
    )
    driver = FakeBoltDriver(1)
    store = BoltOpenCypherGraphStore(
        driver=driver, database="neo4j", owns_driver=False, init_schema=False
    )

    with pytest.raises(StoreError) as caught:
        store.delete_node(NODE)

    assert NODE in caught.value.message
    # Not a driver error, so the managed transaction is not retried.
    assert [tx.ended for tx in driver.transactions] == ["rollback"]
