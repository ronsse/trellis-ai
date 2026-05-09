"""BlobStore contract test suite — runs against every backend.

Mirrors the shape of :mod:`graph_store_contract` and
:mod:`vector_store_contract`: this base class defines the shared
semantics every ``BlobStore`` backend must honour. Backend-specific
test files (``test_local_blob_contract.py``, ``test_s3_blob_contract.py``)
subclass :class:`BlobStoreContractTests` and provide a ``store``
fixture.

The harness deliberately:

* Does **not** test backend-specific TTL / GC sweep behaviour — that
  lives in the per-backend tests (``test_local_blob.py``,
  ``test_s3_blob.py``). The contract suite is *additive*.
* Uses only the public ``BlobStore`` ABC surface — no ``_root``,
  ``_client``, ``_meta_dir`` attribute access.
* Honours the ABC contract that ``get()`` on a missing key returns
  ``None`` (not :class:`~trellis.errors.NotFoundError`). The unit
  brief asked the contract to assert ``NotFoundError``; the existing
  ABC docstring + every backend implementation contradict that, so
  the contract follows the implementation. See the unit's report for
  the divergence flag.

Subclass shape::

    class TestLocalBlobContract(BlobStoreContractTests):
        @pytest.fixture
        def store(self, tmp_path):
            store = LocalBlobStore(tmp_path / "blobs")
            yield store
            store.close()
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from trellis.stores.base.blob import BlobStore


# A 1 MiB payload exercises any chunked / streaming path a backend
# might use without being slow enough to bother CI.
LARGE_BLOB_SIZE = 1 * 1024 * 1024


class BlobStoreContractTests:
    """Contract tests every ``BlobStore`` backend must pass.

    Subclasses must provide a pytest fixture named ``store`` that
    yields a fresh, empty :class:`~trellis.stores.base.blob.BlobStore`
    instance and tears it down afterwards.
    """

    # ------------------------------------------------------------------
    # put / get round-trip
    # ------------------------------------------------------------------

    def test_put_then_get_round_trips_bytes(self, store: BlobStore) -> None:
        store.put("file.bin", b"hello world")
        assert store.get("file.bin") == b"hello world"

    def test_put_returns_uri(self, store: BlobStore) -> None:
        uri = store.put("file.bin", b"x")
        assert isinstance(uri, str)
        assert uri
        # Whatever scheme the backend uses, the URI it returns must
        # equal the URI ``get_uri()`` reports for the same key — the
        # scheme/host shape itself is backend-specific and not part
        # of the cross-backend contract.
        assert uri == store.get_uri("file.bin")

    def test_round_trip_preserves_arbitrary_binary_payload(
        self, store: BlobStore
    ) -> None:
        # Non-UTF-8, includes nulls, high bytes — anything text-mode
        # handling would corrupt.
        payload = bytes(range(256))
        store.put("binary.bin", payload)
        assert store.get("binary.bin") == payload

    def test_round_trip_preserves_checksum(self, store: BlobStore) -> None:
        # Stable checksum is the operational definition of "bytes
        # preserved" — the consumer hashes after reading, not before
        # writing, so the contract is the post-read hash.
        payload = b"the quick brown fox jumps over the lazy dog"
        digest = hashlib.sha256(payload).hexdigest()
        store.put("hash.bin", payload)
        out = store.get("hash.bin")
        assert out is not None
        assert hashlib.sha256(out).hexdigest() == digest

    def test_put_overwrites_existing_key(self, store: BlobStore) -> None:
        store.put("file.bin", b"old")
        store.put("file.bin", b"new")
        assert store.get("file.bin") == b"new"

    # ------------------------------------------------------------------
    # exists
    # ------------------------------------------------------------------

    def test_exists_false_before_put(self, store: BlobStore) -> None:
        assert store.exists("never-put.bin") is False

    def test_exists_true_after_put(self, store: BlobStore) -> None:
        store.put("file.bin", b"x")
        assert store.exists("file.bin") is True

    def test_exists_false_after_delete(self, store: BlobStore) -> None:
        store.put("file.bin", b"x")
        store.delete("file.bin")
        assert store.exists("file.bin") is False

    # ------------------------------------------------------------------
    # get on missing key — returns None per ABC contract
    # ------------------------------------------------------------------

    def test_get_missing_key_returns_none(self, store: BlobStore) -> None:
        # The ABC docstring says: "Returns None if not found." The unit
        # brief asked for ``NotFoundError``; following the actual ABC
        # so the contract describes the implemented behaviour. See
        # ``base/blob.py::BlobStore.get``.
        assert store.get("does-not-exist.bin") is None

    # ------------------------------------------------------------------
    # delete
    # ------------------------------------------------------------------

    def test_delete_returns_true_when_existed(self, store: BlobStore) -> None:
        store.put("file.bin", b"x")
        assert store.delete("file.bin") is True

    def test_delete_returns_false_when_missing(self, store: BlobStore) -> None:
        assert store.delete("never-put.bin") is False

    def test_delete_is_idempotent(self, store: BlobStore) -> None:
        # Calling delete twice on the same key — including once when
        # the key never existed — must not raise. Useful for
        # cleanup-on-failure paths that don't want to track which
        # blobs they've already removed.
        store.delete("missing.bin")
        store.delete("missing.bin")
        store.put("file.bin", b"x")
        assert store.delete("file.bin") is True
        # Second delete on a now-missing key — returns False, no raise.
        assert store.delete("file.bin") is False

    # ------------------------------------------------------------------
    # list_keys
    # ------------------------------------------------------------------

    def test_list_keys_empty_store(self, store: BlobStore) -> None:
        assert store.list_keys() == []

    def test_list_keys_returns_all_keys(self, store: BlobStore) -> None:
        store.put("a.bin", b"a")
        store.put("b.bin", b"b")
        store.put("c.bin", b"c")
        assert sorted(store.list_keys()) == ["a.bin", "b.bin", "c.bin"]

    def test_list_keys_with_prefix(self, store: BlobStore) -> None:
        store.put("docs/readme.md", b"r")
        store.put("docs/changelog.md", b"c")
        store.put("uploads/photo.png", b"p")
        docs = sorted(store.list_keys(prefix="docs/"))
        assert docs == ["docs/changelog.md", "docs/readme.md"]

    def test_list_keys_prefix_no_match(self, store: BlobStore) -> None:
        store.put("docs/readme.md", b"r")
        assert store.list_keys(prefix="missing/") == []

    def test_list_keys_excludes_deleted(self, store: BlobStore) -> None:
        store.put("a.bin", b"a")
        store.put("b.bin", b"b")
        store.delete("a.bin")
        assert store.list_keys() == ["b.bin"]

    # ------------------------------------------------------------------
    # Size edge cases
    # ------------------------------------------------------------------

    def test_empty_blob_round_trip(self, store: BlobStore) -> None:
        # Zero-length payload — separately exercised because some
        # backends special-case empty bodies (S3's PutObject + an
        # empty Body, filesystems creating empty files).
        store.put("empty.bin", b"")
        assert store.exists("empty.bin") is True
        assert store.get("empty.bin") == b""

    def test_large_blob_round_trip_without_truncation(self, store: BlobStore) -> None:
        # 1 MiB of pseudo-random bytes — the pattern is deterministic
        # so the assertion failure (if any) doesn't depend on a seed.
        payload = bytes(range(256)) * (LARGE_BLOB_SIZE // 256)
        assert len(payload) == LARGE_BLOB_SIZE
        store.put("big.bin", payload)
        out = store.get("big.bin")
        assert out is not None
        assert len(out) == LARGE_BLOB_SIZE
        assert out == payload

    # ------------------------------------------------------------------
    # Key shapes
    # ------------------------------------------------------------------

    def test_key_with_slashes_round_trips(self, store: BlobStore) -> None:
        # Both backends model keys as strings with ``/`` as the
        # logical hierarchy separator (S3 by convention, Local maps
        # them to filesystem subdirectories).
        store.put("a/b/c/file.bin", b"deep")
        assert store.get("a/b/c/file.bin") == b"deep"
        assert store.exists("a/b/c/file.bin") is True

    def test_key_with_unicode_round_trips(self, store: BlobStore) -> None:
        # Key contains a non-ASCII codepoint. Both backends accept
        # arbitrary strings — the ABC does not declare a restricted
        # charset.
        key = "docs/résumé.txt"
        store.put(key, b"x")
        assert store.exists(key) is True
        assert store.get(key) == b"x"

    def test_get_uri_includes_key(self, store: BlobStore) -> None:
        uri = store.get_uri("docs/file.bin")
        assert "docs/file.bin" in uri
