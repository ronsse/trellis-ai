"""ULID generation utilities."""

from __future__ import annotations

import ulid


def generate_ulid() -> str:
    """Return a 26-character ULID string."""
    return str(ulid.new())


def generate_prefixed_id(prefix: str) -> str:
    """Return a prefixed ULID in the form ``{prefix}_{ulid}``."""
    return f"{prefix}_{ulid.new()}"


def ulid_to_timestamp(ulid_str: str) -> float:
    """Extract Unix timestamp from a (possibly prefixed) ULID string."""
    # Strip prefix if present
    if "_" in ulid_str:
        ulid_str = ulid_str.rsplit("_", maxsplit=1)[1]
    return float(ulid.from_str(ulid_str).timestamp().timestamp)
