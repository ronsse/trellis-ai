"""A failed SQLite store write rolls its transaction back.

Each write below runs its statements in one transaction and commits it. A
statement that fails after the transaction has taken the database's write
lock must roll the transaction back. Left open, it would keep the lock, so
every other connection's write would wait out its busy timeout and fail
with ``database is locked``, and the store's next commit would write what
the failed call had already done: the close of a current version without
its replacement, a batch's earlier rows, a document without its full-text
row.

A ``BEFORE`` trigger that raises on one synthetic id forces each failure.
``documents_fts`` is an FTS5 table, which takes no trigger, so the document
sites deny one statement on it through the store connection's authorizer.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tests.unit.stores.sqlite_write_lock import committed_rows, write_at_once
from trellis.schemas.parameters import ParameterProposal, ParameterScope
from trellis.stores.base.api_key import ApiKeyRecord
from trellis.stores.sqlite.api_key import SQLiteApiKeyStore
from trellis.stores.sqlite.document import SQLiteDocumentStore
from trellis.stores.sqlite.graph import SQLiteGraphStore
from trellis.stores.sqlite.tuner_state import SQLiteTunerStateStore
from trellis.stores.sqlite.vector import SQLiteVectorStore

Rows = list[tuple[Any, ...]]

#: The error every forcing trigger raises.
FORCED = r"^syn forced failure$"

NODES = (
    "SELECT node_id, properties_json, valid_to IS NULL FROM nodes"
    " ORDER BY node_id, valid_to IS NULL"
)
EDGES = (
    "SELECT source_id, target_id, properties_json, valid_to IS NULL FROM edges"
    " ORDER BY source_id, target_id, valid_to IS NULL"
)
ALIASES = (
    "SELECT raw_id, entity_id, valid_to IS NULL FROM entity_aliases"
    " ORDER BY raw_id, valid_to IS NULL"
)
GRAPH = (
    "SELECT 'alias', raw_id FROM entity_aliases"
    " UNION ALL SELECT 'edge', source_id || '>' || target_id FROM edges"
    " UNION ALL SELECT 'node', node_id FROM nodes ORDER BY 1, 2"
)
DOCUMENTS = (
    "SELECT 'doc', doc_id, content FROM documents"
    " UNION ALL SELECT 'fts', doc_id, content FROM documents_fts ORDER BY 1, 2"
)
VECTORS = "SELECT item_id FROM vectors ORDER BY item_id"
PROPOSALS = "SELECT proposal_id, status FROM proposals ORDER BY proposal_id"
CURSORS = "SELECT tuner, cursor FROM tuner_cursors ORDER BY tuner"
KEYS = "SELECT key_id, revoked_at IS NOT NULL FROM trellis_api_keys ORDER BY key_id"

SEEDED_GRAPH: Rows = [
    ("edge", "syn-a>syn-b"),
    ("node", "syn-a"),
    ("node", "syn-b"),
    ("node", "syn-c"),
]
SEEDED_DOCUMENTS: Rows = [
    ("doc", "syn-d1", "syn v1"),
    ("doc", "syn-d2", "syn v2"),
    ("fts", "syn-d1", "syn v1"),
    ("fts", "syn-d2", "syn v2"),
]


def _ddl(db_path: Path, *statements: str) -> None:
    """Run *statements* at a connection of their own and commit them."""
    conn = sqlite3.connect(db_path)
    try:
        for statement in statements:
            conn.execute(statement)
        conn.commit()
    finally:
        conn.close()


def _trigger(event: str, table: str, when: str) -> Callable[[Any, Path], None]:
    """Make *event* on *table* raise wherever *when* holds."""

    def force(store: Any, db_path: Path) -> None:
        _ddl(
            db_path,
            f"CREATE TRIGGER syn_fail BEFORE {event} ON {table} WHEN {when}"
            " BEGIN SELECT RAISE(ABORT, 'syn forced failure'); END",
        )

    return force


def _deny(action: int, table: str) -> Callable[[Any, Path], None]:
    """Make the store's own connection refuse *action* on *table*."""

    def force(store: Any, db_path: Path) -> None:
        def authorizer(code: int, arg1: str | None, *_: object) -> int:
            if code == action and arg1 == table:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        store._conn.set_authorizer(authorizer)

    return force


def _seed_edge(store: SQLiteGraphStore) -> None:
    for node_id in ("syn-a", "syn-b", "syn-c"):
        store.upsert_node(node_id, "syn-type", {})
    store.upsert_edge("syn-a", "syn-b", "syn-rel", {"v": 1})


def _seed_edge_and_alias(store: SQLiteGraphStore) -> None:
    _seed_edge(store)
    store.upsert_alias("syn-a", "syn-sys", "syn-raw1")


def _delete_the_edge_from_syn_a(store: SQLiteGraphStore) -> bool:
    edge = store.get_edges("syn-a", direction="outgoing")[0]
    return store.delete_edge(edge["edge_id"])


def _seed_documents(store: SQLiteDocumentStore) -> None:
    store.put("syn-d1", "syn v1")
    store.put("syn-d2", "syn v2")


def _seed_vectors(store: SQLiteVectorStore) -> None:
    store.upsert("syn-v1", [1.0, 0.0])
    store.upsert("syn-v2", [0.0, 1.0])


def _proposal(proposal_id: str) -> ParameterProposal:
    return ParameterProposal(
        proposal_id=proposal_id,
        scope=ParameterScope(component_id="syn-component"),
        tuner="syn-tuner",
        proposed_values={"k": 1},
    )


def _seed_tuner_state(store: SQLiteTunerStateStore) -> None:
    store.put_proposal(_proposal("syn-p1"))
    store.set_cursor("syn-t1", "syn-c1")


def _seed_api_key(store: SQLiteApiKeyStore) -> None:
    store.create(
        ApiKeyRecord(
            key_id="synkey000001",
            name="syn-key",
            scopes=("read",),
            secret_hash="a" * 64,
        )
    )


@dataclass(frozen=True)
class Site:
    """A store write, a way to make it fail, and the rows it leaves.

    ``seeded`` is what ``rows`` reads after ``seed``, and ``written`` what it
    reads after ``call`` succeeds.
    """

    store: Callable[[Path], Any]
    seed: Callable[[Any], object]
    force: Callable[[Any, Path], None]
    call: Callable[[Any], object]
    rows: str
    seeded: Rows
    written: Rows
    error: type[sqlite3.Error] = sqlite3.IntegrityError
    message: str = FORCED


SITES: dict[str, Site] = {
    "graph.upsert_node": Site(
        store=SQLiteGraphStore,
        seed=lambda st: st.upsert_node("syn-n1", "syn-type", {"v": 1}),
        force=_trigger("INSERT", "nodes", "NEW.node_id = 'syn-n1'"),
        call=lambda st: st.upsert_node("syn-n1", "syn-type", {"v": 2}),
        rows=NODES,
        seeded=[("syn-n1", '{"v": 1}', 1)],
        written=[("syn-n1", '{"v": 1}', 0), ("syn-n1", '{"v": 2}', 1)],
    ),
    "graph.upsert_nodes_bulk": Site(
        store=SQLiteGraphStore,
        seed=lambda st: st.upsert_node("syn-n1", "syn-type", {"v": 1}),
        force=_trigger("INSERT", "nodes", "NEW.node_id = 'syn-n3'"),
        call=lambda st: st.upsert_nodes_bulk(
            [
                {"node_id": "syn-n1", "node_type": "syn-type", "properties": {"v": 2}},
                {"node_id": "syn-n2", "node_type": "syn-type"},
                {"node_id": "syn-n3", "node_type": "syn-type"},
                {"node_id": "syn-n4", "node_type": "syn-type"},
            ]
        ),
        rows=NODES,
        seeded=[("syn-n1", '{"v": 1}', 1)],
        written=[
            ("syn-n1", '{"v": 1}', 0),
            ("syn-n1", '{"v": 2}', 1),
            ("syn-n2", "{}", 1),
            ("syn-n3", "{}", 1),
            ("syn-n4", "{}", 1),
        ],
    ),
    "graph.upsert_alias": Site(
        store=SQLiteGraphStore,
        seed=lambda st: st.upsert_alias("syn-ent1", "syn-sys", "syn-raw1"),
        force=_trigger("INSERT", "entity_aliases", "NEW.entity_id = 'syn-ent2'"),
        call=lambda st: st.upsert_alias("syn-ent2", "syn-sys", "syn-raw1"),
        rows=ALIASES,
        seeded=[("syn-raw1", "syn-ent1", 1)],
        written=[("syn-raw1", "syn-ent1", 0), ("syn-raw1", "syn-ent2", 1)],
    ),
    "graph.upsert_edge": Site(
        store=SQLiteGraphStore,
        seed=_seed_edge,
        force=_trigger(
            "INSERT", "edges", "NEW.source_id = 'syn-a' AND NEW.target_id = 'syn-b'"
        ),
        call=lambda st: st.upsert_edge("syn-a", "syn-b", "syn-rel", {"v": 2}),
        rows=EDGES,
        seeded=[("syn-a", "syn-b", '{"v": 1}', 1)],
        written=[
            ("syn-a", "syn-b", '{"v": 1}', 0),
            ("syn-a", "syn-b", '{"v": 2}', 1),
        ],
    ),
    "graph.upsert_edges_bulk": Site(
        store=SQLiteGraphStore,
        seed=_seed_edge,
        force=_trigger("INSERT", "edges", "NEW.source_id = 'syn-b'"),
        call=lambda st: st.upsert_edges_bulk(
            [
                {
                    "source_id": "syn-a",
                    "target_id": "syn-b",
                    "edge_type": "syn-rel",
                    "properties": {"v": 2},
                },
                {"source_id": "syn-a", "target_id": "syn-c", "edge_type": "syn-rel"},
                {"source_id": "syn-b", "target_id": "syn-c", "edge_type": "syn-rel"},
                {"source_id": "syn-c", "target_id": "syn-a", "edge_type": "syn-rel"},
            ]
        ),
        rows=EDGES,
        seeded=[("syn-a", "syn-b", '{"v": 1}', 1)],
        written=[
            ("syn-a", "syn-b", '{"v": 1}', 0),
            ("syn-a", "syn-b", '{"v": 2}', 1),
            ("syn-a", "syn-c", "{}", 1),
            ("syn-b", "syn-c", "{}", 1),
            ("syn-c", "syn-a", "{}", 1),
        ],
    ),
    "graph.delete_node": Site(
        store=SQLiteGraphStore,
        seed=_seed_edge_and_alias,
        force=_trigger("DELETE", "nodes", "OLD.node_id = 'syn-a'"),
        call=lambda st: st.delete_node("syn-a"),
        rows=GRAPH,
        seeded=[("alias", "syn-raw1"), *SEEDED_GRAPH],
        written=[("node", "syn-b"), ("node", "syn-c")],
    ),
    "graph.delete_edge": Site(
        store=SQLiteGraphStore,
        seed=_seed_edge,
        force=_trigger("DELETE", "edges", "OLD.source_id = 'syn-a'"),
        call=_delete_the_edge_from_syn_a,
        rows=GRAPH,
        seeded=SEEDED_GRAPH,
        written=[("node", "syn-a"), ("node", "syn-b"), ("node", "syn-c")],
    ),
    "document.put": Site(
        store=SQLiteDocumentStore,
        seed=_seed_documents,
        force=_deny(sqlite3.SQLITE_INSERT, "documents_fts"),
        call=lambda st: st.put("syn-d1", "syn v1 edited"),
        rows=DOCUMENTS,
        seeded=SEEDED_DOCUMENTS,
        written=[
            ("doc", "syn-d1", "syn v1 edited"),
            ("doc", "syn-d2", "syn v2"),
            ("fts", "syn-d1", "syn v1 edited"),
            ("fts", "syn-d2", "syn v2"),
        ],
        error=sqlite3.DatabaseError,
        message=r"^not authorized$",
    ),
    "document.delete": Site(
        store=SQLiteDocumentStore,
        seed=_seed_documents,
        force=_deny(sqlite3.SQLITE_DELETE, "documents_fts"),
        call=lambda st: st.delete("syn-d1"),
        rows=DOCUMENTS,
        seeded=SEEDED_DOCUMENTS,
        written=[("doc", "syn-d2", "syn v2"), ("fts", "syn-d2", "syn v2")],
        error=sqlite3.DatabaseError,
        message=r"^not authorized$",
    ),
    "vector.upsert": Site(
        store=SQLiteVectorStore,
        seed=_seed_vectors,
        force=_trigger("INSERT", "vectors", "NEW.item_id = 'syn-v3'"),
        call=lambda st: st.upsert("syn-v3", [1.0, 1.0]),
        rows=VECTORS,
        seeded=[("syn-v1",), ("syn-v2",)],
        written=[("syn-v1",), ("syn-v2",), ("syn-v3",)],
    ),
    "vector.delete": Site(
        store=SQLiteVectorStore,
        seed=_seed_vectors,
        force=_trigger("DELETE", "vectors", "OLD.item_id = 'syn-v1'"),
        call=lambda st: st.delete("syn-v1"),
        rows=VECTORS,
        seeded=[("syn-v1",), ("syn-v2",)],
        written=[("syn-v2",)],
    ),
    "tuner_state.put_proposal": Site(
        store=SQLiteTunerStateStore,
        seed=_seed_tuner_state,
        force=_trigger("INSERT", "proposals", "NEW.proposal_id = 'syn-p2'"),
        call=lambda st: st.put_proposal(_proposal("syn-p2")),
        rows=PROPOSALS,
        seeded=[("syn-p1", "pending")],
        written=[("syn-p1", "pending"), ("syn-p2", "pending")],
    ),
    "tuner_state.update_status": Site(
        store=SQLiteTunerStateStore,
        seed=_seed_tuner_state,
        force=_trigger("UPDATE", "proposals", "OLD.proposal_id = 'syn-p1'"),
        call=lambda st: st.update_status("syn-p1", "rejected"),
        rows=PROPOSALS,
        seeded=[("syn-p1", "pending")],
        written=[("syn-p1", "rejected")],
    ),
    "tuner_state.set_cursor": Site(
        store=SQLiteTunerStateStore,
        seed=_seed_tuner_state,
        force=_trigger("INSERT", "tuner_cursors", "NEW.tuner = 'syn-t2'"),
        call=lambda st: st.set_cursor("syn-t2", "syn-c2"),
        rows=CURSORS,
        seeded=[("syn-t1", "syn-c1")],
        written=[("syn-t1", "syn-c1"), ("syn-t2", "syn-c2")],
    ),
    "api_key.revoke": Site(
        store=SQLiteApiKeyStore,
        seed=_seed_api_key,
        force=_trigger("UPDATE", "trellis_api_keys", "OLD.key_id = 'synkey000001'"),
        call=lambda st: st.revoke("synkey000001"),
        rows=KEYS,
        seeded=[("synkey000001", 0)],
        written=[("synkey000001", 1)],
    ),
}


@contextmanager
def _seeded(site: Site, db_path: Path) -> Iterator[Any]:
    store = site.store(db_path)
    try:
        site.seed(store)
        yield store
    finally:
        store.close()


@pytest.mark.parametrize("name", list(SITES))
def test_a_failed_write_holds_no_write_lock(name: str, tmp_path: Path) -> None:
    """A failed write is rolled back, not left holding the write lock.

    A write of several statements is made to fail after its first, so with
    nothing pending and no row changed, none of it was kept.
    """
    site = SITES[name]
    db_path = tmp_path / "store.db"
    with _seeded(site, db_path) as store:
        _ddl(db_path, "CREATE TABLE syn_probe (x INTEGER)")
        site.force(store, db_path)

        with pytest.raises(site.error, match=site.message) as err:
            site.call(store)

        assert type(err.value) is site.error
        assert store._conn.in_transaction is False
        write_at_once(db_path, "INSERT INTO syn_probe (x) VALUES (?)", (1,))
        assert committed_rows(db_path, site.rows) == site.seeded


@pytest.mark.parametrize("name", list(SITES))
def test_a_write_commits_at_once(name: str, tmp_path: Path) -> None:
    """A write that returns is committed: a second connection reads it."""
    site = SITES[name]
    db_path = tmp_path / "store.db"
    with _seeded(site, db_path) as store:
        site.call(store)

        assert committed_rows(db_path, site.rows) == site.written


@pytest.mark.parametrize(
    "write",
    [
        pytest.param(
            lambda st: st.upsert_node("syn-d", "syn-type", {}, commit=False),
            id="upsert_node",
        ),
        pytest.param(
            lambda st: st.upsert_edge("syn-b", "syn-c", "syn-rel", commit=False),
            id="upsert_edge",
        ),
    ],
)
def test_a_commit_false_write_waits_for_the_callers_transaction(
    write: Callable[[SQLiteGraphStore], object], tmp_path: Path
) -> None:
    """``commit=False`` leaves the write to the caller's ``transaction()``.

    The write must stay pending until that transaction ends, so the caller's
    rollback still reaches it.
    """
    db_path = tmp_path / "graph.db"
    store = SQLiteGraphStore(db_path)
    committed_inside: list[Rows] = []

    def write_then_fail() -> None:
        with store.transaction():
            write(store)
            committed_inside.append(committed_rows(db_path, GRAPH))
            msg = "syn abort"
            raise RuntimeError(msg)

    try:
        _seed_edge(store)

        with pytest.raises(RuntimeError, match=r"^syn abort$"):
            write_then_fail()

        assert committed_inside == [SEEDED_GRAPH]
        assert committed_rows(db_path, GRAPH) == SEEDED_GRAPH
    finally:
        store.close()


def test_a_commit_false_edge_write_commits_with_the_callers_transaction(
    tmp_path: Path,
) -> None:
    """``commit=False`` joins the caller's lock rather than nesting a BEGIN.

    The write must stay uncommitted until ``transaction()`` commits it (not
    visible to a second connection beforehand), and then land in the same
    commit as the rest of the caller's block — the success-path sibling of
    ``test_a_commit_false_write_waits_for_the_callers_transaction`` above.
    """
    db_path = tmp_path / "graph.db"
    store = SQLiteGraphStore(db_path)
    try:
        _seed_edge(store)
        committed_inside: list[Rows] = []

        with store.transaction():
            store.upsert_edge("syn-b", "syn-c", "syn-rel", commit=False)
            committed_inside.append(committed_rows(db_path, GRAPH))

        assert committed_inside == [SEEDED_GRAPH]
        assert committed_rows(db_path, GRAPH) == sorted(
            [*SEEDED_GRAPH, ("edge", "syn-b>syn-c")]
        )
    finally:
        store.close()


# --- Concurrent writers of one logical edge key -----------------------
#
# idx_edges_current (edge_id) is unique; idx_edges_upsert (source_id,
# target_id, edge_type) — the logical key the contract in
# src/trellis/stores/base/graph.py promises exactly one current row for —
# is not. A ``SQLiteGraphStore`` opens one ``sqlite3.Connection`` per
# thread (sqlite/base.py), so two threads sharing one store already
# exercise two connections against one database file, the same shape as
# two separate processes or store instances.


def test_concurrent_edge_create_leaves_one_current_row(tmp_path: Path) -> None:
    """Two connections racing to create one logical edge leave 1 current row.

    Before the fix, both connections could read "no current row" (the
    SELECT ran outside any transaction) and both insert, leaving 2.
    """
    db_path = tmp_path / "graph.db"
    store = SQLiteGraphStore(db_path)
    try:
        store.upsert_node("syn-a", "syn-type", {})
        store.upsert_node("syn-b", "syn-type", {})
        barrier = threading.Barrier(2)

        def _create(writer: str) -> str:
            barrier.wait(10)
            return store.upsert_edge("syn-a", "syn-b", "syn-rel", {"writer": writer})

        with ThreadPoolExecutor(max_workers=2) as pool:
            fut_x = pool.submit(_create, "x")
            fut_y = pool.submit(_create, "y")
            edge_ids = {fut_x.result(timeout=15), fut_y.result(timeout=15)}

        current = store.get_edges("syn-a", direction="outgoing", edge_type="syn-rel")
        assert len(current) == 1
        assert current[0]["edge_id"] in edge_ids
    finally:
        store.close()


def test_concurrent_commit_false_edge_create_leaves_one_current_row(
    tmp_path: Path,
) -> None:
    """Two ``commit=False`` callers each inside their own ``transaction()``
    still leave exactly 1 current row for the logical edge they both create.

    ``commit=False`` must still take the lock when the caller's transaction
    hasn't already opened one (``not conn.in_transaction``) — this is the
    real-world shape (the first, and only, write in the block), matching
    ``test_a_commit_false_write_waits_for_the_callers_transaction`` above.
    """
    db_path = tmp_path / "graph.db"
    store = SQLiteGraphStore(db_path)
    try:
        store.upsert_node("syn-a", "syn-type", {})
        store.upsert_node("syn-b", "syn-type", {})
        barrier = threading.Barrier(2)

        def _create(writer: str) -> None:
            barrier.wait(10)
            with store.transaction():
                store.upsert_edge(
                    "syn-a", "syn-b", "syn-rel", {"writer": writer}, commit=False
                )

        with ThreadPoolExecutor(max_workers=2) as pool:
            fut_x = pool.submit(_create, "x")
            fut_y = pool.submit(_create, "y")
            fut_x.result(timeout=15)
            fut_y.result(timeout=15)

        current = store.get_edges("syn-a", direction="outgoing", edge_type="syn-rel")
        assert len(current) == 1
    finally:
        store.close()


def test_concurrent_edge_update_leaves_one_current_row(tmp_path: Path) -> None:
    """Two connections racing to update one existing logical edge leave 1 row.

    Measured at the pre-fix base commit this already left exactly 1 current
    row (SQLite's single global writer lock makes the second committer's
    UPDATE ... WHERE valid_to IS NULL match the first's just-closed row,
    unlike Postgres where FOR UPDATE let the loser miss it) — a different
    finding from the Postgres sibling fix. This test pins that outcome as a
    contract, not a regression the fix newly closes.
    """
    db_path = tmp_path / "graph.db"
    store = SQLiteGraphStore(db_path)
    try:
        _seed_edge(store)
        barrier = threading.Barrier(2)

        def _update(writer: str) -> str:
            barrier.wait(10)
            return store.upsert_edge("syn-a", "syn-b", "syn-rel", {"writer": writer})

        with ThreadPoolExecutor(max_workers=2) as pool:
            fut_x = pool.submit(_update, "x")
            fut_y = pool.submit(_update, "y")
            fut_x.result(timeout=15)
            fut_y.result(timeout=15)

        current = store.get_edges("syn-a", direction="outgoing", edge_type="syn-rel")
        assert len(current) == 1
    finally:
        store.close()


def test_concurrent_edges_bulk_overlap_leaves_one_current_row_per_key(
    tmp_path: Path,
) -> None:
    """Two overlapping ``upsert_edges_bulk`` batches don't double a shared key.

    Batch x writes (a, b) and (a, c); batch y writes (a, b) and (a, d). The
    shared key (a, b) must still end with exactly one current row, the keys
    unique to each batch must both land, and neither batch may deadlock or
    raise ``sqlite3.OperationalError: database is locked``.
    """
    db_path = tmp_path / "graph.db"
    store = SQLiteGraphStore(db_path)
    try:
        for node_id in ("syn-a", "syn-b", "syn-c", "syn-d"):
            store.upsert_node(node_id, "syn-type", {})
        barrier = threading.Barrier(2)

        def _batch(writer: str, other_target: str) -> list[str]:
            barrier.wait(10)
            return store.upsert_edges_bulk(
                [
                    {
                        "source_id": "syn-a",
                        "target_id": "syn-b",
                        "edge_type": "syn-rel",
                        "properties": {"writer": writer},
                    },
                    {
                        "source_id": "syn-a",
                        "target_id": other_target,
                        "edge_type": "syn-rel",
                    },
                ]
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            fut_x = pool.submit(_batch, "x", "syn-c")
            fut_y = pool.submit(_batch, "y", "syn-d")
            fut_x.result(timeout=15)
            fut_y.result(timeout=15)

        current = store.get_edges("syn-a", direction="outgoing", edge_type="syn-rel")
        by_target = {edge["target_id"]: edge for edge in current}
        assert set(by_target) == {"syn-b", "syn-c", "syn-d"}
        assert len([e for e in current if e["target_id"] == "syn-b"]) == 1
    finally:
        store.close()
