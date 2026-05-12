"""Run the GraphStore contract suite against ArcadeDBGraphStore.

Skipped unless ``TRELLIS_TEST_ARCADEDB_URI`` is set and the neo4j
driver (the Bolt client) is importable.

Set up a local ArcadeDB for testing:

.. code-block:: bash

    docker run -d --name trellis-arcadedb \\
      -p 2480:2480 -p 7687:7687 \\
      -e JAVA_OPTS='-Darcadedb.server.rootPassword=playwithdata \\
        -Darcadedb.server.plugins=Bolt:com.arcadedb.bolt.BoltProtocolPlugin' \\
      arcadedata/arcadedb:latest

    export TRELLIS_TEST_ARCADEDB_URI=bolt://localhost:7687
    export TRELLIS_TEST_ARCADEDB_PASSWORD=playwithdata
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("neo4j")

from tests.unit.stores.contracts.graph_store_contract import (
    GraphStoreContractTests,
)

URI = os.environ.get("TRELLIS_TEST_ARCADEDB_URI", "")
USER = os.environ.get("TRELLIS_TEST_ARCADEDB_USER", "root")
PASSWORD = os.environ.get("TRELLIS_TEST_ARCADEDB_PASSWORD", "")
DATABASE = os.environ.get("TRELLIS_TEST_ARCADEDB_DATABASE", "trellis_test")
HTTP_URL = os.environ.get("TRELLIS_TEST_ARCADEDB_HTTP_URL", "http://localhost:2480")

pytestmark = [
    pytest.mark.arcadedb,
    pytest.mark.skipif(not URI, reason="TRELLIS_TEST_ARCADEDB_URI not set"),
]


class TestArcadeDBGraphContract(GraphStoreContractTests):
    @pytest.fixture
    def store(self):
        from trellis.stores.arcadedb.graph import ArcadeDBGraphStore

        s = ArcadeDBGraphStore(
            URI,
            user=USER,
            password=PASSWORD,
            database=DATABASE,
            http_url=HTTP_URL,
            ensure_database_exists=True,
        )
        # Wipe everything the graph store knows about between tests.
        with s._driver.session(database=s._database) as session:
            session.run("MATCH (n) WHERE n:Node OR n:Alias DETACH DELETE n")
        yield s
        s.close()
