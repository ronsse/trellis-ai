"""Example extractor — reads 'widgets' + 'containers' from a source.

Fork this file to build your own extractor.  The replace-me
hotspots:

* ``SourceData`` → the shape of whatever you're reading from
  (Unity Catalog metadata, a dbt manifest, an OpenLineage event).
* ``extract()`` body → your real field-mapping logic.
* ``name`` / ``version`` / ``tier`` → identify your package so the
  audit trail can attribute drafts to you.

Pattern reminders:

* Pure function: no network, no disk.  Pass in the already-fetched
  raw data and return a batch.
* Namespaced types (``example.widget``, ``example.contains``).
* Stable ``entity_id`` where possible — makes edges resolvable
  without a two-phase lookup.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from examples.trellis_example_extractor.types import (
    EDGE_KIND_CONTAINS,
    ENTITY_TYPE_CONTAINER,
    ENTITY_TYPE_WIDGET,
    ContainerProperties,
    WidgetProperties,
)
from trellis_sdk.extract import (
    EdgeDraft,
    EntityDraft,
    ExtractionBatch,
    ExtractorTier,
)


@dataclass
class Widget:
    id: str
    name: str
    color: str
    weight_grams: float
    tags: list[str] = field(default_factory=list)


@dataclass
class Container:
    id: str
    name: str
    capacity: int
    location: str
    widget_ids: list[str] = field(default_factory=list)


@dataclass
class SourceData:
    """Replace with whatever your real source system gives you."""

    snapshot_id: str  # stable per-sync identifier; feeds idempotency_key
    widgets: list[Widget] = field(default_factory=list)
    containers: list[Container] = field(default_factory=list)


class ExampleExtractor:
    """Deterministic extractor that maps Widgets + Containers to drafts.

    Conforms to :class:`trellis_sdk.extract.DraftExtractor`.
    """

    name = "examples.trellis_example_extractor.reader"
    version = "0.1.0"
    tier = ExtractorTier.DETERMINISTIC

    def extract(self, raw: SourceData) -> ExtractionBatch:
        entities: list[EntityDraft] = []
        edges: list[EdgeDraft] = []

        for widget in raw.widgets:
            props = WidgetProperties(
                color=widget.color,
                weight_grams=widget.weight_grams,
                tags=widget.tags,
            )
            entities.append(
                EntityDraft(
                    entity_type=ENTITY_TYPE_WIDGET,
                    name=widget.name,
                    entity_id=widget.id,
                    properties=props.model_dump(),
                )
            )

        for container in raw.containers:
            props = ContainerProperties(
                capacity=container.capacity,
                location=container.location,
            )
            entities.append(
                EntityDraft(
                    entity_type=ENTITY_TYPE_CONTAINER,
                    name=container.name,
                    entity_id=container.id,
                    properties=props.model_dump(),
                )
            )
            edges.extend(
                EdgeDraft(
                    source_id=container.id,
                    target_id=widget_id,
                    edge_kind=EDGE_KIND_CONTAINS,
                )
                for widget_id in container.widget_ids
            )

        return ExtractionBatch(
            source="example",
            extractor_name=self.name,
            extractor_version=self.version,
            tier=self.tier,
            entities=entities,
            edges=edges,
            idempotency_key=f"example-sync-{raw.snapshot_id}",
        )


__all__ = ["Container", "ExampleExtractor", "SourceData", "Widget"]
