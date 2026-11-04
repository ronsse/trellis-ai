"""One real learning promotion over an open source document, shared by suites.

Hand-seeding a precedent event and node tests the reader against a shape the
writer may never produce. ``promote`` runs the governed path a reviewed
promotion takes (``build_learning_promotion_payloads`` then
``submit_learning_promotion``), so the stored event and node are what
production holds. The caller relabels the source afterwards, which is the case
the promotion-time rule cannot see.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.unit.retrieve.test_sensitivity import label
from trellis.learning.scoring import (
    build_learning_promotion_payloads,
    submit_learning_promotion,
)
from trellis.mutate import build_curate_executor

if TYPE_CHECKING:
    from trellis.stores.registry import StoreRegistry

FAMILY = "asset_generation"
SOURCE_ID = "doc-quartz-7f3"
SOURCE_TITLE = "Quartz Title Words"
SOURCE_BODY = "Heron opening line SECRETWORD and the rest of the body."
SOURCE_EXCERPT = "Heron opening line"


def promote(registry: StoreRegistry) -> str:
    """Promote one learning candidate sourced from ``SOURCE_ID``; its entity id."""
    registry.knowledge.document_store.put(
        SOURCE_ID, SOURCE_BODY, {"title": SOURCE_TITLE}
    )
    candidate = {
        "candidate_id": "cand:one",
        "intent_family": FAMILY,
        "recommendation_type": "promote_guidance",
        "item_id": SOURCE_ID,
        "title": SOURCE_TITLE,
        "precedent_name": f"Learning: {FAMILY} :: {SOURCE_TITLE}",
        "precedent_name_source": "item_title",
        "metrics": {"success_rate": 0.9, "retry_rate": 0.1},
        "citation_evidence": {"helpful_count": 2, "unhelpful_count": 0},
        "precedent_properties": {"intent_family": FAMILY, "source_item_id": SOURCE_ID},
        "target_entity_ids": [],
        "supporting_run_ids": ["run-1"],
        "phases": ["GENERATE"],
    }
    payloads = build_learning_promotion_payloads(
        candidate=candidate,
        promotion_name="",
        rationale="because",
        document_store=registry.knowledge.document_store,
    )
    outcome = submit_learning_promotion(
        build_curate_executor(registry, clearance="restricted"),
        payloads["entity_payload"],
        payloads["edge_payloads"],
        requested_by="test",
        graph_store=registry.knowledge.graph_store,
        event_log=registry.operational.event_log,
        document_store=registry.knowledge.document_store,
    )
    assert outcome["status"] != "failed", outcome
    return str(payloads["entity_id"])


def relabel_source_restricted(registry: StoreRegistry) -> None:
    """Raise the source above a ``read`` caller after the promotion stored it."""
    registry.knowledge.document_store.put(
        SOURCE_ID, SOURCE_BODY, {"title": SOURCE_TITLE, **label("restricted")}
    )
