"""S3BlobStore — S3-backed blob storage."""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

import structlog

from trellis.core.base import utc_now
from trellis.schemas.blob import BlobGCReport
from trellis.stores.base.blob import BLOB_EXPIRES_AT_KEY, BlobStore
from trellis.stores.base.event_log import EventLog, EventType

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
        *,
        expires_at: datetime | None = None,
    ) -> str:
        full_key = self._full_key(key)
        kwargs: dict[str, Any] = {
            "Bucket": self._bucket,
            "Key": full_key,
            "Body": data,
        }
        merged: dict[str, Any] | None = None
        if metadata or expires_at is not None:
            merged = dict(metadata or {})
            if expires_at is not None:
                merged[BLOB_EXPIRES_AT_KEY] = expires_at.isoformat()
        if merged:
            kwargs["Metadata"] = {k: str(v) for k, v in merged.items()}
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

    def sweep_expired(
        self,
        before: datetime | None = None,
        *,
        prefix: str = "",
        dry_run: bool = False,
        event_log: EventLog | None = None,
    ) -> BlobGCReport:
        """Time-based GC sweep.

        S3 also supports bucket-level lifecycle rules which are usually
        the right knob for coarse retention; this sweep is for deployments
        that need shorter TTLs or ship without infrastructure-level
        policies configured. Walks ``list_keys(prefix)``, calls
        ``head_object`` on each, and deletes those whose
        :data:`BLOB_EXPIRES_AT_KEY` metadata is strictly in the past.
        """
        cutoff = before or utc_now()
        start_ns = time.monotonic_ns()
        swept = 0
        skipped_no_ttl = 0
        skipped_not_yet_expired = 0
        errors = 0

        for key in self.list_keys(prefix=prefix):
            try:
                head = self._client.head_object(
                    Bucket=self._bucket,
                    Key=self._full_key(key),
                )
            except ClientError:
                errors += 1
                logger.exception("blob_head_failed", key=key)
                continue
            raw = (head.get("Metadata") or {}).get(BLOB_EXPIRES_AT_KEY)
            if raw is None:
                skipped_no_ttl += 1
                continue
            try:
                expires_at = datetime.fromisoformat(raw)
            except (TypeError, ValueError):
                errors += 1
                logger.warning(
                    "blob_expires_at_parse_failed", key=key, value=raw
                )
                continue
            if expires_at >= cutoff:
                skipped_not_yet_expired += 1
                continue
            swept += 1
            if not dry_run:
                try:
                    self._client.delete_object(
                        Bucket=self._bucket,
                        Key=self._full_key(key),
                    )
                except ClientError:
                    errors += 1
                    swept -= 1
                    logger.exception("blob_delete_failed", key=key)

        report = BlobGCReport(
            before=cutoff,
            swept=swept,
            skipped_no_ttl=skipped_no_ttl,
            skipped_not_yet_expired=skipped_not_yet_expired,
            errors=errors,
            dry_run=dry_run,
            duration_ms=max((time.monotonic_ns() - start_ns) // 1_000_000, 0),
        )
        logger.info(
            "blob_gc_swept",
            before=cutoff.isoformat(),
            bucket=self._bucket,
            dry_run=dry_run,
            swept=swept,
            skipped_no_ttl=skipped_no_ttl,
            skipped_not_yet_expired=skipped_not_yet_expired,
            errors=errors,
            duration_ms=report.duration_ms,
        )
        if event_log is not None:
            event_log.emit(
                EventType.BLOB_GC_SWEPT,
                source="blob_store",
                payload=report.model_dump(mode="json")
                | {"bucket": self._bucket, "prefix": prefix},
            )
        return report

    def close(self) -> None:
        logger.info("s3_blob_store_closed", bucket=self._bucket)
