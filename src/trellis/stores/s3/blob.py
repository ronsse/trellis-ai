"""S3BlobStore — S3-backed blob storage."""

from __future__ import annotations

from typing import Any

import structlog

from trellis.stores.base.blob import BlobStore

logger = structlog.get_logger(__name__)

try:
    import boto3
    from botocore.exceptions import ClientError

    HAS_BOTO3 = True
except ImportError:
    HAS_BOTO3 = False


class S3BlobStore(BlobStore):
    """Amazon S3-backed blob store.

    Parameters
    ----------
    bucket:
        S3 bucket name.
    prefix:
        Optional key prefix applied to all operations (e.g. ``"blobs/"``).
    region:
        AWS region. If *None*, boto3 uses its default resolution chain.
    """

    def __init__(
        self,
        bucket: str,
        prefix: str = "",
        region: str | None = None,
    ) -> None:
        if not HAS_BOTO3:
            msg = (
                "boto3 is required for S3BlobStore. Install it with: pip install boto3"
            )
            raise ImportError(msg)

        self._bucket = bucket
        self._prefix = prefix
        kwargs: dict[str, Any] = {}
        if region is not None:
            kwargs["region_name"] = region
        self._client = boto3.client("s3", **kwargs)
        logger.info(
            "s3_blob_store_initialized",
            bucket=bucket,
            prefix=prefix,
            region=region,
        )

    # ------------------------------------------------------------------
    # Key helpers
    # ------------------------------------------------------------------

    def _full_key(self, key: str) -> str:
        return f"{self._prefix}{key}"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def put(
        self,
        key: str,
        data: bytes,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        full_key = self._full_key(key)
        kwargs: dict[str, Any] = {
            "Bucket": self._bucket,
            "Key": full_key,
            "Body": data,
        }
        if metadata:
            kwargs["Metadata"] = {k: str(v) for k, v in metadata.items()}
        self._client.put_object(**kwargs)
        logger.debug("blob_stored", key=key, bucket=self._bucket)
        return self.get_uri(key)

    def get(self, key: str) -> bytes | None:
        try:
            response = self._client.get_object(
                Bucket=self._bucket,
                Key=self._full_key(key),
            )
            return bytes(response["Body"].read())
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("NoSuchKey", "404"):
                return None
            raise

    def delete(self, key: str) -> bool:
        existed = self.exists(key)
        self._client.delete_object(
            Bucket=self._bucket,
            Key=self._full_key(key),
        )
        return existed

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(
                Bucket=self._bucket,
                Key=self._full_key(key),
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey"):
                return False
            raise
        else:
            return True

    def list_keys(self, prefix: str = "") -> list[str]:
        full_prefix = self._full_key(prefix)
        keys: list[str] = []
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self._bucket, Prefix=full_prefix):
            for obj in page.get("Contents", []):
                raw_key: str = obj["Key"]
                # Strip the store-level prefix so callers see logical keys.
                if raw_key.startswith(self._prefix):
                    keys.append(raw_key[len(self._prefix) :])
                else:
                    keys.append(raw_key)
        return sorted(keys)

    def get_uri(self, key: str) -> str:
        return f"s3://{self._bucket}/{self._full_key(key)}"

    def close(self) -> None:
        logger.info("s3_blob_store_closed", bucket=self._bucket)
