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
    pytest.mark.skipif(not DSN, reason="TRELLIS_TEST_PG_DSN not set"),
]


class TestPgVectorContract(VectorStoreContractTests):
    @pytest.fixture
    def store(self):
        from trellis.stores.pgvector.store import PgVectorStore

        s = PgVectorStore(dsn=DSN, dimensions=DIMS)
        with s._conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE vectors")
        s._conn.commit()
        yield s
        s.close()
