"""Run the DocumentStore contract suite against SQLiteDocumentStore."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.unit.stores.contracts.document_store_contract import (
    DocumentStoreContractTests,
)
from trellis.stores.sqlite.document import SQLiteDocumentStore


class TestSQLiteDocumentContract(DocumentStoreContractTests):
    @pytest.fixture
    def store(self, tmp_path: Path):
        store = SQLiteDocumentStore(tmp_path / "docs.db")
        yield store
        store.close()
