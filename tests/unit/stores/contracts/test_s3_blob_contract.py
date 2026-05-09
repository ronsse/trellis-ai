# ruff: noqa: N803 — boto3's S3 client uses PascalCase keyword arguments
# (Bucket, Key, Body, Metadata, Prefix); the in-memory fake mirrors them
# exactly so callers see an authentic boto3 signature.
"""Run the BlobStore contract suite against S3BlobStore.

The existing :mod:`tests.unit.stores.test_s3_blob` injects fake
``boto3``/``botocore`` modules with a :class:`MagicMock` client and
asserts on individual call kwargs. The contract suite needs *stateful*
behaviour — round-tripping bytes, listing keys after writes, exists →
delete → exists again — so a MagicMock per call is the wrong tool.

Instead we mirror that file's module-injection trick (so the guarded
import in ``trellis.stores.s3.blob`` succeeds without ``boto3``
installed) and back the client with a tiny in-memory dict that
implements the four S3 operations the contract exercises:
``put_object``, ``get_object``, ``delete_object``, ``head_object``,
and the ``list_objects_v2`` paginator.

This is intentionally NOT ``moto``. The repo does not depend on
``moto`` (the existing s3 test uses MagicMock fakes), and pulling
``moto`` in for one contract suite would expand the dev-dependency
surface for marginal gain — the in-memory backend covers the same
contract semantics faster and with no new wheel.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from types import ModuleType
from typing import Any
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Fake botocore.exceptions.ClientError — must exist before we import
# ``trellis.stores.s3.blob`` so its ``except ClientError`` clauses bind
# to this class.
# ---------------------------------------------------------------------------


class _ClientError(Exception):
    """Minimal stand-in for botocore.exceptions.ClientError."""

    def __init__(self, error_response: dict[str, Any], operation_name: str) -> None:
        self.response = error_response
        self.operation_name = operation_name
        super().__init__(str(error_response))


# ---------------------------------------------------------------------------
# Stateful in-memory S3 client — covers the surface S3BlobStore touches.
# ---------------------------------------------------------------------------


class _InMemoryS3Client:
    """Just enough of the boto3 S3 client to back the contract tests.

    Stores objects in a ``{(bucket, key): {"body": bytes, "metadata":
    dict}}`` dict and serves reads from there. The contract suite
    only exercises the public ``BlobStore`` surface; everything below
    is the minimum needed to make those operations behave like S3.
    """

    def __init__(self) -> None:
        self._objects: dict[tuple[str, str], dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Object operations
    # ------------------------------------------------------------------

    def put_object(
        self,
        *,
        Bucket: str,
        Key: str,
        Body: bytes,
        Metadata: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        self._objects[(Bucket, Key)] = {
            "body": Body,
            "metadata": dict(Metadata or {}),
        }
        return {}

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        try:
            obj = self._objects[(Bucket, Key)]
        except KeyError:
            raise _ClientError(
                {"Error": {"Code": "NoSuchKey", "Message": "not found"}},
                "GetObject",
            ) from None
        body = MagicMock()
        body.read.return_value = obj["body"]
        return {"Body": body, "Metadata": obj["metadata"]}

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        try:
            obj = self._objects[(Bucket, Key)]
        except KeyError:
            raise _ClientError(
                {"Error": {"Code": "404", "Message": "not found"}},
                "HeadObject",
            ) from None
        return {"Metadata": obj["metadata"]}

    def delete_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        # S3's DeleteObject is idempotent — succeeds whether or not
        # the key was present.
        self._objects.pop((Bucket, Key), None)
        return {}

    # ------------------------------------------------------------------
    # Paginator for list_objects_v2
    # ------------------------------------------------------------------

    def get_paginator(self, name: str) -> _ListObjectsPaginator:
        assert name == "list_objects_v2"
        return _ListObjectsPaginator(self._objects)


class _ListObjectsPaginator:
    def __init__(self, objects: dict[tuple[str, str], dict[str, Any]]) -> None:
        self._objects = objects

    def paginate(self, *, Bucket: str, Prefix: str = "") -> Iterator[dict[str, Any]]:
        contents = [
            {"Key": key}
            for (bucket, key) in self._objects
            if bucket == Bucket and key.startswith(Prefix)
        ]
        # boto3 returns Contents only when there's at least one match.
        if contents:
            yield {"Contents": contents}
        else:
            yield {}


# ---------------------------------------------------------------------------
# Module injection — same shape as ``test_s3_blob.py``.
# ---------------------------------------------------------------------------


_fake_botocore = ModuleType("botocore")
_fake_botocore_exceptions = ModuleType("botocore.exceptions")
_fake_botocore_exceptions.ClientError = _ClientError  # type: ignore[attr-defined]
_fake_botocore.exceptions = _fake_botocore_exceptions  # type: ignore[attr-defined]

_fake_boto3 = MagicMock()
_fake_boto3.__name__ = "boto3"


@pytest.fixture(autouse=True)
def _inject_fake_boto3() -> Iterator[None]:
    """Inject fake boto3/botocore into ``sys.modules`` for the test session."""
    saved = {
        k: sys.modules.get(k) for k in ("boto3", "botocore", "botocore.exceptions")
    }
    sys.modules["boto3"] = _fake_boto3
    sys.modules["botocore"] = _fake_botocore
    sys.modules["botocore.exceptions"] = _fake_botocore_exceptions

    # Force reimport so ``trellis.stores.s3.blob`` picks up the fakes.
    sys.modules.pop("trellis.stores.s3.blob", None)
    sys.modules.pop("trellis.stores.s3", None)

    yield

    for k, v in saved.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v


# ---------------------------------------------------------------------------
# Contract subclass
# ---------------------------------------------------------------------------


from tests.unit.stores.contracts.blob_store_contract import (  # noqa: E402
    BlobStoreContractTests,
)


class TestS3BlobContract(BlobStoreContractTests):
    @pytest.fixture
    def store(self) -> Iterator[Any]:
        # Each test gets its own fresh in-memory client so state
        # never leaks between tests.
        client = _InMemoryS3Client()
        _fake_boto3.client.return_value = client

        from trellis.stores.s3.blob import S3BlobStore

        s = S3BlobStore(bucket="contract-bucket", prefix="blobs/")
        yield s
        s.close()
