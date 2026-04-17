"""Tests for JSONRulesExtractor — path walking, field extraction, edges."""

from __future__ import annotations

import pytest

from trellis.extract.base import ExtractorTier
from trellis.extract.json_rules import (
    EdgeRule,
    EntityRule,
    ExtractionRuleBundle,
    JSONRulesExtractor,
)
from trellis.schemas.enums import NodeRole


@pytest.fixture
def simple_rules() -> ExtractionRuleBundle:
    return ExtractionRuleBundle(
        entity_rules=[
            EntityRule(
                name="table",
                path=["tables", "*"],
                entity_type="table",
                id_field="full_name",
                name_field="name",
                property_fields={"schema": "schema", "catalog": "catalog"},
            ),
        ],
    )


class TestEntityWalking:
    async def test_list_iteration(self, simple_rules: ExtractionRuleBundle) -> None:
        ext = JSONRulesExtractor("uc", simple_rules, supported_sources=["uc"])
        raw = {
            "tables": [
                {
                    "full_name": "cat.sch.users",
                    "name": "users",
                    "schema": "sch",
                    "catalog": "cat",
                },
                {
                    "full_name": "cat.sch.orders",
                    "name": "orders",
                    "schema": "sch",
                    "catalog": "cat",
                },
            ],
        }
        result = await ext.extract(raw)
        assert len(result.entities) == 2
        assert {e.name for e in result.entities} == {"users", "orders"}
        assert result.entities[0].properties == {"schema": "sch", "catalog": "cat"}
        assert result.entities[0].entity_id == "cat.sch.users"

    async def test_dict_iteration(self) -> None:
        rules = ExtractionRuleBundle(
            entity_rules=[
                EntityRule(
                    name="node",
                    path=["nodes", "*"],
                    entity_type="model",
                    id_field="unique_id",
                    name_field="name",
                ),
            ],
        )
        ext = JSONRulesExtractor("dbt", rules, supported_sources=["dbt"])
        raw = {
            "nodes": {
                "model.foo.users": {"unique_id": "model.foo.users", "name": "users"},
                "model.foo.orders": {
                    "unique_id": "model.foo.orders",
                    "name": "orders",
                },
            },
        }
        result = await ext.extract(raw)
        assert len(result.entities) == 2
        assert {e.entity_id for e in result.entities} == {
            "model.foo.users",
            "model.foo.orders",
        }

    async def test_nested_path(self) -> None:
        rules = ExtractionRuleBundle(
            entity_rules=[
                EntityRule(
                    name="column",
                    path=["tables", "*", "columns", "*"],
                    entity_type="column",
                    id_field="id",
                    property_fields={"type": "data_type"},
                ),
            ],
        )
        ext = JSONRulesExtractor("uc_cols", rules, supported_sources=["uc"])
        raw = {
            "tables": [
                {
                    "columns": [
                        {"id": "t1.c1", "data_type": "string"},
                        {"id": "t1.c2", "data_type": "bigint"},
                    ],
                },
                {
                    "columns": [
                        {"id": "t2.c1", "data_type": "string"},
                    ],
                },
            ],
        }
        result = await ext.extract(raw)
        assert len(result.entities) == 3
        assert {e.entity_id for e in result.entities} == {
            "t1.c1",
            "t1.c2",
            "t2.c1",
        }

    async def test_missing_id_field_skips(self) -> None:
        rules = ExtractionRuleBundle(
            entity_rules=[
                EntityRule(
                    name="n",
                    path=["items", "*"],
                    entity_type="t",
                    id_field="id",
                ),
            ],
        )
        ext = JSONRulesExtractor("x", rules, supported_sources=["x"])
        raw = {
            "items": [
                {"id": "a"},
                {"other": "b"},  # no "id" field — skipped
                {"id": "c"},
            ],
        }
        result = await ext.extract(raw)
        assert {e.entity_id for e in result.entities} == {"a", "c"}

    async def test_name_field_defaults_to_id(self) -> None:
        rules = ExtractionRuleBundle(
            entity_rules=[
                EntityRule(
                    name="n",
                    path=["items", "*"],
                    entity_type="t",
                    id_field="id",
                ),
            ],
        )
        ext = JSONRulesExtractor("x", rules, supported_sources=["x"])
        raw = {"items": [{"id": "only-id"}]}
        result = await ext.extract(raw)
        assert result.entities[0].name == "only-id"

    async def test_missing_property_field_omitted(self) -> None:
        rules = ExtractionRuleBundle(
            entity_rules=[
                EntityRule(
                    name="n",
                    path=["items", "*"],
                    entity_type="t",
                    id_field="id",
                    property_fields={"schema": "schema", "catalog": "catalog"},
                ),
            ],
        )
        ext = JSONRulesExtractor("x", rules, supported_sources=["x"])
        raw = {"items": [{"id": "a", "schema": "sch"}]}
        result = await ext.extract(raw)
        assert result.entities[0].properties == {"schema": "sch"}

    async def test_dotted_field_path(self) -> None:
        rules = ExtractionRuleBundle(
            entity_rules=[
                EntityRule(
                    name="n",
                    path=["items", "*"],
                    entity_type="t",
                    id_field="meta.id",
                    property_fields={"owner": "meta.owner"},
                ),
            ],
        )
        ext = JSONRulesExtractor("x", rules, supported_sources=["x"])
        raw = {
            "items": [
                {"meta": {"id": "a", "owner": "team-1"}},
            ],
        }
        result = await ext.extract(raw)
        assert result.entities[0].entity_id == "a"
        assert result.entities[0].properties["owner"] == "team-1"

    async def test_node_role_preserved(self) -> None:
        rules = ExtractionRuleBundle(
            entity_rules=[
                EntityRule(
                    name="n",
                    path=["items", "*"],
                    entity_type="column",
                    id_field="id",
                    node_role=NodeRole.STRUCTURAL,
                ),
            ],
        )
        ext = JSONRulesExtractor("x", rules, supported_sources=["x"])
        raw = {"items": [{"id": "a"}]}
        result = await ext.extract(raw)
        assert result.entities[0].node_role == NodeRole.STRUCTURAL


class TestEdgeRules:
    async def test_field_reference_scalar(self) -> None:
        rules = ExtractionRuleBundle(
            entity_rules=[
                EntityRule(
                    name="table",
                    path=["tables", "*"],
                    entity_type="table",
                    id_field="id",
                ),
                EntityRule(
                    name="column",
                    path=["columns", "*"],
                    entity_type="column",
                    id_field="id",
                ),
            ],
            edge_rules=[
                EdgeRule(
                    name="col_belongs_to",
                    source_rule="column",
                    target_rule="table",
                    edge_kind="belongs_to",
                    source_field="table_id",
                ),
            ],
        )
        ext = JSONRulesExtractor("x", rules, supported_sources=["x"])
        raw = {
            "tables": [{"id": "t1"}, {"id": "t2"}],
            "columns": [
                {"id": "c1", "table_id": "t1"},
                {"id": "c2", "table_id": "t1"},
                {"id": "c3", "table_id": "t2"},
            ],
        }
        result = await ext.extract(raw)
        assert len(result.edges) == 3
        assert {(e.source_id, e.target_id) for e in result.edges} == {
            ("c1", "t1"),
            ("c2", "t1"),
            ("c3", "t2"),
        }
        assert all(e.edge_kind == "belongs_to" for e in result.edges)

    async def test_field_reference_list(self) -> None:
        """dbt-style depends_on.nodes list-of-ids."""
        rules = ExtractionRuleBundle(
            entity_rules=[
                EntityRule(
                    name="node",
                    path=["nodes", "*"],
                    entity_type="model",
                    id_field="unique_id",
                ),
            ],
            edge_rules=[
                EdgeRule(
                    name="depends_on",
                    source_rule="node",
                    target_rule="node",
                    edge_kind="depends_on",
                    source_field="depends_on.nodes",
                ),
            ],
        )
        ext = JSONRulesExtractor("dbt", rules, supported_sources=["dbt"])
        raw = {
            "nodes": {
                "a": {"unique_id": "a", "depends_on": {"nodes": ["b", "c"]}},
                "b": {"unique_id": "b", "depends_on": {"nodes": []}},
                "c": {"unique_id": "c", "depends_on": {"nodes": ["b"]}},
            },
        }
        result = await ext.extract(raw)
        assert {(e.source_id, e.target_id) for e in result.edges} == {
            ("a", "b"),
            ("a", "c"),
            ("c", "b"),
        }

    async def test_unknown_target_skipped(self) -> None:
        rules = ExtractionRuleBundle(
            entity_rules=[
                EntityRule(
                    name="column",
                    path=["columns", "*"],
                    entity_type="column",
                    id_field="id",
                ),
                EntityRule(
                    name="table",
                    path=["tables", "*"],
                    entity_type="table",
                    id_field="id",
                ),
            ],
            edge_rules=[
                EdgeRule(
                    name="c_to_t",
                    source_rule="column",
                    target_rule="table",
                    edge_kind="belongs_to",
                    source_field="table_id",
                ),
            ],
        )
        ext = JSONRulesExtractor("x", rules, supported_sources=["x"])
        raw = {
            "tables": [{"id": "t1"}],
            "columns": [
                {"id": "c1", "table_id": "t1"},
                {"id": "c2", "table_id": "missing"},
            ],
        }
        result = await ext.extract(raw)
        assert len(result.edges) == 1
        assert result.edges[0].source_id == "c1"

    async def test_no_source_matches_yields_no_edges(self) -> None:
        rules = ExtractionRuleBundle(
            entity_rules=[
                EntityRule(
                    name="table",
                    path=["tables", "*"],
                    entity_type="table",
                    id_field="id",
                ),
                EntityRule(
                    name="column",
                    path=["columns", "*"],
                    entity_type="column",
                    id_field="id",
                ),
            ],
            edge_rules=[
                EdgeRule(
                    name="e",
                    source_rule="column",
                    target_rule="table",
                    edge_kind="belongs_to",
                    source_field="table_id",
                ),
            ],
        )
        ext = JSONRulesExtractor("x", rules, supported_sources=["x"])
        raw = {"tables": [{"id": "t1"}], "columns": []}
        result = await ext.extract(raw)
        assert result.edges == []


class TestAncestorEdgeRules:
    async def test_column_to_enclosing_table(self) -> None:
        """Columns nested under tables emit belongs_to edges by ancestry."""
        rules = ExtractionRuleBundle(
            entity_rules=[
                EntityRule(
                    name="table",
                    path=["tables", "*"],
                    entity_type="table",
                    id_field="id",
                ),
                EntityRule(
                    name="column",
                    path=["tables", "*", "columns", "*"],
                    entity_type="column",
                    id_field="id",
                ),
            ],
            edge_rules=[
                EdgeRule(
                    name="col_belongs_to",
                    source_rule="column",
                    target_rule="table",
                    edge_kind="belongs_to",
                    via_ancestor=True,
                ),
            ],
        )
        ext = JSONRulesExtractor("nested", rules, supported_sources=["uc"])
        raw = {
            "tables": [
                {"id": "t1", "columns": [{"id": "c1"}, {"id": "c2"}]},
                {"id": "t2", "columns": [{"id": "c3"}]},
            ],
        }
        result = await ext.extract(raw)
        assert {(e.source_id, e.target_id) for e in result.edges} == {
            ("c1", "t1"),
            ("c2", "t1"),
            ("c3", "t2"),
        }
        assert all(e.edge_kind == "belongs_to" for e in result.edges)

    async def test_closest_ancestor_wins(self) -> None:
        """With multiple ancestors of the same type, nearest one gets the edge."""
        rules = ExtractionRuleBundle(
            entity_rules=[
                EntityRule(
                    name="container",
                    path=["groups", "*"],
                    entity_type="group",
                    id_field="id",
                ),
                EntityRule(
                    name="nested_container",
                    path=["groups", "*", "children", "*"],
                    entity_type="group",
                    id_field="id",
                ),
                EntityRule(
                    name="leaf",
                    path=["groups", "*", "children", "*", "items", "*"],
                    entity_type="item",
                    id_field="id",
                ),
            ],
            edge_rules=[
                # Both container rules have entity_type="group"; target_rule
                # disambiguates by rule name, and closest-ancestor wins means
                # the nested_container is picked over the outer container.
                EdgeRule(
                    name="leaf_to_nested",
                    source_rule="leaf",
                    target_rule="nested_container",
                    edge_kind="in",
                    via_ancestor=True,
                ),
            ],
        )
        ext = JSONRulesExtractor("x", rules, supported_sources=["x"])
        raw = {
            "groups": [
                {
                    "id": "g1",
                    "children": [
                        {"id": "g1a", "items": [{"id": "i1"}]},
                        {"id": "g1b", "items": [{"id": "i2"}]},
                    ],
                },
            ],
        }
        result = await ext.extract(raw)
        assert {(e.source_id, e.target_id) for e in result.edges} == {
            ("i1", "g1a"),
            ("i2", "g1b"),
        }

    async def test_missing_ancestor_skips(self) -> None:
        """No edge emitted when no trail ancestor matches the target rule."""
        rules = ExtractionRuleBundle(
            entity_rules=[
                EntityRule(
                    name="orphan_target",
                    path=["unrelated", "*"],
                    entity_type="t",
                    id_field="id",
                ),
                EntityRule(
                    name="leaf",
                    path=["tables", "*", "columns", "*"],
                    entity_type="column",
                    id_field="id",
                ),
            ],
            edge_rules=[
                EdgeRule(
                    name="leaf_to_orphan",
                    source_rule="leaf",
                    target_rule="orphan_target",
                    edge_kind="x",
                    via_ancestor=True,
                ),
            ],
        )
        ext = JSONRulesExtractor("x", rules, supported_sources=["x"])
        raw = {
            "unrelated": [{"id": "u1"}],
            "tables": [{"columns": [{"id": "c1"}]}],
        }
        result = await ext.extract(raw)
        assert result.edges == []


class TestEdgeRuleValidation:
    def test_rejects_both_modes(self) -> None:
        with pytest.raises(ValueError, match="not both"):
            EdgeRule(
                name="bad",
                source_rule="a",
                target_rule="b",
                edge_kind="e",
                source_field="x",
                via_ancestor=True,
            )

    def test_rejects_neither_mode(self) -> None:
        with pytest.raises(ValueError, match="must set source_field or via_ancestor"):
            EdgeRule(
                name="bad",
                source_rule="a",
                target_rule="b",
                edge_kind="e",
            )


class TestResultMetadata:
    async def test_carries_tier_and_provenance(
        self, simple_rules: ExtractionRuleBundle
    ) -> None:
        ext = JSONRulesExtractor(
            "uc",
            simple_rules,
            supported_sources=["uc"],
            version="2.3.4",
        )
        result = await ext.extract({"tables": []}, source_hint="uc")
        assert result.extractor_used == "uc"
        assert result.tier == ExtractorTier.DETERMINISTIC.value
        assert result.provenance.extractor_name == "uc"
        assert result.provenance.extractor_version == "2.3.4"
        assert result.provenance.source_hint == "uc"
        assert result.llm_calls == 0
        assert result.tokens_used == 0
