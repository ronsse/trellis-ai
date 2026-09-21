"""ArcadeDBVectorStore against the shared ``VectorStore`` contract.

ArcadeDB is the blessed vector substrate for self-hosted deployments,
and until #579 its only coverage was the per-backend
``tests/unit/stores/test_arcadedb_vector.py``. The shared contract is
what defines ``VectorStore`` semantics, so the blessed backend belongs
under it.

It is a **shape #2** store — embeddings are a ``LIST OF FLOAT``
property on the ``(:Node)`` rows :class:`ArcadeDBGraphStore` owns, not
rows of its own — so ``upsert`` requires the node to already exist as a
current version. That is a storage prerequisite, not a difference in
vector semantics, and it is the whole of what this subclass adds:
:meth:`provision_storage` creates the backing node through the paired
graph store and writes no vector. Every assertion in the suite is the
one every other backend runs.

Requires the ``neo4j`` driver (the paired graph store speaks Bolt) and
a live ArcadeDB reachable at ``TRELLIS_TEST_ARCADEDB_URI`` +
``TRELLIS_TEST_ARCADEDB_HTTP_URL`` — ``live-infra.yml`` provisions one.
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

URI = os.environ.get("TRELLIS_TEST_ARCADEDB_URI", "")
USER = os.environ.get("TRELLIS_TEST_ARCADEDB_USER", "root")
PASSWORD = os.environ.get("TRELLIS_TEST_ARCADEDB_PASSWORD", "")
DATABASE = os.environ.get("TRELLIS_TEST_ARCADEDB_DATABASE", "trellis_vector_test")
HTTP_URL = os.environ.get("TRELLIS_TEST_ARCADEDB_HTTP_URL", "http://localhost:2480")

# Same index name and width as tests/unit/stores/test_arcadedb_vector.py:
# both suites can run against one database, and an LSM_VECTOR index is
# per (type, property), so a second name over Node(embedding) would be a
# second index on the same property.
INDEX_NAME = "trellis_test_node_embeddings"

pytestmark = [
    pytest.mark.arcadedb,
    pytest.mark.skipif(not URI, reason="TRELLIS_TEST_ARCADEDB_URI not set"),
]


class TestArcadeDBVectorContract(VectorStoreContractTests):
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
        from trellis.stores.arcadedb.graph import ArcadeDBGraphStore
        from trellis.stores.arcadedb.vector import ArcadeDBVectorStore

        graph = ArcadeDBGraphStore(
            URI,
            user=USER,
            password=PASSWORD,
            database=DATABASE,
            http_url=HTTP_URL,
            ensure_database_exists=True,
        )
        with graph._driver.session(database=graph._database) as session:
            session.run(
                "MATCH (n) WHERE n:Node OR n:Alias OR n:AliasClaim DETACH DELETE n"
            )
        self._graph = graph

        vector = ArcadeDBVectorStore(
            http_url=HTTP_URL,
            user=USER,
            password=PASSWORD,
            database=DATABASE,
            dimensions=DIMS,
            index_name=INDEX_NAME,
        )
        yield vector
        vector.close()
        graph.close()
