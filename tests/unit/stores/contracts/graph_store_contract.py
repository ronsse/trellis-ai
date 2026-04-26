"""GraphStore contract test suite — runs against every backend.

Per ``docs/design/adr-canonical-graph-layer.md`` §3, this base class
defines the shared semantics that every ``GraphStore`` backend must
honour. Backend-specific test files (``test_sqlite_graph_contract.py``
etc.) subclass :class:`GraphStoreContractTests` and provide a
``store_factory`` fixture.

The harness deliberately:

* Does **not** test backend-specific schema / index / migration
  behaviour — those tests live in the per-backend ``test_<backend>_*``
  files and stay where they are. The contract suite is *additive*.
* Uses only the public ``GraphStore`` ABC surface — no
  ``_driver`` / ``conn`` / ``_database`` attribute access. If the
  contract needs something the ABC doesn't expose, the ABC needs
  the missing method, not the harness.
* Sleeps briefly between SCD-2 versioning operations that the
  test cares about ordering for — Postgres ``TIMESTAMPTZ`` and
  SQLite ISO strings have similar resolution but the tests should
  not depend on sub-millisecond precision.

Subclass shape::

    class TestSQLiteGraphContract(GraphStoreContractTests):
        @pytest.fixture
        def store(self, tmp_path):
            store = SQLiteGraphStore(tmp_path / "graph.db")
            yield store
            store.close()
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from trellis.stores.base.graph import GraphStore


def _sleep_for_ordering() -> None:
    """Sleep long enough that two SCD-2 operations get distinct timestamps."""
    time.sleep(0.005)


class GraphStoreContractTests:
    """Contract tests every ``GraphStore`` backend must pass.

    Subclasses must provide a pytest fixture named ``store`` that
    yields a fresh, empty :class:`~trellis.stores.base.graph.GraphStore`
    instance and tears it down afterwards.
    """

    # ------------------------------------------------------------------
    # upsert_node / get_node — basic CRUD
    # ------------------------------------------------------------------

    def test_upsert_node_returns_id_when_id_omitted(self, store: GraphStore) -> None:
        nid = store.upsert_node(None, "service", {"name": "auth"})
        assert isinstance(nid, str)
        assert nid

    def test_upsert_node_uses_explicit_id_when_provided(
        self, store: GraphStore
    ) -> None:
        nid = store.upsert_node("explicit_id", "service", {})
        assert nid == "explicit_id"

    def test_get_node_returns_full_dict(self, store: GraphStore) -> None:
        store.upsert_node("n1", "service", {"name": "auth", "tier": 1})
        node = store.get_node("n1")
        assert node is not None
        assert node["node_id"] == "n1"
        assert node["node_type"] == "service"
        assert node["properties"] == {"name": "auth", "tier": 1}

    def test_get_node_returns_none_for_missing(self, store: GraphStore) -> None:
        assert store.get_node("does_not_exist") is None

    def test_get_node_default_role_is_semantic(self, store: GraphStore) -> None:
        store.upsert_node("n1", "service", {})
        node = store.get_node("n1")
        assert node is not None
        assert node["node_role"] == "semantic"

    def test_get_node_includes_document_ids_as_list(self, store: GraphStore) -> None:
        # Contract: document_ids is always a list (possibly empty), never None,
        # so consumers can iterate unconditionally.
        store.upsert_node("n1", "service", {})
        node = store.get_node("n1")
        assert node is not None
        assert isinstance(node["document_ids"], list)

    # ------------------------------------------------------------------
    # SCD Type 2 versioning
    # ------------------------------------------------------------------

    def test_update_node_returns_latest_version(self, store: GraphStore) -> None:
        store.upsert_node("n1", "service", {"v": 1})
        _sleep_for_ordering()
        store.upsert_node("n1", "service", {"v": 2})
        node = store.get_node("n1")
        assert node is not None
        assert node["properties"]["v"] == 2

    def test_update_node_preserves_node_id(self, store: GraphStore) -> None:
        store.upsert_node("n1", "service", {"v": 1})
        _sleep_for_ordering()
        ret = store.upsert_node("n1", "service", {"v": 2})
        assert ret == "n1"

    def test_history_includes_all_versions_newest_first(
        self, store: GraphStore
    ) -> None:
        store.upsert_node("n1", "service", {"v": 1})
        _sleep_for_ordering()
        store.upsert_node("n1", "service", {"v": 2})
        _sleep_for_ordering()
        store.upsert_node("n1", "service", {"v": 3})
        history = store.get_node_history("n1")
        assert len(history) == 3
        assert [h["properties"]["v"] for h in history] == [3, 2, 1]

    def test_history_marks_only_latest_as_current(self, store: GraphStore) -> None:
        store.upsert_node("n1", "service", {"v": 1})
        _sleep_for_ordering()
        store.upsert_node("n1", "service", {"v": 2})
        history = store.get_node_history("n1")
        # Newest has valid_to=None; older versions have valid_to set.
        assert history[0]["valid_to"] is None
        assert history[1]["valid_to"] is not None

    # ------------------------------------------------------------------
    # as_of time-travel reads
    # ------------------------------------------------------------------

    def test_get_node_as_of_returns_version_valid_at_time(
        self, store: GraphStore
    ) -> None:
        store.upsert_node("n1", "service", {"v": 1})
        _sleep_for_ordering()
        # Capture time strictly between the two versions.
        between = _now()
        _sleep_for_ordering()
        store.upsert_node("n1", "service", {"v": 2})
        node = store.get_node("n1", as_of=between)
        assert node is not None
        assert node["properties"]["v"] == 1

    def test_get_node_as_of_returns_none_before_creation(
        self, store: GraphStore
    ) -> None:
        before = _now()
        _sleep_for_ordering()
        store.upsert_node("n1", "service", {})
        assert store.get_node("n1", as_of=before) is None

    # ------------------------------------------------------------------
    # query
    # ------------------------------------------------------------------

    def test_query_filters_by_node_type(self, store: GraphStore) -> None:
        store.upsert_node("a", "service", {})
        store.upsert_node("b", "person", {})
        store.upsert_node("c", "service", {})
        results = store.query(node_type="service")
        assert len(results) == 2
        assert {r["node_id"] for r in results} == {"a", "c"}

    def test_query_returns_empty_for_unknown_type(self, store: GraphStore) -> None:
        store.upsert_node("a", "service", {})
        assert store.query(node_type="ghost_type") == []

    def test_query_filters_by_scalar_property_eq(self, store: GraphStore) -> None:
        store.upsert_node("a", "service", {"team": "platform"})
        store.upsert_node("b", "service", {"team": "growth"})
        results = store.query(node_type="service", properties={"team": "platform"})
        assert len(results) == 1
        assert results[0]["node_id"] == "a"

    def test_query_respects_limit(self, store: GraphStore) -> None:
        for i in range(5):
            store.upsert_node(f"n{i}", "service", {})
        results = store.query(node_type="service", limit=3)
        assert len(results) == 3

    # ------------------------------------------------------------------
    # bulk read
    # ------------------------------------------------------------------

    def test_get_nodes_bulk_returns_requested_nodes(self, store: GraphStore) -> None:
        store.upsert_node("a", "service", {})
        store.upsert_node("b", "service", {})
        store.upsert_node("c", "service", {})
        results = store.get_nodes_bulk(["a", "c"])
        assert {r["node_id"] for r in results} == {"a", "c"}

    def test_get_nodes_bulk_skips_missing(self, store: GraphStore) -> None:
        store.upsert_node("a", "service", {})
        results = store.get_nodes_bulk(["a", "ghost"])
        assert {r["node_id"] for r in results} == {"a"}

    # ------------------------------------------------------------------
    # bulk write — nodes
    # ------------------------------------------------------------------

    def test_upsert_nodes_bulk_creates_all_rows(self, store: GraphStore) -> None:
        ids = store.upsert_nodes_bulk(
            [
                {"node_id": "a", "node_type": "service", "properties": {"v": 1}},
                {"node_id": "b", "node_type": "service", "properties": {"v": 2}},
                {"node_id": "c", "node_type": "service", "properties": {"v": 3}},
            ]
        )
        assert ids == ["a", "b", "c"]
        assert store.get_node("a") is not None
        assert store.get_node("b")["properties"]["v"] == 2
        assert store.get_node("c") is not None

    def test_upsert_nodes_bulk_empty_list_is_noop(self, store: GraphStore) -> None:
        assert store.upsert_nodes_bulk([]) == []

    def test_upsert_nodes_bulk_assigns_ids_when_missing(
        self, store: GraphStore
    ) -> None:
        ids = store.upsert_nodes_bulk(
            [
                {"node_type": "service", "properties": {}},
                {"node_type": "service", "properties": {}},
            ]
        )
        assert len(ids) == 2
        assert all(isinstance(i, str) and i for i in ids)
        assert ids[0] != ids[1]

    def test_upsert_nodes_bulk_updates_existing_creates_new_version(
        self, store: GraphStore
    ) -> None:
        """Bulk update of an existing node closes the old version + creates new."""
        store.upsert_node("n1", "service", {"v": 1})
        _sleep_for_ordering()
        store.upsert_nodes_bulk(
            [{"node_id": "n1", "node_type": "service", "properties": {"v": 2}}]
        )
        node = store.get_node("n1")
        assert node is not None
        assert node["properties"]["v"] == 2
        history = store.get_node_history("n1")
        assert len(history) == 2

    def test_upsert_nodes_bulk_validates_role_immutability(
        self, store: GraphStore
    ) -> None:
        """Role change between versions still raises in bulk mode."""
        store.upsert_node("n1", "service", {}, node_role="semantic")
        _sleep_for_ordering()
        with pytest.raises(ValueError, match="role"):
            store.upsert_nodes_bulk(
                [
                    {
                        "node_id": "n1",
                        "node_type": "service",
                        "properties": {},
                        "node_role": "structural",
                    }
                ]
            )

    def test_upsert_nodes_bulk_rejects_invalid_role(self, store: GraphStore) -> None:
        with pytest.raises(ValueError):
            store.upsert_nodes_bulk(
                [
                    {
                        "node_id": "n1",
                        "node_type": "service",
                        "properties": {},
                        "node_role": "nonsense",
                    }
                ]
            )

    def test_upsert_nodes_bulk_atomic_validation_no_partial_writes(
        self, store: GraphStore
    ) -> None:
        """When a row mid-batch fails per-row validation
        (``validate_node_role_args``, ``validate_document_ids``,
        ``check_node_role_immutable``), no rows from the batch are
        written. Honors the ABC's atomicity-of-validation contract.
        """
        # Row 0 is fine; row 1 has an invalid node_role → should reject
        # the whole batch before row 0 lands.
        before = store.count_nodes()
        with pytest.raises(ValueError, match=r"upsert_nodes_bulk\[1\]"):
            store.upsert_nodes_bulk(
                [
                    {
                        "node_id": "ok-row-0",
                        "node_type": "service",
                        "properties": {},
                    },
                    {
                        "node_id": "bad-row-1",
                        "node_type": "service",
                        "properties": {},
                        "node_role": "nonsense",
                    },
                ]
            )
        assert store.count_nodes() == before
        assert store.get_node("ok-row-0") is None

    def test_upsert_nodes_bulk_atomic_role_immutability_check(
        self, store: GraphStore
    ) -> None:
        """Mid-batch role-immutability conflict rejects the whole batch
        without writing earlier rows."""
        store.upsert_node("existing", "service", {}, node_role="semantic")
        _sleep_for_ordering()
        before = store.count_nodes()
        with pytest.raises(ValueError, match=r"upsert_nodes_bulk\[1\]"):
            store.upsert_nodes_bulk(
                [
                    {
                        "node_id": "fresh-row-0",
                        "node_type": "service",
                        "properties": {},
                    },
                    {
                        "node_id": "existing",
                        "node_type": "service",
                        "properties": {},
                        "node_role": "structural",
                    },
                ]
            )
        assert store.count_nodes() == before
        assert store.get_node("fresh-row-0") is None

    # ------------------------------------------------------------------
    # edges
    # ------------------------------------------------------------------

    def test_upsert_edge_returns_id(self, store: GraphStore) -> None:
        store.upsert_node("a", "service", {})
        store.upsert_node("b", "service", {})
        eid = store.upsert_edge("a", "b", "depends_on")
        assert isinstance(eid, str)
        assert eid

    def test_get_edges_outgoing(self, store: GraphStore) -> None:
        store.upsert_node("a", "service", {})
        store.upsert_node("b", "service", {})
        store.upsert_edge("a", "b", "depends_on")
        edges = store.get_edges("a", direction="outgoing")
        assert len(edges) == 1
        assert edges[0]["target_id"] == "b"

    def test_get_edges_incoming(self, store: GraphStore) -> None:
        store.upsert_node("a", "service", {})
        store.upsert_node("b", "service", {})
        store.upsert_edge("a", "b", "depends_on")
        edges = store.get_edges("b", direction="incoming")
        assert len(edges) == 1
        assert edges[0]["source_id"] == "a"

    def test_get_edges_both_returns_in_and_out(self, store: GraphStore) -> None:
        store.upsert_node("a", "service", {})
        store.upsert_node("b", "service", {})
        store.upsert_node("c", "service", {})
        store.upsert_edge("a", "b", "depends_on")
        store.upsert_edge("c", "b", "depends_on")
        edges = store.get_edges("b", direction="both")
        assert len(edges) == 2

    def test_get_edges_filters_by_edge_type(self, store: GraphStore) -> None:
        store.upsert_node("a", "service", {})
        store.upsert_node("b", "service", {})
        store.upsert_edge("a", "b", "depends_on")
        store.upsert_edge("a", "b", "calls")
        edges = store.get_edges("a", direction="outgoing", edge_type="calls")
        assert len(edges) == 1
        assert edges[0]["edge_type"] == "calls"

    # ------------------------------------------------------------------
    # bulk write — edges
    # ------------------------------------------------------------------

    def test_upsert_edges_bulk_creates_all_edges(self, store: GraphStore) -> None:
        store.upsert_nodes_bulk(
            [
                {"node_id": "a", "node_type": "service", "properties": {}},
                {"node_id": "b", "node_type": "service", "properties": {}},
                {"node_id": "c", "node_type": "service", "properties": {}},
            ]
        )
        ids = store.upsert_edges_bulk(
            [
                {"source_id": "a", "target_id": "b", "edge_type": "depends_on"},
                {"source_id": "b", "target_id": "c", "edge_type": "depends_on"},
            ]
        )
        assert len(ids) == 2
        assert all(isinstance(i, str) and i for i in ids)
        assert len(store.get_edges("a", direction="outgoing")) == 1
        assert len(store.get_edges("b", direction="both")) == 2

    def test_upsert_edges_bulk_empty_list_is_noop(self, store: GraphStore) -> None:
        assert store.upsert_edges_bulk([]) == []

    def test_upsert_edges_bulk_raises_for_missing_endpoint(
        self, store: GraphStore
    ) -> None:
        store.upsert_node("a", "service", {})
        # No "b" node exists; bulk should refuse with an index-bearing error.
        with pytest.raises(ValueError, match="0"):
            store.upsert_edges_bulk(
                [
                    {
                        "source_id": "a",
                        "target_id": "b",  # missing
                        "edge_type": "depends_on",
                    },
                ]
            )

    def test_upsert_edges_bulk_updates_existing_creates_new_version(
        self, store: GraphStore
    ) -> None:
        store.upsert_node("a", "service", {})
        store.upsert_node("b", "service", {})
        store.upsert_edge("a", "b", "depends_on", {"v": 1})
        _sleep_for_ordering()
        store.upsert_edges_bulk(
            [
                {
                    "source_id": "a",
                    "target_id": "b",
                    "edge_type": "depends_on",
                    "properties": {"v": 2},
                }
            ]
        )
        # Only one current edge between a→b of this type — the latest.
        edges = store.get_edges("a", direction="outgoing", edge_type="depends_on")
        assert len(edges) == 1
        assert edges[0]["properties"]["v"] == 2

    def test_upsert_edges_bulk_rejects_missing_keys(self, store: GraphStore) -> None:
        store.upsert_node("a", "service", {})
        store.upsert_node("b", "service", {})
        with pytest.raises(ValueError, match="edge_type"):
            store.upsert_edges_bulk(
                [{"source_id": "a", "target_id": "b"}]  # missing edge_type
            )

    # ------------------------------------------------------------------
    # subgraph traversal
    # ------------------------------------------------------------------

    def test_subgraph_seed_only_at_depth_zero(self, store: GraphStore) -> None:
        store.upsert_node("a", "service", {})
        store.upsert_node("b", "service", {})
        store.upsert_edge("a", "b", "depends_on")
        sg = store.get_subgraph(["a"], depth=0)
        assert {n["node_id"] for n in sg["nodes"]} == {"a"}

    def test_subgraph_follows_edges_to_depth(self, store: GraphStore) -> None:
        store.upsert_node("a", "service", {})
        store.upsert_node("b", "service", {})
        store.upsert_node("c", "service", {})
        store.upsert_edge("a", "b", "depends_on")
        store.upsert_edge("b", "c", "depends_on")
        sg = store.get_subgraph(["a"], depth=2)
        assert {n["node_id"] for n in sg["nodes"]} == {"a", "b", "c"}

    def test_subgraph_respects_depth_limit(self, store: GraphStore) -> None:
        store.upsert_node("a", "service", {})
        store.upsert_node("b", "service", {})
        store.upsert_node("c", "service", {})
        store.upsert_edge("a", "b", "depends_on")
        store.upsert_edge("b", "c", "depends_on")
        sg = store.get_subgraph(["a"], depth=1)
        ids = {n["node_id"] for n in sg["nodes"]}
        assert "a" in ids
        assert "b" in ids
        assert "c" not in ids

    def test_subgraph_filters_by_edge_type(self, store: GraphStore) -> None:
        store.upsert_node("a", "service", {})
        store.upsert_node("b", "service", {})
        store.upsert_node("c", "service", {})
        store.upsert_edge("a", "b", "depends_on")
        store.upsert_edge("a", "c", "calls")
        sg = store.get_subgraph(["a"], depth=1, edge_types=["depends_on"])
        ids = {n["node_id"] for n in sg["nodes"]}
        assert "b" in ids
        assert "c" not in ids

    # ------------------------------------------------------------------
    # aliases
    # ------------------------------------------------------------------

    def test_alias_resolves_to_entity(self, store: GraphStore) -> None:
        store.upsert_node("ent_auth", "service", {})
        store.upsert_alias("ent_auth", "github", "auth-svc")
        resolved = store.resolve_alias("github", "auth-svc")
        assert resolved is not None
        assert resolved["entity_id"] == "ent_auth"

    def test_resolve_alias_returns_none_for_missing(self, store: GraphStore) -> None:
        assert store.resolve_alias("github", "missing") is None

    def test_get_aliases_lists_all_for_entity(self, store: GraphStore) -> None:
        store.upsert_node("ent_auth", "service", {})
        store.upsert_alias("ent_auth", "github", "auth-svc")
        store.upsert_alias("ent_auth", "pagerduty", "AUTH")
        aliases = store.get_aliases("ent_auth")
        assert {a["source_system"] for a in aliases} == {"github", "pagerduty"}

    # ------------------------------------------------------------------
    # deletion
    # ------------------------------------------------------------------

    def test_delete_node_returns_true_when_existed(self, store: GraphStore) -> None:
        store.upsert_node("n1", "service", {})
        assert store.delete_node("n1") is True
        assert store.get_node("n1") is None

    def test_delete_node_returns_false_for_missing(self, store: GraphStore) -> None:
        assert store.delete_node("ghost") is False

    def test_delete_node_cascades_to_edges(self, store: GraphStore) -> None:
        store.upsert_node("a", "service", {})
        store.upsert_node("b", "service", {})
        store.upsert_edge("a", "b", "depends_on")
        store.delete_node("a")
        # Either the edge is gone, or attempting to fetch from the
        # deleted node returns no edges. Both are valid; the contract
        # is that the edge is unreachable from either endpoint.
        edges_from_b = store.get_edges("b", direction="incoming")
        assert edges_from_b == []

    # ------------------------------------------------------------------
    # counts
    # ------------------------------------------------------------------

    def test_count_nodes_only_counts_current_versions(self, store: GraphStore) -> None:
        store.upsert_node("a", "service", {"v": 1})
        _sleep_for_ordering()
        store.upsert_node("a", "service", {"v": 2})  # new version, old closed
        store.upsert_node("b", "service", {})
        assert store.count_nodes() == 2

    def test_count_edges_only_counts_current_versions(self, store: GraphStore) -> None:
        store.upsert_node("a", "service", {})
        store.upsert_node("b", "service", {})
        store.upsert_edge("a", "b", "depends_on")
        assert store.count_edges() == 1

    # ------------------------------------------------------------------
    # node_role + generation_spec
    # ------------------------------------------------------------------

    def test_structural_role_round_trip(self, store: GraphStore) -> None:
        store.upsert_node("col", "uc_column", {}, node_role="structural")
        node = store.get_node("col")
        assert node is not None
        assert node["node_role"] == "structural"

    def test_curated_role_requires_generation_spec(self, store: GraphStore) -> None:
        with pytest.raises(ValueError, match="generation_spec"):
            store.upsert_node("c", "concept", {}, node_role="curated")

    def test_curated_role_round_trip_with_spec(self, store: GraphStore) -> None:
        spec = {
            "generator_name": "louvain",
            "generator_version": "1.0.0",
            "source_node_ids": ["a", "b"],
            "parameters": {"resolution": 1.2},
        }
        store.upsert_node(
            "cluster_x", "concept", {}, node_role="curated", generation_spec=spec
        )
        node = store.get_node("cluster_x")
        assert node is not None
        assert node["node_role"] == "curated"
        assert node["generation_spec"] == spec

    def test_generation_spec_forbidden_on_non_curated(self, store: GraphStore) -> None:
        with pytest.raises(ValueError, match="generation_spec"):
            store.upsert_node(
                "n", "service", {}, generation_spec={"generator_name": "x"}
            )

    def test_unknown_role_rejected(self, store: GraphStore) -> None:
        with pytest.raises(ValueError, match="node_role"):
            store.upsert_node("n", "service", {}, node_role="bogus")

    def test_node_role_immutable_across_versions(self, store: GraphStore) -> None:
        store.upsert_node("n", "service", {}, node_role="structural")
        _sleep_for_ordering()
        with pytest.raises(ValueError, match="node_role"):
            store.upsert_node("n", "service", {}, node_role="semantic")

    # ------------------------------------------------------------------
    # document_ids — Phase 4 of ADR planes-and-substrates
    # ------------------------------------------------------------------

    def test_document_ids_round_trip(self, store: GraphStore) -> None:
        store.upsert_node("n", "service", {}, document_ids=["doc_1", "doc_2"])
        node = store.get_node("n")
        assert node is not None
        assert node["document_ids"] == ["doc_1", "doc_2"]

    def test_document_ids_none_yields_empty_list(self, store: GraphStore) -> None:
        store.upsert_node("n", "service", {})
        node = store.get_node("n")
        assert node is not None
        assert node["document_ids"] == []

    def test_document_ids_duplicates_rejected(self, store: GraphStore) -> None:
        with pytest.raises(ValueError, match="duplicate"):
            store.upsert_node("n", "service", {}, document_ids=["doc_1", "doc_1"])

    def test_document_ids_empty_string_rejected(self, store: GraphStore) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            store.upsert_node("n", "service", {}, document_ids=[""])

    # ------------------------------------------------------------------
    # Temporal reads — as_of on edges, query, subgraph
    # ------------------------------------------------------------------

    def test_get_edges_as_of(self, store: GraphStore) -> None:
        store.upsert_node("a", "service", {})
        store.upsert_node("b", "service", {})
        before = _now()
        _sleep_for_ordering()
        store.upsert_edge("a", "b", "depends_on")
        # Edge didn't exist at `before`.
        assert store.get_edges("a", direction="outgoing", as_of=before) == []
        # Edge exists now.
        assert len(store.get_edges("a", direction="outgoing")) == 1

    def test_query_as_of(self, store: GraphStore) -> None:
        before = _now()
        _sleep_for_ordering()
        store.upsert_node("a", "service", {})
        # Empty at `before`; one result now.
        assert store.query(node_type="service", as_of=before) == []
        assert len(store.query(node_type="service")) == 1

    def test_subgraph_as_of(self, store: GraphStore) -> None:
        store.upsert_node("a", "service", {})
        store.upsert_node("b", "service", {})
        before = _now()
        _sleep_for_ordering()
        store.upsert_edge("a", "b", "depends_on")
        # Subgraph at `before` has the seed but not the edge → no `b`.
        sg = store.get_subgraph(["a"], depth=2, as_of=before)
        ids = {n["node_id"] for n in sg["nodes"]}
        assert "a" in ids
        assert "b" not in ids

    # ------------------------------------------------------------------
    # Alias time-travel
    # ------------------------------------------------------------------

    def test_resolve_alias_as_of(self, store: GraphStore) -> None:
        store.upsert_node("ent_auth", "service", {})
        before = _now()
        _sleep_for_ordering()
        store.upsert_alias("ent_auth", "github", "auth-svc")
        assert store.resolve_alias("github", "auth-svc", as_of=before) is None
        assert store.resolve_alias("github", "auth-svc") is not None

    # ------------------------------------------------------------------
    # Canonical DSL — execute_node_query / execute_subgraph_query
    # ------------------------------------------------------------------

    def test_execute_node_query_eq_node_type(self, store: GraphStore) -> None:
        from trellis.stores.base.graph_query import (
            FilterClause,
            NodeQuery,
        )

        store.upsert_node("a", "service", {})
        store.upsert_node("b", "person", {})
        results = store.execute_node_query(
            NodeQuery(filters=(FilterClause("node_type", "eq", "service"),))
        )
        assert {r["node_id"] for r in results} == {"a"}

    def test_execute_node_query_eq_property(self, store: GraphStore) -> None:
        from trellis.stores.base.graph_query import (
            FilterClause,
            NodeQuery,
        )

        store.upsert_node("a", "service", {"team": "platform"})
        store.upsert_node("b", "service", {"team": "growth"})
        results = store.execute_node_query(
            NodeQuery(
                filters=(
                    FilterClause("node_type", "eq", "service"),
                    FilterClause("properties.team", "eq", "platform"),
                )
            )
        )
        assert {r["node_id"] for r in results} == {"a"}

    def test_execute_node_query_empty_filters(self, store: GraphStore) -> None:
        from trellis.stores.base.graph_query import NodeQuery

        store.upsert_node("a", "service", {})
        store.upsert_node("b", "person", {})
        results = store.execute_node_query(NodeQuery(limit=10))
        assert {r["node_id"] for r in results} >= {"a", "b"}

    def test_execute_node_query_respects_limit(self, store: GraphStore) -> None:
        from trellis.stores.base.graph_query import NodeQuery

        for i in range(5):
            store.upsert_node(f"n{i}", "service", {})
        results = store.execute_node_query(NodeQuery(limit=2))
        assert len(results) == 2

    def test_execute_subgraph_query_round_trip(self, store: GraphStore) -> None:
        from trellis.stores.base.graph_query import SubgraphQuery

        store.upsert_node("a", "service", {})
        store.upsert_node("b", "service", {})
        store.upsert_node("c", "service", {})
        store.upsert_edge("a", "b", "depends_on")
        store.upsert_edge("b", "c", "depends_on")
        result = store.execute_subgraph_query(SubgraphQuery(seed_ids=("a",), depth=2))
        ids = {n["node_id"] for n in result.nodes}
        assert ids == {"a", "b", "c"}

    def test_execute_subgraph_query_edge_type_filter(self, store: GraphStore) -> None:
        from trellis.stores.base.graph_query import SubgraphQuery

        store.upsert_node("a", "service", {})
        store.upsert_node("b", "service", {})
        store.upsert_node("c", "service", {})
        store.upsert_edge("a", "b", "depends_on")
        store.upsert_edge("a", "c", "calls")
        result = store.execute_subgraph_query(
            SubgraphQuery(seed_ids=("a",), depth=1, edge_type_filter=("depends_on",))
        )
        ids = {n["node_id"] for n in result.nodes}
        assert "b" in ids
        assert "c" not in ids

    # ------------------------------------------------------------------
    # DSL — `in` operator (Phase 2 compiler required)
    # ------------------------------------------------------------------

    def test_execute_node_query_in_node_type(self, store: GraphStore) -> None:
        from trellis.stores.base.graph_query import (
            FilterClause,
            NodeQuery,
        )

        store.upsert_node("a", "service", {})
        store.upsert_node("b", "person", {})
        store.upsert_node("c", "team", {})
        results = store.execute_node_query(
            NodeQuery(filters=(FilterClause("node_type", "in", ("service", "person")),))
        )
        assert {r["node_id"] for r in results} == {"a", "b"}

    def test_execute_node_query_in_property(self, store: GraphStore) -> None:
        from trellis.stores.base.graph_query import (
            FilterClause,
            NodeQuery,
        )

        store.upsert_node("a", "service", {"team": "platform"})
        store.upsert_node("b", "service", {"team": "growth"})
        store.upsert_node("c", "service", {"team": "data"})
        results = store.execute_node_query(
            NodeQuery(
                filters=(FilterClause("properties.team", "in", ("platform", "data")),)
            )
        )
        assert {r["node_id"] for r in results} == {"a", "c"}

    # ------------------------------------------------------------------
    # DSL — `exists` operator (Phase 2 compiler required)
    # ------------------------------------------------------------------

    def test_execute_node_query_exists_property(self, store: GraphStore) -> None:
        from trellis.stores.base.graph_query import (
            FilterClause,
            NodeQuery,
        )

        store.upsert_node("a", "service", {"deprecated_at": "2026-01-01"})
        store.upsert_node("b", "service", {})
        results = store.execute_node_query(
            NodeQuery(filters=(FilterClause("properties.deprecated_at", "exists"),))
        )
        assert {r["node_id"] for r in results} == {"a"}


def _now() -> datetime:
    """Return the current time in the same UTC-aware shape stores use."""
    from trellis.core.base import utc_now

    return utc_now()
