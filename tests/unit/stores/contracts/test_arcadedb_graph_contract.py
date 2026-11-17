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


# A property-filtered read takes a first window of max(limit * 10, the
# minimum) rows. The contract's buried-match cases ask for limit=2 behind
# 40 newer non-matching rows, so with the minimum patched to 7 the first
# window is max(20, 7) = 20 rows, all non-matching and full, and the read
# needs a second statement (window 20 * 4 = 80, which returns all 43 rows)
# through real Cypher. With the shipped minimum of 200 one statement would
# answer and no window boundary would be crossed.
_SMALL_MIN_WINDOW = 7


@pytest.fixture(autouse=True)
def _small_property_filter_window(monkeypatch: pytest.MonkeyPatch) -> None:
    from trellis.stores.bolt_opencypher import graph as bolt_graph

    monkeypatch.setattr(bolt_graph, "_PROPERTY_FILTER_MIN_WINDOW", _SMALL_MIN_WINDOW)


class TestArcadeDBGraphContract(GraphStoreContractTests):
    # One Cypher statement checks and creates, with no uniqueness constraint on
    # a current version behind it, so two simultaneous calls can both write.
    ATOMIC_CREATE_IF_ABSENT = False

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
            session.run(
                "MATCH (n) WHERE n:Node OR n:Alias OR n:AliasClaim "
                "OR n:EdgeClaim DETACH DELETE n"
            )
        yield s
        s.close()
