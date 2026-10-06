"""A Bolt purge that cannot finish is a ``StoreError``, not a driver error.

``delete_node`` runs its purge as one managed transaction, so the neo4j
driver runs it again on an error it can retry, such as a ``TransientError``
or a lost connection, until ``max_transaction_retry_time`` runs out. When the
driver gives up, or the error is one it does not retry, the store raises
``StoreError`` naming the node and the error's type, never the server's text.
An exception that is not the driver's own is raised unchanged.
``MutationExecutor`` catches ``StoreError``, so the redaction is FAILED and
audited instead of escaping the executor as a driver error.

A connection lost while the commit is outstanding is ``IncompleteCommit``:
the purge may have committed, so the store reads the node's ``Node`` rows
back in a new session. No row left is the purge, which returns as it would
have without the error; a row left is a purge that failed. When that read
fails too, the outcome is unknown, and the message says so.

No database is needed: ``FakeBoltDriver`` scripts what each purge
transaction's ``DETACH DELETE`` of the ``Node`` rows does and what that read
finds, and ``TestWithTheRealDriver`` uses the real driver, pointed at a port
nothing listens on or already closed.
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

pytest.importorskip("neo4j")

import neo4j
from neo4j.exceptions import (
    ClientError,
    CypherSyntaxError,
    DatabaseError,
    DriverError,
    IncompleteCommit,
    ServiceUnavailable,
    SessionExpired,
    TransientError,
)

from tests.fake_bolt_driver import AtCommit, FakeBoltDriver, Outcome
from trellis.errors import StoreError
from trellis.mutate import build_curate_executor
from trellis.mutate.commands import Command, CommandResult, CommandStatus, Operation
from trellis.stores.base.event_log import EventLog, EventType
from trellis.stores.bolt_opencypher.graph import BoltOpenCypherGraphStore
from trellis.stores.registry import StoreRegistry

NODE = "purge-target"
# What a server message can carry: the statement, and the values it ran with.
SERVER_TEXT = "synthetic server text quoting a value"


def _store(
    *outcomes: Outcome, rows_left: int | Exception | None = None
) -> tuple[BoltOpenCypherGraphStore, FakeBoltDriver]:
    driver = FakeBoltDriver(*outcomes, rows_left=rows_left)
    store = BoltOpenCypherGraphStore(
        driver=driver,
        database="neo4j",
        owns_driver=False,
        init_schema=False,
    )
    return store, driver


def _ended(driver: FakeBoltDriver) -> list[str | None]:
    return [tx.ended for tx in driver.transactions]


class TestDeleteNodeErrors:
    @pytest.mark.parametrize(("rows", "deleted"), [(1, True), (0, False)])
    def test_a_transient_error_runs_the_purge_again(
        self, rows: int, deleted: bool
    ) -> None:
        store, driver = _store(TransientError(SERVER_TEXT), rows)

        assert store.delete_node(NODE) is deleted
        assert _ended(driver) == ["rollback", "commit"]

    @pytest.mark.parametrize(
        "error",
        [
            ClientError(SERVER_TEXT),
            CypherSyntaxError(SERVER_TEXT),
            DatabaseError(SERVER_TEXT),
            DriverError(SERVER_TEXT),
        ],
        ids=lambda error: type(error).__name__,
    )
    def test_an_error_the_driver_does_not_retry_is_a_store_error_at_once(
        self, error: Exception
    ) -> None:
        store, driver = _store(error, 1)

        with pytest.raises(StoreError) as caught:
            store.delete_node(NODE)

        assert caught.value.store == "graph"
        assert caught.value.__cause__ is error
        assert NODE in caught.value.message
        assert f"failed: {type(error).__name__}" in caught.value.message
        # The message reaches the append-only audit log: the error's type,
        # never the server's text.
        assert SERVER_TEXT not in caught.value.message
        assert _ended(driver) == ["rollback"]

    def test_an_error_from_outside_the_driver_is_raised_unchanged(self) -> None:
        bug = RuntimeError(SERVER_TEXT)
        store, driver = _store(bug)

        with pytest.raises(RuntimeError) as caught:
            store.delete_node(NODE)

        assert caught.value is bug
        assert _ended(driver) == ["rollback"]

    @pytest.mark.parametrize(
        "error_type", [TransientError, ServiceUnavailable, SessionExpired]
    )
    def test_an_error_on_every_attempt_is_a_store_error(
        self, error_type: type[Exception]
    ) -> None:
        errors = [error_type(SERVER_TEXT) for _ in range(3)]
        # One attempt more than the driver makes would remove the node.
        store, driver = _store(*errors, 1)

        with pytest.raises(StoreError) as caught:
            store.delete_node(NODE)

        assert caught.value.__cause__ is errors[-1]
        assert NODE in caught.value.message
        assert f"failed: {error_type.__name__}" in caught.value.message
        assert SERVER_TEXT not in caught.value.message
        assert _ended(driver) == ["rollback"] * 3

    @pytest.mark.parametrize(("rows", "deleted"), [(1, True), (0, False)])
    def test_a_lost_commit_with_no_row_left_is_the_purge(
        self, rows: int, deleted: bool
    ) -> None:
        # The purge committed, so it reports what it removed, as it would
        # have without the error.
        lost = IncompleteCommit(SERVER_TEXT)
        store, driver = _store(AtCommit(lost, rows=rows), rows_left=0)

        assert store.delete_node(NODE) is deleted
        assert driver.reads == [{"nid": NODE}]
        # The driver does not run a purge that may have committed again.
        assert _ended(driver) == ["commit raised"]

    @pytest.mark.parametrize("rows_left", [1, 2])
    def test_a_lost_commit_with_a_row_left_is_a_failed_purge(
        self, rows_left: int
    ) -> None:
        lost = IncompleteCommit(SERVER_TEXT)
        store, driver = _store(AtCommit(lost), rows_left=rows_left)

        with pytest.raises(StoreError) as caught:
            store.delete_node(NODE)

        assert caught.value.store == "graph"
        assert caught.value.__cause__ is lost
        assert caught.value.message == f"Purge of node {NODE} failed: IncompleteCommit"
        assert driver.reads == [{"nid": NODE}]
        assert _ended(driver) == ["commit raised"]

    @pytest.mark.parametrize(
        "error",
        [ServiceUnavailable(SERVER_TEXT), ClientError(SERVER_TEXT)],
        ids=lambda error: type(error).__name__,
    )
    def test_a_lost_commit_whose_read_fails_leaves_the_outcome_unknown(
        self, error: Exception
    ) -> None:
        lost = IncompleteCommit(SERVER_TEXT)
        store, driver = _store(AtCommit(lost), rows_left=error)

        with pytest.raises(StoreError) as caught:
            store.delete_node(NODE)

        assert caught.value.store == "graph"
        assert caught.value.__cause__ is lost
        assert caught.value.message == (
            f"Purge of node {NODE} lost its connection during the commit, "
            "so its outcome is unknown: IncompleteCommit"
        )
        assert driver.reads == [{"nid": NODE}]
        assert _ended(driver) == ["commit raised"]

    def test_an_error_from_outside_the_driver_in_that_read_is_raised_unchanged(
        self,
    ) -> None:
        bug = RuntimeError(SERVER_TEXT)
        store, driver = _store(AtCommit(IncompleteCommit(SERVER_TEXT)), rows_left=bug)

        with pytest.raises(RuntimeError) as caught:
            store.delete_node(NODE)

        assert caught.value is bug
        assert driver.reads == [{"nid": NODE}]


class TestWithTheRealDriver:
    def test_an_unreachable_server_is_a_store_error(self) -> None:
        # A bound socket that never listens refuses every connection. The
        # driver's retry window is zero, so its first error ends the purge.
        with socket.socket() as unheard:
            unheard.bind(("127.0.0.1", 0))
            port = unheard.getsockname()[1]
            driver = neo4j.GraphDatabase.driver(
                f"bolt://127.0.0.1:{port}",
                auth=("neo4j", "unused"),
                max_transaction_retry_time=0,
            )
            store = BoltOpenCypherGraphStore(
                driver=driver, database="neo4j", owns_driver=True, init_schema=False
            )
            try:
                with pytest.raises(StoreError) as caught:
                    store.delete_node(NODE)
            finally:
                store.close()

        assert isinstance(caught.value.__cause__, ServiceUnavailable)
        assert str(port) in str(caught.value.__cause__)
        assert "failed: ServiceUnavailable" in caught.value.message
        assert str(port) not in caught.value.message

    def test_a_closed_driver_is_a_store_error(self) -> None:
        # ``Driver.session()`` refuses a closed driver, before any transaction.
        driver = neo4j.GraphDatabase.driver(
            "bolt://127.0.0.1:1", auth=("neo4j", "unused")
        )
        driver.close()
        store = BoltOpenCypherGraphStore(
            driver=driver, database="neo4j", owns_driver=False, init_schema=False
        )

        with pytest.raises(StoreError) as caught:
            store.delete_node(NODE)

        assert isinstance(caught.value.__cause__, DriverError)
        assert "failed: DriverError" in caught.value.message


def _redact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bolt_store: BoltOpenCypherGraphStore,
) -> tuple[str, Command, CommandResult, EventLog]:
    """Redact a node of a SQLite registry through ``bolt_store``'s purge."""
    stores_dir = tmp_path / "stores"
    stores_dir.mkdir()
    registry = StoreRegistry(stores_dir=stores_dir)
    graph = registry.knowledge.graph_store
    node_id = graph.upsert_node(None, "person", {"name": "Purge Target"})
    monkeypatch.setattr(graph, "delete_node", bolt_store.delete_node)
    command = Command(
        operation=Operation.REDACTION_APPLY,
        args={"target_id": node_id, "reason": "synthetic purge"},
    )
    result = build_curate_executor(registry).execute(command)
    return node_id, command, result, registry.operational.event_log


class TestThroughTheExecutor:
    @pytest.mark.parametrize(
        "error",
        [ClientError(SERVER_TEXT), ServiceUnavailable(SERVER_TEXT)],
        ids=lambda error: type(error).__name__,
    )
    def test_a_purge_that_cannot_finish_fails_the_redaction_and_is_audited(
        self, error: Exception, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bolt_store, _driver = _store(error)

        node_id, command, result, event_log = _redact(tmp_path, monkeypatch, bolt_store)

        assert result.status == CommandStatus.FAILED
        assert node_id in result.message
        rejected = event_log.get_events(event_type=EventType.MUTATION_REJECTED)
        assert [(e.payload["command_id"], e.payload["status"]) for e in rejected] == [
            (command.command_id, "failed")
        ]
        assert type(error).__name__ in rejected[0].payload["message"]
        assert SERVER_TEXT not in rejected[0].payload["message"]
        assert event_log.get_events(event_type=EventType.REDACTION_APPLIED) == []

    def test_a_lost_commit_with_no_row_left_applies_the_redaction(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lost = IncompleteCommit(SERVER_TEXT)
        bolt_store, _driver = _store(AtCommit(lost), rows_left=0)

        node_id, command, result, event_log = _redact(tmp_path, monkeypatch, bolt_store)

        assert result.status == CommandStatus.SUCCESS
        applied = event_log.get_events(event_type=EventType.REDACTION_APPLIED)
        assert [(e.payload["target_id"], e.payload["command_id"]) for e in applied] == [
            (node_id, command.command_id)
        ]
        assert event_log.get_events(event_type=EventType.MUTATION_REJECTED) == []

    def test_a_lost_commit_with_a_row_left_fails_the_redaction(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lost = IncompleteCommit(SERVER_TEXT)
        bolt_store, _driver = _store(AtCommit(lost), rows_left=1)

        node_id, command, result, event_log = _redact(tmp_path, monkeypatch, bolt_store)

        assert result.status == CommandStatus.FAILED
        rejected = event_log.get_events(event_type=EventType.MUTATION_REJECTED)
        assert [(e.payload["command_id"], e.payload["status"]) for e in rejected] == [
            (command.command_id, "failed")
        ]
        assert (
            f"Purge of node {node_id} failed: IncompleteCommit"
            in rejected[0].payload["message"]
        )
        assert event_log.get_events(event_type=EventType.REDACTION_APPLIED) == []
