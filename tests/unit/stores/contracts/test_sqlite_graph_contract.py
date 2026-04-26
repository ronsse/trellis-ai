"""Run the GraphStore contract suite against SQLiteGraphStore."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.unit.stores.contracts.graph_store_contract import (
    GraphStoreContractTests,
)
from trellis.stores.sqlite.graph import SQLiteGraphStore


class TestSQLiteGraphContract(GraphStoreContractTests):
    @pytest.fixture
    def store(self, tmp_path: Path):
        store = SQLiteGraphStore(tmp_path / "graph.db")
        yield store
        store.close()
