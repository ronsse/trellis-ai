"""Tests for reference extractors in ``trellis_workers.extract``.

The previous :class:`IngestionWorker` pipeline has been replaced by the
tiered extraction model: extractors are pure functions that return
:class:`ExtractionResult`, and the CLI (or other callers) submits the
resulting drafts through :class:`MutationExecutor`.
"""

from __future__ import annotations

import pytest

from trellis.extract.base import ExtractorTier
from trellis.schemas import well_known as wk
from trellis_workers.extract import (
    DbtManifestExtractor,
    OpenLineageExtractor,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_DBT_MANIFEST: dict = {
    "nodes": {
        "model.my_project.stg_orders": {
            "unique_id": "model.my_project.stg_orders",
            "resource_type": "model",
            "name": "stg_orders",
            "schema": "staging",
            "database": "analytics",
            "description": "Staged orders from source",
            "depends_on": {"nodes": ["source.my_project.raw.orders"]},
            "config": {"materialized": "view"},
            "tags": ["staging"],
        },
        "model.my_project.fct_orders": {
            "unique_id": "model.my_project.fct_orders",
            "resource_type": "model",
            "name": "fct_orders",
            "schema": "marts",
            "description": "Fact table for orders",
            "depends_on": {"nodes": ["model.my_project.stg_orders"]},
            "config": {"materialized": "table"},
            "tags": ["marts"],
        },
        "test.my_project.not_null_stg_orders_id": {
            "unique_id": "test.my_project.not_null_stg_orders_id",
            "resource_type": "test",
            "name": "not_null_stg_orders_id",
            "depends_on": {"nodes": ["model.my_project.stg_orders"]},
            "tags": [],
        },
    },
    "sources": {
        "source.my_project.raw.orders": {
            "unique_id": "source.my_project.raw.orders",
            "resource_type": "source",
            "name": "orders",
            "source_name": "raw",
            "schema": "public",
            "description": "Raw orders table",
        },
    },
}


SAMPLE_OL_EVENTS: list[dict] = [
    {
        "eventType": "COMPLETE",
        "job": {"namespace": "spark", "name": "etl_job"},
        "inputs": [{"namespace": "warehouse", "name": "raw.events"}],
        "outputs": [{"namespace": "warehouse", "name": "analytics.daily_events"}],
    },
    {
        "eventType": "COMPLETE",
        "job": {"namespace": "spark", "name": "agg_job"},
        "inputs": [{"namespace": "warehouse", "name": "analytics.daily_events"}],
        "outputs": [{"namespace": "warehouse", "name": "analytics.weekly_summary"}],
    },
]


# ---------------------------------------------------------------------------
# DbtManifestExtractor
# ---------------------------------------------------------------------------


class TestDbtManifestExtractor:
    def test_metadata(self) -> None:
        ext = DbtManifestExtractor()
        assert ext.name == "dbt_manifest"
        assert ext.tier is ExtractorTier.DETERMINISTIC
        assert "dbt-manifest" in ext.supported_sources

    async def test_produces_entities_for_nodes_and_sources(self) -> None:
        ext = DbtManifestExtractor()
        result = await ext.extract(SAMPLE_DBT_MANIFEST, source_hint="dbt-manifest")

        entity_ids = {e.entity_id for e in result.entities}
        assert "model.my_project.stg_orders" in entity_ids
        assert "source.my_project.raw.orders" in entity_ids
        assert "test.my_project.not_null_stg_orders_id" in entity_ids

        type_map = {e.entity_id: e.entity_type for e in result.entities}
        assert type_map["model.my_project.stg_orders"] == "dbt_model"
        assert type_map["source.my_project.raw.orders"] == "dbt_source"
        assert type_map["test.my_project.not_null_stg_orders_id"] == "dbt_test"

    async def test_model_properties(self) -> None:
        ext = DbtManifestExtractor()
        result = await ext.extract(SAMPLE_DBT_MANIFEST)

        stg = next(
            e for e in result.entities if e.entity_id == "model.my_project.stg_orders"
        )
        assert stg.name == "stg_orders"
        assert stg.properties["schema"] == "staging"
        assert stg.properties["materialized"] == "view"
        assert stg.properties["description"] == "Staged orders from source"

    async def test_source_properties(self) -> None:
        ext = DbtManifestExtractor()
        result = await ext.extract(SAMPLE_DBT_MANIFEST)

        src = next(
            e for e in result.entities if e.entity_id == "source.my_project.raw.orders"
        )
        assert src.properties["source_name"] == "raw"

    async def test_dependency_edges(self) -> None:
        ext = DbtManifestExtractor()
        result = await ext.extract(SAMPLE_DBT_MANIFEST)

        pairs = {(e.source_id, e.target_id, e.edge_kind) for e in result.edges}
        assert (
            "model.my_project.stg_orders",
            "source.my_project.raw.orders",
            "depends_on",
        ) in pairs
        assert (
            "model.my_project.fct_orders",
            "model.my_project.stg_orders",
            "depends_on",
        ) in pairs

    async def test_depends_on_edges_allow_dangling(self) -> None:
        """Cross-manifest depends_on targets may not be in the same batch."""
        ext = DbtManifestExtractor()
        result = await ext.extract(SAMPLE_DBT_MANIFEST)
        assert result.edges, "fixture should produce depends_on edges"
        assert all(edge.allow_dangling for edge in result.edges)

    async def test_empty_manifest(self) -> None:
        ext = DbtManifestExtractor()
        result = await ext.extract({"nodes": {}, "sources": {}})
        assert result.entities == []
        assert result.edges == []

    async def test_missing_unique_id_skipped(self) -> None:
        ext = DbtManifestExtractor()
        result = await ext.extract(
            {
                "nodes": {
                    "bad": {"resource_type": "model", "name": "no_id"},
                    "good": {
                        "unique_id": "model.p.good",
                        "resource_type": "model",
                        "name": "good",
                    },
                },
            }
        )
        ids = {e.entity_id for e in result.entities}
        assert ids == {"model.p.good"}

    async def test_non_dict_input_raises(self) -> None:
        ext = DbtManifestExtractor()
        with pytest.raises(TypeError):
            await ext.extract([1, 2, 3])

    async def test_provenance(self) -> None:
        ext = DbtManifestExtractor()
        result = await ext.extract(SAMPLE_DBT_MANIFEST, source_hint="dbt-manifest")
        assert result.extractor_used == "dbt_manifest"
        assert result.tier == ExtractorTier.DETERMINISTIC.value
        assert result.provenance.extractor_name == "dbt_manifest"
        assert result.provenance.source_hint == "dbt-manifest"


# ---------------------------------------------------------------------------
# OpenLineageExtractor
# ---------------------------------------------------------------------------


class TestOpenLineageExtractor:
    def test_metadata(self) -> None:
        ext = OpenLineageExtractor()
        assert ext.name == "openlineage"
        assert ext.tier is ExtractorTier.DETERMINISTIC
        assert "openlineage" in ext.supported_sources

    async def test_produces_job_and_dataset_entities(self) -> None:
        ext = OpenLineageExtractor()
        result = await ext.extract(SAMPLE_OL_EVENTS)

        ids = {e.entity_id for e in result.entities}
        assert "job:spark:etl_job" in ids
        assert "job:spark:agg_job" in ids
        assert "dataset:warehouse:raw.events" in ids
        assert "dataset:warehouse:analytics.daily_events" in ids
        assert "dataset:warehouse:analytics.weekly_summary" in ids

        type_map = {e.entity_id: e.entity_type for e in result.entities}
        assert type_map["job:spark:etl_job"] == "job"
        assert type_map["dataset:warehouse:raw.events"] == "dataset"

    async def test_reads_and_writes_edges(self) -> None:
        ext = OpenLineageExtractor()
        result = await ext.extract(SAMPLE_OL_EVENTS)

        pairs = {(e.source_id, e.target_id, e.edge_kind) for e in result.edges}
        assert (
            "job:spark:etl_job",
            "dataset:warehouse:raw.events",
            "reads_from",
        ) in pairs
        assert (
            "job:spark:etl_job",
            "dataset:warehouse:analytics.daily_events",
            "writes_to",
        ) in pairs

    async def test_edges_allow_dangling(self) -> None:
        """Streamed OL events may reference datasets emitted in prior batches."""
        ext = OpenLineageExtractor()
        result = await ext.extract(SAMPLE_OL_EVENTS)
        assert result.edges, "fixture should produce reads_from / writes_to edges"
        assert all(edge.allow_dangling for edge in result.edges)

    async def test_deduplicates_edges(self) -> None:
        ext = OpenLineageExtractor()
        duplicate_events = [SAMPLE_OL_EVENTS[0], SAMPLE_OL_EVENTS[0]]
        result = await ext.extract(duplicate_events)
        reads = [e for e in result.edges if e.edge_kind == "reads_from"]
        writes = [e for e in result.edges if e.edge_kind == "writes_to"]
        assert len(reads) == 1
        assert len(writes) == 1

    async def test_skips_events_without_job(self) -> None:
        ext = OpenLineageExtractor()
        result = await ext.extract(
            [
                {"eventType": "COMPLETE", "job": {"namespace": "", "name": ""}},
                {"eventType": "COMPLETE"},
            ]
        )
        assert result.entities == []
        assert result.edges == []

    async def test_non_list_input_raises(self) -> None:
        ext = OpenLineageExtractor()
        with pytest.raises(TypeError):
            await ext.extract({"not": "a list"})

    async def test_provenance(self) -> None:
        ext = OpenLineageExtractor()
        result = await ext.extract(SAMPLE_OL_EVENTS, source_hint="openlineage")
        assert result.extractor_used == "openlineage"
        assert result.tier == ExtractorTier.DETERMINISTIC.value
        assert result.provenance.source_hint == "openlineage"


# ---------------------------------------------------------------------------
# OpenLineageExtractor: datasets that name a Unity Catalog table
#
# openlineage-spark 1.51.0+ on Databricks puts the storage location in the
# dataset's own namespace and name and the table only in a ``unity-catalog``
# symlink; other producers emit ``unitycatalog://<host>`` as the dataset's own
# namespace.  Every name below is invented.
# ---------------------------------------------------------------------------

_STORAGE_NS = "s3://Example-Bucket"
_STORAGE_NAME = "/Warehouse/Orders"
_UC_ORDERS = "dataset:unity_catalog:main.sales.orders"


def _symlink(
    name: str, *, namespace: str = "unity-catalog", kind: str = "TABLE"
) -> dict:
    return {"namespace": namespace, "name": name, "type": kind}


def _dataset(
    namespace: str = _STORAGE_NS,
    name: str = _STORAGE_NAME,
    *,
    symlinks: list[dict] | None = None,
) -> dict:
    ds: dict = {"namespace": namespace, "name": name}
    if symlinks is not None:
        ds["facets"] = {
            "schema": {"fields": [{"name": "order_id", "type": "long"}]},
            "symlinks": {"identifiers": symlinks},
        }
    return ds


def _event(inputs: list[dict] | None = None, outputs: list[dict] | None = None) -> dict:
    return {
        "job": {"namespace": "databricks", "name": "build_revenue"},
        "inputs": inputs or [],
        "outputs": outputs or [],
    }


def _targets(result, kind: str) -> set[str]:
    return {e.target_id for e in result.edges if e.edge_kind == kind}


class TestOpenLineageUnityCatalog:
    @pytest.mark.parametrize(
        "spelling",
        ["main.sales.orders", "Main.Sales.Orders", "`main`.`sales`.`orders`"],
    )
    async def test_a_storage_dataset_with_a_symlink_lands_on_the_table(
        self, spelling: str
    ) -> None:
        event = _event(
            inputs=[_dataset(symlinks=[_symlink(spelling)])],
            outputs=[
                _dataset(
                    "s3://example-bucket",
                    "/w/out",
                    symlinks=[_symlink("main.sales.daily_revenue")],
                )
            ],
        )
        result = await OpenLineageExtractor().extract([event])

        assert _targets(result, "reads_from") == {_UC_ORDERS}
        assert _targets(result, "writes_to") == {
            "dataset:unity_catalog:main.sales.daily_revenue"
        }
        # Not the storage-path node.
        assert f"dataset:{_STORAGE_NS}:{_STORAGE_NAME}" not in {
            e.entity_id for e in result.entities
        }
        assert result.skipped_rows == {}

    async def test_the_placeholder_is_name_only_and_carries_the_storage_alias(
        self,
    ) -> None:
        event = _event(inputs=[_dataset(symlinks=[_symlink("Main.Sales.Orders")])])
        result = await OpenLineageExtractor().extract([event])

        table = next(e for e in result.entities if e.entity_id == _UC_ORDERS)
        assert table.entity_type == "dataset"
        assert table.if_absent is True
        assert table.name == "orders"
        assert table.properties == {
            "name": "orders",
            "full_name": "main.sales.orders",
            "database_name": "main",
            "schema_name": "sales",
            "source_system": "databricks",
            "stub": True,
        }, "no facets, no namespace, no OpenLineage physical_uri"
        assert [(a.source_system, a.raw_id) for a in table.aliases] == [
            ("openlineage_dataset", f"{_STORAGE_NS}/{_STORAGE_NAME}")
        ]

    async def test_two_storage_identities_of_one_table_are_one_entity(self) -> None:
        link = [_symlink("main.sales.orders")]
        event = _event(
            inputs=[
                _dataset(symlinks=link),
                _dataset(_STORAGE_NS, "/Moved", symlinks=link),
            ]
        )
        result = await OpenLineageExtractor().extract([event])

        tables = [e for e in result.entities if e.entity_type == "dataset"]
        assert [t.entity_id for t in tables] == [_UC_ORDERS]
        assert [a.raw_id for a in tables[0].aliases] == [
            f"{_STORAGE_NS}/{_STORAGE_NAME}",
            f"{_STORAGE_NS}//Moved",
        ]

    @pytest.mark.parametrize("kind", ["table", "Table", " TABLE "])
    async def test_the_symlink_type_is_case_insensitive(self, kind: str) -> None:
        event = _event(
            inputs=[_dataset(symlinks=[_symlink("main.sales.orders", kind=kind)])]
        )
        result = await OpenLineageExtractor().extract([event])
        assert _targets(result, "reads_from") == {_UC_ORDERS}

    @pytest.mark.parametrize("kind", ["VOLUME", "", None])
    async def test_a_symlink_that_is_not_a_table_is_not_an_identity(self, kind) -> None:
        link = _symlink("main.sales.orders", kind=kind)
        event = _event(inputs=[_dataset(symlinks=[link])])
        result = await OpenLineageExtractor().extract([event])
        assert _targets(result, "reads_from") == {
            f"dataset:{_STORAGE_NS}:{_STORAGE_NAME}"
        }
        assert result.skipped_rows == {}

    async def test_a_symlink_in_another_namespace_is_ignored(self) -> None:
        link = _symlink("main.sales.orders", namespace="hive")
        result = await OpenLineageExtractor().extract(
            [_event(inputs=[_dataset(symlinks=[link])])]
        )
        assert _targets(result, "reads_from") == {
            f"dataset:{_STORAGE_NS}:{_STORAGE_NAME}"
        }

    async def test_the_scope_of_a_symlink_is_configurable(self) -> None:
        event = _event(inputs=[_dataset(symlinks=[_symlink("main.sales.orders")])])
        for given, scope in (
            ("unitycatalog://Host:443/", "unitycatalog://host"),
            ("custom", "custom"),
            (None, "unity_catalog"),
            ("  ", "unity_catalog"),
        ):
            result = await OpenLineageExtractor(unity_catalog_scope=given).extract(
                [event]
            )
            assert _targets(result, "reads_from") == {
                f"dataset:{scope}:main.sales.orders"
            }

    async def test_a_unitycatalog_namespace_is_its_own_scope(self) -> None:
        event = _event(
            inputs=[
                _dataset("UnityCatalog://Host.Example.net:443/", "Main.Sales.Orders")
            ],
            outputs=[
                _dataset(
                    _STORAGE_NS,
                    _STORAGE_NAME,
                    symlinks=[
                        _symlink(
                            "main.sales.daily",
                            namespace="unitycatalog://other.example.net",
                        )
                    ],
                )
            ],
        )
        result = await OpenLineageExtractor(unity_catalog_scope="ignored").extract(
            [event]
        )
        assert _targets(result, "reads_from") == {
            "dataset:unitycatalog://host.example.net:main.sales.orders"
        }
        assert _targets(result, "writes_to") == {
            "dataset:unitycatalog://other.example.net:main.sales.daily"
        }

    async def test_a_dataset_with_no_unity_catalog_identity_is_untouched(self) -> None:
        """Storage paths are case-sensitive: the id is exactly the OpenLineage pair."""
        ds = _dataset(
            _STORAGE_NS,
            _STORAGE_NAME,
            symlinks=[_symlink("hive_db.Orders", namespace="hive")],
        )
        result = await OpenLineageExtractor().extract([_event(inputs=[ds])])

        did = f"dataset:{_STORAGE_NS}:{_STORAGE_NAME}"
        assert _targets(result, "reads_from") == {did}
        entity = next(e for e in result.entities if e.entity_id == did)
        assert entity.if_absent is False
        assert entity.properties["facets"] == ds["facets"]
        assert entity.properties["physical_uri"] == f"{_STORAGE_NS}/{_STORAGE_NAME}"
        assert [a.raw_id for a in entity.aliases] == [f"{_STORAGE_NS}/{_STORAGE_NAME}"]

    async def test_an_unparseable_name_is_counted_and_falls_back(self) -> None:
        event = _event(
            inputs=[
                _dataset(symlinks=[_symlink("main.sales")]),
                _dataset(_STORAGE_NS, "/Other", symlinks=[_symlink("a..b")]),
                _dataset("unitycatalog://host", "main.sales"),
            ]
        )
        result = await OpenLineageExtractor().extract([event])

        assert _targets(result, "reads_from") == {
            f"dataset:{_STORAGE_NS}:{_STORAGE_NAME}",
            f"dataset:{_STORAGE_NS}:/Other",
            "dataset:unitycatalog://host:main.sales",
        }
        assert result.skipped_rows == {
            "uc_primary_unparsed": 1,
            "uc_symlink_unparsed": 2,
        }

    async def test_an_unparseable_primary_still_uses_a_good_symlink(self) -> None:
        """A name is counted only when nothing on the dataset resolves (per dataset)."""
        ds = _dataset(
            "unitycatalog://host", "orders", symlinks=[_symlink("main.sales.orders")]
        )
        result = await OpenLineageExtractor().extract([_event(inputs=[ds])])
        assert _targets(result, "reads_from") == {_UC_ORDERS}
        assert result.skipped_rows == {}

    async def test_a_dataset_with_several_unparseable_names_is_counted_once(
        self,
    ) -> None:
        ds = _dataset(
            "unitycatalog://host",
            "orders",
            symlinks=[
                _symlink("one"),
                _symlink("a.b", namespace="unitycatalog://host"),
                _symlink("x..y"),
            ],
        )
        result = await OpenLineageExtractor().extract([_event(inputs=[ds])])
        assert _targets(result, "reads_from") == {"dataset:unitycatalog://host:orders"}
        # The first place that failed names the reason: the primary id.
        assert result.skipped_rows == {"uc_primary_unparsed": 1}

    async def test_the_same_unparseable_dataset_is_counted_per_mention(self) -> None:
        """Two mentions are two datasets: the count is per dataset, not per name."""
        ds = _dataset(symlinks=[_symlink("main.sales")])
        result = await OpenLineageExtractor().extract([_event(inputs=[ds, ds])])
        assert result.skipped_rows == {"uc_symlink_unparsed": 2}

    @pytest.mark.parametrize(
        "namespace", ["unitycatalog://", "UnityCatalog:///", " unitycatalog:// "]
    )
    async def test_a_bare_unitycatalog_primary_namespace_is_counted(
        self, namespace: str
    ) -> None:
        ds = _dataset(namespace, "main.sales.orders")
        result = await OpenLineageExtractor().extract([_event(inputs=[ds])])

        # Counted, and still its verbatim node: no host, no scope to guess.
        assert _targets(result, "reads_from") == {
            f"dataset:{namespace}:main.sales.orders"
        }
        assert result.skipped_rows == {"uc_namespace_without_host": 1}

    async def test_a_bare_unitycatalog_symlink_namespace_is_counted(self) -> None:
        ds = _dataset(
            symlinks=[_symlink("main.sales.orders", namespace="unitycatalog://")]
        )
        result = await OpenLineageExtractor().extract([_event(inputs=[ds])])

        assert _targets(result, "reads_from") == {
            f"dataset:{_STORAGE_NS}:{_STORAGE_NAME}"
        }
        assert result.skipped_rows == {"uc_namespace_without_host": 1}

    async def test_a_bare_namespace_is_not_counted_when_another_identifier_resolves(
        self,
    ) -> None:
        ds = _dataset(
            "unitycatalog://",
            "orders",
            symlinks=[
                _symlink("main.sales.orders", namespace="unitycatalog://"),
                _symlink("main.sales.orders"),
            ],
        )
        result = await OpenLineageExtractor().extract([_event(inputs=[ds])])
        assert _targets(result, "reads_from") == {_UC_ORDERS}
        assert result.skipped_rows == {}

    @pytest.mark.parametrize(
        "facets",
        [
            {"symlinks": None},
            {"symlinks": "x"},
            {"symlinks": {"identifiers": "x"}},
            {
                "symlinks": {
                    "identifiers": [None, 3, "x", {}, {"namespace": 1, "name": 2}]
                }
            },
            "x",
        ],
    )
    async def test_malformed_symlink_facets_are_ignored(self, facets) -> None:
        ds = {"namespace": _STORAGE_NS, "name": _STORAGE_NAME, "facets": facets}
        result = await OpenLineageExtractor().extract([_event(inputs=[ds])])
        assert _targets(result, "reads_from") == {
            f"dataset:{_STORAGE_NS}:{_STORAGE_NAME}"
        }
        assert result.skipped_rows == {}


class TestOpenLineageStorageRedirect:
    """One batch, one node: a bare mention of a storage location another mention
    resolved to a table goes to that table too."""

    _BARE = _dataset()  # the storage location with no Unity Catalog identity
    _LINKED = _dataset(symlinks=[_symlink("main.sales.orders")])

    @pytest.mark.parametrize("bare_first", [False, True])
    async def test_a_bare_mention_of_a_resolved_location_joins_the_table(
        self, bare_first: bool
    ) -> None:
        first, second = (
            (self._BARE, self._LINKED) if bare_first else (self._LINKED, self._BARE)
        )
        events = [_event(inputs=[first]), _event(outputs=[second])]
        result = await OpenLineageExtractor().extract(events)

        datasets = [e for e in result.entities if e.entity_type == "dataset"]
        assert [d.entity_id for d in datasets] == [_UC_ORDERS], "one node, not two"
        table = datasets[0]
        assert table.if_absent is True
        assert table.properties.get("stub") is True
        assert "facets" not in table.properties
        assert "namespace" not in table.properties
        assert [a.raw_id for a in table.aliases] == [f"{_STORAGE_NS}/{_STORAGE_NAME}"]
        assert _targets(result, "reads_from") | _targets(result, "writes_to") == {
            _UC_ORDERS
        }
        assert result.skipped_rows == {"uc_storage_redirected": 1}

    async def test_the_bare_mention_in_the_same_event_is_redirected_too(self) -> None:
        result = await OpenLineageExtractor().extract(
            [_event(inputs=[self._LINKED], outputs=[self._BARE])]
        )
        assert _targets(result, "writes_to") == {_UC_ORDERS}
        assert result.skipped_rows == {"uc_storage_redirected": 1}

    async def test_each_redirected_mention_is_counted(self) -> None:
        events = [
            _event(inputs=[self._LINKED]),
            _event(inputs=[self._BARE]),
            _event(outputs=[self._BARE]),
        ]
        result = await OpenLineageExtractor().extract(events)
        assert result.skipped_rows == {"uc_storage_redirected": 2}

    @pytest.mark.parametrize(
        ("namespace", "name"),
        [
            ("s3://example-bucket", _STORAGE_NAME),  # case differs in the namespace
            (_STORAGE_NS, "/warehouse/orders"),  # case differs in the name
            (_STORAGE_NS, "/Warehouse/Orders/"),  # a trailing slash
            ("S3://Example-Bucket", _STORAGE_NAME),
        ],
    )
    async def test_only_the_exact_storage_location_matches(
        self, namespace: str, name: str
    ) -> None:
        other = _dataset(namespace, name)
        result = await OpenLineageExtractor().extract(
            [_event(inputs=[self._LINKED, other])]
        )
        assert _targets(result, "reads_from") == {
            _UC_ORDERS,
            f"dataset:{namespace}:{name}",
        }
        assert result.skipped_rows == {}

    async def test_a_location_resolved_to_two_tables_is_not_guessed(self) -> None:
        events = [
            _event(inputs=[self._LINKED]),
            _event(inputs=[_dataset(symlinks=[_symlink("main.sales.returns")])]),
            _event(outputs=[self._BARE]),
        ]
        result = await OpenLineageExtractor().extract(events)

        assert _targets(result, "writes_to") == {
            f"dataset:{_STORAGE_NS}:{_STORAGE_NAME}"
        }
        assert result.skipped_rows == {"uc_storage_ambiguous": 1}

    async def test_a_dataset_that_resolves_to_its_own_table_is_never_redirected(
        self,
    ) -> None:
        events = [
            _event(inputs=[self._LINKED]),
            _event(inputs=[_dataset(symlinks=[_symlink("main.sales.returns")])]),
        ]
        result = await OpenLineageExtractor().extract(events)
        assert _targets(result, "reads_from") == {
            _UC_ORDERS,
            "dataset:unity_catalog:main.sales.returns",
        }
        assert result.skipped_rows == {}

    async def test_an_event_without_a_job_does_not_resolve_a_location(self) -> None:
        """The pre-pass sees the datasets the drafting pass sees, no others."""
        jobless = {"inputs": [self._LINKED], "outputs": []}
        result = await OpenLineageExtractor().extract(
            [jobless, _event(inputs=[self._BARE])]
        )
        assert _targets(result, "reads_from") == {
            f"dataset:{_STORAGE_NS}:{_STORAGE_NAME}"
        }
        assert result.skipped_rows == {}

    async def test_a_bare_mention_with_an_unreadable_symlink_is_redirected_and_counted(
        self,
    ) -> None:
        unreadable = _dataset(symlinks=[_symlink("main.sales")])
        result = await OpenLineageExtractor().extract(
            [_event(inputs=[self._LINKED, unreadable])]
        )
        assert _targets(result, "reads_from") == {_UC_ORDERS}
        assert result.skipped_rows == {
            "uc_storage_redirected": 1,
            "uc_symlink_unparsed": 1,
        }

    async def test_nothing_redirects_across_batches(self) -> None:
        """The extractor is pure: a later batch's bare mention is its own node."""
        extractor = OpenLineageExtractor()
        await extractor.extract([_event(inputs=[self._LINKED])])
        later = await extractor.extract([_event(inputs=[self._BARE])])
        assert _targets(later, "reads_from") == {
            f"dataset:{_STORAGE_NS}:{_STORAGE_NAME}"
        }
        assert later.skipped_rows == {}


class TestOpenLineageDefaultScopeNamespace:
    """A dataset whose own namespace is the scope string a table id carries has a
    verbatim id that IS a table's id: it must never be a full draft there."""

    @pytest.mark.parametrize("name", ["main.sales.orders", "Main.Sales.Orders"])
    async def test_the_default_scope_as_a_namespace_is_a_table_identity(
        self, name: str
    ) -> None:
        ds = {
            "namespace": "unity_catalog",
            "name": name,
            "facets": {"schema": {"fields": [{"name": "x", "type": "long"}]}},
        }
        result = await OpenLineageExtractor().extract([_event(inputs=[ds])])

        table = next(e for e in result.entities if e.entity_id == _UC_ORDERS)
        assert table.if_absent is True, (
            "a full draft here would replace the catalog row"
        )
        assert table.properties["stub"] is True
        assert "facets" not in table.properties
        assert _targets(result, "reads_from") == {_UC_ORDERS}
        assert result.skipped_rows == {}

    async def test_a_configured_scope_is_reserved_as_well(self) -> None:
        ds = {"namespace": "custom", "name": "main.sales.orders"}
        result = await OpenLineageExtractor(unity_catalog_scope="custom").extract(
            [_event(inputs=[ds])]
        )
        table = next(
            e
            for e in result.entities
            if e.entity_id == "dataset:custom:main.sales.orders"
        )
        assert table.if_absent is True

    async def test_the_default_scope_stays_reserved_under_another_configured_scope(
        self,
    ) -> None:
        """A catalog export with no namespace writes ``unity_catalog`` ids whatever
        scope this extractor reads symlinks in."""
        ds = {"namespace": "unity_catalog", "name": "main.sales.orders"}
        result = await OpenLineageExtractor(unity_catalog_scope="custom").extract(
            [_event(inputs=[ds])]
        )
        assert (
            next(e for e in result.entities if e.entity_id == _UC_ORDERS).if_absent
            is True
        )

    async def test_another_namespace_is_untouched(self) -> None:
        ds = {"namespace": "unity_catalog_x", "name": "main.sales.orders"}
        result = await OpenLineageExtractor().extract([_event(inputs=[ds])])
        entity = next(e for e in result.entities if e.entity_type == "dataset")
        assert entity.entity_id == "dataset:unity_catalog_x:main.sales.orders"
        assert entity.if_absent is False
        assert result.skipped_rows == {}

    async def test_a_name_that_is_no_table_keeps_its_node_and_is_counted(self) -> None:
        """Its id cannot be a table's (those are three-part), so it stays a full
        draft; the count says a table-shaped namespace held no table name."""
        ds = {"namespace": "unity_catalog", "name": "orders"}
        result = await OpenLineageExtractor().extract([_event(inputs=[ds])])
        entity = next(e for e in result.entities if e.entity_type == "dataset")
        assert entity.entity_id == "dataset:unity_catalog:orders"
        assert entity.if_absent is False
        assert result.skipped_rows == {"uc_primary_unparsed": 1}


class TestOpenLineageScopeProperty:
    @pytest.mark.parametrize(
        ("given", "expected"),
        [
            (None, "unity_catalog"),
            ("", "unity_catalog"),
            ("custom", "custom"),
            ("UnityCatalog://Host:443/", "unitycatalog://host"),
        ],
    )
    def test_the_scope_in_use_is_readable_after_normalisation(
        self, given: str | None, expected: str
    ) -> None:
        assert (
            OpenLineageExtractor(unity_catalog_scope=given).unity_catalog_scope
            == expected
        )

    def test_the_version_moved_with_the_ids(self) -> None:
        assert OpenLineageExtractor.version == "0.3.0"


# ---------------------------------------------------------------------------
# Cross-database routing properties contract (B1)
#
# Both reference extractors are expected to populate the canonical routing
# properties from ``trellis.schemas.well_known`` on dataset-shaped entities
# when the upstream source supplies the information.
# ---------------------------------------------------------------------------


_MANIFEST_WITH_ADAPTER: dict = {
    "metadata": {"adapter_type": "snowflake"},
    "nodes": {
        "model.p.fct_orders": {
            "unique_id": "model.p.fct_orders",
            "resource_type": "model",
            "name": "fct_orders",
            "schema": "marts",
            "database": "analytics",
            "config": {"materialized": "table"},
        },
        "test.p.not_null_fct_orders": {
            "unique_id": "test.p.not_null_fct_orders",
            "resource_type": "test",
            "name": "not_null_fct_orders",
            "schema": "marts",
            "database": "analytics",
        },
        "model.p.no_database": {
            "unique_id": "model.p.no_database",
            "resource_type": "model",
            "name": "no_database",
            "schema": "staging",
        },
    },
    "sources": {
        "source.p.raw.orders": {
            "unique_id": "source.p.raw.orders",
            "resource_type": "source",
            "name": "orders",
            "source_name": "raw",
            "schema": "public",
            "database": "raw_landing",
        },
    },
}


class TestDbtRoutingProperties:
    async def test_model_has_full_routing(self) -> None:
        result = await DbtManifestExtractor().extract(_MANIFEST_WITH_ADAPTER)
        fct = next(e for e in result.entities if e.entity_id == "model.p.fct_orders")
        assert fct.properties[wk.DATASET_PROP_SOURCE_SYSTEM] == "snowflake"
        assert fct.properties[wk.DATASET_PROP_SCHEMA_NAME] == "marts"
        assert fct.properties[wk.DATASET_PROP_DATABASE_NAME] == "analytics"
        assert (
            fct.properties[wk.DATASET_PROP_PHYSICAL_URI]
            == "snowflake://analytics/marts/fct_orders"
        )

    async def test_source_has_full_routing(self) -> None:
        result = await DbtManifestExtractor().extract(_MANIFEST_WITH_ADAPTER)
        src = next(e for e in result.entities if e.entity_id == "source.p.raw.orders")
        assert src.properties[wk.DATASET_PROP_SOURCE_SYSTEM] == "snowflake"
        assert src.properties[wk.DATASET_PROP_SCHEMA_NAME] == "public"
        assert src.properties[wk.DATASET_PROP_DATABASE_NAME] == "raw_landing"
        assert (
            src.properties[wk.DATASET_PROP_PHYSICAL_URI]
            == "snowflake://raw_landing/public/orders"
        )

    async def test_tests_skip_physical_uri(self) -> None:
        """dbt tests are not queryable datasets — no physical_uri."""
        result = await DbtManifestExtractor().extract(_MANIFEST_WITH_ADAPTER)
        t = next(
            e for e in result.entities if e.entity_id == "test.p.not_null_fct_orders"
        )
        assert wk.DATASET_PROP_PHYSICAL_URI not in t.properties
        # ``source_system`` is still populated — it's a property of the
        # manifest, not the resource type.
        assert t.properties[wk.DATASET_PROP_SOURCE_SYSTEM] == "snowflake"

    async def test_missing_parts_skip_physical_uri(self) -> None:
        result = await DbtManifestExtractor().extract(_MANIFEST_WITH_ADAPTER)
        m = next(e for e in result.entities if e.entity_id == "model.p.no_database")
        assert wk.DATASET_PROP_PHYSICAL_URI not in m.properties
        # ``database_name`` is absent in source — extractor must not fabricate.
        assert wk.DATASET_PROP_DATABASE_NAME not in m.properties
        assert m.properties[wk.DATASET_PROP_SCHEMA_NAME] == "staging"

    async def test_no_adapter_type_skips_source_system(self) -> None:
        manifest_no_adapter = {
            "nodes": {
                "model.p.x": {
                    "unique_id": "model.p.x",
                    "resource_type": "model",
                    "name": "x",
                    "schema": "s",
                    "database": "d",
                },
            },
        }
        result = await DbtManifestExtractor().extract(manifest_no_adapter)
        e = next(iter(result.entities))
        assert wk.DATASET_PROP_SOURCE_SYSTEM not in e.properties
        assert wk.DATASET_PROP_PHYSICAL_URI not in e.properties
        # Parts are still populated.
        assert e.properties[wk.DATASET_PROP_SCHEMA_NAME] == "s"
        assert e.properties[wk.DATASET_PROP_DATABASE_NAME] == "d"


class TestOpenLineageRoutingProperties:
    async def test_uri_namespace_extracts_scheme(self) -> None:
        events = [
            {
                "eventType": "COMPLETE",
                "job": {"namespace": "spark", "name": "j"},
                "outputs": [
                    {"namespace": "snowflake://acct.region", "name": "db.public.orders"}
                ],
            }
        ]
        result = await OpenLineageExtractor().extract(events)
        ds = next(e for e in result.entities if e.entity_type == "dataset")
        assert ds.properties[wk.DATASET_PROP_SOURCE_SYSTEM] == "snowflake"
        assert (
            ds.properties[wk.DATASET_PROP_PHYSICAL_URI]
            == "snowflake://acct.region/db.public.orders"
        )

    async def test_bare_namespace_used_as_system(self) -> None:
        events = [
            {
                "eventType": "COMPLETE",
                "job": {"namespace": "spark", "name": "j"},
                "outputs": [{"namespace": "warehouse", "name": "raw.events"}],
            }
        ]
        result = await OpenLineageExtractor().extract(events)
        ds = next(e for e in result.entities if e.entity_type == "dataset")
        assert ds.properties[wk.DATASET_PROP_SOURCE_SYSTEM] == "warehouse"
        assert ds.properties[wk.DATASET_PROP_PHYSICAL_URI] == "warehouse:raw.events"

    async def test_routing_only_on_datasets_not_jobs(self) -> None:
        result = await OpenLineageExtractor().extract(SAMPLE_OL_EVENTS)
        job = next(e for e in result.entities if e.entity_type == "job")
        # Jobs are not queryable datasets — routing properties stay empty.
        assert wk.DATASET_PROP_SOURCE_SYSTEM not in job.properties
        assert wk.DATASET_PROP_PHYSICAL_URI not in job.properties
