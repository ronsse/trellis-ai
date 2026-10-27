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

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest

pytest.importorskip("neo4j")

from trellis.stores.arcadedb.graph import ArcadeDBGraphStore
from trellis.stores.base.graph import AliasBindStatus
from trellis.stores.bolt_opencypher import graph as bolt_graph
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
# the winning transaction replaced out from under this one. It arrives
# on ``Neo.DatabaseError.General.UnknownError``, the generic bucket, and
# it carries **no cause text**: ArcadeDB serializes only the outer
# ``CommandExecutionException``'s own message across Bolt, so the
# ``Caused by:`` frame naming the RID never leaves the server log.
#
# This constant read ``"Record #5:0 not found"`` until #556, described
# as captured from a live race. It was not — that string is what the
# *server log* shows — and the fiction is what kept a dead
# message-shaped branch looking covered for a release. The predicate now
# reads the code, and the live subject for all of this is
# ``test_arcadedb_graph.py::TestAliasContentionClassification``, which
# provokes every shape against a real server instead of naming one here.
ARCADEDB_GENERIC_BUCKET_MESSAGE = (
    "Error executing Cypher command: MERGE (c:AliasClaim {claim_key: $claim_key})"
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


def _ticking_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the store's clock a second later on every read.

    Two writes stamped by two reads are told apart, and a stamp reused
    across attempts shows as two equal ones. Same helper as
    ``test_bolt_opencypher_edge_claim_retry.py``'s.
    """
    ticks = iter(range(1, 1000))
    monkeypatch.setattr(
        bolt_graph,
        "utc_now",
        lambda: datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=next(ticks)),
    )


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

    @pytest.mark.parametrize(
        "message",
        [
            # An index-state error naming the claim index exactly (an
            # illustrative shape, not a captured message). Contention
            # cannot clear it, so a retry only delays the real error.
            "Index 'AliasClaim[claim_key]' is not online",
            # #571's case: a parse error quoting the claim's label and key.
            "Invalid input near 'AliasClaim' (claim_key)",
        ],
    )
    def test_rejects_a_message_about_this_constraint_that_is_not_a_violation(
        self, message: str
    ) -> None:
        """Naming the constraint is not the same as violating it.

        Both messages name ``AliasClaim`` and ``claim_key`` as whole
        tokens, so the identity check alone accepts them; only the
        violation phrase separates them from a lost race.
        """
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


class _AttemptTx:
    """One attempt's transaction: records each statement, answers the reads.

    ``answers`` maps a statement's Cypher to what its ``.single()`` returns;
    the writes that follow the reads are consumed and ignored.
    """

    def __init__(self, answers: Callable[[str], Any]) -> None:
        self.statements: list[tuple[str, dict[str, Any]]] = []
        self._answers = answers

    def run(self, cypher: str, **params: Any) -> MagicMock:
        self.statements.append((cypher, params))
        result = MagicMock()
        result.single.return_value = self._answers(cypher)
        return result

    def sent(self, fragment: str) -> list[dict[str, Any]]:
        """The parameters of every statement whose Cypher holds ``fragment``."""
        return [kw for cypher, kw in self.statements if fragment in cypher]


def _lose_the_first_attempt(
    store: BoltOpenCypherGraphStore, answers: Callable[[str], Any]
) -> list[_AttemptTx]:
    """Run the real transaction function; the first attempt fails at commit.

    ArcadeDB detects an ``AliasClaim`` violation at commit, after every
    statement was sent, so the refused attempt has already stamped its
    time when the re-run begins. Returns one :class:`_AttemptTx` per
    attempt, in order.
    """
    attempts: list[_AttemptTx] = []

    def execute_write(tx_function: Callable[[Any], Any]) -> Any:
        tx = _AttemptTx(answers)
        attempts.append(tx)
        result = tx_function(tx)
        if len(attempts) == 1:
            raise _contention()
        return result

    session = MagicMock()
    session.execute_write.side_effect = execute_write
    store._driver.session.return_value.__enter__.return_value = session  # type: ignore[attr-defined]
    return attempts


def _created_alias(tx: _AttemptTx) -> dict[str, Any]:
    """The properties of the one alias version an attempt created."""
    (sent,) = tx.sent("CREATE (a:Alias)")
    return sent["new_props"]


# What a writer finds when the alias already has a current version.
_PRIOR_CREATED_AT = "2025-12-31T00:00:00+00:00"
_PRIOR_ALIAS = {
    "alias_id": "alias_old",
    "entity_id": "ent_old",
    "source_system": "name",
    "raw_id": "hermes",
    "created_at": _PRIOR_CREATED_AT,
    "valid_from": _PRIOR_CREATED_AT,
    "valid_to": None,
}


class TestAliasWritersStampEachAttempt:
    """A re-run stamps its own time, not the refused attempt's (#1005).

    Another writer can commit a newer version of the alias between a refused
    attempt and its re-run. The re-run closes that version with ``$now``, so
    a ``now`` kept from the first attempt would give it a ``valid_to``
    before its own ``valid_from``: an inverted interval that ``as_of``
    reads get wrong. Driven with a fake session that runs the real
    transaction function against a clock that ticks on every read; the
    sibling of ``TestUpsertEdgeStampsEachAttempt`` in
    ``test_bolt_opencypher_edge_claim_retry.py`` (#1002).
    """

    @pytest.mark.parametrize("has_current", [True, False], ids=["close", "create"])
    def test_upsert_alias_re_run_sends_a_later_timestamp(
        self, monkeypatch: pytest.MonkeyPatch, has_current: bool
    ) -> None:
        _ticking_clock(monkeypatch)
        store = _make_store()

        def answers(cypher: str) -> Any:
            if "RETURN a" in cypher and has_current:
                return {"a": dict(_PRIOR_ALIAS)}
            return None

        attempts = _lose_the_first_attempt(store, answers)

        assert store.upsert_alias("ent_new", "name", "hermes") is not None

        assert len(attempts) == 2
        stamped = []
        for tx in attempts:
            created = _created_alias(tx)
            closes = tx.sent("SET a.valid_to = $now")
            # Everything an attempt stamps from ``now`` shares one time:
            # the version closed and the version created.
            assert created["updated_at"] == created["valid_from"]
            if has_current:
                assert [kw["now"] for kw in closes] == [created["valid_from"]]
                assert created["created_at"] == _PRIOR_CREATED_AT
            else:
                assert closes == []
                assert created["created_at"] == created["valid_from"]
            stamped.append(datetime.fromisoformat(created["valid_from"]))
        refused, rerun = stamped
        assert rerun > refused

    @pytest.mark.parametrize(
        ("has_current", "status"),
        [
            pytest.param(True, AliasBindStatus.REBOUND, id="repair-closes-current"),
            pytest.param(False, AliasBindStatus.BOUND, id="first-bind-creates"),
        ],
    )
    def test_bind_alias_if_absent_re_run_sends_a_later_timestamp(
        self,
        monkeypatch: pytest.MonkeyPatch,
        has_current: bool,
        status: AliasBindStatus,
    ) -> None:
        _ticking_clock(monkeypatch)
        store = _make_store()

        def answers(cypher: str) -> Any:
            if "MERGE (c:AliasClaim" in cypher:
                # The claim names the previous owner when there is one (the
                # repair path), and this caller otherwise (a first bind).
                return (
                    {"entity_id": "ent_old", "alias_id": "alias_old"}
                    if has_current
                    else {"entity_id": "ent_new", "alias_id": "alias_new"}
                )
            if "MATCH (a:Alias" in cypher and "RETURN a" in cypher and has_current:
                return {"a": dict(_PRIOR_ALIAS)}
            # The previous owner's node: absent, so its name cannot match
            # the stale-owner key and the repair proceeds.
            return None

        attempts = _lose_the_first_attempt(store, answers)

        result = store.bind_alias_if_absent(
            "ent_new", "name", "hermes", stale_owner_name_key="stale-name"
        )

        # The path the case names is the one that ran.
        assert result.status is status
        assert len(attempts) == 2
        stamped = []
        for tx in attempts:
            created = _created_alias(tx)
            closes = tx.sent("SET a.valid_to = $now")
            assert created["updated_at"] == created["valid_from"]
            if has_current:
                assert [kw["now"] for kw in closes] == [created["valid_from"]]
                assert created["created_at"] == _PRIOR_CREATED_AT
            else:
                assert closes == []
                assert created["created_at"] == created["valid_from"]
            stamped.append(datetime.fromisoformat(created["valid_from"]))
        refused, rerun = stamped
        assert rerun > refused


class TestArcadeDBWidensOverItsGenericErrorChannel:
    """The per-backend seam, and why it is a seam rather than a shared list.

    ArcadeDB reports the *other* half of a lost race — a record the
    winning transaction replaced — as
    ``Neo.DatabaseError.General.UnknownError``, the same generic bucket
    that engine-side evaluation faults land in. Widening the *shared*
    predicate to cover that code would make a **Neo4j** deployment retry
    a genuine engine fault, which is fatal there and will never clear.

    These are the seam's unit-level properties. The behaviour they
    describe is measured against a real server in
    ``test_arcadedb_graph.py::TestAliasContentionClassification``; this
    class exists so the seam cannot be dissolved without a failure in a
    suite that runs on every pull request, because the live one does
    not.
    """

    def test_recognizes_the_generic_bucket_a_stale_record_arrives_on(self) -> None:
        """Matched by code, because no cause text crosses the wire."""
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
            # Every one of these is read as contention, whatever it
            # says. That is the cost #556 accepted, asserted so it is
            # recorded rather than discovered: the bucket carries
            # engine-side evaluation faults too, and no text
            # distinguishes them, because ArcadeDB serializes only the
            # outer exception's own message across Bolt.
            #
            # These cases asserted ``False`` until #556, on a regex over
            # a message the client never receives. That made the branch
            # look both covered and precise while it was neither.
            "Record not found",
            "Schema 'Node' not found",
            "Record #abc:def not found",
            "Record #5:0 was modified",
            "",
        ],
    )
    def test_the_whole_generic_bucket_is_read_as_contention(self, message: str) -> None:
        store = _make_arcadedb_store()
        exc = _FakeNeo4jError(message, "Neo.DatabaseError.General.UnknownError")
        assert store._is_alias_write_contention(exc) is True

    @pytest.mark.parametrize(
        "code",
        [
            # What bounds the imprecision above: ArcadeDB gives every
            # *statement*-level fault its own code, so none of them
            # reaches the branch. Measured 2026-09-22 against
            # ``arcadedata/arcadedb:26.8.1``, the image
            # ``live-infra.yml`` pins — each of these is provoked from a
            # real server in the live suite named in the class
            # docstring.
            #
            # This is the half that keeps the override a classification:
            # without it ``_is_alias_write_contention`` could be
            # ``return True`` and every other case here would pass.
            "Neo.ClientError.Statement.SyntaxError",
            "Neo.ClientError.Statement.ParameterMissing",
            "Neo.ClientError.Statement.ArithmeticError",
        ],
    )
    def test_statement_faults_are_not_read_as_contention(self, code: str) -> None:
        store = _make_arcadedb_store()
        exc = _FakeNeo4jError("Record #5:0 not found", code)
        assert store._is_alias_write_contention(exc) is False

    def test_a_stale_record_is_retried_end_to_end(self) -> None:
        """The override reaches the retry, not just the predicate."""
        store = _make_arcadedb_store()
        winner = MagicMock(status=AliasBindStatus.CONFLICT, entity_id="ent_b")
        execute_write = _arm_execute_write(store, [_stale_record(), winner])

        assert store.bind_alias_if_absent("ent_a", "name", "hermes") is winner
        assert execute_write.call_count == 2
