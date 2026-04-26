"""Run the GraphStore contract suite against PostgresGraphStore.

Skipped unless ``TRELLIS_TEST_PG_DSN`` is set and psycopg is importable.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("psycopg")

from tests.unit.stores.contracts.graph_store_contract import (
    GraphStoreContractTests,
)

DSN = os.environ.get("TRELLIS_TEST_PG_DSN", "")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not DSN, reason="TRELLIS_TEST_PG_DSN not set"),
]


class TestPostgresGraphContract(GraphStoreContractTests):
    @pytest.fixture
    def store(self):
        from trellis.stores.postgres.graph import PostgresGraphStore

        s = PostgresGraphStore(dsn=DSN)
        # Each contract test starts from an empty graph.
        with s.conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE nodes, edges, entity_aliases")
        s.conn.commit()
        yield s
        s.close()
