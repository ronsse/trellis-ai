"""OpenLineage event extractor.

Reference implementation of the :class:`~trellis.extract.base.Extractor`
Protocol for a list of OpenLineage events.  The extractor is pure:
callers parse the file (JSON array or NDJSON) and hand the resulting
list to :meth:`extract`.
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


def _dataset_id(namespace: str, name: str) -> str:
    return f"dataset:{namespace}:{name}"


def _job_id(namespace: str, name: str) -> str:
    return f"job:{namespace}:{name}"


def _ensure_dataset(
    dataset: dict[str, Any],
    seen: dict[str, EntityDraft],
) -> str | None:
    """Register a dataset entity if not already seen.  Returns its id."""
    ds_ns = dataset.get("namespace", "")
    ds_name = dataset.get("name", "")
    if not ds_ns or not ds_name:
        return None
    did = _dataset_id(ds_ns, ds_name)
    if did not in seen:
        props: dict[str, Any] = {
            "namespace": ds_ns,
            "name": ds_name,
        }
        facets = dataset.get("facets") or {}
        if facets:
            props["facets"] = facets
        seen[did] = EntityDraft(
            entity_id=did,
            entity_type="dataset",
            name=ds_name,
            properties=props,
        )
    return did


class OpenLineageExtractor:
    """Deterministic extractor for OpenLineage event streams.

    Accepts a list of OpenLineage event dicts.  Produces job and dataset
    entities plus ``reads_from`` / ``writes_to`` edges, deduplicated by
    ``(source_id, target_id, edge_kind)``.
    """

    name = "openlineage"
    tier = ExtractorTier.DETERMINISTIC
    supported_sources: ClassVar[list[str]] = ["openlineage"]
    version = "0.1.0"

    async def extract(
        self,
        raw_input: Any,
        *,
        source_hint: str | None = None,
        context: ExtractionContext | None = None,
    ) -> ExtractionResult:
        del context  # deterministic — no cost budget

        if not isinstance(raw_input, list):
            msg = (
                "OpenLineageExtractor expects a list of event dicts; "
                f"got {type(raw_input).__name__}"
            )
            raise TypeError(msg)

        seen: dict[str, EntityDraft] = {}
        raw_edges: list[EdgeDraft] = []

        for event in raw_input:
            if not isinstance(event, dict):
                continue
            job_info = event.get("job") or {}
            job_ns = job_info.get("namespace", "")
            job_name = job_info.get("name", "")
            if not job_ns or not job_name:
                continue

            jid = _job_id(job_ns, job_name)
            if jid not in seen:
                seen[jid] = EntityDraft(
                    entity_id=jid,
                    entity_type="job",
                    name=job_name,
                    properties={
                        "namespace": job_ns,
                        "name": job_name,
                    },
                )

            for inp in event.get("inputs", []) or []:
                did = _ensure_dataset(inp, seen)
                if did:
                    raw_edges.append(
                        EdgeDraft(
                            source_id=jid,
                            target_id=did,
                            edge_kind="reads_from",
                        )
                    )

            for out in event.get("outputs", []) or []:
                did = _ensure_dataset(out, seen)
                if did:
                    raw_edges.append(
                        EdgeDraft(
                            source_id=jid,
                            target_id=did,
                            edge_kind="writes_to",
                        )
                    )

        # Deduplicate edges by (source, target, kind).
        unique_edges: list[EdgeDraft] = []
        seen_edge_keys: set[tuple[str, str, str]] = set()
        for edge in raw_edges:
            key = (edge.source_id, edge.target_id, edge.edge_kind)
            if key not in seen_edge_keys:
                seen_edge_keys.add(key)
                unique_edges.append(edge)

        entities = list(seen.values())
        logger.info(
            "openlineage_extracted",
            entities=len(entities),
            edges=len(unique_edges),
            source_hint=source_hint,
        )

        return ExtractionResult(
            entities=entities,
            edges=unique_edges,
            extractor_used=self.name,
            tier=self.tier.value,
            provenance=ExtractionProvenance(
                extractor_name=self.name,
                extractor_version=self.version,
                source_hint=source_hint,
            ),
        )
