"""Run the TraceStore contract suite against SQLiteTraceStore."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.unit.stores.contracts.trace_store_contract import (
    TraceStoreContractTests,
)
from trellis.stores.sqlite.trace import SQLiteTraceStore


class TestSQLiteTraceContract(TraceStoreContractTests):
    @pytest.fixture
    def store(self, tmp_path: Path):
        store = SQLiteTraceStore(tmp_path / "traces.db")
        yield store
        store.close()
