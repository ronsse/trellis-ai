"""Unit tests for the Neo4jGraphStore.upsert_nodes_bulk fast-path branching.

The bulk path measured at ~45 nodes/sec on AuraDB Free with the
``OPTIONAL MATCH`` shape, vs ~3281 nodes/sec for a CREATE-only UNWIND
in the loader script (raw driver, bypassing the store). The store now
branches on whether the pre-fetch found any prior current rows: empty
pre-fetch ⇒ skip the OPTIONAL MATCH and emit a CREATE-only UNWIND.

These tests assert on the **generated Cypher** via a mock driver. Live
throughput is verified separately (see PR description); the unit-level
guarantee here is that the branching picks the right shape.
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

        # Pre-fetch returns one existing row — same node_id as the
        # second input row, so SCD-2 must close-and-recreate that one.
        session.execute_read.return_value = [
            {"node_id": "existing-b", "node_role": "semantic"}
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

    def test_role_immutability_check_runs_before_fast_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A role conflict in the overlap aborts before the write — same
        contract the slow path honours."""
        store, session = _build_store_with_mock_driver(monkeypatch)

        # Pre-fetch returns a structural row; user wants to upsert it
        # as semantic. The contract requires a precise per-row error.
        session.execute_read.return_value = [
            {"node_id": "locked", "node_role": "structural"}
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
