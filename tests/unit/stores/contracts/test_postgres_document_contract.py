"""Run the DocumentStore contract suite against PostgresDocumentStore.

Skipped unless ``TRELLIS_TEST_PG_DSN`` is set and psycopg is importable.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("psycopg")

from tests.unit.stores.contracts.document_store_contract import (
    DocumentStoreContractTests,
)

DSN = os.environ.get("TRELLIS_TEST_PG_DSN", "")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not DSN, reason="TRELLIS_TEST_PG_DSN not set"),
]


class TestPostgresDocumentContract(DocumentStoreContractTests):
    @pytest.fixture
    def store(self):
        from trellis.stores.postgres.document import PostgresDocumentStore

        s = PostgresDocumentStore(dsn=DSN)
        # Each contract test starts from an empty document table.
        with s._conn() as conn, conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE documents")
        yield s
        s.close()
