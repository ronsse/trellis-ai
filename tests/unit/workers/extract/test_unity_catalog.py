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
from trellis.schemas import well_known as wk
from trellis.schemas.enums import NodeRole
from trellis.schemas.extraction import ExtractionResult
from trellis.stores.registry import StoreRegistry
from trellis_workers.extract.openlineage import OpenLineageExtractor
from trellis_workers.extract.unity_catalog import (
    DEFAULT_MAX_COLUMNS_PER_TABLE,
    INPUT_REFUSAL_CODE,
    MAX_COLUMN_NAMES,
    UnityCatalogExtractor,
    check_input,
)
from trellis_workers.extract.unity_catalog_identity import normalize_scope

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
            assert entities[column].allow_structural_leaf is False
        assert edge_set(result, "depends_on") == {(tgt, src)}
        assert (tid("main.sales.orders"), src) in edge_set(result, "contains")
        assert len(edge_set(result, "contains")) == 8  # the six of TABLES, two more

    async def test_columns_are_structural_leaves(self) -> None:
        entities = by_id(await extract(EVERYTHING))
        column = entities[f"uc_column:{NS}:main.sales.orders/order_id"]
        assert column.node_role is NodeRole.STRUCTURAL
        # `uc_column` is not in ENTITY_TYPE_ANTI_PATTERNS: nothing to opt out of.
        assert "uc_column" not in wk.ENTITY_TYPE_ANTI_PATTERNS
        assert column.allow_structural_leaf is False
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
                if key == "column_names":  # the one list: bounded, see TestColumnBounds
                    assert all(isinstance(name, str) for name in value)
                    continue
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
# Column bounds (adr-source-modeling-discipline.md §2.2, §5)
# ---------------------------------------------------------------------------


def wide_table(width: int, name: str = "wide") -> dict[str, Any]:
    return {
        "name": name,
        "catalog_name": "main",
        "schema_name": "sales",
        "full_name": f"main.sales.{name}",
        "columns": [
            {"name": f"c{i:05d}", "type_text": "int", "position": i}
            for i in range(width)
        ],
    }


def column_ids(result: ExtractionResult) -> list[str]:
    return sorted(e.entity_id for e in result.entities if e.entity_type == "uc_column")


class TestColumnBounds:
    async def test_the_table_carries_its_column_names_in_position_order(self) -> None:
        props = by_id(await extract(EVERYTHING))[tid("main.sales.orders")].properties
        assert props["column_names"] == ["order_id", "customer_id"]
        assert props["column_count"] == 2

    async def test_a_small_table_is_unchanged_by_the_cap(self) -> None:
        # Every column of an ordinary table is a node, and nothing is counted.
        result = await extract({"tables": [TABLES["tables"][0]]})
        assert column_ids(result) == [
            f"uc_column:{NS}:main.sales.orders/customer_id",
            f"uc_column:{NS}:main.sales.orders/order_id",
        ]
        assert result.skipped_rows == {}
        order_id = by_id(result)[f"uc_column:{NS}:main.sales.orders/order_id"]
        assert order_id.properties == {
            "name": "order_id",
            "table_full_name": "main.sales.orders",
            "source_system": "databricks",
            "type_text": "bigint",
            "type_name": "LONG",
            "position": 0,
            "nullable": False,
            "description": "Primary key",
        }

    async def test_a_table_at_the_default_cap_loses_nothing(self) -> None:
        result = await extract({"tables": [wide_table(DEFAULT_MAX_COLUMNS_PER_TABLE)]})
        assert len(column_ids(result)) == DEFAULT_MAX_COLUMNS_PER_TABLE
        assert result.skipped_rows == {}

    async def test_columns_past_the_cap_are_counted_not_dropped_silently(self) -> None:
        result = await extract({"tables": [wide_table(5)]}, max_columns_per_table=3)
        assert [c.rsplit("/", 1)[1] for c in column_ids(result)] == [
            "c00000",
            "c00001",
            "c00002",
        ]
        assert result.skipped_rows == {"columns_over_cap": 2}
        props = by_id(result)[tid("main.sales.wide")].properties
        assert props["column_count"] == 5  # the truth, beside the bounded node set
        assert len(props["column_names"]) == 5  # a name costs far less than a node

    async def test_the_default_cap_bounds_a_very_wide_table(self) -> None:
        width = MAX_COLUMN_NAMES + 5
        result = await extract({"tables": [wide_table(width)]})
        assert len(column_ids(result)) == DEFAULT_MAX_COLUMNS_PER_TABLE
        assert result.skipped_rows == {
            "columns_over_cap": width - DEFAULT_MAX_COLUMNS_PER_TABLE,
            "column_names_over_limit": 5,
        }
        props = by_id(result)[tid("main.sales.wide")].properties
        assert len(props["column_names"]) == MAX_COLUMN_NAMES
        assert props["column_count"] == width

    async def test_column_names_survive_with_columns_off(self) -> None:
        result = await extract({"tables": [wide_table(4)]}, include_columns=False)
        assert column_ids(result) == []
        assert result.skipped_rows == {}
        props = by_id(result)[tid("main.sales.wide")].properties
        assert props["column_names"] == ["c00000", "c00001", "c00002", "c00003"]

    async def test_a_column_that_lineage_names_is_minted_past_the_cap(self) -> None:
        rows = [
            {
                "source_table_full_name": "main.sales.wide",
                "target_table_full_name": "main.sales.daily_revenue",
                "source_column_name": "c00004",
                "target_column_name": "revenue",
            }
        ]
        result = await extract(
            [{"tables": [wide_table(5)]}, rows], max_columns_per_table=2
        )
        entities = by_id(result)
        column = f"uc_column:{NS}:main.sales.wide/c00004"
        assert column in entities  # its lineage edge needs both ends
        # The export holds it, so it is minted from its record, not as a stub.
        assert "stub" not in entities[column].properties
        assert entities[column].properties["type_text"] == "int"
        assert entities[column].if_absent is False
        # c00002 and c00003 stay left out; c00004 came back through lineage.
        assert result.skipped_rows == {"columns_over_cap": 2}

    async def test_a_held_column_past_the_cap_keeps_its_shown_name(self) -> None:
        table = wide_table(3)
        table["columns"][2]["name"] = "Revenue_USD"
        rows = [
            {
                "source_table_full_name": "main.sales.wide",
                "target_table_full_name": "main.sales.daily_revenue",
                "source_column_name": "REVENUE_usd",
                "target_column_name": "revenue",
            }
        ]
        result = await extract([{"tables": [table]}, rows], max_columns_per_table=1)
        column = by_id(result)[f"uc_column:{NS}:main.sales.wide/revenue_usd"]
        assert column.name == "Revenue_USD"
        assert column.properties["position"] == 2
        # The target table is not in the export, so its column stays a placeholder.
        target = by_id(result)[f"uc_column:{NS}:main.sales.daily_revenue/revenue"]
        assert target.properties["stub"] is True

    async def test_every_overflow_column_lineage_names_leaves_the_count(self) -> None:
        rows = [
            {
                "source_table_full_name": "main.sales.wide",
                "target_table_full_name": "main.sales.daily_revenue",
                "source_column_name": f"c{i:05d}",
                "target_column_name": f"t{i}",
            }
            for i in (1, 2)
        ]
        result = await extract(
            [{"tables": [wide_table(3)]}, rows], max_columns_per_table=1
        )
        assert "columns_over_cap" not in result.skipped_rows

    @pytest.mark.parametrize("bad", [0, -1])
    def test_a_cap_below_one_is_refused(self, bad: int) -> None:
        with pytest.raises(ValueError, match="at least 1"):
            UnityCatalogExtractor(max_columns_per_table=bad)

    async def test_the_cap_does_not_change_ids_or_order(self) -> None:
        # A cap only chooses a prefix of the same sorted drafts.
        full = await extract({"tables": [wide_table(6)]})
        capped = await extract({"tables": [wide_table(6)]}, max_columns_per_table=4)
        assert set(column_ids(capped)) <= set(column_ids(full))
        assert len(column_ids(capped)) == 4


class TestSkippedRows:
    async def test_a_clean_export_reports_none(self) -> None:
        assert (await extract(EVERYTHING)).skipped_rows == {}

    async def test_skipped_rows_are_counted_by_reason(self) -> None:
        rows = [
            # a path on one side is not a table-to-table fact
            {
                "source_table_full_name": "main.sales.orders",
                "target_table_full_name": None,
            },
            {
                "source_table_full_name": None,
                "target_table_full_name": "main.sales.orders",
            },
            # a column row without both column names
            {
                "source_table_full_name": "main.sales.orders",
                "target_table_full_name": "main.sales.daily_revenue",
                "source_column_name": "order_id",
                "target_column_name": "",
            },
            # a table feeding itself
            {
                "source_table_full_name": "main.sales.orders",
                "target_table_full_name": "main.sales.orders",
            },
        ]
        api = {
            "table_name": "main.sales.orders",
            "upstreams": [{"notTableInfo": 1}],
            "downstreams": [],
        }
        result = await extract([TABLES, rows, api])
        assert result.skipped_rows == {
            "column_lineage_without_two_columns": 1,
            "lineage_item_without_tableInfo": 1,
            "lineage_without_two_tables": 2,
            "self_lineage": 1,
        }

    async def test_column_lineage_with_columns_off_is_counted(self) -> None:
        result = await extract(EVERYTHING, include_columns=False)
        assert result.skipped_rows == {"column_lineage_with_columns_off": 1}

    async def test_a_volume_record_is_counted_not_minted_as_a_table(self) -> None:
        volume = {
            "catalog_name": "main",
            "schema_name": "sales",
            "name": "landing",
            "full_name": "main.sales.landing",
            "volume_type": "MANAGED",
            "volume_id": "v-1",
        }
        result = await extract([volume])
        assert not [
            e for e in result.entities if e.entity_id.endswith("main.sales.landing")
        ]
        assert result.skipped_rows["volume_records"] == 1

    async def test_a_function_record_is_counted_not_minted_as_a_table(self) -> None:
        function = {
            "catalog_name": "main",
            "schema_name": "sales",
            "name": "net_price",
            "full_name": "main.sales.net_price",
            "routine_body": "SQL",
            "function_id": "f-1",
        }
        result = await extract([function])
        assert not [
            e for e in result.entities if e.entity_id.endswith("main.sales.net_price")
        ]
        assert result.skipped_rows == {"function_records": 1}

    async def test_a_wrapped_volumes_response_is_counted_not_refused(self) -> None:
        volumes = {
            "volumes": [
                {
                    "catalog_name": "main",
                    "schema_name": "sales",
                    "name": "landing",
                    "full_name": "main.sales.landing",
                    "volume_type": "EXTERNAL",
                    "volume_id": "v-1",
                },
                {
                    "catalog_name": "main",
                    "schema_name": "sales",
                    "name": "archive",
                    "full_name": "main.sales.archive",
                    "volume_type": "MANAGED",
                    "volume_id": "v-2",
                },
            ],
            "next_page_token": None,
        }
        result = await extract([TABLES, volumes])
        ids = {e.entity_id for e in result.entities}
        assert tid("main.sales.landing") not in ids
        assert tid("main.sales.archive") not in ids
        assert result.skipped_rows == {"volume_records": 2}

    async def test_a_table_without_a_columns_list_is_counted(self) -> None:
        table = {
            "catalog_name": "main",
            "schema_name": "sales",
            "name": "orders",
            "full_name": "main.sales.orders",
            "table_type": "MANAGED",
        }
        result = await extract({"tables": [table]})
        node = by_id(result)[tid("main.sales.orders")]
        assert "stub" not in node.properties
        assert "column_count" not in node.properties
        assert result.skipped_rows == {"tables_without_columns": 1}

    async def test_a_table_with_an_empty_columns_list_is_not_counted(self) -> None:
        table = {
            "catalog_name": "main",
            "schema_name": "sales",
            "name": "orders",
            "full_name": "main.sales.orders",
            "table_type": "MANAGED",
            "columns": [],
        }
        result = await extract({"tables": [table]})
        assert by_id(result)[tid("main.sales.orders")].properties["column_count"] == 0
        assert result.skipped_rows == {}


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

    @pytest.mark.parametrize(
        "record",
        [
            {"catalog_name": "Main", "schema_name": "Sales", "name": "Orders"},
            {"full_name": "Main.Sales.Orders"},
            {"full_name": "Main . Sales . Orders"},
            {"full_name": "`Main`.`Sales`.`Orders`"},
            {"full_name": " `Main` . Sales .`Orders` "},
        ],
    )
    async def test_the_extractor_and_the_lookup_agree_on_the_key(
        self, record: dict[str, str]
    ) -> None:
        """Quoted, mixed-case and spaced spellings: the id the extractor builds
        is the key ``save_knowledge`` looks up, since both come from
        ``normalize_alias_raw_id``."""
        spelling = record.get("full_name") or ".".join(
            record[k] for k in ("catalog_name", "schema_name", "name")
        )
        key = wk.normalize_alias_raw_id("unity_catalog", spelling)
        assert key == "main.sales.orders"

        orders = by_id(await extract({"tables": [record]}))[tid(key)]
        assert {
            a.raw_id for a in orders.aliases if a.source_system == "unity_catalog"
        } == {key}

    @pytest.mark.parametrize("full_name", ["Main..Orders", "Main. .Orders", "``.a.b"])
    async def test_a_name_with_an_empty_part_is_refused(self, full_name: str) -> None:
        with pytest.raises(ValidationError, match="names no catalog, schema and table"):
            await extract({"tables": [{"full_name": full_name}]})

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

    async def test_namespaces_differing_in_case_port_or_slash_are_one(self) -> None:
        raw = [
            {"namespace": NS, **CATALOGS},
            {
                "namespace": NS.upper().replace("UNITYCATALOG", "UnityCatalog")
                + ":443/",
                **TABLES,
            },
        ]
        check_input(raw)
        result = await UnityCatalogExtractor().extract(raw)
        assert tid("main.sales.orders") in by_id(result)

    async def test_the_https_port_is_dropped_and_any_other_is_kept(self) -> None:
        # The OpenLineage naming spec allows `unitycatalog://{host}[:{port}]`;
        # `:443` is the HTTPS default, so it names the same workspace as no port.
        with_443 = await UnityCatalogExtractor(namespace=NS + ":443").extract(TABLES)
        assert tid("main.sales.orders") in by_id(with_443)
        other = await UnityCatalogExtractor(namespace=NS + ":8443").extract(TABLES)
        assert tid("main.sales.orders", NS + ":8443") in by_id(other)
        assert tid("main.sales.orders") not in by_id(other)

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

    @pytest.mark.parametrize("seed", range(4))
    async def test_tag_row_order_does_not_change_the_labels(self, seed: int) -> None:
        tags = [
            {"catalog_name": "main", "tag_name": "classification", "tag_value": "x"},
            {
                "catalog_name": "main",
                "schema_name": "sales",
                "table_name": "orders",
                "tag_name": "classification",
                "tag_value": "restricted",
            },
        ]
        mapping = {"classification": {"*": "confidential", "restricted": "restricted"}}
        extractor = UnityCatalogExtractor(namespace=NS, sensitivity_map=mapping)
        baseline = drafts(await extractor.extract([*EVERYTHING, *tags]))
        mixed = shuffled([*EVERYTHING, *tags], random.Random(seed))  # noqa: S311
        assert drafts(await extractor.extract(mixed)) == baseline


# ---------------------------------------------------------------------------
# Meeting OpenLineage datasets
# ---------------------------------------------------------------------------

JOB = "job:databricks:build_revenue"
#: No namespace: the scope an OpenLineage ``unity-catalog`` symlink uses by default.
BARE = "unity_catalog"
STORAGE = ("abfss://Lake@account.dfs.core.windows.net", "/Tables/Orders")


def storage_dataset(table: str, *, namespace: str = "unity-catalog") -> dict[str, Any]:
    """A dataset as openlineage-spark 1.51+ emits it on Databricks: the storage
    location is the dataset's own name, the table only a symlink."""
    return {
        "namespace": STORAGE[0],
        "name": STORAGE[1],
        "facets": {
            "symlinks": {
                "identifiers": [
                    {"namespace": namespace, "name": table, "type": "TABLE"}
                ]
            }
        },
    }


def lineage_event(*inputs: dict[str, Any]) -> dict[str, Any]:
    return {
        "job": {"namespace": "databricks", "name": "build_revenue"},
        "inputs": list(inputs),
        "outputs": [],
    }


async def uc_ids(**options: Any) -> set[str]:
    """The dataset ids a Unity Catalog extractor with ``options`` mints for TABLES."""
    result = await UnityCatalogExtractor(**options).extract(TABLES)
    return {e.entity_id for e in result.entities if e.entity_type == "dataset"}


class TestScope:
    @pytest.mark.parametrize(
        ("given", "expected"),
        [
            ("unitycatalog://host", "unitycatalog://host"),
            ("unitycatalog://host/", "unitycatalog://host"),
            (" unitycatalog://host// ", "unitycatalog://host"),
            ("UnityCatalog://Host.Example.NET", "unitycatalog://host.example.net"),
            ("unitycatalog://host:443", "unitycatalog://host"),
            ("unitycatalog://HOST:443/", "unitycatalog://host"),
            ("unitycatalog://host:8443", "unitycatalog://host:8443"),
            ("unitycatalog://host:1443", "unitycatalog://host:1443"),
            ("unitycatalog://host/Path/", "unitycatalog://host/Path"),
            ("unity_catalog", "unity_catalog"),
            ("https://Host:443/", "https://Host:443"),
            ("", None),
            ("  /", None),
            ("unitycatalog://", None),
            ("unitycatalog:///", None),
            (None, None),
            (3, None),
        ],
    )
    def test_normalize_scope(self, given: Any, expected: str | None) -> None:
        assert normalize_scope(given) == expected

    async def test_the_extractor_reads_its_scope_through_the_helper(self) -> None:
        spelled = "UnityCatalog://DBC-0000.example.net:443/"
        assert await uc_ids(namespace=spelled) == await uc_ids(namespace=NS)


class TestOpenLineageMeetsTheCatalog:
    @pytest.mark.parametrize(
        "spelling",
        ["main.sales.orders", "Main.Sales.Orders", "`main`.`sales`.`orders`"],
    )
    async def test_a_databricks_symlink_lands_on_the_catalogs_node(
        self, spelling: str
    ) -> None:
        ol = await OpenLineageExtractor().extract(
            [lineage_event(storage_dataset(spelling))]
        )
        edge = next(e for e in ol.edges if e.edge_kind == "reads_from")
        assert edge.source_id == JOB
        assert edge.target_id == tid("main.sales.orders", BARE)
        assert edge.target_id in await uc_ids()  # byte for byte, no namespace set
        assert edge.target_id in {e.entity_id for e in ol.entities}

    async def test_a_namespaced_catalog_meets_a_scoped_extractor(self) -> None:
        event = lineage_event(storage_dataset("Main.Sales.Orders"))
        scoped = await OpenLineageExtractor(unity_catalog_scope=NS + "/").extract(
            [event]
        )
        edge = next(e for e in scoped.edges if e.edge_kind == "reads_from")
        assert edge.target_id == tid("main.sales.orders", NS)
        assert edge.target_id in await uc_ids(namespace=NS)

        # Without the scope the two disagree: the id carries the scope.
        unscoped = await OpenLineageExtractor().extract([event])
        assert next(e.target_id for e in unscoped.edges) not in await uc_ids(
            namespace=NS
        )

    @pytest.mark.parametrize(
        "ol_namespace",
        [
            "unitycatalog://dbc-0000.example.net",
            "unitycatalog://DBC-0000.example.net:443/",
            "UnityCatalog://dbc-0000.EXAMPLE.net:443",
        ],
    )
    async def test_a_unitycatalog_primary_id_meets_the_catalog(
        self, ol_namespace: str
    ) -> None:
        event = lineage_event({"namespace": ol_namespace, "name": "Main.Sales.Orders"})
        ol = await OpenLineageExtractor().extract([event])
        edge = next(e for e in ol.edges if e.edge_kind == "reads_from")
        assert edge.target_id == tid("main.sales.orders")
        assert edge.target_id in await uc_ids(
            namespace="unitycatalog://dbc-0000.example.net"
        )
        assert edge.target_id in await uc_ids(namespace=NS)

    async def test_the_placeholder_has_the_shape_of_the_catalogs_own(self) -> None:
        """Same type, keys and values as the catalog's placeholder for a table it
        can name but not describe, whichever extractor wrote it."""
        event = lineage_event({"namespace": NS, "name": "main.sales.orders"})
        ol = await OpenLineageExtractor().extract([event])
        mine = by_id(ol)[tid("main.sales.orders")]
        uc = await extract(
            LINEAGE_ONLY
        )  # names `orders` in lineage, never describes it
        theirs = by_id(uc)[tid("main.sales.orders")]
        assert theirs.properties["stub"] is True
        assert (mine.entity_type, mine.if_absent) == (theirs.entity_type, True)
        assert mine.properties == theirs.properties
        assert mine.properties["physical_uri"] == (
            "databricks://dbc-0000.example.net/main/sales/orders"
        )

    async def test_the_old_agreement_still_holds_for_a_matching_spelling(self) -> None:
        # The case the extractors agreed on before OpenLineage read symlinks.
        event = lineage_event({"namespace": NS, "name": "main.sales.orders"})
        ol = await OpenLineageExtractor().extract([event])
        assert tid("main.sales.orders") in by_id(ol)
        assert tid("main.sales.orders") in await uc_ids(namespace=NS)


class TestOpenLineageAndCatalogInOneStore:
    """Both write orders through the executor into a real SQLite graph store."""

    @staticmethod
    async def _runs() -> tuple[ExtractionResult, ExtractionResult]:
        uc = await UnityCatalogExtractor().extract(
            [CATALOGS, SCHEMAS, TABLES], source_hint="unity-catalog"
        )
        ol = await OpenLineageExtractor().extract(
            [lineage_event(storage_dataset("Main.Sales.Orders"))], source_hint="ol"
        )
        return uc, ol

    @BOTH_MODES
    async def test_the_catalog_first_keeps_its_metadata_and_gains_the_edge(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lom: bool
    ) -> None:
        store = Store(tmp_path, monkeypatch, lom=lom)
        uc, ol = await self._runs()
        orders = tid("main.sales.orders", BARE)

        store.write(uc)
        before = (store.props(orders), len(store.graph.get_node_history(orders)))
        store.write(ol)

        # Nothing the catalog wrote was touched: same properties, no new version.
        assert (
            store.props(orders),
            len(store.graph.get_node_history(orders)),
        ) == before
        assert store.props(orders)["description"] == "One row per order"
        assert "stub" not in store.props(orders)
        edges = store.graph.get_edges(JOB, direction="outgoing", edge_type="reads_from")
        assert [e["target_id"] for e in edges] == [orders]
        assert store.graph.get_node(f"dataset:{STORAGE[0]}:{STORAGE[1]}") is None

    @BOTH_MODES
    async def test_openlineage_first_leaves_a_placeholder_the_catalog_replaces(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lom: bool
    ) -> None:
        store = Store(tmp_path, monkeypatch, lom=lom)
        uc, ol = await self._runs()
        orders = tid("main.sales.orders", BARE)

        store.write(ol)
        assert store.props(orders)["stub"] is True
        assert "description" not in store.props(orders)

        store.write(uc)
        assert "stub" not in store.props(orders)
        assert store.props(orders)["description"] == "One row per order"
        assert store.props(orders)["table_type"] == "MANAGED"
        edges = store.graph.get_edges(JOB, direction="outgoing", edge_type="reads_from")
        assert [e["target_id"] for e in edges] == [orders]
        # One node for the table, not a storage-path twin beside it.
        datasets = [
            n["node_id"] for n in store.graph.query(node_type="dataset", limit=100)
        ]
        assert sorted(datasets) == [
            tid("main.sales.daily_revenue", BARE),
            orders,
        ]

    @BOTH_MODES
    async def test_a_dataset_in_the_default_scope_namespace_leaves_the_catalog_row(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lom: bool
    ) -> None:
        """Its verbatim id is the table's id; a full draft there replaced the row."""
        store = Store(tmp_path, monkeypatch, lom=lom)
        uc, _ = await self._runs()
        orders = tid("main.sales.orders", BARE)
        shadow = {
            "namespace": BARE,
            "name": "main.sales.orders",
            "facets": {"schema": {"fields": [{"name": "x", "type": "long"}]}},
        }
        ol = await OpenLineageExtractor().extract(
            [lineage_event(shadow)], source_hint="ol"
        )

        store.write(uc)
        before = (store.props(orders), len(store.graph.get_node_history(orders)))
        store.write(ol)

        assert (
            store.props(orders),
            len(store.graph.get_node_history(orders)),
        ) == before
        assert store.props(orders)["description"] == "One row per order"
        edges = store.graph.get_edges(JOB, direction="outgoing", edge_type="reads_from")
        assert [e["target_id"] for e in edges] == [orders]

    @BOTH_MODES
    async def test_a_mixed_batch_writes_one_node_for_the_table(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lom: bool
    ) -> None:
        """A symlinked mention and a bare mention of one location: one node, and the
        job reads and writes it, where the bare mention used to get a twin."""
        store = Store(tmp_path, monkeypatch, lom=lom)
        uc, _ = await self._runs()
        orders = tid("main.sales.orders", BARE)
        bare = {"namespace": STORAGE[0], "name": STORAGE[1]}
        event = lineage_event(storage_dataset("Main.Sales.Orders"))
        event["outputs"] = [bare]
        ol = await OpenLineageExtractor().extract([event], source_hint="ol")
        assert ol.skipped_rows == {"uc_storage_redirected": 1}

        store.write(uc)
        store.write(ol)

        assert store.graph.get_node(f"dataset:{STORAGE[0]}:{STORAGE[1]}") is None
        assert store.props(orders)["description"] == "One row per order"
        for kind in ("reads_from", "writes_to"):
            edges = store.graph.get_edges(JOB, direction="outgoing", edge_type=kind)
            assert [e["target_id"] for e in edges] == [orders]

    @pytest.mark.parametrize("catalog_first", [True, False])
    async def test_an_if_absent_write_on_an_existing_node_still_binds_its_aliases(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, catalog_first: bool
    ) -> None:
        """With link-or-mint on the storage alias lands on the table either way;
        off, no extractor binds an alias (it is the knob's, not this change's)."""
        storage_key = f"{STORAGE[0]}/{STORAGE[1]}"
        (tmp_path / "on").mkdir()
        (tmp_path / "off").mkdir()
        on = Store(tmp_path / "on", monkeypatch, lom=True)
        uc, ol = await self._runs()
        for result in (uc, ol) if catalog_first else (ol, uc):
            on.write(result)
        hit = on.graph.resolve_alias("openlineage_dataset", storage_key)
        assert hit is not None
        assert hit["entity_id"] == tid("main.sales.orders", BARE)
        assert on.graph.resolve_alias("unity_catalog", "main.sales.orders") is not None

        off = Store(tmp_path / "off", monkeypatch, lom=False)
        for result in (uc, ol):
            off.write(result)
        assert off.graph.resolve_alias("openlineage_dataset", storage_key) is None

    @BOTH_MODES
    async def test_a_second_run_changes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lom: bool
    ) -> None:
        store = Store(tmp_path, monkeypatch, lom=lom)
        uc, ol = await self._runs()
        store.write(ol)
        store.write(uc)
        first = store.snapshot()
        store.write(ol)
        store.write(ol)
        # A job is rewritten by every run, as before this change; no dataset is.
        assert [row for row in store.snapshot() if row[0].startswith("dataset:")] == [
            row for row in first if row[0].startswith("dataset:")
        ]
        edges = store.graph.get_edges(JOB, direction="outgoing", edge_type="reads_from")
        assert len(edges) == 1


# ---------------------------------------------------------------------------
# Labels inherited from mapped source tags
# ---------------------------------------------------------------------------

#: The operator's map: which tag key carries classification, and what each value means.
LABEL_MAP = {
    "classification": {"confidential": "confidential", "restricted": "restricted"}
}
ORDERS = {"tables": [TABLES["tables"][0]]}
ORDERS_ID = tid("main.sales.orders")


def _column(name: str, table: str = "main.sales.orders") -> str:
    return f"uc_column:{NS}:{table}/{name}"


def tag(value: str | None = "restricted", **names: str) -> dict[str, Any]:
    """One ``information_schema.*_tags`` row naming ``names`` (catalog first)."""
    row: dict[str, Any] = {"catalog_name": "main", **names}
    row["tag_name"] = "classification"
    if value is not None:
        row["tag_value"] = value
    return row


SCHEMA_TAG = tag("confidential", schema_name="sales")
TABLE_TAG = tag("restricted", schema_name="sales", table_name="orders")


def label(draft: Any) -> str | None:
    record = draft.properties.get("data_classification")
    return None if record is None else record["sensitivity"]


async def labelled(raw: Any, **options: Any) -> ExtractionResult:
    options.setdefault("sensitivity_map", LABEL_MAP)
    return await extract(raw, **options)


class TestSourceLabels:
    async def test_a_table_inherits_the_strictest_mapped_tag_of_itself_and_its_parents(
        self,
    ) -> None:
        result = await labelled([ORDERS, SCHEMA_TAG, TABLE_TAG])
        entities = by_id(result)
        assert label(entities[_schema()]) == "confidential"
        for nid in (ORDERS_ID, _column("order_id"), _column("customer_id")):
            assert label(entities[nid]) == "restricted"
            record = entities[nid].properties["data_classification"]
            assert record["classified_by"] == ["source-inherit"]
        assert "data_classification" not in entities[_catalog()].properties

    async def test_a_catalog_tag_reaches_every_node_below_it(self) -> None:
        entities = by_id(await labelled([ORDERS, tag("confidential")]))
        assert {label(e) for e in entities.values()} == {"confidential"}

    async def test_a_column_tag_raises_only_that_column(self) -> None:
        plain = by_id(await extract(ORDERS))
        column_tag = tag(
            "restricted",
            schema_name="sales",
            table_name="orders",
            column_name="customer_id",
        )
        entities = by_id(await labelled([ORDERS, column_tag]))
        assert label(entities[_column("customer_id")]) == "restricted"
        assert label(entities[_column("order_id")]) is None
        assert entities[ORDERS_ID].properties == plain[ORDERS_ID].properties

    async def test_a_column_never_reads_below_its_table(self) -> None:
        column_tag = tag(
            "confidential",
            schema_name="sales",
            table_name="orders",
            column_name="customer_id",
        )
        entities = by_id(await labelled([ORDERS, TABLE_TAG, column_tag]))
        assert label(entities[_column("customer_id")]) == "restricted"

    async def test_upper_case_row_keys_read_the_same(self) -> None:
        upper = [
            {k.upper(): v for k, v in row.items()} for row in (SCHEMA_TAG, TABLE_TAG)
        ]
        lower = await labelled([ORDERS, SCHEMA_TAG, TABLE_TAG])
        assert drafts(await labelled([ORDERS, *upper])) == drafts(lower)

    async def test_securable_names_fold_like_a_table_record(self) -> None:
        shouted = tag("restricted", schema_name="Sales", table_name=" ORDERS ")
        entities = by_id(await labelled([ORDERS, shouted]))
        assert label(entities[ORDERS_ID]) == "restricted"

    async def test_tag_rows_under_the_tags_wrapper(self) -> None:
        wrapped = await labelled([ORDERS, {"tags": [SCHEMA_TAG, TABLE_TAG]}])
        bare = await labelled([ORDERS, SCHEMA_TAG, TABLE_TAG])
        assert drafts(wrapped) == drafts(bare)

    async def test_an_unmapped_key_is_ignored_and_counted(self) -> None:
        other = {**TABLE_TAG, "tag_name": "cost_centre", "tag_value": "1234"}
        result = await labelled([ORDERS, other])
        assert drafts(result) == drafts(await extract(ORDERS))
        assert result.skipped_rows["tags_unmapped_key"] == 1

    async def test_a_tag_key_is_compared_exactly(self) -> None:
        other = {**TABLE_TAG, "tag_name": "Classification"}
        result = await labelled([ORDERS, other])
        assert label(by_id(result)[ORDERS_ID]) is None
        assert result.skipped_rows["tags_unmapped_key"] == 1

    async def test_an_unmapped_value_of_a_mapped_key_reads_restricted(self) -> None:
        odd = tag("top-secret-ish", schema_name="sales")
        result = await labelled([ORDERS, odd])
        assert label(by_id(result)[_schema()]) == "restricted"
        assert result.skipped_rows["tags_unmapped_value"] == 1

    @pytest.mark.parametrize("value", ["anything", "", None])
    async def test_a_wildcard_matches_any_value(self, value: str | None) -> None:
        mapping = {"pii": {"*": "confidential"}}
        row = {**tag(value, schema_name="sales"), "tag_name": "pii"}
        result = await labelled([ORDERS, row], sensitivity_map=mapping)
        assert label(by_id(result)[_schema()]) == "confidential"
        assert "tags_unmapped_value" not in result.skipped_rows

    async def test_an_exact_value_beats_the_wildcard(self) -> None:
        mapping = {
            "classification": {"*": "restricted", "confidential": "confidential"}
        }
        result = await labelled([ORDERS, SCHEMA_TAG], sensitivity_map=mapping)
        assert label(by_id(result)[_schema()]) == "confidential"

    async def test_a_level_of_internal_writes_no_key(self) -> None:
        mapping = {"classification": {"*": "internal"}}
        result = await labelled([ORDERS, TABLE_TAG], sensitivity_map=mapping)
        assert drafts(result) == drafts(await extract(ORDERS))

    @pytest.mark.parametrize(
        "mapping",
        [
            {"classification": {"open": "public"}},
            {"classification": {"x": "secret"}},
            {"classification": {"x": 3}},
            {"classification": "restricted"},
            {"": {"x": "restricted"}},
            "classification",
        ],
    )
    def test_a_bad_map_is_refused_at_construction(self, mapping: Any) -> None:
        with pytest.raises(ValueError, match="sensitivity_map"):
            UnityCatalogExtractor(sensitivity_map=mapping)

    def test_the_map_is_copied_and_read_only(self) -> None:
        mapping = {"classification": {"x": "restricted"}}
        extractor = UnityCatalogExtractor(sensitivity_map=mapping)
        mapping["classification"]["x"] = "internal"
        assert extractor.sensitivity_map == {"classification": {"x": "restricted"}}
        with pytest.raises(TypeError):
            extractor.sensitivity_map["other"] = {}  # type: ignore[index]
        with pytest.raises(TypeError):
            extractor.sensitivity_map["classification"]["y"] = "restricted"  # type: ignore[index]

    @pytest.mark.parametrize(
        "rows",
        [
            [TABLE_TAG],
            [{"tags": [SCHEMA_TAG]}],
            [{**tag(schema_name="sales"), "volume_name": "landing"}],
        ],
    )
    async def test_tag_rows_with_no_map_refuse_the_run(self, rows: list[Any]) -> None:
        with pytest.raises(ValidationError, match="--sensitivity-map") as caught:
            await extract([ORDERS, *rows])
        assert caught.value.code == INPUT_REFUSAL_CODE

    @pytest.mark.parametrize(
        "row",
        [
            {**TABLE_TAG, "tag_name": ""},
            {**TABLE_TAG, "tag_name": 7},
            {**TABLE_TAG, "catalog_name": None},
            {**TABLE_TAG, "catalog_name": "  "},
            {**TABLE_TAG, "schema_name": None},
            {**tag(schema_name="sales", column_name="customer_id")},
            {**TABLE_TAG, "tag_value": 3},
            {**TABLE_TAG, "tag_value": ["restricted"]},
        ],
    )
    async def test_an_unreadable_tag_row_refuses_the_run(
        self, row: dict[str, Any]
    ) -> None:
        with pytest.raises(ValidationError) as caught:
            await labelled([ORDERS, row])
        assert caught.value.code == INPUT_REFUSAL_CODE

    async def test_a_tag_row_for_an_absent_table_is_counted(self) -> None:
        absent = tag("restricted", schema_name="sales", table_name="refunds")
        result = await labelled([ORDERS, absent])
        assert result.skipped_rows["tags_for_absent_securables"] == 1
        assert tid("main.sales.refunds") not in by_id(result)

    async def test_a_lineage_stub_of_a_tagged_table_carries_the_label(self) -> None:
        stub_tag = tag("restricted", schema_name="raw", table_name="events")
        result = await labelled([TABLE_LINEAGE[1], stub_tag])
        stub = by_id(result)[tid("main.raw.events")]
        assert stub.if_absent
        assert stub.properties["stub"] is True
        assert label(stub) == "restricted"
        assert "tags_for_absent_securables" not in result.skipped_rows

    async def test_a_volume_tag_row_is_counted(self) -> None:
        row = {**tag(schema_name="sales"), "volume_name": "landing"}
        result = await labelled([ORDERS, row])
        assert result.skipped_rows["tag_rows_on_volumes"] == 1
        assert drafts(result) == drafts(await extract(ORDERS))

    async def test_with_no_tag_rows_a_map_changes_nothing(self) -> None:
        plain = await extract(EVERYTHING)
        mapped = await labelled(EVERYTHING)
        assert drafts(mapped) == drafts(plain)
        assert mapped.skipped_rows == plain.skipped_rows

    @BOTH_MODES
    async def test_a_labelled_batch_writes_and_the_node_keeps_its_label(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lom: bool
    ) -> None:
        store = Store(tmp_path, monkeypatch, lom=lom)
        store.write(await labelled([ORDERS, SCHEMA_TAG, TABLE_TAG]))
        record = store.props(ORDERS_ID)["data_classification"]
        assert record["sensitivity"] == "restricted"
        assert record["classified_by"] == ["source-inherit"]
        assert store.props(_schema())["data_classification"]["sensitivity"] == (
            "confidential"
        )
        assert "data_classification" not in store.props(_catalog())
