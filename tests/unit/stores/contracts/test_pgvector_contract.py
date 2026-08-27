"""Run the VectorStore contract suite against PgVectorStore.

Skipped unless ``TRELLIS_TEST_PG_DSN`` is set and psycopg/pgvector are
importable.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("pgvector")

from tests.unit.stores.contracts.vector_store_contract import (
    DIMS,
    VectorStoreContractTests,
)

DSN = os.environ.get("TRELLIS_TEST_PG_DSN", "")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.pgvector,
    pytest.mark.skipif(not DSN, reason="TRELLIS_TEST_PG_DSN not set"),
]


class TestPgVectorContract(VectorStoreContractTests):
    @pytest.fixture
    def store(self):
        from trellis.stores.pgvector.store import PgVectorStore

        s = PgVectorStore(dsn=DSN, dimensions=DIMS)
        # Each contract test starts from an empty vectors table.
        # ``_conn`` is the pooled-connection *context manager* inherited
        # from ``PostgresStoreBase``, not a connection object, and it
        # commits on block exit — see the sibling Postgres contract
        # fixtures.
        with s._conn() as conn, conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE vectors")
        yield s
        s.close()
