"""Base classes for wire DTOs.

:class:`WireModel` is the default for response DTOs: ``extra="forbid"``,
whitespace-trimmed strings, validated defaults.  Not frozen — routes
still populate responses incrementally.

:class:`WireRequestModel` is for request DTOs constructed by clients:
same guarantees as :class:`WireModel` plus ``frozen=True``.  A request
object is a value; mutating it after construction is always a bug.

Both are standalone — **zero dependency on trellis core** so the wire
package stays independently installable.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class WireModel(BaseModel):
    """Base for wire DTOs.  Forbids unknown fields; permits mutation."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_default=True,
        populate_by_name=True,
    )


class WireRequestModel(WireModel):
    """Base for request DTOs.  Immutable once constructed."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_default=True,
        populate_by_name=True,
        frozen=True,
    )


__all__ = ["WireModel", "WireRequestModel"]
