"""Run the BlobStore contract suite against LocalBlobStore."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.unit.stores.contracts.blob_store_contract import (
    BlobStoreContractTests,
)
from trellis.stores.local.blob import LocalBlobStore


class TestLocalBlobContract(BlobStoreContractTests):
    @pytest.fixture
    def store(self, tmp_path: Path) -> Iterator[LocalBlobStore]:
        s = LocalBlobStore(tmp_path / "blobs")
        yield s
        s.close()
