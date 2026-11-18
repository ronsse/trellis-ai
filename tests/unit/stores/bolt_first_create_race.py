"""Concurrent first creates of one absent node id, against a live Bolt server.

Shared by ``test_neo4j_graph.py`` and ``test_arcadedb_graph.py`` (#1232).
:data:`WRITERS` threads, each with its own store instance (its own driver),
are released together by a ``threading.Barrier`` and call
``create_node_if_absent`` on one absent id. Exactly one may return ``True``,
the others ``False`` without raising, and the id must end with one row, which
is current. Each check repeats :data:`ROUNDS` times with fresh ids, so one
lucky interleaving cannot pass it.

Against the pre-#1232 statement (a random ``version_id`` per call) this race
leaves two or more rows in most rounds on ``neo4j:2026.10.2``.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

WRITERS = 8
ROUNDS = 20

#: Per-thread join timeout: a hang fails the check instead of CI.
JOIN_TIMEOUT = 30.0


def _rows(store: Any, node_id: str) -> tuple[int, int]:
    """(all rows, current rows) the id has, read straight from the server."""
    record = store._run_read_single(
        "MATCH (n:Node {node_id: $node_id}) "
        "RETURN count(n) AS total, "
        "sum(CASE WHEN n.valid_to IS NULL THEN 1 ELSE 0 END) AS current",
        node_id=node_id,
    )
    return int(record["total"]), int(record["current"])


def _race(stores: list[Any], node_id: str) -> list[Any]:
    barrier = threading.Barrier(len(stores))
    results: list[Any] = [None] * len(stores)

    def _contend(index: int) -> None:
        try:
            barrier.wait(timeout=JOIN_TIMEOUT)
            results[index] = stores[index].create_node_if_absent(
                node_id, "service", {"by": index}
            )
        except Exception as exc:  # reported by the caller
            results[index] = exc

    threads = [threading.Thread(target=_contend, args=(i,)) for i in range(len(stores))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(JOIN_TIMEOUT)
        assert not thread.is_alive(), f"create_node_if_absent({node_id!r}) hung"
    return results


def _check_round(stores: list[Any], node_id: str) -> None:
    results = _race(stores, node_id)
    errors = [r for r in results if isinstance(r, Exception)]
    assert not errors, (node_id, errors)
    assert sorted(results) == [False] * (len(stores) - 1) + [True], (node_id, results)
    winner = results.index(True)
    assert _rows(stores[0], node_id) == (1, 1), node_id
    node = stores[0].get_node(node_id)
    assert node is not None
    assert node["properties"] == {"by": winner}


def check_concurrent_first_creates_write_one_row(
    make_store: Callable[[], Any], *, rounds: int = ROUNDS, writers: int = WRITERS
) -> None:
    """Each round: one creator wins, every other answers "already exists"."""
    stores = [make_store() for _ in range(writers)]
    try:
        for round_number in range(rounds):
            _check_round(stores, f"first-create-race-{round_number}")
    finally:
        for store in stores:
            store.close()


def check_concurrent_re_creates_after_a_purge_write_one_row(
    make_store: Callable[[], Any], *, rounds: int = 5, writers: int = WRITERS
) -> None:
    """A purged id derives the same first ``version_id`` again; still one row.

    The purge removes every row, so the count the derivation reads is zero
    again and nothing that survives the purge carries that ``version_id``.
    """
    stores = [make_store() for _ in range(writers)]
    try:
        node_id = "first-create-purged"
        for _ in range(rounds):
            _check_round(stores, node_id)
            assert stores[0].delete_node(node_id) is True
            assert _rows(stores[0], node_id) == (0, 0)
    finally:
        for store in stores:
            store.close()
