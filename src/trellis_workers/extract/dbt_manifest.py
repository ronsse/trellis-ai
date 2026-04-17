"""dbt manifest extractor.

Reference implementation of the :class:`~trellis.extract.base.Extractor`
Protocol for dbt ``manifest.json`` payloads.  The extractor is pure: it
accepts the parsed manifest dict and returns an
:class:`~trellis.schemas.extraction.ExtractionResult`.  Callers load the
file and route the resulting drafts through the governed mutation
pipeline.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

import structlog

from trellis.extract.base import ExtractorTier
from trellis.schemas.extraction import (
    EdgeDraft,
    EntityDraft,
    ExtractionProvenance,
    ExtractionResult,
)

if TYPE_CHECKING:
    from trellis.extract.context import ExtractionContext

logger = structlog.get_logger(__name__)


_RESOURCE_TYPE_MAP: dict[str, str] = {
    "model": "dbt_model",
    "seed": "dbt_seed",
    "snapshot": "dbt_snapshot",
    "source": "dbt_source",
    "test": "dbt_test",
}


class DbtManifestExtractor:
    """Deterministic extractor for dbt ``manifest.json`` payloads.

    Produces one :class:`EntityDraft` per model / seed / snapshot / source
    / test, and one :class:`EdgeDraft` (``depends_on``) per entry in each
    resource's ``depends_on.nodes`` list.

    Descriptions are surfaced as an ``EntityDraft.properties["description"]``
    value.  Document-store indexing of those descriptions is a caller
    concern — this extractor has no store access.
    """

    name = "dbt_manifest"
    tier = ExtractorTier.DETERMINISTIC
    supported_sources: ClassVar[list[str]] = ["dbt-manifest"]
    version = "0.1.0"

    async def extract(
        self,
        raw_input: Any,
        *,
        source_hint: str | None = None,
        context: ExtractionContext | None = None,
    ) -> ExtractionResult:
        del context  # deterministic — no cost budget

        if not isinstance(raw_input, dict):
            msg = (
                "DbtManifestExtractor expects a parsed manifest dict; "
                f"got {type(raw_input).__name__}"
            )
            raise TypeError(msg)

        resources: list[dict[str, Any]] = list(raw_input.get("nodes", {}).values())
        resources.extend(raw_input.get("sources", {}).values())

        entities: list[EntityDraft] = []
        edges: list[EdgeDraft] = []

        for resource in resources:
            unique_id = resource.get("unique_id")
            if not unique_id:
                continue
            resource_type = resource.get("resource_type", "model")
            entity_type = _RESOURCE_TYPE_MAP.get(resource_type, f"dbt_{resource_type}")

            properties: dict[str, Any] = {
                "name": resource.get("name", ""),
                "unique_id": unique_id,
            }
            for field in ("schema", "database", "description", "tags"):
                if resource.get(field):
                    properties[field] = resource[field]

            if resource_type == "model":
                materialized = (resource.get("config") or {}).get("materialized")
                if materialized:
                    properties["materialized"] = materialized

            if resource_type == "source":
                source_name = resource.get("source_name")
                if source_name:
                    properties["source_name"] = source_name

            entities.append(
                EntityDraft(
                    entity_id=unique_id,
                    entity_type=entity_type,
                    name=resource.get("name", unique_id),
                    properties=properties,
                )
            )

            depends_on = resource.get("depends_on") or {}
            dep_nodes = (
                depends_on.get("nodes", []) if isinstance(depends_on, dict) else []
            )
            edges.extend(
                EdgeDraft(
                    source_id=unique_id,
                    target_id=dep_id,
                    edge_kind="depends_on",
                )
                for dep_id in dep_nodes
            )

        logger.info(
            "dbt_manifest_extracted",
            entities=len(entities),
            edges=len(edges),
            source_hint=source_hint,
        )

        return ExtractionResult(
            entities=entities,
            edges=edges,
            extractor_used=self.name,
            tier=self.tier.value,
            provenance=ExtractionProvenance(
                extractor_name=self.name,
                extractor_version=self.version,
                source_hint=source_hint,
            ),
        )
