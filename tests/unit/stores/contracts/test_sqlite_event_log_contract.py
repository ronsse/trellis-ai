"""Run the EventLog contract suite against SQLiteEventLog."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.unit.stores.contracts.event_log_contract import (
    EventLogContractTests,
)
from trellis.stores.sqlite.event_log import SQLiteEventLog


class TestSQLiteEventLogContract(EventLogContractTests):
    @pytest.fixture
    def store(self, tmp_path: Path):
        store = SQLiteEventLog(tmp_path / "events.db")
        yield store
        store.close()
