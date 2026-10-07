"""Two real store instances racing writes on one logical edge.

Shared by ``test_neo4j_graph.py`` and ``test_arcadedb_graph.py``. Unlike
``bolt_duplicate_current``, which builds a duplicate current row directly to
exercise the read side, these checks reproduce write-side races: two store
instances (two drivers) in two threads released together by a
``threading.Barrier``. Each check repeats :data:`REPS` times with fresh ids,
so one lucky interleaving cannot pass it.
"""

from __future__ import annotations

import functools
import threading
from collections.abc import Callable
from typing import Any, Protocol

#: Reps per check: enough that a race reproducing in about half of the reps
#: cannot pass by luck, while staying fast on the live-infra legs.
REPS = 10

#: Per-thread join timeout. A hang (not an exception) would mean the lock
#: itself deadlocked -- generous enough to rule out slow CI, tight enough
#: that a genuine deadlock still fails the test instead of hanging CI.
JOIN_TIMEOUT = 20.0


class _GraphStore(Protocol):
    def upsert_node(
        self, node_id: str, node_type: str, properties: dict[str, Any]
    ) -> str: ...

    def upsert_edge(
        self,
        source_id: str,
        target_id: str,
        edge_type: str,
        properties: dict[str, Any] | None = None,
    ) -> str: ...

    def upsert_edges_bulk(self, edges: list[dict[str, Any]]) -> list[str]: ...

    def get_edges(
        self, node_id: str, direction: str = "both", edge_type: str | None = None
    ) -> list[dict[str, Any]]: ...

    def get_node_history(self, node_id: str) -> list[dict[str, Any]]: ...

    def close(self) -> None: ...


def _race(first: Callable[[], object], second: Callable[[], object]) -> None:
    """Release both writes together and wait for both to finish."""
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def _call(write: Callable[[], object]) -> None:
        barrier.wait(JOIN_TIMEOUT)
        try:
            write()
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=_call, args=(w,)) for w in (first, second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(JOIN_TIMEOUT)
    assert not any(thread.is_alive() for thread in threads), (
        "a writer hung (thread still alive)"
    )
    if errors:
        raise errors[0]


def _race_once(
    store_a: _GraphStore,
    store_b: _GraphStore,
    source_id: str,
    target_id: str,
    edge_type: str,
) -> None:
    """Race two ``upsert_edge`` calls on one triplet, as writers ``a`` and ``b``."""
    _race(
        functools.partial(
            store_a.upsert_edge, source_id, target_id, edge_type, {"writer": "a"}
        ),
        functools.partial(
            store_b.upsert_edge, source_id, target_id, edge_type, {"writer": "b"}
        ),
    )


def check_concurrent_edge_create_leaves_one_current_row(
    store: _GraphStore, make_second_store: Any
) -> None:
    """Two instances creating the same new edge leave exactly one current row.

    Repeated :data:`REPS` times with a fresh triplet each rep, each against
    one logical edge that does not exist yet (the create-race shape).
    """
    second = make_second_store()
    try:
        for i in range(REPS):
            source_id = f"race-create-src-{i}"
            target_id = f"race-create-tgt-{i}"
            store.upsert_node(source_id, "race-node", {})
            store.upsert_node(target_id, "race-node", {})

            _race_once(store, second, source_id, target_id, "race-rel")

            current = store.get_edges(
                source_id, direction="outgoing", edge_type="race-rel"
            )
            assert len(current) == 1, (i, [row["edge_id"] for row in current])
    finally:
        second.close()


def check_concurrent_edge_update_leaves_one_current_row(
    store: _GraphStore, make_second_store: Any
) -> None:
    """Two instances updating the same existing edge leave exactly one current row.

    Seeds the edge first (the update-race shape: both writers close the
    same ``old`` row and each create a replacement), then repeats :data:`REPS`
    times with a fresh triplet each rep. The survivor must be one of the
    racers' rows, not the seed.
    """
    second = make_second_store()
    try:
        for i in range(REPS):
            source_id = f"race-update-src-{i}"
            target_id = f"race-update-tgt-{i}"
            store.upsert_node(source_id, "race-node", {})
            store.upsert_node(target_id, "race-node", {})
            store.upsert_edge(source_id, target_id, "race-rel", {"writer": "seed"})

            _race_once(store, second, source_id, target_id, "race-rel")

            current = store.get_edges(
                source_id, direction="outgoing", edge_type="race-rel"
            )
            assert len(current) == 1, (i, [row["edge_id"] for row in current])
            assert current[0]["properties"]["writer"] in {"a", "b"}, (i, current)
    finally:
        second.close()


def check_edge_write_racing_node_upsert_keeps_one_current_node(
    store: _GraphStore, make_second_store: Any
) -> None:
    """An edge write racing ``upsert_node`` on its source keeps one current node row.

    ``upsert_node`` closes the source's current row and adds a new one. The
    edge writer's lock on that row must not write the pre-close ``valid_to``
    back and reopen it. Repeated :data:`REPS` times with fresh ids.
    """
    second = make_second_store()
    try:
        for i in range(REPS):
            source_id = f"race-node-src-{i}"
            target_id = f"race-node-tgt-{i}"
            store.upsert_node(source_id, "race-node", {})
            store.upsert_node(target_id, "race-node", {})

            _race(
                functools.partial(
                    store.upsert_edge, source_id, target_id, "race-rel", {}
                ),
                functools.partial(
                    second.upsert_node, source_id, "race-node", {"rep": i}
                ),
            )

            current = [
                row
                for row in store.get_node_history(source_id)
                if row["valid_to"] is None
            ]
            assert len(current) == 1, (i, len(current))
    finally:
        second.close()


def check_concurrent_bulk_edge_create_leaves_one_current_row(
    store: _GraphStore, make_second_store: Any
) -> None:
    """Two instances calling ``upsert_edges_bulk`` for the same new edge
    leave exactly one current row.

    The bulk-vs-bulk shape of the create race: both writers' batches carry
    one row apiece, for the same triplet. Repeated :data:`REPS` times with a
    fresh triplet each rep, mirroring
    :func:`check_concurrent_edge_create_leaves_one_current_row` at the bulk
    entry point.
    """
    second = make_second_store()
    try:
        for i in range(REPS):
            source_id = f"race-bulk-create-src-{i}"
            target_id = f"race-bulk-create-tgt-{i}"
            store.upsert_node(source_id, "race-node", {})
            store.upsert_node(target_id, "race-node", {})

            _race(
                functools.partial(
                    store.upsert_edges_bulk,
                    [
                        {
                            "source_id": source_id,
                            "target_id": target_id,
                            "edge_type": "race-rel",
                            "properties": {"writer": "a"},
                        }
                    ],
                ),
                functools.partial(
                    second.upsert_edges_bulk,
                    [
                        {
                            "source_id": source_id,
                            "target_id": target_id,
                            "edge_type": "race-rel",
                            "properties": {"writer": "b"},
                        }
                    ],
                ),
            )

            current = store.get_edges(
                source_id, direction="outgoing", edge_type="race-rel"
            )
            assert len(current) == 1, (i, [row["edge_id"] for row in current])
    finally:
        second.close()


def check_bulk_edge_create_racing_single_edge_write_leaves_one_current_row(
    store: _GraphStore, make_second_store: Any
) -> None:
    """``upsert_edges_bulk`` racing single-row ``upsert_edge`` on the same
    triplet leaves exactly one current row.

    The mixed shape #782 follow-up 2 named: one writer uses the bulk entry
    point, the other the single-row one, both for the same new edge.
    Repeated :data:`REPS` times with a fresh triplet each rep.
    """
    second = make_second_store()
    try:
        for i in range(REPS):
            source_id = f"race-bulk-single-src-{i}"
            target_id = f"race-bulk-single-tgt-{i}"
            store.upsert_node(source_id, "race-node", {})
            store.upsert_node(target_id, "race-node", {})

            _race(
                functools.partial(
                    store.upsert_edges_bulk,
                    [
                        {
                            "source_id": source_id,
                            "target_id": target_id,
                            "edge_type": "race-rel",
                            "properties": {"writer": "a"},
                        }
                    ],
                ),
                functools.partial(
                    second.upsert_edge,
                    source_id,
                    target_id,
                    "race-rel",
                    {"writer": "b"},
                ),
            )

            current = store.get_edges(
                source_id, direction="outgoing", edge_type="race-rel"
            )
            assert len(current) == 1, (i, [row["edge_id"] for row in current])
    finally:
        second.close()


def check_bulk_edge_writes_opposite_row_order_do_not_deadlock(
    store: _GraphStore, make_second_store: Any
) -> None:
    """Two ``upsert_edges_bulk`` calls carrying the same two triplets in
    opposite row order neither deadlock nor leave a duplicate current row.

    Each writer's batch touches the same 4 endpoint nodes but orders its
    two rows oppositely (writer one: triplet 1 then triplet 2; writer two:
    triplet 2 then triplet 1), exercising the sorted lock order's deadlock
    avoidance at batch granularity -- the shape the single-row ``x->y`` vs
    ``y->x`` race exercises per edge. Repeated :data:`REPS` times with
    fresh ids each rep.
    """
    second = make_second_store()
    try:
        for i in range(REPS):
            s1, t1 = f"race-order-s1-{i}", f"race-order-t1-{i}"
            s2, t2 = f"race-order-s2-{i}", f"race-order-t2-{i}"
            for node_id in (s1, t1, s2, t2):
                store.upsert_node(node_id, "race-node", {})
            row1 = {
                "source_id": s1,
                "target_id": t1,
                "edge_type": "race-rel",
                "properties": {"writer": "a"},
            }
            row2 = {
                "source_id": s2,
                "target_id": t2,
                "edge_type": "race-rel",
                "properties": {"writer": "b"},
            }

            _race(
                functools.partial(store.upsert_edges_bulk, [row1, row2]),
                functools.partial(second.upsert_edges_bulk, [row2, row1]),
            )

            current_1 = store.get_edges(s1, direction="outgoing", edge_type="race-rel")
            current_2 = store.get_edges(s2, direction="outgoing", edge_type="race-rel")
            assert len(current_1) == 1, (i, [row["edge_id"] for row in current_1])
            assert len(current_2) == 1, (i, [row["edge_id"] for row in current_2])
    finally:
        second.close()
