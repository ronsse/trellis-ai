"""Tests for ``POST /api/v1/extract/drafts``.

Goes through :func:`trellis.testing.in_memory_client` so the full
SDK + wire + translator + route + MutationExecutor stack is
exercised.  That's the realistic shape: a client package calling
``client.submit_drafts(...)`` against a running server.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from trellis.testing import in_memory_client
from trellis_wire import (
    BatchStrategy,
    EdgeDraft,
    EntityDraft,
    ExtractionBatch,
    ExtractorTier,
)


@pytest.fixture
def client(tmp_path: Path):
    with in_memory_client(tmp_path / "stores") as c:
        yield c


class TestMinimalSubmission:
    def test_empty_batch_accepted(self, client):
        batch = ExtractionBatch(
            source="unity_catalog",
            extractor_name="test_extractor",
            extractor_version="0.1.0",
        )
        result = client.submit_drafts(batch)
        assert result.status == "ok"
        assert result.extractor == "test_extractor@0.1.0"
        assert result.entities_submitted == 0
        assert result.edges_submitted == 0
        assert result.succeeded == 0

    def test_single_entity_creates_node(self, client):
        batch = ExtractionBatch(
            source="unity_catalog",
            extractor_name="test_extractor",
            extractor_version="0.1.0",
            entities=[
                EntityDraft(
                    entity_type="unity_catalog.table",
                    name="sales.orders",
                    properties={"owner": "data-team"},
                ),
            ],
        )
        result = client.submit_drafts(batch)
        assert result.entities_submitted == 1
        assert result.succeeded == 1
        assert result.failed == 0
        assert len(result.results) == 1
        assert result.results[0].status == "success"
        assert result.results[0].operation == "entity.create"


class TestMultiEntityAndEdgeSubmission:
    def test_entities_then_edge(self, client):
        batch = ExtractionBatch(
            source="unity_catalog",
            extractor_name="test_extractor",
            extractor_version="0.2.0",
            entities=[
                EntityDraft(
                    entity_type="unity_catalog.table",
                    name="sales.orders",
                    entity_id="orders-id",
                ),
                EntityDraft(
                    entity_type="unity_catalog.table",
                    name="sales.refunds",
                    entity_id="refunds-id",
                ),
            ],
            edges=[
                EdgeDraft(
                    source_id="orders-id",
                    target_id="refunds-id",
                    edge_kind="unity_catalog.derived_from",
                ),
            ],
        )
        result = client.submit_drafts(batch)
        assert result.entities_submitted == 2
        assert result.edges_submitted == 1
        assert result.succeeded == 3  # 2 entities + 1 edge
        assert result.failed == 0


class TestAttribution:
    def test_requested_by_defaults_to_extractor_id(self, client):
        batch = ExtractionBatch(
            source="unity_catalog",
            extractor_name="test_extractor",
            extractor_version="0.3.0",
            entities=[
                EntityDraft(entity_type="t", name="n"),
            ],
        )
        result = client.submit_drafts(batch)
        assert result.extractor == "test_extractor@0.3.0"

    def test_explicit_requested_by_overrides(self, client):
        """Caller-provided requested_by takes precedence.

        We can't observe the requested_by on the audit trail from
        the wire response directly — the ``extractor`` field reflects
        the extractor identity, not the override.  Covered at the
        route level; here we just confirm the call succeeds with a
        custom value.
        """
        batch = ExtractionBatch(
            source="unity_catalog",
            extractor_name="test_extractor",
            extractor_version="0.1.0",
            entities=[EntityDraft(entity_type="t", name="n")],
        )
        result = client.submit_drafts(batch, requested_by="ci-run-42")
        assert result.succeeded == 1


class TestIdempotencyKey:
    def test_header_key_round_trips(self, client):
        batch = ExtractionBatch(
            source="unity_catalog",
            extractor_name="test_extractor",
            extractor_version="0.1.0",
            entities=[EntityDraft(entity_type="t", name="n")],
        )
        result = client.submit_drafts(batch, idempotency_key="uc-sync-001")
        assert result.idempotency_key == "uc-sync-001"

    def test_batch_key_used_when_header_absent(self, client):
        batch = ExtractionBatch(
            source="unity_catalog",
            extractor_name="test_extractor",
            extractor_version="0.1.0",
            entities=[EntityDraft(entity_type="t", name="n")],
            idempotency_key="batch-key-001",
        )
        result = client.submit_drafts(batch)
        assert result.idempotency_key == "batch-key-001"

    def test_header_wins_over_batch_key(self, client):
        batch = ExtractionBatch(
            source="unity_catalog",
            extractor_name="test_extractor",
            extractor_version="0.1.0",
            entities=[EntityDraft(entity_type="t", name="n")],
            idempotency_key="batch-key",
        )
        result = client.submit_drafts(batch, idempotency_key="header-key")
        assert result.idempotency_key == "header-key"


class TestTierTelemetry:
    @pytest.mark.parametrize(
        "tier",
        [
            ExtractorTier.DETERMINISTIC,
            ExtractorTier.HYBRID,
            ExtractorTier.LLM,
        ],
    )
    def test_tier_accepted(self, client, tier):
        batch = ExtractionBatch(
            source="s",
            extractor_name="e",
            extractor_version="0.1.0",
            tier=tier,
            entities=[EntityDraft(entity_type="t", name="n")],
        )
        result = client.submit_drafts(batch)
        assert result.succeeded == 1


class TestStrategyControl:
    def test_continue_on_error_default(self, client):
        batch = ExtractionBatch(
            source="s",
            extractor_name="e",
            extractor_version="0.1.0",
            entities=[EntityDraft(entity_type="t", name="n")],
        )
        result = client.submit_drafts(batch)
        assert result.strategy == "continue_on_error"

    def test_stop_on_error_propagates(self, client):
        batch = ExtractionBatch(
            source="s",
            extractor_name="e",
            extractor_version="0.1.0",
            entities=[EntityDraft(entity_type="t", name="n")],
        )
        result = client.submit_drafts(batch, strategy=BatchStrategy.STOP_ON_ERROR)
        assert result.strategy == "stop_on_error"


class TestNamespacedTypes:
    """Core accepts any string for entity_type / edge_kind — the
    'namespaced types are the extension path' contract."""

    def test_unity_catalog_namespace_accepted(self, client):
        batch = ExtractionBatch(
            source="unity_catalog",
            extractor_name="e",
            extractor_version="0.1.0",
            entities=[
                EntityDraft(
                    entity_type="unity_catalog.table",
                    name="sales.orders",
                ),
                EntityDraft(
                    entity_type="unity_catalog.schema",
                    name="sales",
                ),
            ],
        )
        result = client.submit_drafts(batch)
        assert result.succeeded == 2

    def test_arbitrary_domain_type_accepted(self, client):
        batch = ExtractionBatch(
            source="my_domain",
            extractor_name="e",
            extractor_version="0.1.0",
            entities=[
                EntityDraft(entity_type="my_domain.widget", name="widget-1"),
            ],
        )
        result = client.submit_drafts(batch)
        assert result.succeeded == 1
