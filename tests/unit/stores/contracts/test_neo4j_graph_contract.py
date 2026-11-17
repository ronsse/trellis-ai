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


class TestNeo4jGraphContract(GraphStoreContractTests):
    # One Cypher statement checks and creates, with no uniqueness constraint on
    # a current version behind it, so two simultaneous calls can both write.
    ATOMIC_CREATE_IF_ABSENT = False

    @pytest.fixture
    def store(self):
        from trellis.stores.neo4j.graph import Neo4jGraphStore

        s = Neo4jGraphStore(URI, user=USER, password=PASSWORD, database=DATABASE)
        # Wipe everything the graph store knows about between tests.
        with s._driver.session(database=s._database) as session:
            session.run(
                "MATCH (n) WHERE n:Node OR n:Alias OR n:AliasClaim "
                "OR n:EdgeClaim DETACH DELETE n"
            )
        yield s
        s.close()
