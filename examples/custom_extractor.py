"""Define a custom extractor that turns a domain-specific JSON source into
EntityDraft + EdgeDraft records.

STATUS: PREVIEW — examples are in flux while parallel work lands. Expect
breaking changes before the next minor release.

Extractors are *pure* — they never touch a store. The dispatcher (or your
own wrapper code) routes their drafts through MutationExecutor for governed
creation. This keeps extraction testable in isolation and enforces the
audit pipeline.

This example pretends we have a JSON export from an internal "service
registry" listing services and their dependencies. Real-world parallels:
dbt manifests, OpenLineage events, AWS resource graphs, GitHub repo
metadata.

Run:
    python examples/custom_extractor.py
"""

from __future__ import annotations

from typing import Any

from trellis.extract.base import Extractor, ExtractorTier
from trellis.extract.context import ExtractionContext
from trellis.schemas.extraction import (
    EdgeDraft,
    EntityDraft,
    ExtractionProvenance,
    ExtractionResult,
)


class ServiceRegistryExtractor:
    """Deterministic extractor for our fictitious service-registry JSON.

    Input shape:
        {
          "services": [
            {"id": "orders", "language": "python",
             "depends_on": ["payments", "users"]},
            ...
          ]
        }
    """

    name = "service-registry"
    tier = ExtractorTier.DETERMINISTIC
    supported_sources = ["service-registry"]
    version = "0.1.0"

    async def extract(
        self,
        raw_input: Any,
        *,
        source_hint: str | None = None,
        context: ExtractionContext | None = None,
    ) -> ExtractionResult:
        services = raw_input.get("services", [])
        entities: list[EntityDraft] = []
        edges: list[EdgeDraft] = []

        # First pass: emit one EntityDraft per service. We use the registry
        # ID directly so cross-references in the same payload resolve cleanly.
        for svc in services:
            entities.append(
                EntityDraft(
                    entity_id=svc["id"],
                    entity_type="service",
                    name=svc["id"],
                    properties={"language": svc.get("language", "unknown")},
                )
            )

        # Second pass: emit edges. Draft IDs are resolved downstream when the
        # CLI/API layer turns these into LINK_CREATE commands.
        for svc in services:
            for dep in svc.get("depends_on", []):
                edges.append(
                    EdgeDraft(
                        source_id=svc["id"],
                        target_id=dep,
                        edge_kind="entity_depends_on",
                    )
                )

        return ExtractionResult(
            entities=entities,
            edges=edges,
            extractor_used=self.name,
            tier=self.tier.value,
            llm_calls=0,
            tokens_used=0,
            overall_confidence=1.0,
            provenance=ExtractionProvenance(
                extractor_name=self.name,
                extractor_version=self.version,
                source_hint=source_hint,
            ),
        )


async def main() -> None:
    extractor: Extractor = ServiceRegistryExtractor()

    raw = {
        "services": [
            {"id": "orders", "language": "python", "depends_on": ["payments"]},
            {"id": "payments", "language": "go", "depends_on": ["users"]},
            {"id": "users", "language": "python", "depends_on": []},
        ]
    }

    result = await extractor.extract(raw, source_hint="service-registry")
    print(f"Extracted {len(result.entities)} entities, {len(result.edges)} edges")
    for e in result.entities:
        print(f"  entity: {e.entity_id} ({e.entity_type}) {e.properties}")
    for edge in result.edges:
        print(f"  edge:   {edge.source_id} -[{edge.edge_kind}]-> {edge.target_id}")

    # To wire this into the governed pipeline, register it with the
    # ExtractionDispatcher and let it route drafts through
    # MutationExecutor. See src/trellis/extract/dispatcher.py and
    # docs/agent-guide/playbooks.md for the full path.


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
