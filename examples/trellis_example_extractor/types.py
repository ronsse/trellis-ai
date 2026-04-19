"""Typed Pydantic models for the example domain.

These are optional — the wire contract accepts ``properties`` as a
plain ``dict[str, Any]`` escape hatch.  Defining typed shapes
here lets your extractor validate client-side before submission
and gives your team IDE autocomplete when constructing drafts.

When/if a server-side schema registry lands (deferred per
[TODO.md](../../TODO.md)), you'd register these shapes and the
server would validate incoming drafts against them.  Until then,
client-side validation is the only enforcement.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# Namespaced type constants.  Keep as module-level so forks can
# ``from your_pkg.types import ENTITY_TYPE_TABLE`` and avoid typos.
ENTITY_TYPE_WIDGET = "example.widget"
ENTITY_TYPE_CONTAINER = "example.container"

EDGE_KIND_CONTAINS = "example.contains"
EDGE_KIND_LIKES = "example.likes"


class WidgetProperties(BaseModel):
    """Properties payload for an ``example.widget`` entity."""

    model_config = ConfigDict(extra="forbid")

    color: Literal["red", "green", "blue"]
    weight_grams: float = Field(ge=0)
    tags: list[str] = Field(default_factory=list)


class ContainerProperties(BaseModel):
    """Properties payload for an ``example.container`` entity."""

    model_config = ConfigDict(extra="forbid")

    capacity: int = Field(ge=0)
    location: str


__all__ = [
    "EDGE_KIND_CONTAINS",
    "EDGE_KIND_LIKES",
    "ENTITY_TYPE_CONTAINER",
    "ENTITY_TYPE_WIDGET",
    "ContainerProperties",
    "WidgetProperties",
]
