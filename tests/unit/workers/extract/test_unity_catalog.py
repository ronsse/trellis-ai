"""Unity Catalog extractor: input shapes, graph shape, identity, determinism.

Every name is invented.  The fixture is the smallest export that exercises each
rule: one managed table, one view, a lineage row naming a table the metadata
export lacks, a system catalog that must stay out, and one column-level fact.
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

import pytest

from trellis.errors import ValidationError
from trellis.extract.commands import result_to_batch
from trellis.mutate import build_curate_executor
from trellis.mutate.commands import CommandStatus
from trellis.schemas.enums import NodeRole
from trellis.schemas.extraction import ExtractionResult
from trellis.stores.registry import StoreRegistry
from trellis_workers.extract.openlineage import OpenLineageExtractor
from trellis_workers.extract.unity_catalog import (
    INPUT_REFUSAL_CODE,
    UnityCatalogExtractor,
    check_input,
)

NS = "unitycatalog://dbc-0000.example.net"

CATALOGS = {
    "catalogs": [
        {
            "name": "main",
            "comment": "Primary catalog",
            "owner": "data-eng",
            "catalog_type": "MANAGED_CATALOG",
            "updated_at": 1700000000000,
        },
        {"name": "system", "catalog_type": "SYSTEM_CATALOG"},
    ],
    "next_page_token": None,
}
SCHEMAS = {
    "schemas": [
        {"name": "sales", "catalog_name": "main", "full_name": "main.sales"},
        {"name": "access", "catalog_name": "system", "full_name": "system.access"},
    ]
}
TABLES = {
    "tables": [
        {
            "name": "orders",
            "catalog_name": "main",
            "schema_name": "sales",
            "full_name": "main.sales.orders",
            "table_type": "MANAGED",
            "data_source_format": "DELTA",
            "owner": "data-eng",
            "comment": "One row per order",
            "storage_location": "s3://example-bucket/orders",
            "created_at": 1690000000000,
            "updated_at": 1700000000000,
            "properties": {"delta.minReaderVersion": "1"},
            "columns": [
                {
                    "name": "customer_id",
                    "type_text": "bigint",
                    "type_name": "LONG",
                    "position": 1,
                    "nullable": True,
                },
                {
                    "name": "order_id",
                    "type_text": "bigint",
                    "type_name": "LONG",
                    "position": 0,
                    "nullable": False,
                    "comment": "Primary key",
                },
            ],
        },
        {
            "name": "daily_revenue",
            "catalog_name": "main",
            "schema_name": "sales",
            "full_name": "main.sales.daily_revenue",
            "table_type": "VIEW",
            "columns": [{"name": "revenue", "type_text": "double", "position": 0}],
        },
        {
            "name": "audit",
            "catalog_name": "system",
            "schema_name": "access",
            "full_name": "system.access.audit",
        },
    ]
}
TABLE_LINEAGE = [
    {
        "source_table_full_name": "main.sales.orders",
        "target_table_full_name": "main.sales.daily_revenue",
        "entity_type": "NOTEBOOK",
        "event_time": "2024-01-05T10:00:00.000+00:00",
    },
    {
        "source_table_full_name": "main.raw.events",
        "target_table_full_name": "main.sales.orders",
        "entity_type": "JOB",
        "event_time": "2024-01-06T10:00:00.000+00:00",
    },
    {
        "source_table_full_name": "system.access.audit",
        "target_table_full_name": "main.sales.orders",
        "event_time": "2024-01-07T10:00:00.000+00:00",
    },
]
COLUMN_LINEAGE = [
    {
        "source_table_full_name": "main.sales.orders",
        "target_table_full_name": "main.sales.daily_revenue",
        "source_column_name": "order_id",
        "target_column_name": "revenue",
        "event_time": "2024-01-05T10:00:00.000+00:00",
    }
]
LINEAGE_API = {
    "table_name": "main.sales.daily_revenue",
    "upstreams": [
        {
            "tableInfo": {
                "name": "orders",
                "catalog_name": "main",
                "schema_name": "sales",
                "table_type": "TABLE",
                "lineage_timestamp": "2024-01-05T10:00:00.000",
            }
        }
    ],
    "downstreams": [
        {"tableInfo": {"name": "kpi", "catalog_name": "main", "schema_name": "bi"}}
    ],
}
EVERYTHING = [CATALOGS, SCHEMAS, TABLES, TABLE_LINEAGE, COLUMN_LINEAGE]


def tid(full_name: str, scope: str = NS) -> str:
    return f"dataset:{scope}:{full_name}"


async def extract(raw: Any, **options: Any) -> ExtractionResult:
    return await UnityCatalogExtractor(namespace=NS, **options).extract(
        raw, source_hint="unity-catalog"
    )


def drafts(result: ExtractionResult) -> str:
    """The entity and edge drafts as JSON; provenance carries a wall-clock stamp."""
    return json.dumps(
        {
            "entities": [e.model_dump(mode="json") for e in result.entities],
            "edges": [e.model_dump(mode="json") for e in result.edges],
        }
    )


def by_id(result: ExtractionResult) -> dict[str, Any]:
    return {e.entity_id: e for e in result.entities}


def edge_set(result: ExtractionResult, kind: str) -> set[tuple[str, str]]:
    return {(e.source_id, e.target_id) for e in result.edges if e.edge_kind == kind}


# ---------------------------------------------------------------------------
# Input shapes
# ---------------------------------------------------------------------------


class TestInputShapes:
    @pytest.mark.parametrize(
        ("raw", "entity_count"),
        [
            ({"tables": [], "next_page_token": None}, 0),
            ({}, 0),
            ([], 0),
            (CATALOGS, 1),  # `system` is left out
            (CATALOGS["catalogs"], 1),
            (SCHEMAS, 2),  # main.sales plus the catalog it hangs from
            (SCHEMAS["schemas"], 2),
            (TABLES["tables"][:1], 5),  # catalog, schema, table, two columns
            (TABLES["tables"][1], 4),  # one bare record, not in a list
            (TABLES, 7),
            (TABLE_LINEAGE[:1], 4),  # two stub tables, their schema and catalog
            (LINEAGE_API, 6),  # three stub tables, schemas sales and bi, catalog main
        ],
    )
    async def test_shape_parses(self, raw: Any, entity_count: int) -> None:
        result = await extract(raw)
        assert len(result.entities) == entity_count

    async def test_page_boundaries_do_not_change_the_result(self) -> None:
        pages = [
            {"tables": [t], "next_page_token": f"p{i}"}
            for i, t in enumerate(TABLES["tables"])
        ]
        whole = await extract([CATALOGS, SCHEMAS, TABLES])
        paged = await extract([CATALOGS, SCHEMAS, *pages])
        assert drafts(paged) == drafts(whole)

    async def test_lineage_tracking_response_reads_both_directions(self) -> None:
        result = await extract(LINEAGE_API)
        orders, revenue, kpi = (
            tid("main.sales.orders"),
            tid("main.sales.daily_revenue"),
            tid("main.bi.kpi"),
        )
        # A downstream depends on its upstream.
        assert edge_set(result, "depends_on") == {(revenue, orders), (kpi, revenue)}
        edge = next(e for e in result.edges if e.source_id == revenue)
        assert edge.properties["last_event_time"] == "2024-01-05T10:00:00.000+00:00"

    async def test_namespace_inside_the_document_sets_the_scope(self) -> None:
        raw = {**TABLES, "namespace": NS}
        result = await UnityCatalogExtractor().extract(raw)
        assert tid("main.sales.orders") in by_id(result)

    async def test_no_namespace_falls_back_to_a_fixed_scope(self) -> None:
        result = await UnityCatalogExtractor().extract(TABLES)
        assert tid("main.sales.orders", "unity_catalog") in by_id(result)
        assert all(
            a.source_system == "unity_catalog"
            for e in result.entities
            for a in e.aliases
        )


class TestRefusals:
    @pytest.mark.parametrize(
        "raw",
        [
            "just text",
            42,
            [None],
            {"tables": "not-a-list"},
            {"tables": [{"name": "orders"}]},  # no catalog or schema
            {"schemas": [{"name": "sales"}]},  # no catalog
            {"catalogs": [{"comment": "nameless"}]},
            {"unrelated": 1},
            {"upstreams": []},  # lineage response with no table_name
            [{"namespace": "a://x"}, {"namespace": "b://y"}],
        ],
    )
    async def test_unreadable_input_is_a_typed_refusal(self, raw: Any) -> None:
        with pytest.raises(ValidationError) as excinfo:
            await extract(raw)
        assert excinfo.value.code == INPUT_REFUSAL_CODE
        with pytest.raises(ValidationError):
            check_input(raw)

    async def test_refusal_names_a_position_not_the_content(self) -> None:
        marker = "do-not-echo-this-value"
        with pytest.raises(ValidationError) as excinfo:
            await extract({"tables": [{"name": marker}]})
        assert "tables[0]" in str(excinfo.value)
        assert marker not in str(excinfo.value)


# ---------------------------------------------------------------------------
# Graph shape
# ---------------------------------------------------------------------------


class TestGraphShape:
    async def test_hierarchy_and_lineage_counts(self) -> None:
        result = await extract(EVERYTHING)
        assert len(result.entities) == 9
        assert len(result.edges) == 11

    async def test_contains_edges_run_parent_to_child(self) -> None:
        result = await extract(EVERYTHING)
        main, sales, raw = (
            f"uc_catalog:{NS}:main",
            f"uc_schema:{NS}:main.sales",
            f"uc_schema:{NS}:main.raw",
        )
        orders = tid("main.sales.orders")
        assert edge_set(result, "contains") >= {
            (main, sales),
            (main, raw),
            (sales, orders),
            (sales, tid("main.sales.daily_revenue")),
            (raw, tid("main.raw.events")),
            (orders, f"uc_column:{NS}:main.sales.orders/order_id"),
        }
        assert len(edge_set(result, "contains")) == 8

    async def test_table_lineage_points_downstream_to_upstream(self) -> None:
        result = await extract(EVERYTHING)
        table_edges = {
            pair
            for pair in edge_set(result, "depends_on")
            if pair[0].startswith("dataset:")
        }
        assert table_edges == {
            (tid("main.sales.daily_revenue"), tid("main.sales.orders")),
            (tid("main.sales.orders"), tid("main.raw.events")),
        }

    async def test_column_lineage_joins_column_entities(self) -> None:
        result = await extract(EVERYTHING)
        cols = {
            pair
            for pair in edge_set(result, "depends_on")
            if pair[0].startswith("uc_column:")
        }
        assert cols == {
            (
                f"uc_column:{NS}:main.sales.daily_revenue/revenue",
                f"uc_column:{NS}:main.sales.orders/order_id",
            )
        }

    async def test_lineage_edges_may_dangle_across_batches(self) -> None:
        result = await extract(EVERYTHING)
        assert all(
            e.allow_dangling for e in result.edges if e.edge_kind == "depends_on"
        )

    async def test_lineage_naming_an_unknown_table_mints_a_stub(self) -> None:
        # OpenLineage mints a dataset it only saw named in an event; so does this.
        entities = by_id(await extract(EVERYTHING))
        stub = entities[tid("main.raw.events")]
        assert stub.properties["stub"] is True
        assert "stub" not in entities[tid("main.sales.orders")].properties

    async def test_columns_are_structural_leaves(self) -> None:
        entities = by_id(await extract(EVERYTHING))
        column = entities[f"uc_column:{NS}:main.sales.orders/order_id"]
        assert column.node_role is NodeRole.STRUCTURAL
        assert column.allow_structural_leaf is True
        assert column.properties["type_text"] == "bigint"
        assert column.properties["position"] == 0
        assert column.properties["nullable"] is False
        assert column.properties["description"] == "Primary key"
        # Tables, schemas and catalogs stay semantic: they are what retrieval serves.
        assert entities[tid("main.sales.orders")].node_role is NodeRole.SEMANTIC

    async def test_columns_off_drops_column_entities_and_column_lineage(self) -> None:
        result = await extract(EVERYTHING, include_columns=False)
        assert not [e for e in result.entities if e.entity_type == "uc_column"]
        assert len(result.entities) == 6
        assert by_id(result)[tid("main.sales.orders")].properties["column_count"] == 2
        assert len(edge_set(result, "depends_on")) == 2

    async def test_a_view_is_a_dataset_too(self) -> None:
        entities = by_id(await extract(EVERYTHING))
        view = entities[tid("main.sales.daily_revenue")]
        assert view.entity_type == "dataset"
        assert view.properties["table_type"] == "VIEW"


class TestProperties:
    async def test_table_points_back_to_its_source(self) -> None:
        props = by_id(await extract(EVERYTHING))[tid("main.sales.orders")].properties
        assert props["full_name"] == "main.sales.orders"
        assert props["table_type"] == "MANAGED"
        assert props["data_source_format"] == "DELTA"
        assert props["owner"] == "data-eng"
        assert props["storage_location"] == "s3://example-bucket/orders"
        assert props["description"] == "One row per order"
        assert (
            props["physical_uri"]
            == "databricks://dbc-0000.example.net/main/sales/orders"
        )
        assert props["column_count"] == 2

    async def test_source_clock_comes_from_updated_at_and_event_time(self) -> None:
        result = await extract(EVERYTHING)
        props = by_id(result)[tid("main.sales.orders")].properties
        assert props["source_updated_at"] == "2023-11-14T22:13:20.000+00:00"
        assert props["source_created_at"] == "2023-07-22T04:26:40.000+00:00"
        edge = next(
            e
            for e in result.edges
            if (e.source_id, e.target_id)
            == (tid("main.sales.orders"), tid("main.raw.events"))
        )
        assert edge.properties["last_event_time"] == "2024-01-06T10:00:00.000+00:00"

    async def test_properties_are_bounded_scalars(self) -> None:
        # Unity Catalog caps a name part at 255 characters; everything else a
        # record can carry (comment, location, the raw property map) is unbounded.
        huge = {
            **TABLES["tables"][0],
            "name": "n" * 255,
            "full_name": "main.sales." + "n" * 255,
            "comment": "c" * 5000,
            "storage_location": "s3://example-bucket/" + "p" * 5000,
            "properties": {f"k{i}": "v" * 1000 for i in range(500)},
        }
        result = await extract({"tables": [huge]})
        for entity in result.entities:
            for key, value in entity.properties.items():
                assert isinstance(value, (str, int, float, bool)), key
                assert len(str(value)) <= 1000, key
            assert "properties" not in entity.properties
        table = by_id(result)[tid("main.sales." + "n" * 255)]
        assert len(table.properties["description"]) == 1000

    @pytest.mark.parametrize("bad", ["not a time", -1, True, None, 1e30, {}, ""])
    async def test_an_unparseable_clock_is_dropped_not_guessed(self, bad: Any) -> None:
        table = {**TABLES["tables"][1], "updated_at": bad}
        props = by_id(await extract({"tables": [table]}))[
            tid("main.sales.daily_revenue")
        ].properties
        assert props.get("source_updated_at") in (None, "1969-12-31T23:59:59.999+00:00")


# ---------------------------------------------------------------------------
# System catalogs
# ---------------------------------------------------------------------------


class TestSystemCatalogs:
    async def test_excluded_by_default(self) -> None:
        result = await extract(EVERYTHING)
        assert not [e for e in result.entities if "system" in e.entity_id]
        assert not [e for e in result.edges if "system" in e.source_id + e.target_id]

    async def test_internal_catalog_is_excluded_too(self) -> None:
        raw = [{"name": "__databricks_internal"}, {"name": "main"}]
        entities = by_id(await extract(raw))
        assert list(entities) == [f"uc_catalog:{NS}:main"]

    async def test_option_keeps_them(self) -> None:
        result = await extract(EVERYTHING, include_system=True)
        assert len(result.entities) == 12
        assert len(result.edges) == 14
        assert (tid("main.sales.orders"), tid("system.access.audit")) in edge_set(
            result, "depends_on"
        )


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


class TestIdentity:
    @pytest.mark.parametrize(
        "spelling",
        ["main.sales.orders", "Main.Sales.Orders", "`main`.`sales`.`orders`"],
    )
    async def test_case_and_quoting_variants_are_one_table(self, spelling: str) -> None:
        rows = [
            {
                "source_table_full_name": spelling,
                "target_table_full_name": "main.sales.daily_revenue",
            }
        ]
        result = await extract([TABLES, rows])
        ids = [e.entity_id for e in result.entities if e.entity_type == "dataset"]
        assert sorted(ids) == [
            tid("main.sales.daily_revenue"),
            tid("main.sales.orders"),
        ]
        assert (
            tid("main.sales.daily_revenue"),
            tid("main.sales.orders"),
        ) in edge_set(result, "depends_on")

    async def test_table_aliases_carry_both_systems_keys(self) -> None:
        orders = by_id(await extract(TABLES))[tid("main.sales.orders")]
        assert {(a.source_system, a.raw_id) for a in orders.aliases} == {
            ("unity_catalog", "main.sales.orders"),
            ("openlineage_dataset", f"{NS}/main.sales.orders"),
        }

    async def test_agrees_with_openlineage_on_one_table(self) -> None:
        event = {
            "job": {"namespace": "databricks", "name": "build_revenue"},
            "inputs": [{"namespace": NS, "name": "main.sales.orders"}],
            "outputs": [{"namespace": NS, "name": "main.sales.daily_revenue"}],
        }
        ol = await OpenLineageExtractor().extract([event], source_hint="ol")
        uc = await extract(EVERYTHING)
        ol_datasets = {
            e.entity_id: e for e in ol.entities if e.entity_type == "dataset"
        }
        uc_datasets = by_id(uc)
        for name in ("main.sales.orders", "main.sales.daily_revenue"):
            assert tid(name) in ol_datasets
            assert tid(name) in uc_datasets
            ol_aliases = {
                (a.source_system, a.raw_id) for a in ol_datasets[tid(name)].aliases
            }
            uc_aliases = {
                (a.source_system, a.raw_id) for a in uc_datasets[tid(name)].aliases
            }
            assert ol_aliases <= uc_aliases
            assert (
                uc_datasets[tid(name)].entity_type == ol_datasets[tid(name)].entity_type
            )

    @pytest.mark.parametrize("uc_first", [True, False])
    async def test_both_extractors_write_one_node(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, uc_first: bool
    ) -> None:
        monkeypatch.setenv("TRELLIS_KNOWLEDGE_LINK_OR_MINT", "1")
        stores = tmp_path / "stores"
        stores.mkdir()
        registry = StoreRegistry(stores_dir=stores)
        event = {
            "job": {"namespace": "databricks", "name": "build_revenue"},
            "inputs": [{"namespace": NS, "name": "main.sales.orders"}],
            "outputs": [],
        }
        uc = await extract(TABLES)
        ol = await OpenLineageExtractor().extract([event], source_hint="ol")
        for result in (uc, ol) if uc_first else (ol, uc):
            batch = result_to_batch(result, requested_by="test", bind_identity=True)
            outcomes = build_curate_executor(
                registry, clearance="restricted"
            ).execute_batch(batch)
            assert all(r.status is CommandStatus.SUCCESS for r in outcomes)

        graph = registry.knowledge.graph_store
        uc_hit = graph.resolve_alias("unity_catalog", "main.sales.orders")
        ol_hit = graph.resolve_alias("openlineage_dataset", f"{NS}/main.sales.orders")
        assert uc_hit is not None
        assert ol_hit is not None
        assert uc_hit["entity_id"] == ol_hit["entity_id"] == tid("main.sales.orders")
        assert graph.count_nodes_by_type().get("dataset") == 2  # orders, daily_revenue

    async def test_a_second_run_creates_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_KNOWLEDGE_LINK_OR_MINT", "1")
        stores = tmp_path / "stores"
        stores.mkdir()
        registry = StoreRegistry(stores_dir=stores)
        graph = registry.knowledge.graph_store
        result = await extract(EVERYTHING)
        counts = []
        for _ in range(2):
            batch = result_to_batch(result, requested_by="test", bind_identity=True)
            outcomes = build_curate_executor(
                registry, clearance="restricted"
            ).execute_batch(batch)
            assert all(r.status is CommandStatus.SUCCESS for r in outcomes)
            counts.append(
                (
                    graph.count_nodes(),
                    len(graph.get_aliases(tid("main.sales.orders"))),
                    len(graph.get_edges(tid("main.sales.orders"), direction="both")),
                )
            )
        assert counts[0] == counts[1]
        assert counts[0][0] == 9


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def shuffled(obj: Any, rng: random.Random) -> Any:
    if isinstance(obj, list):
        items = [shuffled(i, rng) for i in obj]
        rng.shuffle(items)
        return items
    if isinstance(obj, dict):
        keys = list(obj)
        rng.shuffle(keys)
        return {k: shuffled(obj[k], rng) for k in keys}
    return obj


class TestDeterminism:
    async def test_same_input_is_byte_identical(self) -> None:
        first = await extract(EVERYTHING)
        second = await extract(json.loads(json.dumps(EVERYTHING)))
        assert drafts(first) == drafts(second)

    @pytest.mark.parametrize("seed", range(8))
    async def test_input_order_does_not_change_the_output(self, seed: int) -> None:
        baseline = drafts(await extract(EVERYTHING))
        mixed = shuffled(EVERYTHING, random.Random(seed))  # noqa: S311
        assert drafts(await extract(mixed)) == baseline

    async def test_output_is_sorted(self) -> None:
        result = await extract(EVERYTHING)
        ids = [e.entity_id for e in result.entities]
        assert ids == sorted(ids)
        edges = [(e.edge_kind, e.source_id, e.target_id) for e in result.edges]
        assert edges == sorted(edges)

    @pytest.mark.parametrize("flip", [False, True])
    async def test_duplicate_records_resolve_to_the_later_updated_at(
        self, flip: bool
    ) -> None:
        old = {**TABLES["tables"][1], "updated_at": 1000, "comment": "old"}
        new = {**TABLES["tables"][1], "updated_at": 2000, "comment": "new"}
        records = [new, old] if flip else [old, new]
        entities = by_id(await extract({"tables": records}))
        assert (
            entities[tid("main.sales.daily_revenue")].properties["description"] == "new"
        )

    @pytest.mark.parametrize("flip", [False, True])
    async def test_repeated_lineage_keeps_the_latest_event_time(
        self, flip: bool
    ) -> None:
        rows = [
            {**TABLE_LINEAGE[0], "event_time": "2024-01-01T00:00:00.000+00:00"},
            {**TABLE_LINEAGE[0], "event_time": "2024-02-01T00:00:00.000+00:00"},
        ]
        result = await extract(rows[::-1] if flip else rows)
        edge = next(e for e in result.edges if e.edge_kind == "depends_on")
        assert edge.properties["last_event_time"] == "2024-02-01T00:00:00.000+00:00"
