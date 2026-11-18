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
