"""Tests for S3BlobStore using mocked boto3."""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Build fake boto3 / botocore modules so the guarded import in blob.py
# succeeds even when boto3 is not installed.
# ---------------------------------------------------------------------------


class _ClientError(Exception):
    """Minimal stand-in for botocore.exceptions.ClientError."""

    def __init__(self, error_response: dict, operation_name: str) -> None:
        self.response = error_response
        self.operation_name = operation_name
        super().__init__(str(error_response))


_fake_botocore = ModuleType("botocore")
_fake_botocore_exceptions = ModuleType("botocore.exceptions")
_fake_botocore_exceptions.ClientError = _ClientError  # type: ignore[attr-defined]
_fake_botocore.exceptions = _fake_botocore_exceptions  # type: ignore[attr-defined]

_fake_boto3 = MagicMock()
_fake_boto3.__name__ = "boto3"


@pytest.fixture(autouse=True)
def _inject_fake_boto3():
    """Inject fake boto3/botocore into sys.modules for the test session."""
    saved = {
        k: sys.modules.get(k) for k in ("boto3", "botocore", "botocore.exceptions")
    }
    sys.modules["boto3"] = _fake_boto3
    sys.modules["botocore"] = _fake_botocore
    sys.modules["botocore.exceptions"] = _fake_botocore_exceptions

    # Force reimport so the module picks up the fakes.
    if "trellis.stores.s3.blob" in sys.modules:
        del sys.modules["trellis.stores.s3.blob"]
    if "trellis.stores.s3" in sys.modules:
        del sys.modules["trellis.stores.s3"]

    yield

    # Restore original module state.
    for k, v in saved.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v


@pytest.fixture
def mock_client():
    """Return a fresh MagicMock S3 client and wire it into fake boto3."""
    client = MagicMock()
    _fake_boto3.client.return_value = client
    return client


@pytest.fixture
def store(mock_client):
    """Create an S3BlobStore backed by the mocked client."""
    from trellis.stores.s3.blob import S3BlobStore

    return S3BlobStore(bucket="test-bucket", prefix="blobs/", region="us-east-1")


# ---- helpers ---------------------------------------------------------------


def _client_error(code: str, message: str = "") -> _ClientError:
    return _ClientError(
        {"Error": {"Code": code, "Message": message}},
        "TestOp",
    )


# ---- tests -----------------------------------------------------------------


class TestPut:
    def test_returns_s3_uri(self, store, mock_client):
        uri = store.put("docs/file.txt", b"hello")
        assert uri == "s3://test-bucket/blobs/docs/file.txt"

    def test_calls_put_object(self, store, mock_client):
        store.put("key.bin", b"\x00\x01", metadata={"author": "agent"})
        mock_client.put_object.assert_called_once_with(
            Bucket="test-bucket",
            Key="blobs/key.bin",
            Body=b"\x00\x01",
            Metadata={"author": "agent"},
        )

    def test_put_without_metadata(self, store, mock_client):
        store.put("key.bin", b"data")
        call_kwargs = mock_client.put_object.call_args[1]
        assert "Metadata" not in call_kwargs


class TestGet:
    def test_returns_bytes(self, store, mock_client):
        body = MagicMock()
        body.read.return_value = b"contents"
        mock_client.get_object.return_value = {"Body": body}

        result = store.get("key.bin")
        assert result == b"contents"
        mock_client.get_object.assert_called_once_with(
            Bucket="test-bucket",
            Key="blobs/key.bin",
        )

    def test_returns_none_on_no_such_key(self, store, mock_client):
        mock_client.get_object.side_effect = _client_error("NoSuchKey", "not found")
        assert store.get("missing.bin") is None

    def test_returns_none_on_404(self, store, mock_client):
        mock_client.get_object.side_effect = _client_error("404", "not found")
        assert store.get("missing.bin") is None

    def test_raises_on_other_error(self, store, mock_client):
        mock_client.get_object.side_effect = _client_error("AccessDenied", "forbidden")
        with pytest.raises(_ClientError):
            store.get("forbidden.bin")


class TestDelete:
    def test_returns_true_when_existed(self, store, mock_client):
        mock_client.head_object.return_value = {}
        assert store.delete("key.bin") is True
        mock_client.delete_object.assert_called_once()

    def test_returns_false_when_missing(self, store, mock_client):
        mock_client.head_object.side_effect = _client_error("404")
        assert store.delete("missing.bin") is False
        mock_client.delete_object.assert_called_once()


class TestExists:
    def test_returns_true(self, store, mock_client):
        mock_client.head_object.return_value = {}
        assert store.exists("key.bin") is True

    def test_returns_false_on_404(self, store, mock_client):
        mock_client.head_object.side_effect = _client_error("404")
        assert store.exists("missing.bin") is False


class TestListKeys:
    def test_returns_keys_stripped_of_prefix(self, store, mock_client):
        paginator = MagicMock()
        mock_client.get_paginator.return_value = paginator
        paginator.paginate.return_value = [
            {
                "Contents": [
                    {"Key": "blobs/a.txt"},
                    {"Key": "blobs/b.txt"},
                ]
            }
        ]
        keys = store.list_keys()
        assert keys == ["a.txt", "b.txt"]

    def test_empty_bucket(self, store, mock_client):
        paginator = MagicMock()
        mock_client.get_paginator.return_value = paginator
        paginator.paginate.return_value = [{}]
        assert store.list_keys() == []

    def test_prefix_filtering(self, store, mock_client):
        paginator = MagicMock()
        mock_client.get_paginator.return_value = paginator
        paginator.paginate.return_value = [
            {"Contents": [{"Key": "blobs/docs/readme.md"}]}
        ]
        keys = store.list_keys(prefix="docs/")
        assert keys == ["docs/readme.md"]
        paginator.paginate.assert_called_once_with(
            Bucket="test-bucket", Prefix="blobs/docs/"
        )


class TestGetUri:
    def test_format(self, store):
        assert store.get_uri("path/to/file") == "s3://test-bucket/blobs/path/to/file"

    def test_no_prefix(self, mock_client):
        from trellis.stores.s3.blob import S3BlobStore

        s = S3BlobStore(bucket="b", prefix="")
        assert s.get_uri("key") == "s3://b/key"


class TestClose:
    def test_close_does_not_raise(self, store):
        store.close()
