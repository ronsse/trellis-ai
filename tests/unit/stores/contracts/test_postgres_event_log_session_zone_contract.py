"""Run the EventLog contract suite against PostgresEventLog under a non-UTC session.

``events.occurred_at`` is ``TIMESTAMPTZ``. A naive ``datetime`` bound as
``timestamp`` is converted by the server in the *session's* time zone, so the
same naive value names a different instant under each zone. SQLite reads a
naive value as UTC, and ``PostgresEventLog`` binds one as UTC
(``_bound_instant``) so the two backends agree whatever the session says.

``test_postgres_event_log_contract.py`` runs the contract under the server's
default session, which is UTC. There a naive value and an aware UTC value are
the same instant whether or not the store converts, so a regression in that
bind passes. This module runs the same contract with the zone set through the
connection's ``options``, where it fails.

Skipped unless ``TRELLIS_TEST_PG_DSN`` is set and psycopg is importable.
"""

from __future__ import annotations

import pytest

pytest.importorskip("psycopg")

from psycopg.conninfo import make_conninfo

from tests.pg_scratch import configured_dsn, scratch_dsn
from tests.unit.stores.contracts.event_log_contract import EventLogContractTests

DSN = configured_dsn()

#: A zone west of UTC, so a naive wall-clock value read in the session's zone
#: lands hours *after* the instant it names in UTC.
SESSION_ZONE = "America/Los_Angeles"

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not DSN, reason="TRELLIS_TEST_PG_DSN not set"),
]


class TestPostgresEventLogContractInANonUtcSession(EventLogContractTests):
    @pytest.fixture
    def store(self):
        from trellis.stores.postgres.event_log import PostgresEventLog

        # ``scratch_dsn`` first: it refuses a database that holds data before
        # the TRUNCATE below. The zone is added to the DSN it vetted.
        dsn = make_conninfo(scratch_dsn(), options=f"-c timezone={SESSION_ZONE}")
        s = PostgresEventLog(dsn=dsn)
        # Each contract test starts from an empty event log.
        with s._conn() as conn, conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE events")
        yield s
        s.close()

    def test_the_session_runs_in_the_requested_zone(self, store) -> None:
        # Without this, a connection that dropped ``options`` would run the
        # whole contract under UTC and pass, which is the gap this module
        # exists to close.
        with store._conn() as conn, conn.cursor() as cur:
            cur.execute("SHOW TimeZone")
            row = cur.fetchone()
        assert row is not None
        assert row[0] == SESSION_ZONE
