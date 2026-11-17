"""Neo4jVectorStore against the shared ``VectorStore`` contract.

A **shape #2** store, like ArcadeDB's: embeddings are an optional
``embedding`` property on the ``(:Node)`` rows :class:`Neo4jGraphStore`
owns, so ``upsert`` requires the node to already exist as a current
version. :meth:`provision_storage` declares that prerequisite by creating
the backing node through the paired graph store and writing no vector;
every assertion is the one every other backend runs.

Every case runs, including the ones that reach
:meth:`Neo4jVectorStore.query`. ``neo4j:2025.12``, the image
``live-infra.yml`` provisions, refuses its Cypher 25 ``SEARCH`` clause, so
on that image those cases run through the store's
``db.index.vector.queryNodes`` fallback (they skipped behind a capability
gate before the fallback existed). The ``SEARCH`` form itself runs on no CI
server.

One thing differs from ``test_arcadedb_vector_contract.py``, and it is
about sharing the CI container rather than about vector semantics.

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


class TestNeo4jVectorContract(VectorStoreContractTests):
    """The shared contract, with shape #2's storage prerequisite declared."""

    _graph: Any

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
                "MATCH (n) WHERE n:Node OR n:Alias OR n:AliasClaim "
                "OR n:EdgeClaim DETACH DELETE n"
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
