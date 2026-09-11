"""The ``AliasClaim`` race is retried, and only that race is retried.

These run unconditionally — no live ArcadeDB or Neo4j is required. The
live round-trip is
``test_arcadedb_graph_contract.py::...::test_bind_alias_if_absent_is_atomic_for_concurrent_contenders``,
which ran nowhere until #351 wired the ArcadeDB contract into CI and
which has failed on ``main`` ever since. A live-backend test is the wrong
and only place to pin this: it needs a service container to say anything
at all, so a regression in the retry is invisible to ``make test`` and to
the PR ``tests.yml`` job.

The messages asserted below are **verbatim from the two engines**, not
paraphrases — the predicate reads message text, so a paraphrase would
pin the test to itself rather than to what the servers actually send.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

pytest.importorskip("neo4j")

from trellis.stores.arcadedb.graph import ArcadeDBGraphStore
from trellis.stores.base.graph import AliasBindStatus
from trellis.stores.bolt_opencypher.graph import (
    _ALIAS_CLAIM_KEY_PROPERTY,
    _ALIAS_CLAIM_LABEL,
    _ALIAS_CLAIM_RETRY_ATTEMPTS,
    _DEFAULT_SCHEMA_STATEMENTS,
    BoltOpenCypherGraphStore,
    _is_alias_claim_contention,
)

# Measured 2026-09-11 against ``arcadedata/arcadedb:26.8.1``, the image
# pinned by ``live-infra.yml``. Note the code is about a *missing
# transaction* while the message is about a *duplicate key*: ArcadeDB
# detects the violation at commit time and maps it onto the nearest
# Neo4j code, which is why the predicate cannot key on a code.
ARCADEDB_MESSAGE = (
    'Duplicated key [["name","hermes"]] found on index '
    "'AliasClaim[claim_key]' already assigned to record #9:0"
)

# Neo4j's own wording for a violation of the same constraint.
# Verified 2026-09-11 by provoking it against ``neo4j:2025.12``: the
# format is byte-identical, only the allocated node id differs per run.
NEO4J_MESSAGE = (
    "Node(17) already exists with label `AliasClaim` and property "
    '`claim_key` = \'["name","hermes"]\''
)

# The *other* shape ArcadeDB produces for the same lost race: a record
# the winning transaction replaced out from under this one. Captured
# 2026-09-11 from a live two-thread race against
# ``arcadedata/arcadedb:26.8.1``. The code is
# ``Neo.DatabaseError.General.UnknownError`` — the generic bucket — so
# the RID shape in the message is the only thing that makes it specific.
ARCADEDB_STALE_RECORD_MESSAGE = "Record #5:0 not found"


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
        ARCADEDB_STALE_RECORD_MESSAGE, "Neo.DatabaseError.General.UnknownError"
    )


def _make_arcadedb_store() -> ArcadeDBGraphStore:
    """An ArcadeDB store with a mocked driver — no server, no HTTP.

    ``__new__`` deliberately skips ``__init__``: the constructor builds a
    Bolt driver and ensures the database over HTTP, neither of which this
    file has or wants.
    """
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
        assert _is_alias_claim_contention(_contention()) is True

    def test_recognizes_the_message_neo4j_actually_sends(self) -> None:
        assert _is_alias_claim_contention(_contention(NEO4J_MESSAGE)) is True

    def test_reads_str_when_the_error_carries_no_message_attribute(self) -> None:
        assert _is_alias_claim_contention(RuntimeError(ARCADEDB_MESSAGE)) is True

    @pytest.mark.parametrize(
        "message",
        [
            # A different constraint on the same store.
            "Node(4) already exists with label `Alias` and property `version_id`",
            # The claim label without its key property: a label scan, an
            # index build — not a violation of the unique key.
            "Index 'AliasClaim[entity_id]' is not online",
            # The key property under a different label.
            "Duplicated key found on index 'Node[claim_key]'",
            # A different index whose name merely *starts* with ours. A
            # substring test reads this as claim contention and retries a
            # violation that will never clear.
            "Duplicated key found on index 'AliasClaimArchive[claim_key]'",
            "Duplicated key found on index 'AliasClaim[claim_key_v2]'",
            "",
        ],
    )
    def test_rejects_everything_that_is_not_this_constraint(self, message: str) -> None:
        assert _is_alias_claim_contention(_contention(message)) is False

    def test_the_shipped_ddl_names_both_tokens_the_predicate_matches(self) -> None:
        """The premise the predicate rests on, checked against the schema.

        The predicate is sound only because a violation of this
        constraint necessarily names the label and the key property. That
        is a fact about the DDL, so assert it against the DDL rather than
        trusting the two to stay in step.
        """
        claim_ddl = [
            stmt for stmt in _DEFAULT_SCHEMA_STATEMENTS if "alias_claim_unique" in stmt
        ]
        assert len(claim_ddl) == 1
        # Asserted as equality, not membership: ``AliasClaim`` is a
        # substring of ``AliasClaimX``, so a containment check passes
        # against a constraint declared on a different label.
        assert claim_ddl[0] == (
            "CREATE CONSTRAINT alias_claim_unique IF NOT EXISTS "
            f"FOR (c:{_ALIAS_CLAIM_LABEL}) "
            f"REQUIRE c.{_ALIAS_CLAIM_KEY_PROPERTY} IS UNIQUE"
        )


class TestBindAliasIfAbsentRetries:
    def test_a_lost_race_is_re_run_and_its_result_returned(self) -> None:
        """The retry's answer is the caller's answer.

        Losing the race is how a contender learns another entity owns the
        pair, so the re-run must be allowed to return ``CONFLICT`` — not
        be converted into an error.
        """
        store = _make_store()
        winner = MagicMock(status=AliasBindStatus.CONFLICT, entity_id="ent_b")
        execute_write = _arm_execute_write(store, [_contention(), winner])

        result = store.bind_alias_if_absent("ent_a", "name", "hermes")

        assert result is winner
        assert execute_write.call_count == 2

    def test_an_unrelated_error_is_not_retried(self) -> None:
        store = _make_store()
        boom = _FakeNeo4jError("Node(4) already exists with label `Alias`", "Neo.X")
        execute_write = _arm_execute_write(store, [boom, MagicMock()])

        with pytest.raises(_FakeNeo4jError):
            store.bind_alias_if_absent("ent_a", "name", "hermes")

        assert execute_write.call_count == 1

    def test_persistent_contention_raises_rather_than_spinning(self) -> None:
        """The bound is what separates contention from a real violation."""
        store = _make_store()
        execute_write = _arm_execute_write(
            store, [_contention() for _ in range(_ALIAS_CLAIM_RETRY_ATTEMPTS + 2)]
        )

        with pytest.raises(_FakeNeo4jError):
            store.bind_alias_if_absent("ent_a", "name", "hermes")

        assert execute_write.call_count == _ALIAS_CLAIM_RETRY_ATTEMPTS

    def test_a_clean_first_attempt_does_not_retry(self) -> None:
        store = _make_store()
        expected = MagicMock(status=AliasBindStatus.BOUND)
        execute_write = _arm_execute_write(store, [expected])

        assert store.bind_alias_if_absent("ent_a", "name", "hermes") is expected
        assert execute_write.call_count == 1


class TestUpsertAliasRetries:
    """The sibling ``MERGE`` site, which no live test covers.

    ``upsert_alias`` MERGEs the same claim node under the same unique
    constraint, so it has always carried the identical exposure — the
    contract suite simply has no concurrent test for it.
    """

    def test_a_lost_race_is_re_run(self) -> None:
        store = _make_store()
        execute_write = _arm_execute_write(store, [_contention(), "alias_01"])

        assert store.upsert_alias("ent_a", "name", "hermes") == "alias_01"
        assert execute_write.call_count == 2

    def test_an_unrelated_error_is_not_retried(self) -> None:
        store = _make_store()
        boom = _FakeNeo4jError("connection refused", "Neo.TransientError.X")
        execute_write = _arm_execute_write(store, [boom, "alias_01"])

        with pytest.raises(_FakeNeo4jError):
            store.upsert_alias("ent_a", "name", "hermes")

        assert execute_write.call_count == 1


class TestArcadeDBWidensOverItsGenericErrorChannel:
    """The per-backend seam, and why it is a seam rather than a shared list.

    ArcadeDB reports the *other* half of a lost race — a record the
    winning transaction replaced — as
    ``Neo.DatabaseError.General.UnknownError``, the same generic bucket
    that unrelated failures land in. Widening the shared predicate to
    cover it would make a **Neo4j** deployment retry a genuine
    "record not found", which is fatal there and will never clear.
    """

    def test_recognizes_the_stale_record_message_arcadedb_actually_sends(self) -> None:
        store = _make_arcadedb_store()
        assert store._is_alias_write_contention(_stale_record()) is True

    def test_still_recognizes_the_base_predicates_duplicate_key(self) -> None:
        """The override widens the base; it must not replace it."""
        store = _make_arcadedb_store()
        assert store._is_alias_write_contention(_contention()) is True

    def test_the_base_class_does_not_widen(self) -> None:
        """The seam's whole purpose, asserted directly.

        A Neo4j deployment must never retry a missing record: nothing
        about it is contention there, and a retry would spin to the
        bound and re-raise later than it should have.
        """
        assert _is_alias_claim_contention(_stale_record()) is False
        assert _make_store()._is_alias_write_contention(_stale_record()) is False

    @pytest.mark.parametrize(
        "message",
        [
            # The generic bucket carries unrelated failures too. Without
            # the RID shape there is nothing to tell them apart, so a
            # looser match retries failures that are genuinely fatal.
            "Record not found",
            "Schema 'Node' not found",
            "Record #abc:def not found",
            "Record #5:0 was modified",
            "",
        ],
    )
    def test_rejects_the_rest_of_the_generic_bucket(self, message: str) -> None:
        store = _make_arcadedb_store()
        exc = _FakeNeo4jError(message, "Neo.DatabaseError.General.UnknownError")
        assert store._is_alias_write_contention(exc) is False

    def test_a_stale_record_is_retried_end_to_end(self) -> None:
        """The override reaches the retry, not just the predicate."""
        store = _make_arcadedb_store()
        winner = MagicMock(status=AliasBindStatus.CONFLICT, entity_id="ent_b")
        execute_write = _arm_execute_write(store, [_stale_record(), winner])

        assert store.bind_alias_if_absent("ent_a", "name", "hermes") is winner
        assert execute_write.call_count == 2
