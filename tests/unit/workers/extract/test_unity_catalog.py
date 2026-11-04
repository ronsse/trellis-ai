"""Unity Catalog extractor: input shapes, graph shape, identity, determinism.

Every name is invented.  The fixture is the smallest export that exercises each
rule: one managed table, one view, a lineage row naming a table the metadata
export lacks, a system catalog that must stay out, and one column-level fact.
A table, column, schema or catalog that only lineage (or a child's parent chain)
names is a placeholder draft: ``if_absent`` and ``stub``, created only when the
node is missing, so a partial run can never overwrite what a fuller run wrote.
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
            (TABLE_LINEAGE[:1], 4),  # catalog, schema, two placeholder tables
            (LINEAGE_API, 6),  # catalog, two schemas, three placeholder tables
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
            # A column record is not a catalog because it has a `name`.
            {"name": "amount", "type_text": "int", "position": 0},
            [{"name": "orders", "table_type": "MANAGED"}],
            [{"name": "orders", "columns": []}],
            [{"name": "amount", "nullable": True}],
            # A dotted `full_name` is a schema's or a table's, whose catalog
            # field the record lost; a catalog's is its own name.
            {"name": "orders", "full_name": "main.sales.orders"},
            {"name": "sales", "full_name": "main.sales", "comment": "Sales marts"},
            {"name": "main", "full_name": "other"},
        ],
    )
    async def test_unreadable_input_is_a_typed_refusal(self, raw: Any) -> None:
        with pytest.raises(ValidationError) as excinfo:
            await extract(raw)
        assert excinfo.value.code == INPUT_REFUSAL_CODE
        with pytest.raises(ValidationError):
            check_input(raw)

    async def test_a_bare_catalog_record_still_parses_with_real_fields(self) -> None:
        # The catalogs list response carries these beside `name`.
        record = {
            "name": "main",
            "owner": "data-eng",
            "comment": "Primary",
            "catalog_type": "MANAGED_CATALOG",
            "storage_root": "s3://example-bucket/main",
            "metastore_id": "00000000-0000-0000-0000-000000000000",
            "created_at": 1690000000000,
            "created_by": "someone",
            "updated_at": 1700000000000,
            "securable_type": "CATALOG",
            "browse_only": False,
        }
        entities = by_id(await extract(record))
        assert list(entities) == [f"uc_catalog:{NS}:main"]

    @pytest.mark.parametrize("full_name", ["main", "Main", " main "])
    async def test_a_bare_catalog_whose_full_name_is_its_name_parses(
        self, full_name: str
    ) -> None:
        record = {"name": "main", "full_name": full_name, "owner": "data-eng"}
        assert list(by_id(await extract(record))) == [f"uc_catalog:{NS}:main"]

    async def test_a_column_record_inside_a_catalogs_list_is_not_filtered(self) -> None:
        # Under an explicit `catalogs` key the caller said what the records are.
        result = await extract({"catalogs": [{"name": "main", "type_text": "x"}]})
        assert list(by_id(result)) == [f"uc_catalog:{NS}:main"]

    @pytest.mark.parametrize("depth", [2000, 100_000])
    async def test_input_nested_too_deep_is_a_typed_refusal(self, depth: int) -> None:
        deep: Any = []
        for _ in range(depth):
            deep = [deep]
        with pytest.raises(ValidationError) as excinfo:
            await extract(deep)
        assert excinfo.value.code == INPUT_REFUSAL_CODE
        assert str(excinfo.value) == "input: nested too deeply to read"
        with pytest.raises(ValidationError):
            check_input(deep)

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
        main, sales = f"uc_catalog:{NS}:main", f"uc_schema:{NS}:main.sales"
        raw = f"uc_schema:{NS}:main.raw"
        orders = tid("main.sales.orders")
        assert edge_set(result, "contains") == {
            (main, sales),
            (main, raw),
            (raw, tid("main.raw.events")),
            (sales, orders),
            (sales, tid("main.sales.daily_revenue")),
            (orders, f"uc_column:{NS}:main.sales.orders/order_id"),
            (orders, f"uc_column:{NS}:main.sales.orders/customer_id"),
            (
                tid("main.sales.daily_revenue"),
                f"uc_column:{NS}:main.sales.daily_revenue/revenue",
            ),
        }

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

    async def test_no_edge_is_dangling(self) -> None:
        # The Bolt stores refuse a dangling edge even with `allow_dangling`, so
        # every endpoint is an entity of the same batch, real or placeholder.
        result = await extract(EVERYTHING)
        ids = set(by_id(result))
        assert result.edges
        assert not [e for e in result.edges if e.allow_dangling]
        for edge in result.edges:
            assert edge.source_id in ids, edge
            assert edge.target_id in ids, edge

    async def test_lineage_naming_an_unknown_table_gets_a_placeholder(self) -> None:
        result = await extract(EVERYTHING)
        entities = by_id(result)
        events = entities[tid("main.raw.events")]
        # Bounded and name-only: identity, parentage and the placeholder marks.
        assert events.if_absent is True
        assert events.properties["stub"] is True
        assert set(events.properties) <= {
            "name",
            "full_name",
            "database_name",
            "schema_name",
            "source_system",
            "stub",
            "physical_uri",
            "extraction_confidence",
        }
        # So are the schema and catalog it hangs from, when the export lacks them.
        raw = entities[f"uc_schema:{NS}:main.raw"]
        assert (raw.if_absent, raw.properties["stub"]) == (True, True)
        assert (tid("main.sales.orders"), tid("main.raw.events")) in edge_set(
            result, "depends_on"
        )

    async def test_only_what_the_export_lacks_is_a_placeholder(self) -> None:
        entities = by_id(await extract(EVERYTHING))
        placeholders = {k for k, e in entities.items() if e.if_absent}
        assert placeholders == {tid("main.raw.events"), f"uc_schema:{NS}:main.raw"}
        for key, entity in entities.items():
            assert ("stub" in entity.properties) == (key in placeholders), key

    async def test_a_parent_the_export_lacks_is_a_placeholder(self) -> None:
        # A tables-only export holds neither the schema nor the catalog.
        entities = by_id(await extract(TABLES))
        schema = entities[f"uc_schema:{NS}:main.sales"]
        catalog = entities[f"uc_catalog:{NS}:main"]
        for parent in (schema, catalog):
            assert parent.if_absent is True
            assert parent.properties["stub"] is True
            assert not {"owner", "description"} & set(parent.properties)
        assert entities[tid("main.sales.orders")].if_absent is False

    async def test_column_lineage_naming_an_unknown_column_gets_a_placeholder(
        self,
    ) -> None:
        rows = [
            {
                "source_table_full_name": "main.sales.orders",
                "target_table_full_name": "main.sales.daily_revenue",
                "source_column_name": "Amount",
                "target_column_name": "revenue_total",
            }
        ]
        result = await extract([TABLES, rows])
        entities = by_id(result)
        src = f"uc_column:{NS}:main.sales.orders/amount"
        tgt = f"uc_column:{NS}:main.sales.daily_revenue/revenue_total"
        for column in (src, tgt):
            assert entities[column].if_absent is True
            assert entities[column].properties["stub"] is True
            assert entities[column].node_role is NodeRole.STRUCTURAL
            assert entities[column].allow_structural_leaf is True
        assert edge_set(result, "depends_on") == {(tgt, src)}
        assert (tid("main.sales.orders"), src) in edge_set(result, "contains")
        assert len(edge_set(result, "contains")) == 8  # the six of TABLES, two more

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
        assert (
            len(result.entities) == 6
        )  # catalog, schema, two tables, two placeholders
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
        assert len(result.entities) == 12  # the nine of EVERYTHING, plus system's three
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

    @pytest.mark.parametrize("suffix", ["/", "//", " /"])
    async def test_a_trailing_slash_on_the_namespace_is_dropped(
        self, suffix: str
    ) -> None:
        by_option = await UnityCatalogExtractor(namespace=NS + suffix).extract(TABLES)
        in_document = await UnityCatalogExtractor().extract(
            {**TABLES, "namespace": NS + suffix}
        )
        plain = await extract(TABLES)
        assert drafts(by_option) == drafts(plain) == drafts(in_document)

    async def test_namespaces_differing_only_in_a_trailing_slash_are_one(self) -> None:
        raw = [{"namespace": NS, **CATALOGS}, {"namespace": NS + "/", **TABLES}]
        check_input(raw)
        result = await UnityCatalogExtractor().extract(raw)
        assert tid("main.sales.orders") in by_id(result)

    async def test_a_port_is_part_of_the_namespace_and_so_of_the_id(self) -> None:
        # OpenLineage's spec allows `unitycatalog://{host}[:{port}]`; the two
        # spellings are two namespaces, hence two ids, for one workspace.
        with_port = await UnityCatalogExtractor(namespace=NS + ":443").extract(TABLES)
        assert tid("main.sales.orders", NS + ":443") in by_id(with_port)
        assert tid("main.sales.orders") not in by_id(with_port)

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
# Separate runs against one store
# ---------------------------------------------------------------------------

#: Table lineage plus one column fact, naming only objects `TABLES` holds.
LINEAGE_ONLY = [*TABLE_LINEAGE[:1], *COLUMN_LINEAGE]

#: A schema record that carries what a placeholder must never overwrite.
RICH_SCHEMAS = {
    "schemas": [
        {
            "name": "sales",
            "catalog_name": "main",
            "full_name": "main.sales",
            "owner": "sales-eng",
            "comment": "Sales marts",
        }
    ]
}

#: Both link-or-mint settings: off writes bare creates, on adds bind-if-absent aliases.
BOTH_MODES = pytest.mark.parametrize("lom", [False, True], ids=["lom_off", "lom_on"])


class Store:
    """A real SQLite-backed registry and the write path an extraction batch takes."""

    def __init__(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, lom: bool
    ) -> None:
        monkeypatch.setenv("TRELLIS_KNOWLEDGE_LINK_OR_MINT", "1" if lom else "0")
        stores = tmp_path / "stores"
        stores.mkdir()
        self.registry = StoreRegistry(stores_dir=stores)
        self.lom = lom

    @property
    def graph(self) -> Any:
        return self.registry.knowledge.graph_store

    def write(self, result: ExtractionResult) -> None:
        batch = result_to_batch(result, requested_by="test", bind_identity=self.lom)
        outcomes = build_curate_executor(
            self.registry, clearance="restricted"
        ).execute_batch(batch)
        assert all(r.status is CommandStatus.SUCCESS for r in outcomes)

    def props(self, node_id: str) -> dict[str, Any]:
        node = self.graph.get_node(node_id)
        assert node is not None, node_id
        return dict(node["properties"])

    def snapshot(self) -> list[tuple[str, str, int]]:
        """Every node's id, properties and version count, to compare store states.

        The version count shows a write that left the properties as they were:
        a replace adds an SCD-2 version, a skip adds none.
        """
        return sorted(
            (
                nid,
                json.dumps(self.props(nid), sort_keys=True),
                len(self.graph.get_node_history(nid)),
            )
            for nid in (n["node_id"] for n in self.graph.query(limit=1000))
        )


def _catalog(name: str = "main") -> str:
    return f"uc_catalog:{NS}:{name}"


def _schema(full_name: str = "main.sales") -> str:
    return f"uc_schema:{NS}:{full_name}"


class TestSeparateRuns:
    @BOTH_MODES
    @pytest.mark.parametrize("metadata_first", [True, False])
    async def test_a_lineage_only_run_does_not_overwrite_metadata(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        lom: bool,
        metadata_first: bool,
    ) -> None:
        store = Store(tmp_path, monkeypatch, lom=lom)
        metadata, lineage = await extract(TABLES), await extract(LINEAGE_ONLY)
        for result in (metadata, lineage) if metadata_first else (lineage, metadata):
            store.write(result)

        orders = store.props(tid("main.sales.orders"))
        assert orders["description"] == "One row per order"
        assert orders["owner"] == "data-eng"
        assert orders["table_type"] == "MANAGED"
        assert "stub" not in orders
        column = store.props(f"uc_column:{NS}:main.sales.orders/order_id")
        assert column["type_text"] == "bigint"
        assert column["description"] == "Primary key"
        assert "stub" not in column

        revenue = tid("main.sales.daily_revenue")
        for node_id, upstream in (
            (revenue, tid("main.sales.orders")),
            (
                f"uc_column:{NS}:main.sales.daily_revenue/revenue",
                f"uc_column:{NS}:main.sales.orders/order_id",
            ),
        ):
            edges = store.graph.get_edges(
                node_id, direction="outgoing", edge_type="depends_on"
            )
            assert [e["target_id"] for e in edges] == [upstream]

    @BOTH_MODES
    async def test_a_tables_only_run_after_a_full_export_keeps_catalog_and_schema(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lom: bool
    ) -> None:
        # The first re-gate finding: the parents the tables-only run re-named
        # were full creates, so they replaced what the full export had written.
        store = Store(tmp_path, monkeypatch, lom=lom)
        store.write(await extract([CATALOGS, RICH_SCHEMAS, TABLES]))
        catalog, schema = store.props(_catalog()), store.props(_schema())
        assert catalog["owner"] == "data-eng"
        assert catalog["description"] == "Primary catalog"
        assert schema["owner"] == "sales-eng"
        assert schema["description"] == "Sales marts"
        versions = [
            len(store.graph.get_node_history(n)) for n in (_catalog(), _schema())
        ]

        store.write(await extract(TABLES))
        assert store.props(_catalog()) == catalog
        assert store.props(_schema()) == schema
        # Not even a same-valued rewrite: the tables are real and are written
        # again, the parents are placeholders and are not.
        assert versions == [
            len(store.graph.get_node_history(n)) for n in (_catalog(), _schema())
        ]

    @BOTH_MODES
    async def test_a_schemas_only_run_after_a_full_export_keeps_the_catalog(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lom: bool
    ) -> None:
        store = Store(tmp_path, monkeypatch, lom=lom)
        store.write(await extract([CATALOGS, RICH_SCHEMAS, TABLES]))
        catalog = store.props(_catalog())
        assert catalog["owner"] == "data-eng"

        store.write(await extract(RICH_SCHEMAS))
        assert store.props(_catalog()) == catalog
        assert store.props(_schema())["owner"] == "sales-eng"

    @BOTH_MODES
    async def test_a_lineage_only_run_after_a_full_export_keeps_every_node(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lom: bool
    ) -> None:
        store = Store(tmp_path, monkeypatch, lom=lom)
        store.write(await extract([CATALOGS, RICH_SCHEMAS, TABLES, LINEAGE_ONLY]))
        before = store.snapshot()

        store.write(await extract(LINEAGE_ONLY))
        assert store.snapshot() == before
        assert store.props(tid("main.sales.orders"))["owner"] == "data-eng"

    @BOTH_MODES
    async def test_a_placeholder_is_marked_and_the_real_record_replaces_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lom: bool
    ) -> None:
        store = Store(tmp_path, monkeypatch, lom=lom)
        store.write(await extract(LINEAGE_ONLY))
        for node_id in (
            tid("main.sales.orders"),
            _schema(),
            _catalog(),
            f"uc_column:{NS}:main.sales.orders/order_id",
        ):
            assert store.props(node_id)["stub"] is True, node_id

        # The metadata that arrives later is a full create, so it lands whole
        # and carries no placeholder mark.
        store.write(await extract([CATALOGS, RICH_SCHEMAS, TABLES]))
        for node_id in (
            tid("main.sales.orders"),
            _schema(),
            _catalog(),
            f"uc_column:{NS}:main.sales.orders/order_id",
        ):
            assert "stub" not in store.props(node_id), node_id
        assert store.props(tid("main.sales.orders"))["owner"] == "data-eng"
        assert store.props(_catalog())["owner"] == "data-eng"
        assert store.props(_schema())["description"] == "Sales marts"

    @BOTH_MODES
    async def test_a_lineage_only_run_alone_writes_placeholders_and_whole_edges(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lom: bool
    ) -> None:
        store = Store(tmp_path, monkeypatch, lom=lom)
        result = await extract(LINEAGE_ONLY)
        assert {e.entity_id for e in result.entities if not e.if_absent} == set()
        store.write(result)

        # catalog, schema, two tables and two columns; five contains and two
        # depends_on edges, none dangling.
        assert store.graph.count_nodes() == 6
        assert store.graph.count_edges() == 7
        for edge in result.edges:
            assert store.graph.get_node(edge.source_id) is not None
            assert store.graph.get_node(edge.target_id) is not None
        edges = store.graph.get_edges(
            tid("main.sales.daily_revenue"),
            direction="outgoing",
            edge_type="depends_on",
        )
        assert [e["target_id"] for e in edges] == [tid("main.sales.orders")]

    @BOTH_MODES
    async def test_a_rerun_of_each_leaves_the_graph_unchanged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lom: bool
    ) -> None:
        store = Store(tmp_path, monkeypatch, lom=lom)
        counts = []
        for _ in range(2):
            for raw in (TABLES, LINEAGE_ONLY):
                store.write(await extract(raw))
            counts.append((store.graph.count_nodes(), store.graph.count_edges()))
        assert counts[0] == counts[1]


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
