"""Tests for trellis.schemas.well_known — canonical entity / edge names."""

from __future__ import annotations

import pytest

from trellis.schemas import well_known as wk
from trellis.schemas.enums import EdgeKind, EntityType

# ---------------------------------------------------------------------------
# Entity type — canonical / alias / unknown
# ---------------------------------------------------------------------------


class TestCanonicalizeEntityType:
    @pytest.mark.parametrize(
        ("alias", "canonical"),
        [
            ("person", wk.PERSON),
            ("team", wk.TEAM),
            ("system", wk.SOFTWARE_APPLICATION),
            ("service", wk.SOFTWARE_APPLICATION),
            ("tool", wk.SOFTWARE_APPLICATION),
            ("document", wk.CREATIVE_WORK),
            ("file", wk.FILE),
            ("project", wk.PROJECT),
            ("concept", wk.CONCEPT),
        ],
    )
    def test_alias_resolves_to_canonical(self, alias: str, canonical: str) -> None:
        assert wk.canonicalize_entity_type(alias) == canonical

    def test_canonical_passes_through(self) -> None:
        for canonical in wk.CANONICAL_ENTITY_TYPES:
            assert wk.canonicalize_entity_type(canonical) == canonical

    def test_unknown_value_passes_through(self) -> None:
        assert wk.canonicalize_entity_type("dbt_model") == "dbt_model"
        assert wk.canonicalize_entity_type("custom_type") == "custom_type"

    def test_canonicalize_is_idempotent(self) -> None:
        for value in [
            "person",
            wk.PERSON,
            "system",
            wk.SOFTWARE_APPLICATION,
            "unknown",
            "",
        ]:
            once = wk.canonicalize_entity_type(value)
            twice = wk.canonicalize_entity_type(once)
            assert once == twice

    def test_domain_legacy_value_is_not_aliased(self) -> None:
        # ``domain`` is intentionally not in the alias map — it collides
        # with ContentTags.domain. It passes through as an open string.
        assert wk.canonicalize_entity_type("domain") == "domain"
        assert not wk.is_canonical_entity_type("domain")
        assert not wk.is_known_entity_type("domain")


class TestEntityTypePredicates:
    def test_is_canonical_for_canonical(self) -> None:
        assert wk.is_canonical_entity_type(wk.PERSON)
        assert wk.is_canonical_entity_type(wk.AGENT)
        assert wk.is_canonical_entity_type(wk.ACTIVITY)

    def test_is_canonical_false_for_alias(self) -> None:
        assert not wk.is_canonical_entity_type("person")
        assert not wk.is_canonical_entity_type("system")

    def test_is_known_for_canonical_and_alias(self) -> None:
        assert wk.is_known_entity_type(wk.PERSON)
        assert wk.is_known_entity_type("person")
        assert wk.is_known_entity_type("system")

    def test_is_known_false_for_arbitrary(self) -> None:
        assert not wk.is_known_entity_type("dbt_model")
        assert not wk.is_known_entity_type("")


class TestEntityTypeStructure:
    def test_every_alias_resolves_to_a_canonical(self) -> None:
        for alias, target in wk.ENTITY_TYPE_ALIASES.items():
            assert target in wk.CANONICAL_ENTITY_TYPES, (
                f"Alias {alias!r} maps to {target!r} which is not canonical"
            )

    def test_no_alias_collides_with_a_canonical_name(self) -> None:
        # Aliases are lowercase-only; canonical names are PascalCase.
        # If an alias is also a canonical name, ``canonicalize`` becomes
        # ambiguous — guard against that drift.
        assert wk.ENTITY_TYPE_ALIASES.keys().isdisjoint(wk.CANONICAL_ENTITY_TYPES)

    def test_legacy_enum_values_are_aliased_or_dropped(self) -> None:
        # Every legacy ``EntityType`` member is either an alias (resolves
        # to canonical) or explicitly dropped (currently only ``domain``).
        dropped = {"domain"}
        for member in EntityType:
            value = member.value
            if value in dropped:
                assert not wk.is_known_entity_type(value)
            else:
                assert wk.is_known_entity_type(value), (
                    f"EntityType.{member.name} = {value!r} has no canonical"
                )


# ---------------------------------------------------------------------------
# Edge kind — canonical / alias / unknown
# ---------------------------------------------------------------------------


class TestCanonicalizeEdgeKind:
    @pytest.mark.parametrize(
        ("alias", "canonical"),
        [
            ("trace_used_evidence", wk.USED),
            ("trace_produced_artifact", wk.WAS_GENERATED_BY),
            ("trace_touched_entity", wk.WAS_INFORMED_BY),
            ("trace_promoted_to_precedent", wk.WAS_DERIVED_FROM),
            ("precedent_derived_from", wk.WAS_DERIVED_FROM),
            ("entity_related_to", wk.RELATED_TO),
            ("entity_part_of", wk.PART_OF),
            ("entity_depends_on", wk.DEPENDS_ON),
            ("evidence_attached_to", wk.ATTACHED_TO),
            ("evidence_supports", wk.SUPPORTS),
            ("precedent_applies_to", wk.APPLIES_TO),
        ],
    )
    def test_alias_resolves_to_canonical(self, alias: str, canonical: str) -> None:
        assert wk.canonicalize_edge_kind(alias) == canonical

    def test_canonical_passes_through(self) -> None:
        for canonical in wk.CANONICAL_EDGE_KINDS:
            assert wk.canonicalize_edge_kind(canonical) == canonical

    def test_unknown_value_passes_through(self) -> None:
        assert wk.canonicalize_edge_kind("dbt_model_references") == (
            "dbt_model_references"
        )

    def test_canonicalize_is_idempotent(self) -> None:
        for value in [
            "trace_used_evidence",
            wk.USED,
            "entity_part_of",
            wk.PART_OF,
            "unknown_edge",
        ]:
            once = wk.canonicalize_edge_kind(value)
            twice = wk.canonicalize_edge_kind(once)
            assert once == twice

    def test_two_legacy_kinds_collapse_onto_was_derived_from(self) -> None:
        # Both ``trace_promoted_to_precedent`` and ``precedent_derived_from``
        # capture the same PROV-O concept.
        assert (
            wk.canonicalize_edge_kind("trace_promoted_to_precedent")
            == wk.canonicalize_edge_kind("precedent_derived_from")
            == wk.WAS_DERIVED_FROM
        )


class TestEdgeKindStructure:
    def test_every_alias_resolves_to_a_canonical(self) -> None:
        for alias, target in wk.EDGE_KIND_ALIASES.items():
            assert target in wk.CANONICAL_EDGE_KINDS, (
                f"Alias {alias!r} maps to {target!r} which is not canonical"
            )

    def test_no_alias_collides_with_a_canonical_name(self) -> None:
        assert wk.EDGE_KIND_ALIASES.keys().isdisjoint(wk.CANONICAL_EDGE_KINDS)

    def test_legacy_enum_values_are_all_aliased(self) -> None:
        # Unlike EntityType (which dropped ``domain``), every existing
        # EdgeKind value has a canonical mapping.
        for member in EdgeKind:
            assert wk.is_known_edge_kind(member.value), (
                f"EdgeKind.{member.name} = {member.value!r} has no canonical"
            )


# ---------------------------------------------------------------------------
# Naming conventions
# ---------------------------------------------------------------------------


class TestNamingConventions:
    def test_canonical_entity_types_are_pascal_case(self) -> None:
        for name in wk.CANONICAL_ENTITY_TYPES:
            assert name[0].isupper(), f"{name!r} should start uppercase"
            assert "_" not in name, f"{name!r} should not contain underscore"

    def test_canonical_edge_kinds_are_camel_case(self) -> None:
        for name in wk.CANONICAL_EDGE_KINDS:
            assert name[0].islower(), f"{name!r} should start lowercase"
            assert "_" not in name, f"{name!r} should not contain underscore"

    def test_aliases_are_snake_case(self) -> None:
        all_aliases = list(wk.ENTITY_TYPE_ALIASES.keys()) + list(
            wk.EDGE_KIND_ALIASES.keys()
        )
        for alias in all_aliases:
            assert alias == alias.lower(), f"alias {alias!r} should be lowercase"


# ---------------------------------------------------------------------------
# schema_alignment URIs (Phase 1)
# ---------------------------------------------------------------------------


class TestSchemaAlignmentForEntityType:
    @pytest.mark.parametrize(
        ("value", "uri"),
        [
            (wk.PERSON, "schema.org/Person"),
            ("person", "schema.org/Person"),
            (wk.ORGANIZATION, "schema.org/Organization"),
            (wk.TEAM, "schema.org/Organization"),  # Team subset of Organization
            (wk.SOFTWARE_APPLICATION, "schema.org/SoftwareApplication"),
            ("system", "schema.org/SoftwareApplication"),
            ("service", "schema.org/SoftwareApplication"),
            ("tool", "schema.org/SoftwareApplication"),
            ("document", "schema.org/CreativeWork"),
            (wk.DATASET, "schema.org/Dataset"),
            (wk.CREATIVE_WORK, "schema.org/CreativeWork"),
            (wk.PRODUCT, "schema.org/Product"),
            (wk.EVENT, "schema.org/Event"),
            (wk.PLACE, "schema.org/Place"),
            (wk.FILE, "schema.org/MediaObject"),  # File → MediaObject subtype
            (wk.AGENT, "prov:Agent"),
            (wk.ACTIVITY, "prov:Activity"),
        ],
    )
    def test_alignment_for_known_types(self, value: str, uri: str) -> None:
        assert wk.schema_alignment_for_entity_type(value) == uri

    def test_trellis_specific_canonicals_have_no_alignment(self) -> None:
        # Project / Concept are canonical Trellis-specific entity types
        # (no schema.org analogue); Phase 4 RDF export should skip them.
        assert wk.schema_alignment_for_entity_type(wk.PROJECT) is None
        assert wk.schema_alignment_for_entity_type(wk.CONCEPT) is None

    def test_unknown_returns_none(self) -> None:
        # Open-string types must not get a fabricated URI; a downstream
        # JSON-LD exporter would mislabel them otherwise.
        assert wk.schema_alignment_for_entity_type("dbt_model") is None
        assert wk.schema_alignment_for_entity_type("custom") is None

    def test_domain_returns_none(self) -> None:
        # ``domain`` is intentionally not aliased and not canonical.
        assert wk.schema_alignment_for_entity_type("domain") is None


class TestSchemaAlignmentForEdgeKind:
    @pytest.mark.parametrize(
        ("value", "uri"),
        [
            (wk.USED, "prov:used"),
            ("trace_used_evidence", "prov:used"),
            (wk.WAS_GENERATED_BY, "prov:wasGeneratedBy"),
            ("trace_produced_artifact", "prov:wasGeneratedBy"),
            (wk.WAS_INFORMED_BY, "prov:wasInformedBy"),
            ("trace_touched_entity", "prov:wasInformedBy"),
            (wk.WAS_DERIVED_FROM, "prov:wasDerivedFrom"),
            ("trace_promoted_to_precedent", "prov:wasDerivedFrom"),
            ("precedent_derived_from", "prov:wasDerivedFrom"),
            (wk.WAS_ATTRIBUTED_TO, "prov:wasAttributedTo"),
            (wk.WAS_ASSOCIATED_WITH, "prov:wasAssociatedWith"),
            (wk.PART_OF, "schema.org/isPartOf"),
            ("entity_part_of", "schema.org/isPartOf"),
            (wk.RELATED_TO, "schema.org/relatedTo"),
            ("entity_related_to", "schema.org/relatedTo"),
        ],
    )
    def test_alignment_for_known_kinds(self, value: str, uri: str) -> None:
        assert wk.schema_alignment_for_edge_kind(value) == uri

    def test_trellis_specific_edges_have_no_alignment(self) -> None:
        for kind in (wk.DEPENDS_ON, wk.ATTACHED_TO, wk.SUPPORTS, wk.APPLIES_TO):
            assert wk.schema_alignment_for_edge_kind(kind) is None

    def test_unknown_returns_none(self) -> None:
        assert wk.schema_alignment_for_edge_kind("mentions") is None
        assert wk.schema_alignment_for_edge_kind("dbt_references") is None


# ---------------------------------------------------------------------------
# Query expansion (Phase 2)
# ---------------------------------------------------------------------------


class TestExpandEntityTypeQuery:
    def test_canonical_query_includes_aliases(self) -> None:
        expanded = wk.expand_entity_type_query(wk.SOFTWARE_APPLICATION)
        # Canonical first, then sorted aliases.
        assert expanded[0] == wk.SOFTWARE_APPLICATION
        assert set(expanded) == {
            wk.SOFTWARE_APPLICATION,
            "service",
            "system",
            "tool",
        }

    def test_legacy_alias_normalises_then_expands(self) -> None:
        # Querying for ``"person"`` should still bucket alongside
        # canonical Person rows.
        expanded = wk.expand_entity_type_query("person")
        assert expanded[0] == wk.PERSON
        assert "person" in expanded

    def test_canonical_with_no_aliases_returns_singleton(self) -> None:
        # Organization is canonical but has no legacy alias.
        expanded = wk.expand_entity_type_query(wk.ORGANIZATION)
        assert expanded == (wk.ORGANIZATION,)

    def test_unknown_value_returns_singleton(self) -> None:
        # Open-string types pass through verbatim — there are no aliases
        # to mix in.
        expanded = wk.expand_entity_type_query("dbt_model")
        assert expanded == ("dbt_model",)


class TestExpandEdgeKindQuery:
    def test_canonical_query_includes_aliases(self) -> None:
        expanded = wk.expand_edge_kind_query(wk.WAS_DERIVED_FROM)
        assert expanded[0] == wk.WAS_DERIVED_FROM
        assert set(expanded) == {
            wk.WAS_DERIVED_FROM,
            "precedent_derived_from",
            "trace_promoted_to_precedent",
        }

    def test_legacy_alias_normalises_then_expands(self) -> None:
        expanded = wk.expand_edge_kind_query("trace_used_evidence")
        assert expanded[0] == wk.USED
        assert "trace_used_evidence" in expanded

    def test_unknown_value_returns_singleton(self) -> None:
        assert wk.expand_edge_kind_query("mentions") == ("mentions",)
