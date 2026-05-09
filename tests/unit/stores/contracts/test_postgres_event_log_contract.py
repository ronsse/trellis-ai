"""Run the EventLog contract suite against PostgresEventLog.

Skipped unless ``TRELLIS_TEST_PG_DSN`` is set and psycopg is importable.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("psycopg")

from tests.unit.stores.contracts.event_log_contract import EventLogContractTests

DSN = os.environ.get("TRELLIS_TEST_PG_DSN", "")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not DSN, reason="TRELLIS_TEST_PG_DSN not set"),
]


class TestPostgresEventLogContract(EventLogContractTests):
    @pytest.fixture
    def store(self):
        from trellis.stores.postgres.event_log import PostgresEventLog

        s = PostgresEventLog(dsn=DSN)
        # Each contract test starts from an empty event log.
        with s._conn() as conn, conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE events")
        yield s
        s.close()
