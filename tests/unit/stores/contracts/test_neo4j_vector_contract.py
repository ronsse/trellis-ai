"""Neo4jVectorStore against the shared ``VectorStore`` contract.

A **shape #2** store, like ArcadeDB's: embeddings are an optional
``embedding`` property on the ``(:Node)`` rows :class:`Neo4jGraphStore`
owns, so ``upsert`` requires the node to already exist as a current
version. :meth:`provision_storage` declares that prerequisite by creating
the backing node through the paired graph store and writing no vector;
every assertion is the one every other backend runs.

Two things differ from ``test_arcadedb_vector_contract.py``, and both are
about the container CI provisions rather than about vector semantics.

**The ``SEARCH`` cases are gated per test, not per class.**
:meth:`Neo4jVectorStore.query` issues the Cypher 25
``SEARCH ... IN (VECTOR INDEX ...)`` clause, which ``neo4j:2025.12``
rejects at parse time. The cases in :data:`SEARCH_ISSUING_TESTS` take the
shared ``require_neo4j_vector_search`` capability gate from
``tests/conftest.py`` and skip there; every other case runs. The roster is
hand-read and pinned both directions against an AST scan of the contract
by ``tests/unit/test_neo4j_vector_live_infra_rule.py``, so a contract case
added later that calls ``query`` fails that rule instead of turning
live-infra red — or, worse, being gated by a stale name that no longer
exists.

**It takes the production index name, at the contract's 3 dims.** Neo4j
holds one vector index per ``(label, property)``, and a second
``CREATE VECTOR INDEX ... IF NOT EXISTS`` under another name returns
success and never exists, after which *the suite that asked* times out
30s per test. This file runs in the same ``live-infra.yml`` invocation as
``test_neo4j_vector.py`` and ``tests/integration/test_neo4j_e2e.py``, both
of which resolve to the store's default name at 3 dims, so the shared
name makes this suite's ``CREATE`` a no-op. Same rule as those suites,
pinned by the same rule module.

Requires the ``neo4j`` driver and a Neo4j at ``TRELLIS_TEST_NEO4J_URI``;
``live-infra.yml`` provisions one.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

import pytest

pytest.importorskip("neo4j")

from tests.unit.stores.contracts.vector_store_contract import (
    DIMS,
    VectorStoreContractTests,
)

if TYPE_CHECKING:
    from trellis.stores.base.vector import VectorStore

URI = os.environ.get("TRELLIS_TEST_NEO4J_URI", "")
USER = os.environ.get("TRELLIS_TEST_NEO4J_USER", "neo4j")
PASSWORD = os.environ.get("TRELLIS_TEST_NEO4J_PASSWORD", "")
DATABASE = os.environ.get("TRELLIS_TEST_NEO4J_DATABASE", "neo4j")

pytestmark = [
    pytest.mark.neo4j,
    pytest.mark.skipif(not URI, reason="TRELLIS_TEST_NEO4J_URI not set"),
]

#: Contract cases whose body reaches ``Neo4jVectorStore.query``'s Cypher on
#: this backend, and therefore need the ``SEARCH`` capability. Hand-read.
#: One contract case calls ``query`` and is deliberately absent:
#: ``test_reset_storage_matches_supports_reset`` only queries after a
#: successful ``reset_storage``, which this store does not implement, so on
#: this backend it takes the ``NotImplementedError`` branch and returns
#: first. It runs, and passes, on ``neo4j:2025.12``. The reason is recorded
#: in ``CONTRACT_UNGATED_QUERY_TESTS`` in
#: ``tests/unit/test_neo4j_vector_live_infra_rule.py``, which also pins its
#: premise.
SEARCH_ISSUING_TESTS = frozenset(
    {
        "test_empty_query_returns_empty_list",
        "test_get_then_reupsert_keeps_row_queryable",
        "test_provisioning_alone_stores_no_vector",
        "test_query_filter_by_int_metadata",
        "test_query_filter_by_str_metadata",
        "test_query_filter_no_match_returns_empty",
        "test_query_filter_on_unknown_key_returns_empty",
        "test_query_filter_returns_matches_ranked_below_non_matches",
        "test_query_filter_with_multiple_keys_is_and",
        "test_query_orders_by_similarity_descending",
        "test_query_result_shape",
        "test_query_returns_metadata",
        "test_query_self_match_is_top",
        "test_query_top_k_caps_results",
        "test_upsert_bulk_results_visible_to_query",
    }
)


class TestNeo4jVectorContract(VectorStoreContractTests):
    """The shared contract, with shape #2's storage prerequisite declared."""

    _graph: Any

    @pytest.fixture(autouse=True)
    def _search_capability_gate(self, request: pytest.FixtureRequest) -> None:
        """Route :data:`SEARCH_ISSUING_TESTS` through the shared gate.

        The contract's bodies are inherited, so the gate cannot be a
        parameter in their signatures the way it is in
        ``test_neo4j_vector.py``; requesting it by name here is the same
        mechanism, one frame up. It runs before ``store`` is built, so a
        skipped case never provisions the index.
        """
        if request.node.originalname in SEARCH_ISSUING_TESTS:
            request.getfixturevalue("require_neo4j_vector_search")

    def provision_storage(self, store: VectorStore, *item_ids: str) -> None:
        """Create the ``(:Node)`` rows the vector store attaches to.

        Nodes only — no embedding, no vector metadata. What the store
        under test is asked to do is exactly what it is asked to do for
        every other backend.
        """
        for item_id in item_ids:
            self._graph.upsert_node(item_id, "doc", {})

    @pytest.fixture
    def store(self):
        from trellis.stores.neo4j.graph import Neo4jGraphStore
        from trellis.stores.neo4j.vector import Neo4jVectorStore

        graph = Neo4jGraphStore(URI, user=USER, password=PASSWORD, database=DATABASE)
        # The database is shared with every other Neo4j suite in the
        # live-infra invocation, so wipe what the graph store owns before
        # each case, as test_neo4j_vector.py and the graph contract do.
        with graph._driver.session(database=graph._database) as session:
            session.run(
                "MATCH (n) WHERE n:Node OR n:Alias OR n:AliasClaim DETACH DELETE n"
            )
        self._graph = graph

        # No ``index_name`` override, deliberately: the production default
        # is the one (:Node, embedding) index every other Neo4j suite in
        # live-infra resolves to, at the same 3 dims. A private name here
        # is silently discarded by Neo4j and the suite times out — see the
        # module docstring.
        vector = Neo4jVectorStore(
            URI,
            user=USER,
            password=PASSWORD,
            database=DATABASE,
            dimensions=DIMS,
        )
        yield vector
        vector.close()
        graph.close()
