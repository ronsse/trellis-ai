"""A pure-function extractor that turns your domain source data into drafts.

Shape-conforms to :class:`trellis_sdk.extract.DraftExtractor` — no
inheritance. Three class attributes (``name``, ``version``, ``tier``)
plus an ``extract()`` method is the whole contract.

**Replace-me hotspots:**

* ``ServiceCatalogSnapshot`` → the shape your real source returns
  (e.g. the tuple you get back from Backstage, a YAML registry, an
  internal API, etc.).
* ``extract()`` body → your field-mapping logic.
* The ``name`` / ``version`` / ``source`` strings → identify your
  package so the audit trail attributes drafts to you.
* The ``idempotency_key`` → must be stable per logical sync so reruns
  of the same snapshot deduplicate at the mutation pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from examples.client_starter.types import (
    EDGE_KIND_OWNED_BY,
    EDGE_KIND_READS_FROM,
    EDGE_KIND_WRITES_TO,
    ENTITY_TYPE_DATASET,
    ENTITY_TYPE_SERVICE,
    ENTITY_TYPE_TEAM,
    DatasetProperties,
    ServiceProperties,
    TeamProperties,
)
from trellis_sdk.extract import (
    EdgeDraft,
    EntityDraft,
    ExtractionBatch,
    ExtractorTier,
)

# --- Dataclasses mirroring your (fictional) source system ---------------


@dataclass
class Service:
    id: str
    name: str
    language: str
    repo_url: str
    criticality: str
    owner_team_id: str
    reads_dataset_ids: list[str] = field(default_factory=list)
    writes_dataset_ids: list[str] = field(default_factory=list)


@dataclass
class Team:
    id: str
    name: str
    slack_channel: str
    pager_rotation: str | None = None


@dataclass
class Dataset:
    id: str
    name: str
    platform: str
    row_estimate: int
    pii: bool = False


@dataclass
class ServiceCatalogSnapshot:
    """Whatever your real source fetcher returns — dataclass it."""

    snapshot_id: str  # stable per sync; feeds idempotency_key
    services: list[Service] = field(default_factory=list)
    teams: list[Team] = field(default_factory=list)
    datasets: list[Dataset] = field(default_factory=list)


# --- The extractor itself -----------------------------------------------


class ServiceCatalogExtractor:
    """Deterministic mapper: service catalog → entity/edge drafts.

    Pure function — no network, no disk. The caller fetches the snapshot
    and passes it in; ``submit_drafts()`` later is what crosses the wire.
    """

    name = "examples.client_starter.ServiceCatalogExtractor"
    version = "0.1.0"
    tier = ExtractorTier.DETERMINISTIC

    def extract(self, raw: ServiceCatalogSnapshot) -> ExtractionBatch:
        entities: list[EntityDraft] = [
            EntityDraft(
                entity_type=ENTITY_TYPE_TEAM,
                name=team.name,
                entity_id=team.id,
                properties=TeamProperties(
                    slack_channel=team.slack_channel,
                    pager_rotation=team.pager_rotation,
                ).model_dump(),
            )
            for team in raw.teams
        ]
        entities.extend(
            EntityDraft(
                entity_type=ENTITY_TYPE_DATASET,
                name=dataset.name,
                entity_id=dataset.id,
                properties=DatasetProperties(
                    platform=dataset.platform,
                    row_estimate=dataset.row_estimate,
                    pii=dataset.pii,
                ).model_dump(),
            )
            for dataset in raw.datasets
        )
        entities.extend(
            EntityDraft(
                entity_type=ENTITY_TYPE_SERVICE,
                name=svc.name,
                entity_id=svc.id,
                properties=ServiceProperties(
                    language=svc.language,
                    repo_url=svc.repo_url,
                    criticality=svc.criticality,
                ).model_dump(),
            )
            for svc in raw.services
        )

        edges: list[EdgeDraft] = []
        for svc in raw.services:
            edges.append(
                EdgeDraft(
                    source_id=svc.id,
                    target_id=svc.owner_team_id,
                    edge_kind=EDGE_KIND_OWNED_BY,
                )
            )
            edges.extend(
                EdgeDraft(
                    source_id=svc.id,
                    target_id=dataset_id,
                    edge_kind=EDGE_KIND_READS_FROM,
                )
                for dataset_id in svc.reads_dataset_ids
            )
            edges.extend(
                EdgeDraft(
                    source_id=svc.id,
                    target_id=dataset_id,
                    edge_kind=EDGE_KIND_WRITES_TO,
                )
                for dataset_id in svc.writes_dataset_ids
            )

        return ExtractionBatch(
            source="mycompany.catalog",
            extractor_name=self.name,
            extractor_version=self.version,
            tier=self.tier,
            entities=entities,
            edges=edges,
            idempotency_key=f"catalog-sync-{raw.snapshot_id}",
        )


__all__ = [
    "Dataset",
    "Service",
    "ServiceCatalogExtractor",
    "ServiceCatalogSnapshot",
    "Team",
]
