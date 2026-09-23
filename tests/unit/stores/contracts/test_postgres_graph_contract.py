"""Run the GraphStore contract suite against PostgresGraphStore.

Skipped unless ``TRELLIS_TEST_PG_DSN`` is set and psycopg is importable.
"""

from __future__ import annotations

import pytest

pytest.importorskip("psycopg")

from tests.pg_scratch import configured_dsn, scratch_dsn
from tests.unit.stores.contracts.graph_store_contract import (
    GraphStoreContractTests,
)

DSN = configured_dsn()

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not DSN, reason="TRELLIS_TEST_PG_DSN not set"),
]


class TestPostgresGraphContract(GraphStoreContractTests):
    @pytest.fixture
    def store(self):
        from trellis.stores.postgres.graph import PostgresGraphStore

        s = PostgresGraphStore(dsn=scratch_dsn())
        # Each contract test starts from an empty graph.
        with s._conn() as conn, conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE nodes, edges, entity_aliases")
        yield s
        s.close()
