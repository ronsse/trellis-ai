"""A Bolt purge that cannot finish is a ``StoreError``, not a driver error.

``delete_node`` runs its purge as one managed transaction, so the neo4j
driver runs it again on an error it can retry, such as a ``TransientError``
or a lost connection, until ``max_transaction_retry_time`` runs out. When the
driver gives up, or the error is one it does not retry, the store raises
``StoreError`` naming the node and the error's type, never the server's text.
``MutationExecutor`` catches ``StoreError``, so the redaction is FAILED and
audited instead of escaping the executor as a driver error.

A connection lost while the commit is outstanding is ``IncompleteCommit``:
the purge may have committed, so the message says its outcome is unknown.

No database is needed: ``FakeBoltDriver`` scripts what each purge
transaction's ``DETACH DELETE`` of the ``Node`` rows does, and
``TestWithTheRealDriver`` points the real driver at a port nothing listens on.
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
from trellis.mutate.commands import Command, CommandStatus, Operation
from trellis.stores.base.event_log import EventType
from trellis.stores.bolt_opencypher.graph import BoltOpenCypherGraphStore
from trellis.stores.registry import StoreRegistry

NODE = "purge-target"
# What a server message can carry: the statement, and the values it ran with.
SERVER_TEXT = "synthetic server text quoting a value"


def _store(*outcomes: Outcome) -> tuple[BoltOpenCypherGraphStore, FakeBoltDriver]:
    driver = FakeBoltDriver(*outcomes)
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

    def test_a_connection_lost_during_the_commit_leaves_the_outcome_unknown(
        self,
    ) -> None:
        lost = IncompleteCommit(SERVER_TEXT)
        store, driver = _store(AtCommit(lost), 1)

        with pytest.raises(StoreError) as caught:
            store.delete_node(NODE)

        assert caught.value.__cause__ is lost
        assert NODE in caught.value.message
        assert "outcome is unknown: IncompleteCommit" in caught.value.message
        assert SERVER_TEXT not in caught.value.message
        # The driver does not run a purge that may have committed again.
        assert _ended(driver) == ["commit raised"]


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


class TestThroughTheExecutor:
    @pytest.mark.parametrize(
        "error",
        [ClientError(SERVER_TEXT), ServiceUnavailable(SERVER_TEXT)],
        ids=lambda error: type(error).__name__,
    )
    def test_a_purge_that_cannot_finish_fails_the_redaction_and_is_audited(
        self, error: Exception, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stores_dir = tmp_path / "stores"
        stores_dir.mkdir()
        registry = StoreRegistry(stores_dir=stores_dir)
        graph = registry.knowledge.graph_store
        node_id = graph.upsert_node(None, "person", {"name": "Purge Target"})
        bolt_store, _driver = _store(error)
        monkeypatch.setattr(graph, "delete_node", bolt_store.delete_node)
        command = Command(
            operation=Operation.REDACTION_APPLY,
            args={"target_id": node_id, "reason": "synthetic purge failure"},
        )

        result = build_curate_executor(registry).execute(command)

        assert result.status == CommandStatus.FAILED
        assert node_id in result.message
        event_log = registry.operational.event_log
        rejected = event_log.get_events(event_type=EventType.MUTATION_REJECTED)
        assert [(e.payload["command_id"], e.payload["status"]) for e in rejected] == [
            (command.command_id, "failed")
        ]
        assert type(error).__name__ in rejected[0].payload["message"]
        assert SERVER_TEXT not in rejected[0].payload["message"]
        assert event_log.get_events(event_type=EventType.REDACTION_APPLIED) == []
