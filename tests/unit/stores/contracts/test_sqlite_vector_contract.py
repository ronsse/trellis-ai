"""Run the VectorStore contract suite against SQLiteVectorStore."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.unit.stores.contracts.vector_store_contract import (
    VectorStoreContractTests,
)
from trellis.stores.sqlite.vector import SQLiteVectorStore


class TestSQLiteVectorContract(VectorStoreContractTests):
    @pytest.fixture
    def store(self, tmp_path: Path):
        store = SQLiteVectorStore(tmp_path / "vec.db")
        yield store
        store.close()
