"""The alias-claim contention retry (#530 follow-up).

``MERGE (c:AliasClaim {claim_key: ...})`` under a unique constraint has two
legitimate backend behaviours. Neo4j locks the index entry, so a concurrent
loser *waits* and its ``MERGE`` then matches the winner's row — which is what
``bind_alias_if_absent``'s "Re-read after MERGE" comment assumes. ArcadeDB is
optimistic: both transactions proceed and the loser is rejected at commit,
surfacing a driver error where the algorithm expects a matched row. That took
``main`` red on ``live-infra`` for four days.

These run unconditionally, and that is the point. The live contract
(:file:`contracts/test_arcadedb_graph_contract.py`) proves the retry *works*,
but it can only ever produce the one violation the claim index raises — so it
cannot observe whether the match is **narrow**. Measured: widening
:func:`_is_alias_claim_contention` to ignore the label leaves the whole live
ArcadeDB contract suite green.

**Today's schema cannot observe the narrowness either, and that is why the
evasion corpus below is synthetic.** The three unique constraints are
``Node[version_id]``, ``Alias[version_id]`` and ``AliasClaim[claim_key]``, so
no other index shares *either* marker and each half of the conjunction
discriminates perfectly on its own — dropping one is an equivalent mutant
against every message this schema can produce. The conjunction is insurance
against a *sibling* index, so a sibling index is what has to be handed to the
shipped predicate for the insurance to be checkable at all.
"""

from __future__ import annotations

import inspect
import re
from typing import Any
from unittest.mock import MagicMock

import pytest
from structlog.testing import capture_logs

pytest.importorskip("neo4j")

from neo4j.exceptions import ClientError, Neo4jError, ServiceUnavailable

from trellis.stores.bolt_opencypher.graph import (
    _ALIAS_CLAIM_KEY_PROPERTY,
    _ALIAS_CLAIM_LABEL,
    _CLAIM_CONTENTION_ATTEMPTS,
    _DEFAULT_SCHEMA_STATEMENTS,
    _DRIVER_ERRORS,
    BoltOpenCypherGraphStore,
    _is_alias_claim_contention,
)

#: ``FOR (c:AliasClaim) REQUIRE c.claim_key IS UNIQUE`` → ``(AliasClaim, claim_key)``.
_UNIQUE_CONSTRAINT = re.compile(r"FOR \((\w+):(\w+)\) REQUIRE \1\.(\w+) IS UNIQUE")

#: The verbatim message ArcadeDB 26.8.1 returns to the loser of a claim race,
#: captured from the failing run rather than paraphrased — the predicate reads
#: this text, so a hand-written approximation would test the approximation.
ARCADEDB_LOST_RACE = (
    'Duplicated key [["name","hermes"]] found on index '
    "'AliasClaim[claim_key]' already assigned to record #9:2048"
)

#: Neo4j's spelling of the same fact.
NEO4J_LOST_RACE = (
    "Node(42) already exists with label `AliasClaim` and property "
    '`claim_key` = \'["name","hermes"]\''
)


def _unique_indexes() -> list[tuple[str, str]]:
    """Every ``(label, property)`` the shipped DDL makes unique."""
    return [
        (match.group(2), match.group(3))
        for statement in _DEFAULT_SCHEMA_STATEMENTS
        if (match := _UNIQUE_CONSTRAINT.search(statement)) is not None
    ]


def _duplicate_key_message(label: str, prop: str) -> str:
    """Render ArcadeDB's duplicate-key message for a ``label[prop]`` index.

    One template, so the corpus below is *derived* from an index name rather
    than hand-written per case — a decoy that accidentally omits the shape the
    predicate keys on proves nothing.
    """
    return (
        'Duplicated key [["name","hermes"]] found on index '
        f"'{label}[{prop}]' already assigned to record #9:2048"
    )


def _client_error(message: str) -> ClientError:
    """Build the driver error a backend raises, with its message intact."""
    return ClientError(message)


class TestTheContentionPredicate:
    """What counts as "a concurrent contender won the claim"."""

    def test_the_arcadedb_lost_race_is_recognised(self) -> None:
        assert _is_alias_claim_contention(_client_error(ARCADEDB_LOST_RACE))

    def test_the_rendered_message_matches_the_captured_one(self) -> None:
        """The template must reproduce the real message byte for byte.

        Everything below is built from :func:`_duplicate_key_message`, so a
        template that drifted from ArcadeDB's actual wording would quietly
        turn the whole derived corpus into a test of the template.
        """
        assert (
            _duplicate_key_message(_ALIAS_CLAIM_LABEL, _ALIAS_CLAIM_KEY_PROPERTY)
            == ARCADEDB_LOST_RACE
        )

    def test_the_neo4j_lost_race_is_recognised(self) -> None:
        """Neo4j normally blocks rather than raising, so this is defence.

        The two backends share this code path, and the driver documents
        ``MERGE`` under a uniqueness constraint as retryable on constraint
        violation. Recognising both spellings costs one tuple entry; missing
        Neo4j's would turn a retryable race into a hard alias-bind failure.
        """
        assert _is_alias_claim_contention(_client_error(NEO4J_LOST_RACE))

    def test_only_the_claim_index_counts_as_contention(self) -> None:
        """Every *other* unique index in the shipped DDL is a negative control.

        Derived rather than listed: a constraint added later is swept in
        automatically, and a roster cannot rot into agreeing with itself.
        """
        indexes = _unique_indexes()
        assert len(indexes) >= 3, "the unique-constraint DDL shrank; re-read it"
        assert (_ALIAS_CLAIM_LABEL, _ALIAS_CLAIM_KEY_PROPERTY) in indexes

        recognised = [
            (label, prop)
            for label, prop in indexes
            if _is_alias_claim_contention(
                _client_error(_duplicate_key_message(label, prop))
            )
        ]
        assert recognised == [(_ALIAS_CLAIM_LABEL, _ALIAS_CLAIM_KEY_PROPERTY)]

    @pytest.mark.parametrize(
        ("label", "prop"),
        [
            # Shares the property, different label: a second index on
            # ``claim_key`` elsewhere in the schema.
            ("Alias", _ALIAS_CLAIM_KEY_PROPERTY),
            # Shares the label, different property: a second unique column on
            # the claim row itself.
            (_ALIAS_CLAIM_LABEL, "tenant_key"),
            # Shares a *prefix* of each — substring matching is the mechanism,
            # so a neighbouring name is the obvious way it goes wrong.
            ("AliasClaimAudit", "claim_key_hash"),
        ],
    )
    def test_a_sibling_index_is_not_contention(self, label: str, prop: str) -> None:
        """The synthetic evasion corpus, and the only thing that can fail here.

        Each of these shares exactly one of the two markers, so a predicate
        that checks either half alone accepts it — and retrying would swallow
        a genuine duplicate-key defect (a ULID collision on ``version_id``
        being the live example) and then write the colliding row anyway.
        None can arise from today's DDL, which is precisely why the live
        ArcadeDB contract cannot catch this and these cases must be built.
        """
        assert (label, prop) not in _unique_indexes(), (
            "this decoy became real DDL — it is no longer a negative control"
        )
        assert not _is_alias_claim_contention(
            _client_error(_duplicate_key_message(label, prop))
        )

    def test_an_unrelated_driver_error_is_not_contention(self) -> None:
        assert not _is_alias_claim_contention(ServiceUnavailable("no route to host"))

    def test_the_claim_label_alone_is_not_contention(self) -> None:
        """A message naming the claim but reporting some other failure.

        Without the unique-violation marker the predicate would retry any
        error that happened to mention the claim — including a syntax error
        in the claim's own Cypher, which retrying cannot fix.
        """
        assert not _is_alias_claim_contention(
            _client_error(
                f"Invalid input near '{_ALIAS_CLAIM_LABEL}' "
                f"({_ALIAS_CLAIM_KEY_PROPERTY})"
            )
        )

    def test_the_markers_are_derived_from_the_schema(self) -> None:
        """The predicate matches a *message*, so its constants must not drift.

        ArcadeDB gives no error code worth keying on — it reports the
        duplicate under ``Neo.ClientError.Transaction.TransactionNotFound``,
        which is simply wrong — so the index's label and property are the only
        signal. Renaming either in the DDL without updating the predicate
        would silently stop the retry and take ``live-infra`` red again.
        """
        claim_ddl = [
            statement
            for statement in _DEFAULT_SCHEMA_STATEMENTS
            if _ALIAS_CLAIM_LABEL in statement and "UNIQUE" in statement
        ]
        assert len(claim_ddl) == 1, "the claim's uniqueness DDL moved or multiplied"
        assert f"(c:{_ALIAS_CLAIM_LABEL})" in claim_ddl[0]
        assert f"c.{_ALIAS_CLAIM_KEY_PROPERTY}" in claim_ddl[0]


class TestTheRetryRunner:
    """How many times, and for which failures."""

    @staticmethod
    def _store_raising(*errors: BaseException | None) -> tuple[Any, list[int]]:
        """A store whose Nth write raises ``errors[N]`` (``None`` succeeds).

        Returns the store and a list recording one entry per attempt, so a
        test can assert the *number* of transactions rather than inferring it
        from the outcome — which is what separates "did not retry" from
        "retried and failed the same way".
        """
        attempts: list[int] = []
        store = BoltOpenCypherGraphStore.__new__(BoltOpenCypherGraphStore)
        store._database = "test"  # type: ignore[attr-defined]
        store._owns_driver = False  # type: ignore[attr-defined]

        def _execute_write(tx_fn: Any) -> str:
            index = len(attempts)
            attempts.append(index)
            error = errors[index] if index < len(errors) else None
            if error is not None:
                raise error
            return "bound"

        session = MagicMock()
        session.execute_write.side_effect = _execute_write
        driver = MagicMock()
        driver.session.return_value.__enter__.return_value = session
        store._driver = driver  # type: ignore[attr-defined]
        return store, attempts

    def test_a_lost_race_is_retried_and_then_succeeds(self) -> None:
        store, attempts = self._store_raising(_client_error(ARCADEDB_LOST_RACE))

        assert store._write_alias_claim(MagicMock()) == "bound"
        assert len(attempts) == 2

    def test_a_win_costs_exactly_one_transaction(self) -> None:
        """The retry must be invisible to the uncontended path, which is
        every alias bind on a single-writer deployment.
        """
        store, attempts = self._store_raising()

        assert store._write_alias_claim(MagicMock()) == "bound"
        assert len(attempts) == 1

    def test_another_server_error_is_not_retried(self) -> None:
        """A ``Neo4jError`` that is not contention must propagate untouched.

        This is the case that exercises the guard: the retry re-runs the whole
        transaction body, which is only safe because a *lost race* rolled back
        whole, and nothing else earns that assumption. Note it has to be a
        server-side error — a transport failure never reaches the guard at all
        (see below), so using one here would test nothing.
        """
        store, attempts = self._store_raising(
            _client_error(_duplicate_key_message("Node", "version_id"))
        )

        with pytest.raises(ClientError):
            store._write_alias_claim(MagicMock())
        assert len(attempts) == 1

    def test_a_transport_failure_is_not_caught_at_all(self) -> None:
        """``_DRIVER_ERRORS`` is server errors only, deliberately.

        ``ServiceUnavailable`` is a ``DriverError``, not a ``Neo4jError``, so
        it never reaches the predicate. Pinned because it looks like an
        oversight and is not: a session that lost its connection has no
        rollback guarantee to retry on.
        """
        assert not issubclass(ServiceUnavailable, Neo4jError)
        store, attempts = self._store_raising(ServiceUnavailable("backend down"))

        with pytest.raises(ServiceUnavailable):
            store._write_alias_claim(MagicMock())
        assert len(attempts) == 1

    def test_the_attempt_budget_is_finite_and_the_last_loss_propagates(
        self,
    ) -> None:
        """Losing every attempt raises rather than looping or returning None.

        Not reachable by contention — once any winner commits, every later
        ``MERGE`` matches — so this pins the behaviour for a backend that
        raises the same message for some other reason.
        """
        store, attempts = self._store_raising(
            *([_client_error(ARCADEDB_LOST_RACE)] * _CLAIM_CONTENTION_ATTEMPTS)
        )

        with pytest.raises(ClientError):
            store._write_alias_claim(MagicMock())
        assert len(attempts) == _CLAIM_CONTENTION_ATTEMPTS

    def test_the_driver_error_tuple_is_populated_when_neo4j_is_installed(
        self,
    ) -> None:
        """``except ()`` is the correct degradation, but only without a driver.

        The import guard falls back to an empty tuple so the module still
        loads without the optional ``neo4j`` extra — where there is no driver,
        hence no store, hence no reachable path. This module is skipped
        without that extra, so here the tuple must have teeth.
        """
        assert _DRIVER_ERRORS
        assert all(issubclass(exc, BaseException) for exc in _DRIVER_ERRORS)


class TestBothAliasWritersUseTheRunner:
    """The contract test contends on one writer; two share the claim row."""

    @pytest.mark.parametrize("name", ["upsert_alias", "bind_alias_if_absent"])
    def test_neither_alias_writer_opens_its_own_session(self, name: str) -> None:
        """``upsert_alias`` MERGEs the same unique ``AliasClaim`` key as
        ``bind_alias_if_absent`` and so meets the identical race — it simply
        has no contract test contending on it, which is how the second site
        went unnoticed. Both must route through the retry; a writer that opens
        its own session is one the retry cannot reach.
        """
        source = inspect.getsource(getattr(BoltOpenCypherGraphStore, name))
        assert f"MERGE (c:{_ALIAS_CLAIM_LABEL}" in source, (
            f"{name} no longer claims — this test is watching the wrong method"
        )
        assert "_write_alias_claim" in source, f"{name} bypasses the retry"
        assert "execute_write" not in source, f"{name} runs its own session"


class TestExhaustionIsObservable:
    """If the budget ever runs out, something is wrong with the premise."""

    def test_exhausting_the_budget_warns(self) -> None:
        """The only evidence a rare exhaustion will ever leave.

        The per-attempt retry lines are ``debug``, which fires under no
        shipped log configuration (the CLI pins ``WARNING``, the MCP server
        filters below it) — so an exhausted budget would otherwise surface as
        a bare driver error with nothing saying it was a contention loss after
        three tries. Observed once against live ArcadeDB and never reproduced
        in 44 subsequent runs; this is what makes the next one diagnosable.
        """
        store, _ = TestTheRetryRunner._store_raising(
            *([_client_error(ARCADEDB_LOST_RACE)] * _CLAIM_CONTENTION_ATTEMPTS)
        )

        with capture_logs() as logs, pytest.raises(ClientError):
            store._write_alias_claim(MagicMock())

        exhausted = [
            entry
            for entry in logs
            if entry["event"] == "alias_claim_contention_exhausted"
        ]
        assert len(exhausted) == 1
        assert exhausted[0]["log_level"] == "warning"
        assert exhausted[0]["attempts"] == _CLAIM_CONTENTION_ATTEMPTS

    def test_an_unrelated_final_failure_does_not_warn(self) -> None:
        """The warning names contention, so it must not fire on anything else.

        Reachable only on the last attempt, where a non-contention error is
        caught by the same ``except`` and re-raised.
        """
        store, _ = TestTheRetryRunner._store_raising(
            _client_error(ARCADEDB_LOST_RACE),
            _client_error(ARCADEDB_LOST_RACE),
            _client_error(_duplicate_key_message("Node", "version_id")),
        )

        with capture_logs() as logs, pytest.raises(ClientError):
            store._write_alias_claim(MagicMock())

        assert not [e for e in logs if e["event"] == "alias_claim_contention_exhausted"]
