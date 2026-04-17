"""Tests for entity schemas."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from trellis.schemas.entity import Entity, EntityAlias, EntitySource, GenerationSpec
from trellis.schemas.enums import EntityType, NodeRole


class TestEntityDefaults:
    """Entity creates with sensible defaults."""

    def test_minimal_entity(self) -> None:
        entity = Entity(
            entity_type=EntityType.PERSON,
            name="Alice",
        )
        assert entity.entity_id  # ULID generated
        assert len(entity.entity_id) == 26
        assert entity.entity_type == EntityType.PERSON
        assert entity.name == "Alice"
        assert entity.properties == {}
        assert entity.source is None
        assert entity.metadata == {}
        assert entity.created_at is not None
        assert entity.updated_at is not None
        assert entity.schema_version == "0.1.0"


class TestEntityWithPropertiesAndSource:
    """Entity with properties and source."""

    def test_entity_with_properties_and_source(self) -> None:
        src = EntitySource(
            origin="import",
            detail="CSV row 42",
            trace_id="trace-abc",
        )
        entity = Entity(
            entity_type=EntityType.SERVICE,
            name="auth-service",
            properties={"url": "https://auth.example.com", "version": "2.1"},
            source=src,
            metadata={"imported": True},
        )
        assert entity.entity_type == EntityType.SERVICE
        assert entity.properties["url"] == "https://auth.example.com"
        assert entity.source is not None
        assert entity.source.origin == "import"
        assert entity.source.detail == "CSV row 42"
        assert entity.source.trace_id == "trace-abc"
        assert entity.metadata == {"imported": True}

    def test_entity_source_minimal(self) -> None:
        src = EntitySource(origin="manual")
        assert src.origin == "manual"
        assert src.detail is None
        assert src.trace_id is None


class TestEntityTypeIsOpenString:
    """``entity_type`` accepts any string — the enum is advisory.

    Storage, mutation handlers, and the REST API all take ``entity_type``
    as a free-form string so domain-specific integrations (Unity Catalog
    tables, dbt models, etc.) can define their own types without
    extending the core enum. Lock that contract in at the schema layer
    so the ``Entity`` pydantic model can't silently drift back to a
    closed enum.
    """

    def test_custom_entity_type_string_accepted(self) -> None:
        entity = Entity(entity_type="uc_table", name="main.analytics.orders")
        assert entity.entity_type == "uc_table"

    def test_enum_value_still_accepted(self) -> None:
        entity = Entity(entity_type=EntityType.SERVICE, name="auth")
        # StrEnum values compare equal to their string literal
        assert entity.entity_type == EntityType.SERVICE
        assert entity.entity_type == "service"


class TestEntityForbidsExtras:
    """Entity rejects unknown fields."""

    def test_entity_forbids_extra_fields(self) -> None:
        with pytest.raises(ValidationError, match="extra_forbidden"):
            Entity(
                entity_type=EntityType.TOOL,
                name="hammer",
                nope="bad",  # type: ignore[call-arg]
            )

    def test_entity_source_forbids_extra_fields(self) -> None:
        with pytest.raises(ValidationError, match="extra_forbidden"):
            EntitySource(
                origin="x",
                bad_field=1,  # type: ignore[call-arg]
            )


class TestEntityAlias:
    """EntityAlias carries cross-system identity mappings."""

    def test_minimal_entity_alias(self) -> None:
        alias = EntityAlias(
            entity_id="ent_123",
            source_system="unity_catalog",
            raw_id="main.analytics.orders",
        )
        assert alias.alias_id
        assert alias.entity_id == "ent_123"
        assert alias.source_system == "unity_catalog"
        assert alias.raw_id == "main.analytics.orders"
        assert alias.raw_name is None
        assert alias.match_confidence == 1.0
        assert alias.is_primary is False

    def test_entity_alias_with_optional_fields(self) -> None:
        alias = EntityAlias(
            entity_id="ent_123",
            source_system="dbt",
            raw_id="model.project.orders",
            raw_name="orders",
            match_confidence=0.82,
            is_primary=True,
        )
        assert alias.raw_name == "orders"
        assert alias.match_confidence == 0.82
        assert alias.is_primary is True

    def test_entity_alias_forbids_extra_fields(self) -> None:
        with pytest.raises(ValidationError, match="extra_forbidden"):
            EntityAlias(
                entity_id="ent_123",
                source_system="git",
                raw_id="repo://pipelines/orders.sql",
                bad_field="nope",  # type: ignore[call-arg]
            )


class TestEntityNodeRole:
    """Entity carries a node_role and (for curated) a generation_spec."""

    def test_default_node_role_is_semantic(self) -> None:
        entity = Entity(entity_type=EntityType.SERVICE, name="auth")
        assert entity.node_role == NodeRole.SEMANTIC
        assert entity.generation_spec is None

    def test_structural_entity(self) -> None:
        entity = Entity(
            entity_type=EntityType.CONCEPT,
            name="orders.customer_id",
            node_role=NodeRole.STRUCTURAL,
        )
        assert entity.node_role == NodeRole.STRUCTURAL

    def test_curated_entity_requires_generation_spec(self) -> None:
        with pytest.raises(ValidationError, match="generation_spec is required"):
            Entity(
                entity_type=EntityType.DOMAIN,
                name="payments",
                node_role=NodeRole.CURATED,
            )

    def test_curated_entity_accepts_generation_spec(self) -> None:
        spec = GenerationSpec(
            generator_name="community_detection_louvain",
            generator_version="1.0.0",
            source_node_ids=["ent_a", "ent_b", "ent_c"],
            parameters={"resolution": 1.2},
        )
        entity = Entity(
            entity_type=EntityType.DOMAIN,
            name="payments",
            node_role=NodeRole.CURATED,
            generation_spec=spec,
        )
        assert entity.node_role == NodeRole.CURATED
        assert entity.generation_spec is not None
        assert entity.generation_spec.generator_name == "community_detection_louvain"
        assert entity.generation_spec.source_node_ids == ["ent_a", "ent_b", "ent_c"]

    def test_semantic_entity_rejects_generation_spec(self) -> None:
        spec = GenerationSpec(
            generator_name="precedent_promotion",
            generator_version="1.0.0",
        )
        with pytest.raises(ValidationError, match="generation_spec must be None"):
            Entity(
                entity_type=EntityType.SERVICE,
                name="auth",
                generation_spec=spec,
            )

    def test_structural_entity_rejects_generation_spec(self) -> None:
        spec = GenerationSpec(
            generator_name="precedent_promotion",
            generator_version="1.0.0",
        )
        with pytest.raises(ValidationError, match="generation_spec must be None"):
            Entity(
                entity_type=EntityType.CONCEPT,
                name="column",
                node_role=NodeRole.STRUCTURAL,
                generation_spec=spec,
            )


class TestGenerationSpec:
    """GenerationSpec is a minimal provenance record for curated nodes."""

    def test_minimal_generation_spec(self) -> None:
        spec = GenerationSpec(
            generator_name="precedent_promotion",
            generator_version="1.0.0",
        )
        assert spec.generator_name == "precedent_promotion"
        assert spec.generator_version == "1.0.0"
        assert spec.source_node_ids == []
        assert spec.source_trace_ids == []
        assert spec.parameters == {}
        assert spec.generated_at is not None

    def test_generation_spec_forbids_extras(self) -> None:
        with pytest.raises(ValidationError, match="extra_forbidden"):
            GenerationSpec(
                generator_name="x",
                generator_version="1",
                bogus="nope",  # type: ignore[call-arg]
            )
