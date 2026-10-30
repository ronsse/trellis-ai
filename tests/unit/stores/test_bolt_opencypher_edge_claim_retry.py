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

import re
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from structlog.testing import capture_logs

pytest.importorskip("neo4j")

from trellis.stores.arcadedb.graph import ArcadeDBGraphStore
from trellis.stores.bolt_opencypher import graph as bolt_graph
from trellis.stores.bolt_opencypher.graph import (
    _ALIAS_CLAIM_RETRY_ATTEMPTS,
    _DEFAULT_SCHEMA_STATEMENTS,
    _EDGE_CLAIM_KEY_PROPERTY,
    _EDGE_CLAIM_LABEL,
    _EDGE_DUPLICATE_RERUN_ATTEMPTS,
    _EDGE_ENDPOINT_RETRY_BACKOFF_SECONDS,
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


def _ticking_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the store's clock a second later on every read.

    Two writes stamped by two reads are told apart, and a stamp reused
    across attempts shows as two equal ones.
    """
    ticks = iter(range(1, 1000))
    monkeypatch.setattr(
        bolt_graph,
        "utc_now",
        lambda: datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=next(ticks)),
    )


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


class TestUpsertEdgeCollapsesADuplicateCurrentRow:
    """Two writers of one new edge can both commit on ArcadeDB (#1078).

    The live create race (``bolt_edge_create_race``) caught it about once in
    2,500 reps on CI: both writers' transactions committed, ``EdgeClaim``
    row included, with no error on either side, and the triplet was left with
    two current rows. Neither writer saw the other's row, so each took its own
    candidate ``edge_id``. The fake driver forces that interleaving by letting
    the first write return normally and the post-commit re-read count two
    current rows; the ArcadeDB store must then re-run the write, which closes
    every current row of the triplet before it creates one.
    """

    def test_two_current_rows_after_a_clean_write_are_re_run(self) -> None:
        store = _make_arcadedb_store()
        execute_write = _arm_execute_write(
            store, [{"edge_id": "edge_a"}, {"edge_id": "edge_b"}]
        )
        store._count_current_edge_rows = MagicMock(side_effect=[2, 1])  # type: ignore[method-assign]

        with capture_logs() as logs:
            result = store.upsert_edge("src", "tgt", "race-rel", {})

        assert result == "edge_b"
        assert execute_write.call_count == 2
        assert store._count_current_edge_rows.call_args_list == [
            (("src", "tgt", "race-rel"),),
            (("src", "tgt", "race-rel"),),
        ]
        assert [e for e in logs if e["event"] == "edge_duplicate_current_rerun"] == [
            {
                "event": "edge_duplicate_current_rerun",
                "log_level": "warning",
                "reruns": 1,
                "resolved": True,
                "remaining": 1,
                "outcome": "resolved",
                "source_id": "src",
                "target_id": "tgt",
                "edge_type": "race-rel",
            }
        ]

    def test_a_duplicate_the_last_allowed_re_run_collapses_reads_resolved(self) -> None:
        """The recount after the last re-run is what makes ``resolved`` true."""
        store = _make_arcadedb_store()
        _arm_execute_write(
            store,
            [
                {"edge_id": f"edge_{i}"}
                for i in range(_EDGE_DUPLICATE_RERUN_ATTEMPTS + 1)
            ],
        )
        counts = [2] * _EDGE_DUPLICATE_RERUN_ATTEMPTS + [1]
        store._count_current_edge_rows = MagicMock(side_effect=counts)  # type: ignore[method-assign]

        with capture_logs() as logs:
            store.upsert_edge("src", "tgt", "race-rel", {})

        (warning,) = (e for e in logs if e["event"] == "edge_duplicate_current_rerun")
        assert warning["reruns"] == _EDGE_DUPLICATE_RERUN_ATTEMPTS
        assert warning["resolved"] is True
        assert warning["remaining"] == 1
        assert warning["outcome"] == "resolved"

    def test_one_current_row_is_not_re_run(self) -> None:
        store = _make_arcadedb_store()
        execute_write = _arm_execute_write(store, [{"edge_id": "edge_a"}])
        store._count_current_edge_rows = MagicMock(return_value=1)  # type: ignore[method-assign]

        with capture_logs() as logs:
            assert store.upsert_edge("src", "tgt", "race-rel", {}) == "edge_a"

        assert execute_write.call_count == 1
        store._count_current_edge_rows.assert_called_once_with("src", "tgt", "race-rel")
        assert not [e for e in logs if e["event"] == "edge_duplicate_current_rerun"]

    def test_a_triplet_that_stays_duplicated_stops_at_the_bound(self) -> None:
        """The bound is what keeps a busy triplet from spinning its caller."""
        store = _make_arcadedb_store()
        execute_write = _arm_execute_write(
            store,
            [
                {"edge_id": f"edge_{i}"}
                for i in range(_EDGE_DUPLICATE_RERUN_ATTEMPTS + 3)
            ],
        )
        store._count_current_edge_rows = MagicMock(return_value=2)  # type: ignore[method-assign]

        with capture_logs() as logs:
            result = store.upsert_edge("src", "tgt", "race-rel", {})

        assert execute_write.call_count == 1 + _EDGE_DUPLICATE_RERUN_ATTEMPTS
        assert result == f"edge_{_EDGE_DUPLICATE_RERUN_ATTEMPTS}"
        # The write has committed, so a duplicate that outlasts the bound is
        # reported, not raised, and the recount after the last re-run says so.
        (warning,) = (e for e in logs if e["event"] == "edge_duplicate_current_rerun")
        assert warning["reruns"] == _EDGE_DUPLICATE_RERUN_ATTEMPTS
        assert warning["resolved"] is False
        assert warning["remaining"] == 2
        assert warning["outcome"] == "unresolved"

    def test_a_re_run_that_finds_an_endpoint_gone_keeps_the_committed_edge(
        self,
    ) -> None:
        store = _make_arcadedb_store()
        execute_write = _arm_execute_write(store, [{"edge_id": "edge_a"}, None])
        store._count_current_edge_rows = MagicMock(return_value=2)  # type: ignore[method-assign]
        store._endpoints_have_a_version_row = MagicMock(return_value=False)  # type: ignore[method-assign]

        with capture_logs() as logs:
            assert store.upsert_edge("src", "tgt", "race-rel", {}) == "edge_a"

        assert execute_write.call_count == 2
        (warning,) = (e for e in logs if e["event"] == "edge_duplicate_current_rerun")
        assert warning["outcome"] == "endpoint_missing"
        assert warning["resolved"] is False

    def test_a_re_run_waits_out_an_endpoint_that_is_momentarily_unreadable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A concurrent re-version can hide an endpoint's new row mid-commit.

        The re-run goes through the same bounded backoff as the first write,
        so one empty read does not leave the duplicate behind.
        """
        sleeps: list[float] = []
        monkeypatch.setattr(bolt_graph.time, "sleep", sleeps.append)
        store = _make_arcadedb_store()
        store._endpoints_have_a_version_row = MagicMock(return_value=True)  # type: ignore[method-assign]
        execute_write = _arm_execute_write(
            store, [{"edge_id": "edge_a"}, None, {"edge_id": "edge_b"}]
        )
        store._count_current_edge_rows = MagicMock(side_effect=[2, 1])  # type: ignore[method-assign]

        with capture_logs() as logs:
            assert store.upsert_edge("src", "tgt", "race-rel", {}) == "edge_b"

        assert execute_write.call_count == 3
        assert sleeps == [bolt_graph._EDGE_ENDPOINT_RETRY_BACKOFF_SECONDS[0]]
        (warning,) = (e for e in logs if e["event"] == "edge_duplicate_current_rerun")
        assert warning["outcome"] == "resolved"

    def test_the_re_run_carries_the_earliest_current_rows_edge_id(self) -> None:
        """The first committer already returned its id, so the survivor keeps it.

        Writer A commits and counts one row, so A returns its own ``edge_id``.
        Writer B commits unseen, counts two, and re-runs: the re-run must
        carry A's id (the earlier ``valid_from``), not B's, or A's caller is
        left holding the id of a closed row. The first write keeps the shown
        row's pick, which the duplicate-heal contract pins.
        """
        store = _make_arcadedb_store()
        store._count_current_edge_rows = MagicMock(side_effect=[2, 1])  # type: ignore[method-assign]
        tx = MagicMock()
        tx.run.return_value.single.return_value = {"edge_id": "edge_a"}
        session = MagicMock()
        session.execute_write.side_effect = lambda fn: fn(tx)
        store._driver.session.return_value.__enter__.return_value = session  # type: ignore[attr-defined]

        store.upsert_edge("src", "tgt", "race-rel", {})

        first, rerun = (call.args[0] for call in tx.run.call_args_list)
        shown = f"{bolt_graph._shown_row('old_rows')} AS old"
        earliest = f"{bolt_graph._earliest_row('old_rows')} AS old"
        assert shown in first
        assert earliest not in first
        assert earliest in rerun
        assert shown not in rerun
        assert first.replace(shown, earliest) == rerun

    def test_the_earliest_row_selector_orders_by_valid_from_then_version(self) -> None:
        cypher = bolt_graph._earliest_row("rows")

        assert "r.valid_from < kept.valid_from" in cypher
        assert "r.version_id < kept.version_id" in cypher
        assert "r.valid_from > " not in cypher

    def test_a_re_run_stamps_a_later_time_than_the_write_it_follows(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The re-run closes rows a racer committed after the first ``now`` was read."""
        _ticking_clock(monkeypatch)
        store = _make_arcadedb_store()
        store._count_current_edge_rows = MagicMock(side_effect=[2, 1])  # type: ignore[method-assign]
        tx = MagicMock()
        tx.run.return_value.single.side_effect = [
            {"edge_id": "edge_a"},
            {"edge_id": "edge_b"},
        ]
        session = MagicMock()
        session.execute_write.side_effect = lambda fn: fn(tx)
        store._driver.session.return_value.__enter__.return_value = session  # type: ignore[attr-defined]

        assert store.upsert_edge("src", "tgt", "race-rel", {}) == "edge_b"

        first, rerun = (
            datetime.fromisoformat(call.kwargs["now"]) for call in tx.run.call_args_list
        )
        assert rerun > first

    def test_the_base_bolt_store_does_not_re_read(self) -> None:
        """Neo4j's ``MERGE`` blocks the second writer, so there is nothing to check."""
        assert BoltOpenCypherGraphStore.VERIFY_EDGE_WRITE_UNIQUENESS is False
        assert ArcadeDBGraphStore.VERIFY_EDGE_WRITE_UNIQUENESS is True
        store = _make_store()
        execute_write = _arm_execute_write(store, [{"edge_id": "edge_a"}])
        store._count_current_edge_rows = MagicMock(return_value=2)  # type: ignore[method-assign]

        assert store.upsert_edge("src", "tgt", "race-rel", {}) == "edge_a"

        assert execute_write.call_count == 1
        store._count_current_edge_rows.assert_not_called()

    def test_the_count_reads_current_rows_of_the_triplet_on_any_endpoint_row(
        self,
    ) -> None:
        store = _make_arcadedb_store()
        store._run_read_list = MagicMock(return_value=[{"n": 2}])  # type: ignore[method-assign]

        assert store._count_current_edge_rows("src", "tgt", "race-rel") == 2

        (cypher,), params = store._run_read_list.call_args
        assert "valid_to IS NULL" in cypher
        assert "count(e)" in cypher
        assert params == {
            "source_id": "src",
            "target_id": "tgt",
            "edge_type": "race-rel",
        }

    def test_an_empty_read_counts_zero(self) -> None:
        store = _make_arcadedb_store()
        store._run_read_list = MagicMock(return_value=[])  # type: ignore[method-assign]

        assert store._count_current_edge_rows("src", "tgt", "race-rel") == 0


class TestEndpointThatIsMomentarilyUnreadable:
    """An endpoint a concurrent ``upsert_node`` is re-versioning can read as absent.

    Measured against ``neo4j:2025.12``: right after a re-version commits, the
    edge write's current-row match can miss the new row for a millisecond
    or so while the closed row still reads, which is what the row probe
    relies on. The edge write then sees "no current version" for a node
    that never lacked one
    (``test_edge_write_racing_node_upsert_keeps_one_current_node`` in
    ``live-infra.yml``). A node with no row at all is a different answer
    and is refused at once, as above.
    """

    @staticmethod
    def _record_sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
        sleeps: list[float] = []
        monkeypatch.setattr(bolt_graph.time, "sleep", sleeps.append)
        return sleeps

    def test_a_none_row_is_re_run_when_both_endpoints_have_rows(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sleeps = self._record_sleeps(monkeypatch)
        store = _make_store()
        store._endpoints_have_a_version_row = MagicMock(return_value=True)  # type: ignore[method-assign]
        execute_write = _arm_execute_write(store, [None, {"edge_id": "edge_01"}])

        with capture_logs() as logs:
            assert store.upsert_edge("src", "tgt", "race-rel", {}) == "edge_01"

        assert execute_write.call_count == 2
        rereads = [e for e in logs if e["event"] == "edge_endpoint_reread"]
        assert rereads == [
            {
                "event": "edge_endpoint_reread",
                "log_level": "warning",
                "source_id": "src",
                "target_id": "tgt",
                "rereads": 1,
                "recovered": True,
            }
        ]
        assert sleeps == [_EDGE_ENDPOINT_RETRY_BACKOFF_SECONDS[0]]
        store._endpoints_have_a_version_row.assert_called_once_with("src", "tgt")

    def test_an_endpoint_that_stays_unreadable_raises_after_the_bound(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The bound is what separates a blip from a node that is gone."""
        sleeps = self._record_sleeps(monkeypatch)
        store = _make_store()
        store._endpoints_have_a_version_row = MagicMock(return_value=True)  # type: ignore[method-assign]
        attempts = 1 + len(_EDGE_ENDPOINT_RETRY_BACKOFF_SECONDS)
        execute_write = _arm_execute_write(store, [None] * (attempts + 2))

        with (
            capture_logs() as logs,
            pytest.raises(ValueError, match="no current version"),
        ):
            store.upsert_edge("src", "tgt", "race-rel", {})

        assert execute_write.call_count == attempts
        rereads = [e for e in logs if e["event"] == "edge_endpoint_reread"]
        assert [(e["rereads"], e["recovered"]) for e in rereads] == [
            (len(_EDGE_ENDPOINT_RETRY_BACKOFF_SECONDS), False)
        ]
        assert sleeps == list(_EDGE_ENDPOINT_RETRY_BACKOFF_SECONDS)

    def test_an_endpoint_with_no_row_at_all_is_refused_without_sleeping(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A dangling edge must not pay the retry's latency."""
        sleeps = self._record_sleeps(monkeypatch)
        store = _make_store()
        store._endpoints_have_a_version_row = MagicMock(return_value=False)  # type: ignore[method-assign]
        execute_write = _arm_execute_write(store, [None, {"edge_id": "edge_01"}])

        with pytest.raises(ValueError, match="no current version"):
            store.upsert_edge("src", "tgt", "race-rel", {})

        assert execute_write.call_count == 1
        assert sleeps == []

    def test_the_row_probe_asks_for_any_version_of_both_endpoints(self) -> None:
        """Closed rows count: during the blip the old row is all that reads."""
        store = _make_store()
        session = MagicMock()
        session.execute_read.return_value = [{"node_id": "src"}, {"node_id": "tgt"}]
        store._driver.session.return_value.__enter__.return_value = session  # type: ignore[attr-defined]

        assert store._endpoints_have_a_version_row("src", "tgt") is True

        tx = MagicMock()
        tx.run.return_value = []
        session.execute_read.call_args.args[0](tx)
        cypher = tx.run.call_args.args[0]
        assert "valid_to" not in cypher
        assert tx.run.call_args.kwargs == {"ids": ["src", "tgt"]}

    def test_the_row_probe_is_false_when_either_endpoint_has_none(self) -> None:
        store = _make_store()
        session = MagicMock()
        session.execute_read.return_value = [{"node_id": "src"}]
        store._driver.session.return_value.__enter__.return_value = session  # type: ignore[attr-defined]

        assert store._endpoints_have_a_version_row("src", "tgt") is False


class TestUpsertEdgeStampsEachAttempt:
    """A re-run stamps its own time, not the refused attempt's (#1002).

    Another writer can commit a newer version of the edge between a refused
    attempt and its re-run. The re-run closes that version with ``$now``, so
    a ``now`` kept from the first attempt would give it a ``valid_to``
    before its own ``valid_from``: an inverted interval that ``as_of``
    reads get wrong. Driven with a fake session that runs the real
    transaction function against a clock that ticks on every read.
    """

    @pytest.mark.parametrize(
        "refusal",
        [
            pytest.param(None, id="endpoint-read-as-absent"),
            pytest.param(_contention(), id="claim-race-lost"),
        ],
    )
    def test_a_re_run_sends_a_later_timestamp_than_the_refused_attempt(
        self, monkeypatch: pytest.MonkeyPatch, refusal: Any
    ) -> None:
        monkeypatch.setattr(bolt_graph.time, "sleep", lambda _seconds: None)
        _ticking_clock(monkeypatch)
        store = _make_store()
        store._endpoints_have_a_version_row = MagicMock(return_value=True)  # type: ignore[method-assign]
        tx = MagicMock()
        tx.run.return_value.single.side_effect = [refusal, {"edge_id": "edge_01"}]
        session = MagicMock()
        session.execute_write.side_effect = lambda fn: fn(tx)
        store._driver.session.return_value.__enter__.return_value = session  # type: ignore[attr-defined]

        assert store.upsert_edge("src", "tgt", "race-rel", {}) == "edge_01"

        sent = [call.kwargs for call in tx.run.call_args_list]
        assert len(sent) == 2
        refused, rerun = (datetime.fromisoformat(kw["now"]) for kw in sent)
        assert rerun > refused
        # Everything stamped from ``now`` moves with it: the version
        # created and the version closed share one time per attempt.
        for kwargs in sent:
            assert kwargs["base_props"]["valid_from"] == kwargs["now"]


class TestBulkEndpointThatIsMomentarilyUnreadable:
    """``upsert_edges_bulk`` re-runs a refusal the way ``upsert_edge`` does (#975).

    Its round trip 1 and its ``UNWIND`` read the endpoints as ``upsert_edge``
    does, so a concurrent ``upsert_node`` caught mid-commit can make either
    one refuse a node that never lacked a current row. A refused attempt
    writes nothing, so the whole attempt (round trip 1, then the write) is
    re-run, while the refused row's endpoints still have a row of some
    version. Driven with a fake session, not a timing race: the interleaving
    is the order of the scripted reads and writes.
    """

    EDGES = (
        {"source_id": "a", "target_id": "b", "edge_type": "links_to"},
        {"source_id": "c", "target_id": "d", "edge_type": "links_to"},
    )

    @staticmethod
    def _rows(*node_ids: str) -> list[dict[str, str]]:
        return [{"node_id": node_id} for node_id in node_ids]

    @staticmethod
    def _store(
        monkeypatch: pytest.MonkeyPatch,
        *,
        reads: list[Any],
        writes: list[Any],
        has_rows: bool | list[bool],
    ) -> tuple[BoltOpenCypherGraphStore, MagicMock, MagicMock, list[float]]:
        """A store whose session answers ``reads`` and ``writes`` in order.

        A write is a list of records the transaction function's one
        ``tx.run`` returns, so the real function runs, ``_MissingEdgeRowError``
        included. ``has_rows`` is what the row probe answers (a list: one
        answer per probe).
        """
        sleeps: list[float] = []
        monkeypatch.setattr(bolt_graph.time, "sleep", sleeps.append)
        store = _make_store()
        probe = MagicMock(
            side_effect=has_rows if isinstance(has_rows, list) else None,
            return_value=has_rows,
        )
        store._endpoints_have_a_version_row = probe  # type: ignore[method-assign]
        tx = MagicMock()
        tx.run.side_effect = writes
        session = MagicMock()
        session.execute_read.side_effect = reads
        session.execute_write.side_effect = lambda fn: fn(tx)
        # So a test can read what each attempt sent.
        session.tx = tx
        store._driver.session.return_value.__enter__.return_value = session  # type: ignore[attr-defined]
        return store, session, probe, sleeps

    def test_a_row_the_write_dropped_is_re_run_and_its_edge_ids_returned(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        both_rows = [
            {"row_index": 0, "edge_id": "e0"},
            {"row_index": 1, "edge_id": "e1"},
        ]
        store, session, probe, sleeps = self._store(
            monkeypatch,
            reads=[self._rows("a", "b", "c", "d")] * 2,
            writes=[[{"row_index": 1, "edge_id": "e1"}], both_rows],
            has_rows=True,
        )

        with capture_logs() as logs:
            result = store.upsert_edges_bulk(list(self.EDGES))

        assert result == ["e0", "e1"]
        assert session.execute_write.call_count == 2
        # Only the refused row's endpoints are probed.
        probe.assert_called_once_with("a", "b")
        assert sleeps == [_EDGE_ENDPOINT_RETRY_BACKOFF_SECONDS[0]]
        assert [e for e in logs if e["event"] == "edge_endpoint_reread"] == [
            {
                "event": "edge_endpoint_reread",
                "log_level": "warning",
                "rereads": 1,
                "recovered": True,
                "edges": 2,
            }
        ]

    def test_a_re_run_stamps_every_row_later_than_the_dropped_attempt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A re-run closes versions a racing writer committed since the
        dropped attempt, so it must not carry that attempt's ``valid_from``
        (#1002)."""
        _ticking_clock(monkeypatch)
        both_rows = [
            {"row_index": 0, "edge_id": "e0"},
            {"row_index": 1, "edge_id": "e1"},
        ]
        store, session, _probe, _sleeps = self._store(
            monkeypatch,
            reads=[self._rows("a", "b", "c", "d")] * 2,
            writes=[[{"row_index": 1, "edge_id": "e1"}], both_rows],
            has_rows=True,
        )

        assert store.upsert_edges_bulk(list(self.EDGES)) == ["e0", "e1"]

        sent = [call.kwargs["rows"] for call in session.tx.run.call_args_list]
        assert len(sent) == 2
        # One time per attempt, shared by every row it wrote.
        refused, rerun = (
            {datetime.fromisoformat(row["props"]["valid_from"]) for row in rows}
            for rows in sent
        )
        assert len(refused) == len(rerun) == 1
        assert min(rerun) > max(refused)

    def test_round_trip_1_reading_an_endpoint_as_absent_is_re_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The pre-check is a read too, so it has the window as well."""
        store, session, probe, sleeps = self._store(
            monkeypatch,
            reads=[self._rows("b", "c", "d"), self._rows("a", "b", "c", "d")],
            writes=[
                [{"row_index": 0, "edge_id": "e0"}, {"row_index": 1, "edge_id": "e1"}]
            ],
            has_rows=True,
        )

        assert store.upsert_edges_bulk(list(self.EDGES)) == ["e0", "e1"]

        # The first attempt never reached the write.
        assert session.execute_write.call_count == 1
        probe.assert_called_once_with("a", "b")
        assert sleeps == [_EDGE_ENDPOINT_RETRY_BACKOFF_SECONDS[0]]

    def test_a_row_that_stays_dropped_raises_after_the_bound(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        attempts = 1 + len(_EDGE_ENDPOINT_RETRY_BACKOFF_SECONDS)
        store, session, _probe, sleeps = self._store(
            monkeypatch,
            reads=[self._rows("a", "b", "c", "d")] * (attempts + 1),
            writes=[[{"row_index": 1, "edge_id": "e1"}]] * attempts,
            has_rows=True,
        )

        with (
            capture_logs() as logs,
            pytest.raises(
                ValueError,
                match=r"upsert_edges_bulk\[0\]: source 'a' or target 'b' was not cur",
            ),
        ):
            store.upsert_edges_bulk(list(self.EDGES))

        assert session.execute_write.call_count == attempts
        assert sleeps == list(_EDGE_ENDPOINT_RETRY_BACKOFF_SECONDS)
        rereads = [e for e in logs if e["event"] == "edge_endpoint_reread"]
        assert [(e["rereads"], e["recovered"]) for e in rereads] == [
            (len(_EDGE_ENDPOINT_RETRY_BACKOFF_SECONDS), False)
        ]

    def test_an_endpoint_read_as_absent_by_round_trip_1_raises_after_the_bound(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A node whose rows are all closed is refused, not waited on forever,
        and the write never runs."""
        attempts = 1 + len(_EDGE_ENDPOINT_RETRY_BACKOFF_SECONDS)
        store, session, _probe, sleeps = self._store(
            monkeypatch,
            reads=[self._rows("a", "c", "d")] * attempts,
            writes=[],
            has_rows=True,
        )

        with pytest.raises(
            ValueError,
            match=r"upsert_edges_bulk\[0\]: target 'b' has no current version",
        ):
            store.upsert_edges_bulk(list(self.EDGES))

        session.execute_write.assert_not_called()
        assert session.execute_read.call_count == attempts
        assert sleeps == list(_EDGE_ENDPOINT_RETRY_BACKOFF_SECONDS)

    def test_an_endpoint_with_no_row_at_all_is_refused_without_sleeping(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A dangling edge in a batch must not pay the re-run's latency."""
        store, session, probe, sleeps = self._store(
            monkeypatch,
            reads=[self._rows("a", "c", "d")],
            writes=[],
            has_rows=False,
        )

        with pytest.raises(ValueError, match="target 'b' has no current version"):
            store.upsert_edges_bulk(list(self.EDGES))

        probe.assert_called_once_with("a", "b")
        session.execute_write.assert_not_called()
        assert session.execute_read.call_count == 1
        assert sleeps == []

    def test_a_clean_first_attempt_is_not_re_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store, session, probe, sleeps = self._store(
            monkeypatch,
            reads=[self._rows("a", "b", "c", "d")],
            writes=[
                [{"row_index": 0, "edge_id": "e0"}, {"row_index": 1, "edge_id": "e1"}]
            ],
            has_rows=True,
        )

        with capture_logs() as logs:
            assert store.upsert_edges_bulk(list(self.EDGES)) == ["e0", "e1"]

        assert session.execute_write.call_count == 1
        probe.assert_not_called()
        assert sleeps == []
        assert [e for e in logs if e["event"] == "edge_endpoint_reread"] == []


class TestArcadeDBWidensEdgeClaimOverItsGenericErrorChannel:
    """The per-backend seam, kept narrow and logged.

    ArcadeDB's generic ``Neo.DatabaseError.General.UnknownError`` bucket is
    where a runtime fault on this package's fixed Cypher literal would
    surface (the affordability argument :meth:`_is_alias_write_contention`
    makes). No live trigger has been observed for ``EdgeClaim``
    specifically: the update-race shape this widening was once thought to
    cover (the claim's old ``_cas_lock`` touch of an *existing* row)
    measured as a plain no-op with no error at all, and the fix's real
    content write (``ec.claim_token``) conflicts on a different,
    already-retryable channel (``DeadlockDetected``) instead. Widening the
    shared predicate itself would still make a Neo4j deployment retry a
    genuine engine fault, which is fatal there and will never clear.
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

    def test_the_generic_bucket_match_logs_the_message_at_warning(self) -> None:
        """A real occurrence must be visible, not silently retried away."""
        store = _make_arcadedb_store()
        with capture_logs() as logs:
            assert store._is_edge_write_contention(_stale_record()) is True
        warnings = [log for log in logs if log["log_level"] == "warning"]
        assert len(warnings) == 1
        assert warnings[0]["event"] == "arcadedb_edge_claim_generic_bucket_contention"
        assert ARCADEDB_GENERIC_BUCKET_MESSAGE in warnings[0]["exc_message"]

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


class _RecordingTx:
    """Stand-in for ``ManagedTransaction`` that records every ``.run()``.

    Unlike ``_arm_execute_write`` above (which replaces
    ``session.execute_write`` wholesale and never invokes the real ``_tx``
    closure, letting a mutant that deletes the whole claim prelude from
    ``upsert_edge``'s Cypher survive every test in this file), this fake
    actually calls the closure and captures the Cypher text and bound
    parameters it sends — the only way to pin the claim write's content
    against a mutant that deletes the whole prelude.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def run(self, cypher: str, **params: Any) -> Any:
        self.calls.append((cypher, params))
        return _SingleResult({"edge_id": "edge_01"})


class _SingleResult:
    def __init__(self, record: dict[str, Any]) -> None:
        self._record = record

    def single(self) -> dict[str, Any]:
        return self._record


class _RecordingSession:
    """Runs ``execute_write``'s function against one ``_RecordingTx``.

    ``fail_times`` simulates a commit-time conflict discovered *after*
    the transaction function already ran (as a real lost claim race
    would): the Cypher is still sent and recorded, but the attempt as a
    whole is reported as failed, so ``_execute_edge_claim_write`` retries
    by calling the closure again.
    """

    def __init__(self, tx: _RecordingTx, fail_times: int = 0) -> None:
        self._tx = tx
        self._fail_times = fail_times
        self._attempts = 0

    def execute_write(self, fn: Any) -> Any:
        self._attempts += 1
        result = fn(self._tx)
        if self._attempts <= self._fail_times:
            raise _contention()
        return result

    def __enter__(self) -> _RecordingSession:
        return self

    def __exit__(self, *exc_info: Any) -> bool:
        return False


def _make_recording_store(session: _RecordingSession) -> BoltOpenCypherGraphStore:
    store = _make_store()
    store._driver.session.return_value = session  # type: ignore[attr-defined]
    return store


class TestClaimWritePrimitiveIsPinned:
    """A test that fails when the claim prelude is deleted, or when its
    write stops being unconditional.

    A mutant that deletes the ``MERGE (ec:EdgeClaim ...) SET
    ec.claim_token = ...`` block entirely fails this test (the mock-only
    tests above keep passing because they never look at the Cypher text
    at all). So does a mutant that moves the token write onto
    ``ON CREATE SET`` (making it fire only when the claim is first
    created, not on every upsert of an already-claimed triplet) or that
    follows it with ``REMOVE ec.claim_token`` (recreating the no-op
    touch this rework replaced). Both of those changes read as
    reasonable optimizations and are invisible to every other test in
    this file; live, both measurably reopen the race this claim exists
    to close.
    """

    def test_claim_prelude_is_sent_with_a_fresh_token_each_call(self) -> None:
        tx = _RecordingTx()
        store = _make_recording_store(_RecordingSession(tx))

        first = store.upsert_edge("src", "tgt", "race-rel", {})
        second = store.upsert_edge("src", "tgt", "race-rel", {})

        assert first == "edge_01"
        assert second == "edge_01"
        assert len(tx.calls) == 2
        tokens = []
        for cypher, params in tx.calls:
            assert "MERGE (ec:EdgeClaim {claim_key: $edge_claim_key})" in cypher
            assert "ec.claim_token = $edge_claim_token" in cypher
            assert re.search(
                r"MERGE \(ec:EdgeClaim \{claim_key: \$edge_claim_key\}\)\s*\n\s*"
                r"SET ec\.claim_token = \$edge_claim_token\b",
                cypher,
            ), (
                "the token write must be an unconditional SET right after the "
                "MERGE, not ON CREATE SET"
            )
            assert "REMOVE ec.claim_token" not in cypher, (
                "the claim write must not remove the token it just set -- "
                "that recreates the no-op SET/REMOVE touch this rework replaced"
            )
            assert "edge_claim_token" in params
            tokens.append(params["edge_claim_token"])
        assert tokens[0] != tokens[1]

    def test_a_driver_level_retry_of_one_call_also_gets_a_fresh_token(self) -> None:
        """The token is generated inside ``_tx``, not once per call.

        A driver-level retry of ``DeadlockDetected`` re-invokes the
        ``_tx`` closure directly (module docstring); this simulates that
        by failing the first attempt of a single ``upsert_edge`` call
        after its Cypher already ran, and checking the second attempt's
        recorded token differs from the first's.
        """
        tx = _RecordingTx()
        store = _make_recording_store(_RecordingSession(tx, fail_times=1))

        result = store.upsert_edge("src", "tgt", "race-rel", {})

        assert result == "edge_01"
        assert len(tx.calls) == 2
        tokens = [params["edge_claim_token"] for _, params in tx.calls]
        assert tokens[0] != tokens[1]
