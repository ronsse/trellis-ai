"""Run the TraceStore contract suite against PostgresTraceStore.

Skipped unless ``TRELLIS_TEST_PG_DSN`` is set and psycopg is importable.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("psycopg")

from tests.unit.stores.contracts.trace_store_contract import (
    TraceStoreContractTests,
)

DSN = os.environ.get("TRELLIS_TEST_PG_DSN", "")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not DSN, reason="TRELLIS_TEST_PG_DSN not set"),
]


class TestPostgresTraceContract(TraceStoreContractTests):
    @pytest.fixture
    def store(self):
        from trellis.stores.postgres.trace import PostgresTraceStore

        s = PostgresTraceStore(dsn=DSN)
        # Each contract test starts from an empty trace store.
        with s._conn() as conn, conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE traces")
        yield s
        s.close()
