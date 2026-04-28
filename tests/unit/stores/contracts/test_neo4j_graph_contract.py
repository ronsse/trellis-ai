"""Run the GraphStore contract suite against Neo4jGraphStore.

Skipped unless ``TRELLIS_TEST_NEO4J_URI`` is set and the neo4j driver
is importable.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("neo4j")

from tests.unit.stores.contracts.graph_store_contract import (
    GraphStoreContractTests,
)

URI = os.environ.get("TRELLIS_TEST_NEO4J_URI", "")
USER = os.environ.get("TRELLIS_TEST_NEO4J_USER", "neo4j")
PASSWORD = os.environ.get("TRELLIS_TEST_NEO4J_PASSWORD", "")
DATABASE = os.environ.get("TRELLIS_TEST_NEO4J_DATABASE", "neo4j")

pytestmark = [
    pytest.mark.neo4j,
    pytest.mark.skipif(not URI, reason="TRELLIS_TEST_NEO4J_URI not set"),
]


class TestNeo4jGraphContract(GraphStoreContractTests):
    @pytest.fixture
    def store(self):
        from trellis.stores.neo4j.graph import Neo4jGraphStore

        s = Neo4jGraphStore(URI, user=USER, password=PASSWORD, database=DATABASE)
        # Wipe everything the graph store knows about between tests.
        with s._driver.session(database=s._database) as session:
            session.run("MATCH (n) WHERE n:Node OR n:Alias DETACH DELETE n")
        yield s
        s.close()
