"""Unit tests for the Neo4jGraphStore bulk upserts, through a mock driver.

Most cover upsert_nodes_bulk's fast-path branching.

The bulk path measured at ~45 nodes/sec on AuraDB Free with the
``OPTIONAL MATCH`` shape, vs ~3281 nodes/sec for a CREATE-only UNWIND
in the loader script (raw driver, bypassing the store). The store now
branches on whether the pre-fetch found any prior current rows: empty
pre-fetch ⇒ skip the OPTIONAL MATCH and emit a CREATE-only UNWIND.

These tests assert on the **generated Cypher** via a mock driver. Live
throughput is verified separately (see PR description); the unit-level
guarantee here is that the branching picks the right shape.

``TestUpsertNodesBulkRepeatedId`` counts round trips instead: a repeated
``node_id`` is refused before the store opens a session.
``TestUpsertEdgesBulkDroppedRow`` checks the message for a row the write
dropped though both its endpoints read as current afterwards.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

pytest.importorskip("neo4j")


def _build_store_with_mock_driver(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[object, MagicMock]:
    """Construct Neo4jGraphStore with a mock driver; return (store, session_mock).

    The session mock captures all calls so the test can inspect the
    Cypher passed to ``execute_read`` (pre-fetch) and ``execute_write``
    (the bulk write).
    """
    monkeypatch.setattr(
        "trellis.stores.neo4j.graph.Neo4jGraphStore._init_schema",
        lambda self: None,
    )

    from trellis.stores.neo4j.graph import Neo4jGraphStore

    driver = MagicMock(name="driver")
    session = MagicMock(name="session")
    driver.session.return_value.__enter__.return_value = session

    store = Neo4jGraphStore("bolt://x", user="u", driver=driver)
    return store, session


def _captured_write_cypher(session: MagicMock) -> str:
    """Pull the Cypher string out of the last execute_write call."""
    fn = session.execute_write.call_args.args[0]
    tx = MagicMock(name="tx")
    tx.run.return_value.consume.return_value = None
    fn(tx)
    return str(tx.run.call_args.args[0])


class TestUpsertNodesBulkFastPath:
    def test_fresh_batch_uses_create_only_cypher(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pre-fetch returns nothing ⇒ no OPTIONAL MATCH in the write Cypher."""
        store, session = _build_store_with_mock_driver(monkeypatch)

        # Pre-fetch returns no existing roles for any of these node_ids.
        session.execute_read.return_value = []

        store.upsert_nodes_bulk(
            [
                {"node_id": "fresh-a", "node_type": "doc", "properties": {}},
                {"node_id": "fresh-b", "node_type": "doc", "properties": {}},
            ]
        )

        cypher = _captured_write_cypher(session)
        assert "OPTIONAL MATCH" not in cypher, (
            "fresh batch should skip OPTIONAL MATCH for the speedup"
        )
        assert "CREATE (n:Node)" in cypher
        # Single SET — created_at is included in row.props from Python,
        # so the hot path doesn't need a follow-up SET to override it.
        assert cypher.count("SET ") == 1, (
            "fast path should emit one SET per row (loader-equivalent)"
        )

    def test_overlapping_batch_keeps_optional_match(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pre-fetch finds a prior row ⇒ keep OPTIONAL MATCH for SCD-2 semantics."""
        store, session = _build_store_with_mock_driver(monkeypatch)

        # Pre-fetch returns one existing full row (``RETURN n`` shape) —
        # same node_id as the second input row but with *different*
        # content, so SCD-2 must close-and-recreate that one (identical
        # content would be skipped as a version-preserving no-op, #195).
        session.execute_read.return_value = [
            {
                "n": {
                    "node_id": "existing-b",
                    "node_type": "doc",
                    "node_role": "semantic",
                    "properties_json": '{"v": 1}',
                }
            }
        ]

        store.upsert_nodes_bulk(
            [
                {"node_id": "fresh-a", "node_type": "doc", "properties": {}},
                {"node_id": "existing-b", "node_type": "doc", "properties": {}},
            ]
        )

        cypher = _captured_write_cypher(session)
        assert "OPTIONAL MATCH" in cypher, (
            "overlapping batch must keep OPTIONAL MATCH so SCD-2 closes the prior row"
        )
        assert "coalesce(old.created_at" in cypher

    def test_unchanged_reupsert_skips_write_entirely(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A re-upsert with identical content is a version-preserving
        no-op (#195): a fresh :Node row would strand the node's current
        edges on the closed old row, so no write may run at all."""
        store, session = _build_store_with_mock_driver(monkeypatch)

        session.execute_read.return_value = [
            {
                "n": {
                    "node_id": "same-a",
                    "node_type": "doc",
                    "node_role": "semantic",
                    "properties_json": "{}",
                }
            }
        ]

        ids = store.upsert_nodes_bulk(
            [{"node_id": "same-a", "node_type": "doc", "properties": {}}]
        )

        assert ids == ["same-a"]
        session.execute_write.assert_not_called()

    def test_role_immutability_check_runs_before_fast_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A role conflict in the overlap aborts before the write — same
        contract the slow path honours."""
        store, session = _build_store_with_mock_driver(monkeypatch)

        # Pre-fetch returns a structural row; user wants to upsert it
        # as semantic. The contract requires a precise per-row error.
        session.execute_read.return_value = [
            {
                "n": {
                    "node_id": "locked",
                    "node_type": "doc",
                    "node_role": "structural",
                    "properties_json": "{}",
                }
            }
        ]

        with pytest.raises(ValueError, match=r"upsert_nodes_bulk\[1\]"):
            store.upsert_nodes_bulk(
                [
                    {"node_id": "ok-a", "node_type": "doc", "properties": {}},
                    {
                        "node_id": "locked",
                        "node_type": "doc",
                        "properties": {},
                        "node_role": "semantic",
                    },
                ]
            )
        # Write must NOT have run.
        session.execute_write.assert_not_called()


def _batch(third_id: str) -> list[dict[str, object]]:
    return [
        {"node_id": "rt-a", "node_type": "doc", "properties": {}},
        {"node_id": "rt-b", "node_type": "doc", "properties": {}},
        {"node_id": third_id, "node_type": "doc", "properties": {}},
    ]


class TestUpsertNodesBulkRepeatedId:
    def test_a_repeated_id_is_refused_before_any_round_trip(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The refusal runs before the store opens a session, so neither the
        pre-fetch nor the write runs."""
        store, session = _build_store_with_mock_driver(monkeypatch)
        driver = store._driver  # type: ignore[attr-defined]
        opened = driver.session.call_count

        with pytest.raises(ValueError, match=r"upsert_nodes_bulk\[2\].*duplicate"):
            store.upsert_nodes_bulk(_batch("rt-a"))  # type: ignore[attr-defined]

        assert driver.session.call_count == opened
        session.execute_read.assert_not_called()
        session.execute_write.assert_not_called()

    def test_the_batch_without_the_repeat_runs_both_round_trips(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The control for the test above: the same counts see the session,
        the pre-fetch and the write when no id repeats."""
        store, session = _build_store_with_mock_driver(monkeypatch)
        driver = store._driver  # type: ignore[attr-defined]
        opened = driver.session.call_count
        session.execute_read.return_value = []

        store.upsert_nodes_bulk(_batch("rt-c"))  # type: ignore[attr-defined]

        assert driver.session.call_count == opened + 1
        assert session.execute_read.call_count == 1
        assert session.execute_write.call_count == 1


class TestUpsertEdgesBulkDroppedRow:
    def test_a_dropped_row_whose_endpoints_read_as_current_names_both(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The write returns no record for row 0, and the read that names the
        missing endpoint finds both current, as when one was re-created in
        between: the refusal names both rather than blaming one.

        The endpoint re-read now happens after the transaction has rolled
        back, via ``session.execute_read`` (the same round trip round trip 1
        uses), not via ``tx.run`` inside the aborting transaction — so
        ``session.execute_read`` answers for both the round-trip-1 check and
        this post-rollback read, and ``tx.run`` only ever sees the UNWIND.
        """
        store, session = _build_store_with_mock_driver(monkeypatch)
        current = [{"node_id": "a"}, {"node_id": "b"}]
        session.execute_read.return_value = current
        tx = MagicMock(name="tx")
        tx.run.return_value = []  # the UNWIND write drops row 0
        session.execute_write.side_effect = lambda fn: fn(tx)

        with pytest.raises(
            ValueError,
            match=r"upsert_edges_bulk\[0\]: source 'a' or target 'b' was not current",
        ):
            store.upsert_edges_bulk(  # type: ignore[attr-defined]
                [{"source_id": "a", "target_id": "b", "edge_type": "links_to"}]
            )
        # tx.run was called exactly once, for the UNWIND — the re-read no
        # longer runs inside the transaction.
        assert tx.run.call_count == 1


class TestUpsertEdgesBulkMissingEndpointMessageIsShared:
    def test_round_trip_1_and_the_post_rollback_read_report_identical_text(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Round trip 1's up-front check and the post-rollback re-read raise
        ``ValueError`` for the same (row index, endpoint, id) through one
        shared message helper, so the two can't drift apart. Drive both
        paths to a "source 'a' has no current version" refusal and compare
        the exact text."""
        store, session = _build_store_with_mock_driver(monkeypatch)

        # Round trip 1: "a" is missing from the start, so the call never
        # reaches the write.
        session.execute_read.return_value = [{"node_id": "b"}]
        with pytest.raises(ValueError) as round_trip_1:
            store.upsert_edges_bulk(  # type: ignore[attr-defined]
                [{"source_id": "a", "target_id": "b", "edge_type": "links_to"}]
            )
        session.execute_write.assert_not_called()

        # Post-rollback: both endpoints read as current at round trip 1, the
        # write drops the row, and "a" reads as gone on the re-read that
        # follows the rollback — the same defect, caught one round trip
        # later.
        store2, session2 = _build_store_with_mock_driver(monkeypatch)
        session2.execute_read.side_effect = [
            [{"node_id": "a"}, {"node_id": "b"}],
            [{"node_id": "b"}],
        ]
        tx = MagicMock(name="tx")
        tx.run.return_value = []  # the UNWIND write drops the one row
        session2.execute_write.side_effect = lambda fn: fn(tx)
        with pytest.raises(ValueError) as post_rollback:
            store2.upsert_edges_bulk(  # type: ignore[attr-defined]
                [{"source_id": "a", "target_id": "b", "edge_type": "links_to"}]
            )

        assert str(round_trip_1.value) == str(post_rollback.value)
        assert str(round_trip_1.value) == (
            "upsert_edges_bulk[0]: source 'a' has no current version"
        )
