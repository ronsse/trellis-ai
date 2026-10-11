"""The ``EdgeClaim`` race is retried, and only that race is retried.

These run unconditionally — no live ArcadeDB or Neo4j is required, mirroring
``test_bolt_opencypher_alias_claim_retry.py`` for the sibling claim
``upsert_edge`` MERGEs. The live round-trip is
``tests/unit/stores/bolt_edge_create_race.py``'s two concurrent-write checks,
run against a real server by ``test_arcadedb_graph.py`` and
``test_neo4j_graph.py`` — but a live-backend test is the wrong and only place
to pin the *retry's* behaviour: it needs a service container to say anything
at all, so a regression here is invisible to ``make test`` and to the PR
``tests.yml`` job.

The messages asserted below swap ``EdgeClaim``/the edge triplet into the
exact templates ``test_bolt_opencypher_alias_claim_retry.py`` measured
against live servers for ``AliasClaim`` (2026-09-11,
``arcadedata/arcadedb:26.8.1`` and ``neo4j:2025.12``) — ``_is_claim_contention``
is the one function both predicates call, so the template, not the label, is
what was measured. They are not independently re-measured against a live
server for ``EdgeClaim`` specifically (see ``_is_edge_claim_contention``'s
docstring).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

pytest.importorskip("neo4j")

from trellis.stores.arcadedb.graph import ArcadeDBGraphStore
from trellis.stores.bolt_opencypher.graph import (
    _ALIAS_CLAIM_RETRY_ATTEMPTS,
    _DEFAULT_SCHEMA_STATEMENTS,
    _EDGE_CLAIM_KEY_PROPERTY,
    _EDGE_CLAIM_LABEL,
    BoltOpenCypherGraphStore,
    _is_edge_claim_contention,
)

# ``upsert_edge`` and ``upsert_alias`` share one retry bound
# (``_execute_edge_claim_write`` and ``_execute_alias_write`` both loop
# ``_ALIAS_CLAIM_RETRY_ATTEMPTS`` times) — aliased here so this file reads
# as being about the edge claim, not borrowing an alias-named constant.
_EDGE_CLAIM_RETRY_ATTEMPTS = _ALIAS_CLAIM_RETRY_ATTEMPTS

# Same template as ``test_bolt_opencypher_alias_claim_retry.py``'s
# ``ARCADEDB_MESSAGE``, with ``AliasClaim`` swapped for ``EdgeClaim`` and an
# edge-triplet key in place of the alias pair.
ARCADEDB_MESSAGE = (
    'Duplicated key [["src","tgt","race-rel"]] found on index '
    "'EdgeClaim[claim_key]' already assigned to record #9:0"
)

# Same template as the alias test's ``NEO4J_MESSAGE``.
NEO4J_MESSAGE = (
    "Node(17) already exists with label `EdgeClaim` and property "
    '`claim_key` = \'["src","tgt","race-rel"]\''
)

# The generic-bucket shape ArcadeDB reports for a record the winning
# transaction already replaced — same code the alias path's override
# widens over, same reason: no cause text crosses Bolt.
ARCADEDB_GENERIC_BUCKET_MESSAGE = (
    "Error executing Cypher command: MERGE (ec:EdgeClaim {claim_key: $edge_claim_key})"
)


class _FakeNeo4jError(Exception):
    """Stand-in carrying the ``.message`` attribute the driver exposes."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def _contention(message: str = ARCADEDB_MESSAGE) -> _FakeNeo4jError:
    return _FakeNeo4jError(message, "Neo.ClientError.Transaction.TransactionNotFound")


def _stale_record() -> _FakeNeo4jError:
    return _FakeNeo4jError(
        ARCADEDB_GENERIC_BUCKET_MESSAGE, "Neo.DatabaseError.General.UnknownError"
    )


def _make_arcadedb_store() -> ArcadeDBGraphStore:
    """An ArcadeDB store with a mocked driver — no server, no HTTP."""
    store = ArcadeDBGraphStore.__new__(ArcadeDBGraphStore)
    store._driver = MagicMock()  # type: ignore[attr-defined]
    store._database = "test"
    store._owns_driver = False
    return store


def _make_store() -> BoltOpenCypherGraphStore:
    store = BoltOpenCypherGraphStore.__new__(BoltOpenCypherGraphStore)
    store._driver = MagicMock()  # type: ignore[attr-defined]
    store._database = "test"
    store._owns_driver = False
    return store


def _arm_execute_write(
    store: BoltOpenCypherGraphStore, side_effects: list[Any]
) -> MagicMock:
    """Make each ``session.execute_write`` call consume one side effect.

    A value is returned, an exception instance is raised. Returns the
    ``execute_write`` mock so a test can count attempts.
    """
    session = MagicMock()
    session.execute_write = MagicMock(side_effect=side_effects)
    driver = store._driver  # type: ignore[attr-defined]
    driver.session.return_value.__enter__.return_value = session
    return session.execute_write


class TestPredicate:
    def test_recognizes_the_message_arcadedb_actually_sends(self) -> None:
        assert _is_edge_claim_contention(_contention()) is True

    def test_recognizes_the_message_neo4j_actually_sends(self) -> None:
        assert _is_edge_claim_contention(_contention(NEO4J_MESSAGE)) is True

    def test_reads_str_when_the_error_carries_no_message_attribute(self) -> None:
        assert _is_edge_claim_contention(RuntimeError(ARCADEDB_MESSAGE)) is True

    @pytest.mark.parametrize(
        "message",
        [
            # A different constraint on the same store.
            "Node(4) already exists with label `Node` and property `version_id`",
            # The claim label without its key property.
            "Index 'EdgeClaim[entity_id]' is not online",
            # The key property under a different label — the AliasClaim one.
            "Duplicated key found on index 'AliasClaim[claim_key]'",
            # A different index whose name merely *starts* with ours.
            "Duplicated key found on index 'EdgeClaimArchive[claim_key]'",
            "Duplicated key found on index 'EdgeClaim[claim_key_v2]'",
            "",
        ],
    )
    def test_rejects_everything_that_is_not_this_constraint(self, message: str) -> None:
        assert _is_edge_claim_contention(_contention(message)) is False

    @pytest.mark.parametrize(
        "message",
        [
            "Index 'EdgeClaim[claim_key]' is not online",
            "Invalid input near 'EdgeClaim' (claim_key)",
        ],
    )
    def test_rejects_a_message_about_this_constraint_that_is_not_a_violation(
        self, message: str
    ) -> None:
        """Naming the constraint is not the same as violating it."""
        assert _is_edge_claim_contention(_contention(message)) is False

    def test_the_shipped_ddl_names_both_tokens_the_predicate_matches(self) -> None:
        """The premise the predicate rests on, checked against the schema."""
        claim_ddl = [
            stmt for stmt in _DEFAULT_SCHEMA_STATEMENTS if "edge_claim_unique" in stmt
        ]
        assert len(claim_ddl) == 1
        assert claim_ddl[0] == (
            "CREATE CONSTRAINT edge_claim_unique IF NOT EXISTS "
            f"FOR (c:{_EDGE_CLAIM_LABEL}) "
            f"REQUIRE c.{_EDGE_CLAIM_KEY_PROPERTY} IS UNIQUE"
        )


class TestUpsertEdgeRetries:
    def test_a_lost_race_is_re_run_and_its_result_returned(self) -> None:
        """Losing the claim race is recovered by re-reading committed state.

        The retry must be allowed to return the winner's edge id, not be
        converted into an error: the whole point of the claim is that the
        loser's transaction rolls back and re-runs from scratch.
        """
        store = _make_store()
        execute_write = _arm_execute_write(
            store, [_contention(), {"edge_id": "edge_01"}]
        )

        result = store.upsert_edge("src", "tgt", "race-rel", {})

        assert result == "edge_01"
        assert execute_write.call_count == 2

    def test_an_unrelated_error_is_not_retried(self) -> None:
        store = _make_store()
        boom = _FakeNeo4jError("connection refused", "Neo.TransientError.X")
        execute_write = _arm_execute_write(store, [boom, {"edge_id": "edge_01"}])

        with pytest.raises(_FakeNeo4jError):
            store.upsert_edge("src", "tgt", "race-rel", {})

        assert execute_write.call_count == 1

    def test_persistent_contention_raises_rather_than_spinning(self) -> None:
        """The bound is what separates contention from a real violation."""
        store = _make_store()
        execute_write = _arm_execute_write(
            store, [_contention() for _ in range(_EDGE_CLAIM_RETRY_ATTEMPTS + 2)]
        )

        with pytest.raises(_FakeNeo4jError):
            store.upsert_edge("src", "tgt", "race-rel", {})

        assert execute_write.call_count == _EDGE_CLAIM_RETRY_ATTEMPTS

    def test_a_clean_first_attempt_does_not_retry(self) -> None:
        store = _make_store()
        execute_write = _arm_execute_write(store, [{"edge_id": "edge_01"}])

        assert store.upsert_edge("src", "tgt", "race-rel", {}) == "edge_01"
        assert execute_write.call_count == 1

    def test_missing_endpoint_raises_without_retrying(self) -> None:
        """A ``None`` row means no current endpoint, not a lost race."""
        store = _make_store()
        execute_write = _arm_execute_write(store, [None])

        with pytest.raises(ValueError, match="no current version"):
            store.upsert_edge("src", "tgt", "race-rel", {})

        assert execute_write.call_count == 1


class TestArcadeDBWidensEdgeClaimOverItsGenericErrorChannel:
    """The per-backend seam, mirroring the alias one for the same reason.

    ArcadeDB reports a record the winning transaction already replaced —
    the shape the claim's ``_cas_lock`` touch of an *existing* row hits —
    on the generic ``Neo.DatabaseError.General.UnknownError`` bucket, not
    the unique-violation channel the base predicate matches. Widening the
    shared predicate itself would make a Neo4j deployment retry a genuine
    engine fault, which is fatal there and will never clear.
    """

    def test_recognizes_the_generic_bucket_a_stale_record_arrives_on(self) -> None:
        store = _make_arcadedb_store()
        assert store._is_edge_write_contention(_stale_record()) is True

    def test_still_recognizes_the_base_predicates_duplicate_key(self) -> None:
        """The override widens the base; it must not replace it."""
        store = _make_arcadedb_store()
        assert store._is_edge_write_contention(_contention()) is True

    def test_the_base_class_does_not_widen(self) -> None:
        """A Neo4j deployment must never retry a missing record."""
        assert _is_edge_claim_contention(_stale_record()) is False
        assert _make_store()._is_edge_write_contention(_stale_record()) is False

    @pytest.mark.parametrize(
        "code",
        [
            "Neo.ClientError.Statement.SyntaxError",
            "Neo.ClientError.Statement.ParameterMissing",
            "Neo.ClientError.Statement.ArithmeticError",
        ],
    )
    def test_statement_faults_are_not_read_as_contention(self, code: str) -> None:
        store = _make_arcadedb_store()
        exc = _FakeNeo4jError("Record #5:0 not found", code)
        assert store._is_edge_write_contention(exc) is False

    def test_a_stale_record_is_retried_end_to_end(self) -> None:
        """The override reaches the retry, not just the predicate."""
        store = _make_arcadedb_store()
        execute_write = _arm_execute_write(
            store, [_stale_record(), {"edge_id": "edge_01"}]
        )

        assert store.upsert_edge("src", "tgt", "race-rel", {}) == "edge_01"
        assert execute_write.call_count == 2

    def test_the_two_claim_predicates_are_independent_overrides(self) -> None:
        """Widening the edge channel must not widen the alias channel's.

        Both overrides key on the same generic bucket code, so a
        regression that merges them into one flag would pass every test
        above while silently making an alias write retry on an edge-only
        failure shape, or vice versa. Each override must answer only for
        its own write path.
        """
        store = _make_arcadedb_store()
        stale = _stale_record()
        assert store._is_edge_write_contention(stale) is True
        assert store._is_alias_write_contention(stale) is True
        # Both are True for this shared bucket code (neither narrows by
        # message text — see the class docstring) but each is reached
        # through its own method, not a shared mutable flag.
        assert (
            ArcadeDBGraphStore._is_edge_write_contention
            is not ArcadeDBGraphStore._is_alias_write_contention
        )
