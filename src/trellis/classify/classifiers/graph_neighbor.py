"""GraphNeighborClassifier — infers tags from connected nodes' existing tags."""

from __future__ import annotations

from typing import Any

from trellis.classify.protocol import (
    ClassificationContext,
    ClassificationResult,
)

_PROPAGATABLE_FACETS = ("domain", "content_type", "scope")


class GraphNeighborClassifier:
    """Infer tags from 1-hop graph neighbors via majority vote.

    This classifier does not look at content — it looks at the graph
    neighborhood. If a node's neighbors agree on a tag with sufficient
    confidence, the tag propagates with a confidence decay.

    **Enrichment-only** — graph state changes between ingest and enrichment.
    Running inline at ingest time bakes in a transient snapshot; running at
    enrichment time uses the richer, stabilised graph.
    """

    def __init__(
        self,
        graph_store: Any,
        min_neighbor_confidence: float = 0.8,
        min_vote_fraction: float = 0.5,
        confidence_decay: float = 0.85,
    ) -> None:
        self._store = graph_store
        self._min_neighbor_confidence = min_neighbor_confidence
        self._min_vote_fraction = min_vote_fraction
        self._confidence_decay = confidence_decay

    @property
    def name(self) -> str:
        return "graph_neighbor"

    @property
    def allowed_modes(self) -> frozenset[str]:
        from trellis.classify.protocol import ENRICHMENT_ONLY  # noqa: PLC0415

        return ENRICHMENT_ONLY

    def classify(  # noqa: PLR0912
        self,
        content: str,  # noqa: ARG002
        *,
        context: ClassificationContext | None = None,
    ) -> ClassificationResult:
        if not context or not context.node_id:
            return ClassificationResult(
                tags={}, confidence=0.0, classifier_name=self.name
            )

        edges = self._store.get_edges(context.node_id, direction="both")
        if not edges:
            return ClassificationResult(
                tags={}, confidence=0.0, classifier_name=self.name
            )

        # Collect neighbor IDs from both directions
        neighbor_ids = []
        for e in edges:
            if e["source_id"] == context.node_id:
                neighbor_ids.append(e["target_id"])
            else:
                neighbor_ids.append(e["source_id"])

        neighbors = self._store.get_nodes_bulk(neighbor_ids)
        if not neighbors:
            return ClassificationResult(
                tags={}, confidence=0.0, classifier_name=self.name
            )

        total_neighbors = len(neighbors)

        # Collect votes per facet from high-confidence neighbors
        facet_votes: dict[str, dict[str, float]] = {}

        for node in neighbors:
            props = node.get("properties", {})
            neighbor_tags = props.get("content_tags", {})
            neighbor_conf = float(props.get("classification_confidence", 0.0))

            if neighbor_conf < self._min_neighbor_confidence:
                continue

            for facet in _PROPAGATABLE_FACETS:
                values = neighbor_tags.get(facet, [])
                if isinstance(values, str):
                    values = [values]
                for value in values:
                    facet_votes.setdefault(facet, {}).setdefault(value, 0.0)
                    facet_votes[facet][value] += 1.0

        # Keep values where enough neighbors agree
        tags: dict[str, list[str]] = {}
        for facet, votes in facet_votes.items():
            for value, count in votes.items():
                if count / total_neighbors >= self._min_vote_fraction:
                    tags.setdefault(facet, []).append(value)

        if not tags:
            return ClassificationResult(
                tags={}, confidence=0.0, classifier_name=self.name
            )

        return ClassificationResult(
            tags=tags,
            confidence=self._confidence_decay,
            classifier_name=self.name,
        )
