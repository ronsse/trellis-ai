"""The loser of a Bolt first-create race answers as a serial second create.

``create_node_if_absent`` derives the first ``version_id`` of a node from its
id and the count of its rows, so concurrent first creators of one absent id
write the same ``version_id`` and ``node_version_unique`` rejects all but one
(#1232). These pin the loser path without a server: the constraint error is
caught, the statement runs again, finds the winner's current row and returns
``False``. The live subject is
``test_neo4j_graph.py::test_concurrent_first_creates_write_one_row`` and the
contract's ``test_create_node_if_absent_is_atomic_for_concurrent_writers``.

The messages are verbatim from the engines (measured 2026-11-18 against
``neo4j:2026.10.2`` and ``arcadedata/arcadedb:26.8.1``), because the predicate
reads message text.
"""

from __future__ import annotations

import inspect

import pytest

pytest.importorskip("neo4j")

from neo4j.exceptions import Neo4jError

from tests.fake_bolt_driver import FakeBoltDriver
from trellis.core.ids import generate_ulid
from trellis.stores.bolt_opencypher.graph import (
    _DEFAULT_SCHEMA_STATEMENTS,
    _FIRST_CREATE_ATTEMPTS,
    BoltOpenCypherGraphStore,
    _first_version_prefix,
    _is_first_create_contention,
)

NEO4J_LOSER = Neo4jError._hydrate_neo4j(
    code="Neo.ClientError.Schema.ConstraintValidationFailed",
    message=(
        "Node(9) already exists with label `Node` and property `version_id` "
        "= 'first-probe-neo4j-0-0'"
    ),
)
ARCADEDB_LOSER = Neo4jError._hydrate_neo4j(
    code="Neo.ClientError.Transaction.TransactionNotFound",
    message=(
        "Duplicated key [first-probe-arcadedb-0-0] found on index "
        "'Node[version_id]' already assigned to record #1:8192"
    ),
)
# A violation of another constraint: not this race, so never re-run.
ALIAS_CLAIM_VIOLATION = Neo4jError._hydrate_neo4j(
    code="Neo.ClientError.Schema.ConstraintValidationFailed",
    message=(
        "Node(17) already exists with label `AliasClaim` and property `claim_key` = 'x'"
    ),
)


def _store(driver: FakeBoltDriver) -> BoltOpenCypherGraphStore:
    return BoltOpenCypherGraphStore(
        driver=driver,  # type: ignore[arg-type]
        database="neo4j",
        owns_driver=False,
        init_schema=False,
    )


@pytest.mark.parametrize(
    "loser", [NEO4J_LOSER, ARCADEDB_LOSER], ids=["neo4j", "arcadedb"]
)
def test_the_loser_re_reads_and_answers_already_exists(loser: Neo4jError) -> None:
    driver = FakeBoltDriver(creates=[loser, False])

    assert _store(driver).create_node_if_absent("n1", "service", {}) is False

    assert [tx.ended for tx in driver.transactions] == ["rollback", "commit"]
    # Both runs derive the same first version_id: the re-run is the same write.
    prefixes = {p["first_version_prefix"] for p in driver.create_params}
    assert prefixes == {_first_version_prefix("n1")}


def test_an_uncontended_create_runs_once() -> None:
    driver = FakeBoltDriver(creates=[True])

    assert _store(driver).create_node_if_absent("n1", "service", {}) is True
    assert [tx.ended for tx in driver.transactions] == ["commit"]
    # The derived id replaces the ULID; nothing else carries a version_id.
    assert "version_id" not in driver.create_params[0]["new_props"]


def test_another_violation_propagates_without_a_re_run() -> None:
    driver = FakeBoltDriver(creates=[ALIAS_CLAIM_VIOLATION, True])

    with pytest.raises(Neo4jError) as caught:
        _store(driver).create_node_if_absent("n1", "service", {})

    assert caught.value is ALIAS_CLAIM_VIOLATION
    assert len(driver.transactions) == 1


def test_a_violation_that_persists_raises_after_the_bound() -> None:
    driver = FakeBoltDriver(creates=[NEO4J_LOSER] * (_FIRST_CREATE_ATTEMPTS + 1))

    with pytest.raises(Neo4jError) as caught:
        _store(driver).create_node_if_absent("n1", "service", {})

    assert caught.value is NEO4J_LOSER
    assert len(driver.transactions) == _FIRST_CREATE_ATTEMPTS


def test_the_predicate_names_the_node_version_constraint_only() -> None:
    assert _is_first_create_contention(NEO4J_LOSER)
    assert _is_first_create_contention(ARCADEDB_LOSER)
    assert not _is_first_create_contention(ALIAS_CLAIM_VIOLATION)
    # Neo4j opens every message with ``Node(<id>)``: the label must be read
    # from ``label `...```, so a version_id violation on another label is not
    # this race.
    assert not _is_first_create_contention(
        Neo4jError._hydrate_neo4j(
            code="Neo.ClientError.Schema.ConstraintValidationFailed",
            message=(
                "Node(12) already exists with label `Alias` and property "
                "`version_id` = 'x'"
            ),
        )
    )
    assert not _is_first_create_contention(
        Neo4jError._hydrate_neo4j(
            code="Neo.ClientError.Transaction.TransactionNotFound",
            message="Duplicated key [x] found on index 'Alias[version_id]'",
        )
    )
    assert not _is_first_create_contention(
        Neo4jError._hydrate_neo4j(
            code="Neo.ClientError.Statement.SyntaxError",
            message="Invalid input 'version_id' on label Node",
        )
    )
    # The constraint the predicate names is the one the DDL creates.
    assert (
        "CREATE CONSTRAINT node_version_unique IF NOT EXISTS "
        "FOR (n:Node) REQUIRE n.version_id IS UNIQUE"
    ) in _DEFAULT_SCHEMA_STATEMENTS


def test_the_first_version_prefix_is_per_id_and_never_a_ulid() -> None:
    assert _first_version_prefix("n1") == _first_version_prefix("n1")
    assert _first_version_prefix("n1") != _first_version_prefix("n2")
    assert len(_first_version_prefix("x" * 10_000)) == len(_first_version_prefix(""))
    ulid = generate_ulid()
    assert ulid == ulid.upper()
    assert _first_version_prefix("n1").startswith("first-")


class _VerifyingStore(BoltOpenCypherGraphStore):
    """The ArcadeDB setting, with the re-read and the delete scripted."""

    VERIFY_FIRST_CREATE_UNIQUENESS = True

    def __init__(self, driver: FakeBoltDriver, rows: list[tuple[str, str, str]]):
        super().__init__(
            driver=driver,  # type: ignore[arg-type]
            database="neo4j",
            owns_driver=False,
            init_schema=False,
        )
        self.rows = rows
        self.removed: list[list[str]] = []
        # Row ids with a relationship attached, as the edge read reports them.
        self.edged: set[str] = set()
        self.edge_reads: list[list[str]] = []

    def _current_node_rows(self, node_id: str) -> list[tuple[str, str, str]]:
        return list(self.rows)

    def _rows_with_edges(self, node_id: str, row_ids: list[str]) -> set[str]:
        self.edge_reads.append(row_ids)
        return self.edged & set(row_ids)

    def _remove_node_rows(self, node_id: str, row_ids: list[str]) -> None:
        self.removed.append(row_ids)


def test_the_re_read_of_a_single_row_writes_nothing_more() -> None:
    store = _VerifyingStore(
        FakeBoltDriver(creates=["#1:1"]), [("2026-01-01", "first-x-0", "#1:1")]
    )

    assert store.create_node_if_absent("n1", "service", {}) is True
    assert store.removed == []


# Two writers whose commits the engine let both through: same version_id,
# both rows current. Each re-read sees both and picks the same survivor.
_BOTH = [
    ("2026-01-01T00:00:00.000002+00:00", "first-x-0", "#1:1"),
    ("2026-01-01T00:00:00.000001+00:00", "first-x-0", "#1:2"),
]


def test_a_duplicate_found_by_the_loser_is_removed_and_it_answers_false() -> None:
    store = _VerifyingStore(FakeBoltDriver(creates=["#1:1"]), _BOTH)

    assert store.create_node_if_absent("n1", "service", {}) is False
    assert store.removed == [["#1:1"]]


def test_the_survivor_writer_answers_true_and_removes_the_same_row() -> None:
    store = _VerifyingStore(FakeBoltDriver(creates=["#1:2"]), _BOTH)

    assert store.create_node_if_absent("n1", "service", {}) is True
    assert store.removed == [["#1:1"]]


def test_a_writer_whose_row_was_already_removed_answers_false() -> None:
    # The survivor's writer settled first: this writer's re-read finds one
    # row, and it is not its own.
    store = _VerifyingStore(
        FakeBoltDriver(creates=["#1:1"]),
        [("2026-01-01T00:00:00.000001+00:00", "first-x-0", "#1:2")],
    )

    assert store.create_node_if_absent("n1", "service", {}) is False
    assert store.removed == []


def test_a_create_deleted_before_the_re_read_still_answers_true() -> None:
    store = _VerifyingStore(FakeBoltDriver(creates=["#1:1"]), [])

    assert store.create_node_if_absent("n1", "service", {}) is True
    assert store.removed == []


def test_an_equal_stamp_tie_breaks_on_the_row_id() -> None:
    tied = [("t", "first-x-0", "#1:9"), ("t", "first-x-0", "#1:10")]
    answers = [
        _VerifyingStore(FakeBoltDriver(creates=[own]), tied).create_node_if_absent(
            "n1", "service", {}
        )
        for own in ("#1:9", "#1:10")
    ]

    assert sorted(answers) == [False, True]


def test_the_one_row_with_an_edge_survives_and_its_writer_answers_true() -> None:
    # An edge attaches to the shown row, the later ``valid_from`` (#1:1), which
    # is not the earliest-stamp pick. A plain DELETE of it is refused, so the
    # settle keeps it and removes the edgeless row instead.
    store = _VerifyingStore(FakeBoltDriver(creates=["#1:1"]), _BOTH)
    store.edged = {"#1:1"}

    assert store.create_node_if_absent("n1", "service", {}) is True
    assert store.removed == [["#1:2"]]
    assert store.edge_reads == [["#1:1", "#1:2"]]


def test_the_other_writer_also_keeps_the_row_with_an_edge() -> None:
    store = _VerifyingStore(FakeBoltDriver(creates=["#1:2"]), _BOTH)
    store.edged = {"#1:1"}

    assert store.create_node_if_absent("n1", "service", {}) is False
    assert store.removed == [["#1:2"]]


def test_an_edge_on_the_earliest_row_keeps_the_earliest_row() -> None:
    store = _VerifyingStore(FakeBoltDriver(creates=["#1:2"]), _BOTH)
    store.edged = {"#1:2"}

    assert store.create_node_if_absent("n1", "service", {}) is True
    assert store.removed == [["#1:1"]]


def test_edges_on_both_rows_fall_back_to_the_earliest_row_and_say_so() -> None:
    # No row can be removed without its edge; the settle does not pick a
    # different victim, and the warning records why both rows stay.
    from structlog.testing import capture_logs

    store = _VerifyingStore(FakeBoltDriver(creates=["#1:2"]), _BOTH)
    store.edged = {"#1:1", "#1:2"}

    with capture_logs() as logs:
        assert store.create_node_if_absent("n1", "service", {}) is True
    assert store.removed == [["#1:1"]]
    (settled,) = (
        e for e in logs if e["event"] == "node_first_create_duplicate_settled"
    )
    assert settled["rows_with_edges"] == 2
    assert settled["node_id"] == "n1"


def test_a_failed_edge_read_keeps_the_earliest_row() -> None:
    class _EdgeReadFails(_VerifyingStore):
        def _rows_with_edges(self, node_id: str, row_ids: list[str]) -> set[str]:
            msg = "edge read failed"
            raise RuntimeError(msg)

    from structlog.testing import capture_logs

    store = _EdgeReadFails(FakeBoltDriver(creates=["#1:2"]), _BOTH)
    with capture_logs() as logs:
        assert store.create_node_if_absent("n1", "service", {}) is True
    assert store.removed == [["#1:1"]]
    (failed,) = (
        e for e in logs if e["event"] == "node_first_create_duplicate_edge_read_failed"
    )
    assert failed["node_id"] == "n1"
    assert failed["error_type"] == "RuntimeError"


def test_one_current_row_never_reads_edges() -> None:
    store = _VerifyingStore(
        FakeBoltDriver(creates=["#1:1"]), [("2026-01-01", "first-x-0", "#1:1")]
    )
    store.edged = {"#1:1"}

    assert store.create_node_if_absent("n1", "service", {}) is True
    assert store.edge_reads == []


def test_the_edge_read_names_only_the_current_rows_of_the_node() -> None:
    # Through the fake driver: the read reaches the server with the node id
    # and the duplicate rows' ids, and reports the rows it returns.
    driver = FakeBoltDriver(creates=[True], node_reads=[[{"row_id": "#1:1"}]])
    store = _store(driver)

    assert store._rows_with_edges("n1", ["#1:1", "#1:2"]) == {"#1:1"}
    assert driver.node_read_params == [{"node_id": "n1", "row_ids": ["#1:1", "#1:2"]}]


# Two settling writers that share one store state. ArcadeDB refuses a delete
# whose target row has a relationship, and refuses the statement whole.
class _SharedRows:
    def __init__(self) -> None:
        self.rows = {row[2]: row for row in _BOTH}
        self.edged: set[str] = set()


class _SharedWriter(_VerifyingStore):
    def __init__(self, world: _SharedRows, own: str, after_edge_read=None) -> None:
        super().__init__(FakeBoltDriver(creates=[own]), _BOTH)
        self.world = world
        self.after_edge_read = after_edge_read

    def _current_node_rows(self, node_id: str) -> list[tuple[str, str, str]]:
        return list(self.world.rows.values())

    def _rows_with_edges(self, node_id: str, row_ids: list[str]) -> set[str]:
        self.edge_reads.append(row_ids)
        seen = self.world.edged & set(row_ids) & set(self.world.rows)
        if self.after_edge_read is not None:
            hook, self.after_edge_read = self.after_edge_read, None
            hook()
        return seen

    def _remove_node_rows(self, node_id: str, row_ids: list[str]) -> None:
        self.removed.append(row_ids)
        if self.world.edged & set(row_ids):
            msg = "row has a relationship"
            raise RuntimeError(msg)
        for row_id in row_ids:
            self.world.rows.pop(row_id, None)


def test_an_edge_landing_after_one_writers_edge_read_tells_only_the_keeper_true() -> (
    None
):
    # Writer 1 (the earliest row, #1:2) reads no edge and keeps its own row;
    # an edge then lands on #1:1, so its delete of #1:1 is refused. Writer 2
    # sees the edge, keeps #1:1 and removes #1:2. Only #1:1 is left, so
    # writer 1 must hear False and re-apply through the update branch.
    from structlog.testing import capture_logs

    world = _SharedRows()
    w1 = _SharedWriter(world, "#1:2", after_edge_read=lambda: world.edged.add("#1:1"))
    w2 = _SharedWriter(world, "#1:1")

    with capture_logs() as logs:
        first = w1.create_node_if_absent("n1", "service", {})
    second = w2.create_node_if_absent("n1", "service", {})

    assert sorted(world.rows) == ["#1:1"]
    assert (first, second) == (False, True)
    assert w1.removed == [["#1:1"], ["#1:2"]]
    settled = [e for e in logs if e["event"] == "node_first_create_duplicate_settled"]
    assert [(e["settle_round"], e["removed"], e["kept_own_row"]) for e in settled] == [
        (1, False, True),
        (2, True, False),
    ]
    assert settled[-1]["rows_with_edges"] == 1


def test_a_failed_round_two_edge_read_leaves_exactly_one_writer_told_true() -> None:
    # Writer 1's round-two edge read raises, so the pick falls back to its
    # own earliest row and the delete of the edged row is refused again.
    # Picking its own row is not enough: only a delete that went through
    # confirms it.
    class _SecondEdgeReadFails(_SharedWriter):
        edge_read_count = 0

        def _rows_with_edges(self, node_id: str, row_ids: list[str]) -> set[str]:
            self.edge_read_count += 1
            if self.edge_read_count == 2:
                msg = "edge read failed"
                raise RuntimeError(msg)
            return super()._rows_with_edges(node_id, row_ids)

    world = _SharedRows()
    w1 = _SecondEdgeReadFails(
        world, "#1:2", after_edge_read=lambda: world.edged.add("#1:1")
    )
    w2 = _SharedWriter(world, "#1:1")

    first = w1.create_node_if_absent("n1", "service", {})
    second = w2.create_node_if_absent("n1", "service", {})

    assert (first, second) == (False, True)
    assert sorted(world.rows) == ["#1:1"]


def test_a_refused_delete_then_a_late_edge_leaves_exactly_one_writer_told_true() -> (
    None
):
    # Writer 1's first delete is refused for a reason that is not an edge;
    # an edge then lands on #1:1 after its round-two edge read, so the
    # round-two delete is refused too.
    class _FirstDeleteFails(_SharedWriter):
        def _remove_node_rows(self, node_id: str, row_ids: list[str]) -> None:
            if not self.removed:
                self.removed.append(row_ids)
                msg = "transient"
                raise RuntimeError(msg)
            super()._remove_node_rows(node_id, row_ids)

    world = _SharedRows()
    w1 = _FirstDeleteFails(world, "#1:2")

    def land_edge_on_second_read() -> None:
        world.edged.add("#1:1")

    original = w1._rows_with_edges

    def edge_read(node_id: str, row_ids: list[str]) -> set[str]:
        seen = original(node_id, row_ids)
        if len(w1.edge_reads) == 2:
            land_edge_on_second_read()
        return seen

    w1._rows_with_edges = edge_read  # type: ignore[method-assign]
    w2 = _SharedWriter(world, "#1:1")

    first = w1.create_node_if_absent("n1", "service", {})
    second = w2.create_node_if_absent("n1", "service", {})

    assert len(w1.removed) == 2
    assert (first, second) == (False, True)
    assert sorted(world.rows) == ["#1:1"]


def test_a_refused_delete_re_picks_once_and_then_answers() -> None:
    # Every delete refused: one extra round, no third.
    class _AlwaysRefused(_VerifyingStore):
        def _remove_node_rows(self, node_id: str, row_ids: list[str]) -> None:
            self.removed.append(row_ids)
            msg = "refused"
            raise RuntimeError(msg)

    class _CountingReads(_AlwaysRefused):
        row_reads = 0

        def _current_node_rows(self, node_id: str) -> list[tuple[str, str, str]]:
            self.row_reads += 1
            return list(self.rows)

    store = _CountingReads(FakeBoltDriver(creates=["#1:2"]), _BOTH)

    # Its own row was picked both times, but no delete went through, so it
    # is not confirmed as the sole creator and re-applies as an update.
    assert store.create_node_if_absent("n1", "service", {}) is False
    assert store.removed == [["#1:1"], ["#1:1"]]
    assert len(store.edge_reads) == 2
    assert store.row_reads == 2


def test_a_failed_re_read_after_a_refused_delete_does_not_escape() -> None:
    from structlog.testing import capture_logs

    class _ReReadFails(_VerifyingStore):
        reads = 0

        def _current_node_rows(self, node_id: str) -> list[tuple[str, str, str]]:
            self.reads += 1
            if self.reads > 1:
                msg = "re-read failed"
                raise RuntimeError(msg)
            return list(self.rows)

        def _remove_node_rows(self, node_id: str, row_ids: list[str]) -> None:
            msg = "refused"
            raise RuntimeError(msg)

    store = _ReReadFails(FakeBoltDriver(creates=["#1:2"]), _BOTH)
    with capture_logs() as logs:
        assert store.create_node_if_absent("n1", "service", {}) is False
    assert "node_first_create_duplicate_reread_failed" in [e["event"] for e in logs]


def test_the_edge_read_counts_edges_either_way_and_closed_ones() -> None:
    # ArcadeDB refuses to delete a row any relationship touches, incoming or
    # outgoing, current or closed, so the read that predicts the refusal
    # matches undirected and does not filter on ``valid_to``.
    driver = FakeBoltDriver(creates=[True], node_reads=[[{"row_id": "#1:2"}]])
    store = _store(driver)
    statements: list[str] = []
    run_read_list = store._run_read_list

    def recording(cypher: str, **params: object) -> list[dict[str, object]]:
        statements.append(cypher)
        return run_read_list(cypher, **params)

    store._run_read_list = recording  # type: ignore[method-assign]

    assert store._rows_with_edges("n1", ["#1:1", "#1:2"]) == {"#1:2"}
    (cypher,) = statements
    assert "MATCH (n)-[]-()" in cypher
    assert "->" not in cypher
    assert "<-" not in cypher
    assert "valid_to" not in cypher


def test_neo4j_does_not_re_read_and_arcadedb_does() -> None:
    from trellis.stores.arcadedb.graph import ArcadeDBGraphStore
    from trellis.stores.neo4j.graph import Neo4jGraphStore

    assert Neo4jGraphStore.VERIFY_FIRST_CREATE_UNIQUENESS is False
    assert ArcadeDBGraphStore.VERIFY_FIRST_CREATE_UNIQUENESS is True


# The shape the loser's re-run raised on ``arcadedata/arcadedb:26.8.1`` once the
# winner's commit was landing (captured 2026-11-18): the server log names
# ``RecordNotFoundException: Record #1:0 not found``, but only the outer text,
# echoing the statement, crosses Bolt.
_FIRST_CREATE_CYPHER_ECHO = (
    "Error executing Cypher command: \n"
    "        OPTIONAL MATCH (row:Node {node_id: $node_id})\n"
    "        WITH collect(row) AS rows\n"
    "        WITH rows, [r IN rows WHERE r.valid_to IS NULL] AS current_rows\n"
    "        WHERE size(current_rows) = 0\n"
    "        CREATE (n:Node)\n"
    "        SET n = $new_props\n"
    "        SET n.version_id = $first_version_prefix + toString(size(rows))\n"
    "        RETURN n.node_id AS node_id, elementId(n) AS row_id\n"
)
ARCADEDB_STALE_RE_READ = Neo4jError._hydrate_neo4j(
    code="Neo.DatabaseError.General.UnknownError",
    message=_FIRST_CREATE_CYPHER_ECHO,
)


def test_arcadedb_re_runs_its_stale_re_read_and_neo4j_does_not() -> None:
    from trellis.stores.arcadedb.graph import ArcadeDBGraphStore

    # The predicate reads no state, so a bare instance is enough.
    arcadedb = object.__new__(ArcadeDBGraphStore)
    assert arcadedb._is_first_create_write_contention(ARCADEDB_STALE_RE_READ)
    assert arcadedb._is_first_create_write_contention(ARCADEDB_LOSER)
    # The generic code from any other statement is not this race.
    assert not arcadedb._is_first_create_write_contention(
        Neo4jError._hydrate_neo4j(
            code="Neo.DatabaseError.General.UnknownError",
            message="Error executing Cypher command: MATCH (n:Node) RETURN n",
        )
    )
    # Nor is this statement's text under another code.
    assert not arcadedb._is_first_create_write_contention(
        Neo4jError._hydrate_neo4j(
            code="Neo.ClientError.Statement.SyntaxError",
            message=_FIRST_CREATE_CYPHER_ECHO,
        )
    )
    assert not arcadedb._is_first_create_write_contention(ALIAS_CLAIM_VIOLATION)

    # The portable store (and Neo4j, which inherits it) raises it at once.
    driver = FakeBoltDriver(creates=[ARCADEDB_STALE_RE_READ, False])
    with pytest.raises(Neo4jError) as caught:
        _store(driver).create_node_if_absent("n1", "service", {})
    assert caught.value is ARCADEDB_STALE_RE_READ
    assert len(driver.transactions) == 1


def test_the_marker_names_the_first_create_statement() -> None:
    from trellis.stores.arcadedb.graph import _FIRST_CREATE_STATEMENT_MARKER

    # The statement that echoes it is the one ``create_node_if_absent`` runs.
    source = inspect.getsource(BoltOpenCypherGraphStore.create_node_if_absent)
    assert _FIRST_CREATE_STATEMENT_MARKER in source
    assert _FIRST_CREATE_STATEMENT_MARKER in _FIRST_CREATE_CYPHER_ECHO


def _arcadedb_store(driver: FakeBoltDriver) -> BoltOpenCypherGraphStore:
    """An ArcadeDB store on the fake driver, so the retry loop itself runs.

    ``ArcadeDBGraphStore.__init__`` needs a URI, so the base initializer is
    called on a bare instance. The settle re-read is stubbed to one row.
    """
    from trellis.stores.arcadedb.graph import ArcadeDBGraphStore

    class _Store(ArcadeDBGraphStore):
        def _current_node_rows(self, node_id: str) -> list[tuple[str, str, str]]:
            return [("t", "first-x-0", "#1:1")]

    store = object.__new__(_Store)
    BoltOpenCypherGraphStore.__init__(
        store, driver=driver, database="neo4j", owns_driver=False, init_schema=False
    )
    return store


def test_arcadedb_create_re_runs_a_stale_re_read_and_answers_present() -> None:
    # The predicate test above does not reach the call site: this one fails
    # when ``create_node_if_absent`` asks the portable predicate instead.
    driver = FakeBoltDriver(creates=[ARCADEDB_STALE_RE_READ, False])
    assert _arcadedb_store(driver).create_node_if_absent("n1", "service", {}) is False
    assert [t.ended for t in driver.transactions] == ["rollback", "commit"]


def test_arcadedb_create_re_runs_a_stale_re_read_and_answers_created() -> None:
    driver = FakeBoltDriver(creates=[ARCADEDB_STALE_RE_READ, "#1:1"])
    assert _arcadedb_store(driver).create_node_if_absent("n1", "service", {}) is True


def test_arcadedb_persistent_stale_re_read_is_raised_after_the_bound() -> None:
    driver = FakeBoltDriver(creates=[ARCADEDB_STALE_RE_READ] * 5)
    with pytest.raises(Neo4jError) as caught:
        _arcadedb_store(driver).create_node_if_absent("n1", "service", {})
    assert caught.value is ARCADEDB_STALE_RE_READ
    assert len(driver.transactions) == 3


def test_an_absorbed_generic_fault_is_a_warning_with_its_code() -> None:
    # The edge path warns on the same code; the first-create path must too, or
    # a transient fault that is not a race disappears once retried.
    from structlog.testing import capture_logs

    driver = FakeBoltDriver(creates=[ARCADEDB_STALE_RE_READ, False])
    with capture_logs() as logs:
        assert (
            _arcadedb_store(driver).create_node_if_absent("n1", "service", {}) is False
        )
    warnings = [e for e in logs if e["log_level"] == "warning"]
    assert warnings == [
        {
            "event": "arcadedb_first_create_generic_fault_retried",
            "log_level": "warning",
            "node_id": "n1",
            "attempt": 1,
            "error_code": "Neo.DatabaseError.General.UnknownError",
        }
    ]


@pytest.mark.parametrize("arcadedb", [False, True], ids=["portable", "arcadedb"])
def test_a_lost_duplicate_key_race_stays_at_debug(arcadedb: bool) -> None:
    from structlog.testing import capture_logs

    loser = ARCADEDB_LOSER if arcadedb else NEO4J_LOSER
    driver = FakeBoltDriver(creates=[loser, False])
    store = _arcadedb_store(driver) if arcadedb else _store(driver)
    with capture_logs() as logs:
        assert store.create_node_if_absent("n1", "service", {}) is False
    assert [e["log_level"] for e in logs] == ["debug"]
    assert logs[0]["event"] == "bolt_first_create_lost_race"


# The settle re-read's own statement, echoed under the generic code: the shape
# the create statement's stale read arrives in, applied to the read that runs
# after the create committed. Not reproduced live; the re-read is defensive.
ARCADEDB_STALE_SETTLE_READ = Neo4jError._hydrate_neo4j(
    code="Neo.DatabaseError.General.UnknownError",
    message=(
        "Error executing Cypher command: MATCH (n:Node {node_id: $node_id}) "
        "WHERE n.valid_to IS NULL RETURN n.valid_from AS valid_from, "
        "n.version_id AS version_id, elementId(n) AS row_id"
    ),
)
_OWN_ROW = {"valid_from": "t", "version_id": "first-x-0", "row_id": "#1:1"}


def _arcadedb_store_reading(driver: FakeBoltDriver) -> BoltOpenCypherGraphStore:
    """An ArcadeDB store on the fake driver whose settle read reaches the driver."""
    from trellis.stores.arcadedb.graph import ArcadeDBGraphStore

    store = object.__new__(ArcadeDBGraphStore)
    BoltOpenCypherGraphStore.__init__(
        store, driver=driver, database="neo4j", owns_driver=False, init_schema=False
    )
    return store


def test_a_stale_settle_read_is_re_read_without_re_running_the_create() -> None:
    from structlog.testing import capture_logs

    driver = FakeBoltDriver(
        creates=["#1:1"], node_reads=[ARCADEDB_STALE_SETTLE_READ, [_OWN_ROW]]
    )
    with capture_logs() as logs:
        assert (
            _arcadedb_store_reading(driver).create_node_if_absent("n1", "service", {})
            is True
        )
    # One create, committed; only the read ran twice.
    assert [t.ended for t in driver.transactions] == ["commit"]
    assert len(driver.create_params) == 1
    assert driver.node_read_params == [{"node_id": "n1"}, {"node_id": "n1"}]
    assert [e for e in logs if e["log_level"] == "warning"] == [
        {
            "event": "bolt_first_create_settle_read_retried",
            "log_level": "warning",
            "node_id": "n1",
            "attempt": 1,
            "error_code": "Neo.DatabaseError.General.UnknownError",
        }
    ]


def test_a_persistent_settle_read_fault_is_raised_after_the_bound() -> None:
    driver = FakeBoltDriver(
        creates=["#1:1"], node_reads=[ARCADEDB_STALE_SETTLE_READ] * 5
    )
    with pytest.raises(Neo4jError) as caught:
        _arcadedb_store_reading(driver).create_node_if_absent("n1", "service", {})
    assert caught.value is ARCADEDB_STALE_SETTLE_READ
    assert len(driver.node_read_params) == _FIRST_CREATE_ATTEMPTS
    assert len(driver.create_params) == 1


def test_another_settle_read_error_is_raised_at_once() -> None:
    other = Neo4jError._hydrate_neo4j(
        code="Neo.ClientError.Statement.SyntaxError", message="bad"
    )
    driver = FakeBoltDriver(creates=["#1:1"], node_reads=[other, [_OWN_ROW]])
    with pytest.raises(Neo4jError) as caught:
        _arcadedb_store_reading(driver).create_node_if_absent("n1", "service", {})
    assert caught.value is other
    assert len(driver.node_read_params) == 1


def test_the_portable_settle_read_retries_nothing() -> None:
    # The portable store and Neo4j never re-read a failed settle read.
    store = _store(FakeBoltDriver(creates=[True]))
    assert not store._is_settle_read_contention(ARCADEDB_STALE_SETTLE_READ)


def test_a_stale_settle_read_waits_before_each_re_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The settle re-read waits on the module's one backoff schedule for a row
    # not yet readable, the edge write's, bounded by the read's own attempts.
    from trellis.stores.bolt_opencypher import graph as bolt_graph

    sleeps: list[float] = []
    monkeypatch.setattr(bolt_graph.time, "sleep", sleeps.append)
    driver = FakeBoltDriver(
        creates=["#1:1"],
        node_reads=[ARCADEDB_STALE_SETTLE_READ, ARCADEDB_STALE_SETTLE_READ, [_OWN_ROW]],
    )

    assert (
        _arcadedb_store_reading(driver).create_node_if_absent("n1", "service", {})
        is True
    )
    assert sleeps == list(
        bolt_graph._EDGE_ENDPOINT_RETRY_BACKOFF_SECONDS[: _FIRST_CREATE_ATTEMPTS - 1]
    )
    assert sum(sleeps) < 0.5


def test_a_persistent_settle_read_fault_waits_only_between_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trellis.stores.bolt_opencypher import graph as bolt_graph

    sleeps: list[float] = []
    monkeypatch.setattr(bolt_graph.time, "sleep", sleeps.append)
    driver = FakeBoltDriver(
        creates=["#1:1"], node_reads=[ARCADEDB_STALE_SETTLE_READ] * 5
    )
    with pytest.raises(Neo4jError):
        _arcadedb_store_reading(driver).create_node_if_absent("n1", "service", {})
    # No wait after the last read: the fault is raised at once.
    assert len(sleeps) == _FIRST_CREATE_ATTEMPTS - 1


def test_a_clean_settle_read_and_another_error_do_not_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trellis.stores.bolt_opencypher import graph as bolt_graph

    sleeps: list[float] = []
    monkeypatch.setattr(bolt_graph.time, "sleep", sleeps.append)
    clean = FakeBoltDriver(creates=["#1:1"], node_reads=[[_OWN_ROW]])
    assert _arcadedb_store_reading(clean).create_node_if_absent("n1", "s", {}) is True
    other = Neo4jError._hydrate_neo4j(
        code="Neo.ClientError.Statement.SyntaxError", message="bad"
    )
    failing = FakeBoltDriver(creates=["#1:1"], node_reads=[other])
    with pytest.raises(Neo4jError):
        _arcadedb_store_reading(failing).create_node_if_absent("n1", "s", {})
    assert sleeps == []
