"""Alias binding in the extraction bridge (``knowledge_link_or_mint`` knob).

``result_to_batch(..., bind_identity=True)`` turns each draft's
``aliases`` into bind-if-absent ``alias.upsert`` commands and stamps the
result's extractor tier on every edge. ``bind_identity=False`` is the
historical output, which ``tests/unit/mcp/test_knowledge_link_or_mint_
differential.py`` pins against a golden captured from ``main``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from trellis.extract.base import ExtractorTier
from trellis.extract.commands import batch_draft_counts, result_to_batch
from trellis.extract.json_rules import (
    EntityRule,
    ExtractionRuleBundle,
    JSONRulesExtractor,
)
from trellis.mutate import build_curate_executor
from trellis.mutate.commands import Command, CommandStatus, Operation
from trellis.schemas.extraction import (
    AliasDraft,
    EdgeDraft,
    EntityDraft,
    ExtractionProvenance,
    ExtractionResult,
)
from trellis.stores.registry import StoreRegistry
from trellis_workers.extract.dbt_manifest import DbtManifestExtractor
from trellis_workers.extract.openlineage import OpenLineageExtractor


@pytest.fixture
def registry(tmp_path: Path) -> StoreRegistry:
    stores_dir = tmp_path / "stores"
    stores_dir.mkdir()
    return StoreRegistry(stores_dir=stores_dir)


def _run(registry: StoreRegistry, result: ExtractionResult, *, bind: bool):
    batch = result_to_batch(result, requested_by="test", bind_identity=bind)
    results = build_curate_executor(registry, clearance="restricted").execute_batch(
        batch
    )
    return batch, results


def _result(
    entities: list[EntityDraft],
    edges: list[EdgeDraft] | None = None,
    *,
    tier: str = "deterministic",
) -> ExtractionResult:
    return ExtractionResult(
        entities=entities,
        edges=edges or [],
        extractor_used="test",
        tier=tier,
        provenance=ExtractionProvenance(extractor_name="test"),
    )


def _table(entity_id: str, system: str, raw_id: str) -> EntityDraft:
    return EntityDraft(
        entity_id=entity_id,
        entity_type="table",
        name=raw_id.rsplit(".", 1)[-1],
        aliases=[AliasDraft(source_system=system, raw_id=raw_id)],
    )


_MANIFEST = {
    "metadata": {"adapter_type": "postgres"},
    "nodes": {
        "model.shop.orders": {
            "unique_id": "model.shop.orders",
            "resource_type": "model",
            "name": "orders",
            "schema": "analytics",
            "database": "warehouse",
            "depends_on": {"nodes": ["source.shop.raw.orders"]},
        }
    },
    "sources": {
        "source.shop.raw.orders": {
            "unique_id": "source.shop.raw.orders",
            "resource_type": "source",
            "name": "orders",
            "source_name": "raw",
            "schema": "raw",
            "database": "warehouse",
        }
    },
}

_LINEAGE = [
    {
        "job": {"namespace": "airflow", "name": "load_orders"},
        "inputs": [{"namespace": "postgres://db:5432", "name": "raw.orders"}],
        "outputs": [{"namespace": "postgres://db:5432", "name": "mart.orders"}],
    }
]


class TestBridgeShape:
    async def test_off_emits_no_alias_commands(self) -> None:
        result = await DbtManifestExtractor().extract(_MANIFEST, source_hint="dbt")
        batch = result_to_batch(result, requested_by="test")
        assert Operation.ALIAS_UPSERT not in {c.operation for c in batch.commands}
        assert all(
            "extractor_tier" not in c.args
            for c in batch.commands
            if c.operation is Operation.LINK_CREATE
        )

    async def test_on_adds_alias_commands_and_leaves_draft_counts_alone(self) -> None:
        result = await DbtManifestExtractor().extract(_MANIFEST, source_hint="dbt")
        off = result_to_batch(result, requested_by="test")
        on = result_to_batch(result, requested_by="test", bind_identity=True)
        assert batch_draft_counts(on) == batch_draft_counts(off) == (2, 1)
        assert sum(c.operation is Operation.ALIAS_UPSERT for c in on.commands) == 2

    async def test_alias_command_follows_its_entity_and_never_rebinds(self) -> None:
        result = _result([_table("t:1", "unity_catalog", "main.sales.orders")])
        batch = result_to_batch(result, requested_by="test", bind_identity=True)
        create, alias = batch.commands
        assert create.operation is Operation.ENTITY_CREATE
        assert alias.operation is Operation.ALIAS_UPSERT
        assert alias.args["if_absent"] is True
        assert alias.args["require_entity"] is True
        assert "stale_owner_name_key" not in alias.args

    def test_entity_without_an_id_binds_nothing(self) -> None:
        draft = EntityDraft(
            entity_type="table",
            name="orders",
            aliases=[AliasDraft(source_system="dbt", raw_id="model.x")],
        )
        batch = result_to_batch(_result([draft]), requested_by="t", bind_identity=True)
        assert [c.operation for c in batch.commands] == [Operation.ENTITY_CREATE]


class TestBindIfAbsentThroughTheExecutor:
    async def test_dbt_unique_id_binds_to_its_node(
        self, registry: StoreRegistry
    ) -> None:
        result = await DbtManifestExtractor().extract(_MANIFEST, source_hint="dbt")
        _run(registry, result, bind=True)
        graph = registry.knowledge.graph_store
        for unique_id in ("model.shop.orders", "source.shop.raw.orders"):
            row = graph.resolve_alias("dbt", unique_id)
            assert row is not None, unique_id
            assert row["entity_id"] == unique_id

    async def test_openlineage_job_and_dataset_bind_under_separate_systems(
        self, registry: StoreRegistry
    ) -> None:
        result = await OpenLineageExtractor().extract(_LINEAGE, source_hint="ol")
        _run(registry, result, bind=True)
        graph = registry.knowledge.graph_store
        job = graph.resolve_alias("openlineage_job", "airflow/load_orders")
        dataset = graph.resolve_alias(
            "openlineage_dataset", "postgres://db:5432/raw.orders"
        )
        assert job is not None
        assert dataset is not None
        assert job["entity_id"] != dataset["entity_id"]
        # A job and a dataset may share ``namespace/name``; neither system
        # answers for the other.
        assert graph.resolve_alias("openlineage_dataset", "airflow/load_orders") is None

    async def test_rerun_is_a_noop(self, registry: StoreRegistry) -> None:
        result = await DbtManifestExtractor().extract(_MANIFEST, source_hint="dbt")
        _run(registry, result, bind=True)
        graph = registry.knowledge.graph_store
        before = graph.get_aliases("model.shop.orders")
        _run(registry, result, bind=True)
        assert graph.get_aliases("model.shop.orders") == before

    def test_second_claim_on_a_bound_identity_does_not_rebind(
        self, registry: StoreRegistry
    ) -> None:
        graph = registry.knowledge.graph_store
        _run(registry, _result([_table("t:first", "dbt", "model.x")]), bind=True)
        _run(registry, _result([_table("t:second", "dbt", "model.x")]), bind=True)
        row = graph.resolve_alias("dbt", "model.x")
        assert row is not None
        assert row["entity_id"] == "t:first"
        assert graph.get_node("t:second") is not None
        assert [
            a for a in graph.get_aliases("t:second") if a["source_system"] == "dbt"
        ] == []

    def test_alias_for_an_entity_that_does_not_exist_is_refused(
        self, registry: StoreRegistry
    ) -> None:
        executor = build_curate_executor(registry, clearance="restricted")
        result = executor.execute(
            Command(
                operation=Operation.ALIAS_UPSERT,
                args={
                    "entity_id": "t:ghost",
                    "source_system": "dbt",
                    "raw_id": "model.ghost",
                    "if_absent": True,
                    "require_entity": True,
                },
                requested_by="test",
            )
        )
        assert result.status != CommandStatus.SUCCESS
        assert (
            registry.knowledge.graph_store.resolve_alias("dbt", "model.ghost") is None
        )

    def test_without_the_opt_in_the_handler_is_unchanged(
        self, registry: StoreRegistry
    ) -> None:
        """``require_entity`` is opt-in: a caller that omits it binds as before."""
        executor = build_curate_executor(registry, clearance="restricted")
        result = executor.execute(
            Command(
                operation=Operation.ALIAS_UPSERT,
                args={
                    "entity_id": "t:later",
                    "source_system": "dbt",
                    "raw_id": "model.later",
                    "if_absent": True,
                },
                requested_by="test",
            )
        )
        assert result.status == CommandStatus.SUCCESS


class TestUnityCatalogShape:
    """A three-part ``catalog.schema.table`` id needs no special case."""

    async def test_three_part_id_binds_under_unity_catalog(
        self, registry: StoreRegistry
    ) -> None:
        extractor = JSONRulesExtractor(
            "uc",
            ExtractionRuleBundle(
                entity_rules=[
                    EntityRule(
                        name="table",
                        path=["tables", "*"],
                        entity_type="table",
                        id_field="full_name",
                        name_field="name",
                        alias_source_system="unity_catalog",
                    )
                ]
            ),
            supported_sources=["uc"],
        )
        raw = {"tables": [{"full_name": "main.sales.orders", "name": "orders"}]}
        result = await extractor.extract(raw, source_hint="uc")
        _run(registry, result, bind=True)
        row = registry.knowledge.graph_store.resolve_alias(
            "unity_catalog", "main.sales.orders"
        )
        assert row is not None
        assert row["entity_id"] == "main.sales.orders"

    @pytest.mark.parametrize(
        "full_name",
        ["Main.Sales.Orders", "`main`.`sales`.`orders`", "MAIN.sales.`Orders`"],
    )
    async def test_the_bound_key_is_case_folded_and_unquoted(
        self, registry: StoreRegistry, full_name: str
    ) -> None:
        extractor = JSONRulesExtractor(
            "uc",
            ExtractionRuleBundle(
                entity_rules=[
                    EntityRule(
                        name="table",
                        path=["tables", "*"],
                        entity_type="table",
                        id_field="full_name",
                        name_field="name",
                        alias_source_system="unity_catalog",
                    )
                ]
            ),
            supported_sources=["uc"],
        )
        raw = {"tables": [{"full_name": full_name, "name": "orders"}]}
        result = await extractor.extract(raw, source_hint="uc")
        assert [a.raw_id for a in result.entities[0].aliases] == ["main.sales.orders"]
        _run(registry, result, bind=True)
        graph = registry.knowledge.graph_store
        assert graph.resolve_alias("unity_catalog", "main.sales.orders") is not None
        assert graph.resolve_alias("unity_catalog", full_name) is None or (
            full_name == "main.sales.orders"
        )

    async def test_another_system_binds_the_id_as_given(
        self, registry: StoreRegistry
    ) -> None:
        extractor = JSONRulesExtractor(
            "x",
            ExtractionRuleBundle(
                entity_rules=[
                    EntityRule(
                        name="table",
                        path=["tables", "*"],
                        entity_type="table",
                        id_field="full_name",
                        alias_source_system="warehouse",
                    )
                ]
            ),
            supported_sources=["x"],
        )
        result = await extractor.extract(
            {"tables": [{"full_name": "Main.Sales.Orders"}]}, source_hint="x"
        )
        assert [a.raw_id for a in result.entities[0].aliases] == ["Main.Sales.Orders"]

    def test_a_rule_without_a_system_binds_nothing(self) -> None:
        rules = ExtractionRuleBundle(
            entity_rules=[
                EntityRule(
                    name="t", path=["t", "*"], entity_type="table", id_field="id"
                )
            ]
        )
        assert rules.entity_rules[0].alias_source_system is None


class TestExtractorTier:
    async def test_tier_lands_on_the_stored_edge(self, registry: StoreRegistry) -> None:
        result = await DbtManifestExtractor().extract(_MANIFEST, source_hint="dbt")
        assert result.tier == ExtractorTier.DETERMINISTIC.value
        _run(registry, result, bind=True)
        edges = registry.knowledge.graph_store.get_edges(
            "model.shop.orders", direction="outgoing"
        )
        assert edges
        assert {e["extractor_tier"] for e in edges} == {"DETERMINISTIC"}

    async def test_off_stamps_nothing(self, registry: StoreRegistry) -> None:
        result = await DbtManifestExtractor().extract(_MANIFEST, source_hint="dbt")
        _run(registry, result, bind=False)
        edges = registry.knowledge.graph_store.get_edges(
            "model.shop.orders", direction="outgoing"
        )
        assert edges
        assert {e.get("extractor_tier") for e in edges} == {None}

    def test_tier_is_a_command_arg_not_a_property(self) -> None:
        edge = EdgeDraft(source_id="a", target_id="b", edge_kind="depends_on")
        batch = result_to_batch(
            _result([], [edge], tier="llm"), requested_by="t", bind_identity=True
        )
        args = batch.commands[0].args
        assert args["extractor_tier"] == "LLM"
        assert "extractor_tier" not in args["properties"]

    def test_a_malformed_tier_is_skipped_not_raised(self) -> None:
        edge = EdgeDraft(source_id="a", target_id="b", edge_kind="depends_on")
        batch = result_to_batch(
            _result([], [edge], tier="not-a-tier"), requested_by="t", bind_identity=True
        )
        assert "extractor_tier" not in batch.commands[0].args
