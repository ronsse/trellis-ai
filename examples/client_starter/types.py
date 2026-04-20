"""Domain types for a fictional internal service catalog.

Replace the constants and property shapes with your own domain. The
important pattern here is **namespaced strings** — Trellis does not
enforce a closed type enum at the storage layer, so every client is
expected to pick a prefix (``mycompany.*``) and keep its types there.

Typed property models are optional but recommended: they catch drift
client-side before a draft hits the wire. The server accepts a plain
``dict[str, Any]`` for ``properties`` today — client-side validation
is the only enforcement until the schema-registry item in TODO.md lands.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# --- Entity type constants ------------------------------------------------

ENTITY_TYPE_SERVICE = "mycompany.service"
ENTITY_TYPE_TEAM = "mycompany.team"
ENTITY_TYPE_DATASET = "mycompany.dataset"

# --- Edge type constants --------------------------------------------------

EDGE_KIND_OWNED_BY = "mycompany.owned_by"    # service  → team
EDGE_KIND_READS_FROM = "mycompany.reads_from"  # service  → dataset
EDGE_KIND_WRITES_TO = "mycompany.writes_to"    # service  → dataset


# --- Typed property payloads ---------------------------------------------


class ServiceProperties(BaseModel):
    """Properties for a ``mycompany.service`` entity."""

    model_config = ConfigDict(extra="forbid")

    language: Literal["python", "typescript", "go", "rust", "java"]
    repo_url: str
    criticality: Literal["tier1", "tier2", "tier3"] = "tier3"


class TeamProperties(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slack_channel: str
    pager_rotation: str | None = None


class DatasetProperties(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: Literal["snowflake", "bigquery", "s3", "postgres"]
    row_estimate: int = Field(ge=0)
    pii: bool = False


__all__ = [
    "DatasetProperties",
    "EDGE_KIND_OWNED_BY",
    "EDGE_KIND_READS_FROM",
    "EDGE_KIND_WRITES_TO",
    "ENTITY_TYPE_DATASET",
    "ENTITY_TYPE_SERVICE",
    "ENTITY_TYPE_TEAM",
    "ServiceProperties",
    "TeamProperties",
]
