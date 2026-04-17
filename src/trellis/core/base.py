"""Base Pydantic models for Trellis."""

from __future__ import annotations

import functools
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "0.1.0"


def utc_now() -> datetime:
    """Return the current UTC datetime."""
    return datetime.now(tz=UTC)


@functools.lru_cache(maxsize=1)
def get_version() -> str:
    """Return the package version, falling back to ``0.0.0-dev``."""
    try:
        from trellis._version import (  # noqa: PLC0415
            __version__,  # type: ignore[import-not-found]
        )
    except ImportError:
        return "0.0.0-dev"
    else:
        return __version__  # type: ignore[no-any-return]


class TrellisModel(BaseModel):
    """Base model with strict defaults."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_default=True,
        populate_by_name=True,
    )


class VersionedModel(TrellisModel):
    """Model that carries a schema version."""

    schema_version: str = Field(default=SCHEMA_VERSION)


class TimestampedModel(TrellisModel):
    """Model with automatic created/updated timestamps."""

    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
