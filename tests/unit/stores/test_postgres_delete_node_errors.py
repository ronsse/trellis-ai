"""A Postgres purge that cannot finish is a ``StoreError``, not a psycopg error.

PostgreSQL aborts a ``delete_node`` transaction as a deadlock victim when it
and another purge, or a writer, lock the same rows in opposite orders. The
abort rolls the purge back whole, so the store runs it again, at most
``_PURGE_ATTEMPTS`` times in all, and raises ``StoreError`` naming the node
when no attempt finishes. Any other database error is a ``StoreError`` at
once. ``MutationExecutor`` catches ``StoreError``, so the redaction is
FAILED and audited instead of escaping the executor as a psycopg error.

No database is needed: ``FakePool`` scripts what each purge transaction's
first ``DELETE FROM nodes`` does. The live reproducer is
``tests/unit/stores/test_postgres_stores.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("psycopg_pool")

import psycopg

from tests.fake_pg_pool import FakePool
from trellis.errors import StoreError
from trellis.mutate import build_curate_executor
from trellis.mutate.commands import Command, CommandStatus, Operation
from trellis.stores.base.event_log import EventType
from trellis.stores.postgres import graph as pg_graph
from trellis.stores.registry import StoreRegistry

NODE = "purge-target"


def _store(
    *outcomes: BaseException | int,
) -> tuple[pg_graph.PostgresGraphStore, FakePool]:
    pool = FakePool(*outcomes)
    return pg_graph.PostgresGraphStore("postgresql://unused", pool=pool), pool


def _deadlock() -> BaseException:
    return psycopg.errors.DeadlockDetected("deadlock detected")


def _ended(pool: FakePool) -> list[str | None]:
    return [purge.ended for purge in pool.purges()]


class TestDeleteNodeRetry:
    def test_a_deadlock_victim_runs_again_and_reports_the_purge(self) -> None:
        store, pool = _store(_deadlock(), 1)

        assert store.delete_node(NODE) is True
        assert _ended(pool) == ["rollback", "commit"]

    def test_a_serialization_failure_runs_again(self) -> None:
        # The second attempt finds nothing left: a concurrent purge won.
        store, pool = _store(
            psycopg.errors.SerializationFailure("could not serialize access"), 0
        )

        assert store.delete_node(NODE) is False
        assert _ended(pool) == ["rollback", "commit"]

    def test_the_last_attempt_still_counts(self) -> None:
        aborted = [_deadlock() for _ in range(pg_graph._PURGE_ATTEMPTS - 1)]
        store, pool = _store(*aborted, 2)

        assert store.delete_node(NODE) is True
        assert len(pool.purges()) == pg_graph._PURGE_ATTEMPTS

    def test_a_purge_that_loses_every_attempt_is_a_store_error(self) -> None:
        aborted = [_deadlock() for _ in range(pg_graph._PURGE_ATTEMPTS)]
        # One attempt more than the store makes would remove the node.
        store, pool = _store(*aborted, 1)

        with pytest.raises(StoreError) as caught:
            store.delete_node(NODE)

        assert caught.value.store == "graph"
        assert NODE in caught.value.message
        assert "DeadlockDetected" in caught.value.message
        assert caught.value.__cause__ is aborted[-1]
        assert _ended(pool) == ["rollback"] * pg_graph._PURGE_ATTEMPTS

    def test_any_other_database_error_is_a_store_error_at_once(self) -> None:
        # A lost connection leaves the commit unknown, so the purge is not
        # run again: a second run would report a finished purge as False.
        lost = psycopg.OperationalError("server closed the connection unexpectedly")
        store, pool = _store(lost, 1)

        with pytest.raises(StoreError) as caught:
            store.delete_node(NODE)

        assert caught.value.__cause__ is lost
        assert NODE in caught.value.message
        assert "OperationalError" in caught.value.message
        # The message reaches the append-only audit log: the error's type,
        # never the server's text.
        assert "server closed" not in caught.value.message
        assert _ended(pool) == ["rollback"]


class TestThroughTheExecutor:
    def test_a_purge_that_cannot_finish_fails_the_redaction_and_is_audited(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stores_dir = tmp_path / "stores"
        stores_dir.mkdir()
        registry = StoreRegistry(stores_dir=stores_dir)
        graph = registry.knowledge.graph_store
        node_id = graph.upsert_node(None, "person", {"name": "Purge Target"})
        # Every purge deadlocks, however many times the store runs it.
        pg_store, _pool = _store(_deadlock())
        monkeypatch.setattr(graph, "delete_node", pg_store.delete_node)
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
        assert "DeadlockDetected" in rejected[0].payload["message"]
        assert event_log.get_events(event_type=EventType.REDACTION_APPLIED) == []
