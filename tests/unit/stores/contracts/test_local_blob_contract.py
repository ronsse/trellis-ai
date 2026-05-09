"""Run the BlobStore contract suite against LocalBlobStore.

The two ``xfail(strict=True)`` markers below pin a real cross-backend
drift that the contract suite was created to surface:

* :meth:`LocalBlobStore.list_keys` returns keys joined with the OS
  path separator (``\\`` on Windows). S3 returns forward slashes.
  Callers ``put`` keys with ``/`` and reasonably expect to read them
  back with ``/``.
* :meth:`LocalBlobStore.get_uri` formats the URI with the OS-native
  resolved path on Windows, so the per-key segment is also
  backslash-separated.

Both are implementation issues in the Local backend — the contract
encodes the right invariant. Resolution belongs to a future unit
that's allowed to touch ``src/trellis/stores/local/blob.py``;
this unit is tests-only.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.unit.stores.contracts.blob_store_contract import (
    BlobStoreContractTests,
)
from trellis.stores.local.blob import LocalBlobStore

_WINDOWS = sys.platform == "win32"
_WINDOWS_SEP_DRIFT = (
    "LocalBlobStore returns OS-native path separators on Windows; "
    "S3 returns forward slashes — fix in a follow-up unit."
)


class TestLocalBlobContract(BlobStoreContractTests):
    @pytest.fixture
    def store(self, tmp_path: Path) -> Iterator[LocalBlobStore]:
        s = LocalBlobStore(tmp_path / "blobs")
        yield s
        s.close()

    @pytest.mark.xfail(_WINDOWS, reason=_WINDOWS_SEP_DRIFT, strict=True)
    def test_list_keys_with_prefix(self, store: LocalBlobStore) -> None:  # type: ignore[override]
        super().test_list_keys_with_prefix(store)

    @pytest.mark.xfail(_WINDOWS, reason=_WINDOWS_SEP_DRIFT, strict=True)
    def test_get_uri_includes_key(self, store: LocalBlobStore) -> None:  # type: ignore[override]
        super().test_get_uri_includes_key(store)
