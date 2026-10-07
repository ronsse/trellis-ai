"""Two real instances racing ``upsert_edge`` on one logical edge.

Shared by ``test_neo4j_graph.py`` and ``test_arcadedb_graph.py``. Unlike
``bolt_duplicate_current``, which builds a duplicate current row directly to
exercise the read side, these checks reproduce the write-side race itself:
two store instances (two drivers), two threads released by a
``threading.Barrier``, both calling ``upsert_edge`` for the same
``(source_id, target_id, edge_type)`` triplet at once. Before the fix in
``BoltOpenCypherGraphStore.upsert_edge`` (a write-lock self-assignment on the
source endpoint's shown row, taken before the existing-edge ``OPTIONAL
MATCH``), both writers could read "no current edge" and both ``CREATE`` one,
leaving two current rows for one logical edge — measured 20/20 and 30/30
reps on Neo4j (create and update shapes) against the pristine source before
this change landed; see the PR description for the full count. ArcadeDB's
own optimistic-concurrency retry happened to heal the same race even at
base (0/40 measured), so these checks matter most on Neo4j, but run on both
engines because the Cypher is shared.

Each check repeats a fixed number of times rather than once, so a single
lucky interleaving can't make the test pass by chance; every rep asserts
exactly one current row for the triplet.
"""

from __future__ import annotations

import threading
from typing import Any, Protocol

#: Reps per check. 20/20 and 30/30 reproduced the race at base in the
#: probe this module's checks are built from, so this count is already
#: well above what base needed to show the defect, while staying fast
#: enough for a live-infra CI leg running twice (per engine).
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

    def get_edges(
        self, node_id: str, direction: str = "both", edge_type: str | None = None
    ) -> list[dict[str, Any]]: ...

    def close(self) -> None: ...


def _race_once(
    store_a: _GraphStore,
    store_b: _GraphStore,
    source_id: str,
    target_id: str,
    edge_type: str,
) -> None:
    """Release both writers together and wait for both to finish."""
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def _call(store: _GraphStore, writer: str) -> None:
        barrier.wait(JOIN_TIMEOUT)
        try:
            store.upsert_edge(source_id, target_id, edge_type, {"writer": writer})
        except BaseException as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=_call, args=(store_a, "a")),
        threading.Thread(target=_call, args=(store_b, "b")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(JOIN_TIMEOUT)
    assert not any(thread.is_alive() for thread in threads), (
        "a writer hung (thread still alive)"
    )
    if errors:
        raise errors[0]


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
    times with a fresh triplet each rep.
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
    finally:
        second.close()
