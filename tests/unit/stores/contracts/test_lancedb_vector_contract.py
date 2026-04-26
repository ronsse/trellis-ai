"""Run the VectorStore contract suite against LanceVectorStore.

Skipped unless lancedb + pyarrow are importable. No env var gate
required — LanceDB is embedded.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("lancedb")
pytest.importorskip("pyarrow")

from tests.unit.stores.contracts.vector_store_contract import (
    VectorStoreContractTests,
)


class TestLanceVectorContract(VectorStoreContractTests):
    @pytest.fixture
    def store(self, tmp_path: Path):
        from trellis.stores.lancedb.store import LanceVectorStore

        s = LanceVectorStore(uri=tmp_path / "lance", metric="cosine")
        yield s
        s.close()
