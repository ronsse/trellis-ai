"""Tests for the graph-shape sensor.

Two properties get most of the attention here, because they are the two the
report exists to hold.

The **single read pass** is asserted structurally, not by timing: the store
is wrapped in a proxy that raises on ``get_edges``, so an N+1 creeping back
in fails loudly rather than making the sensor quietly expensive on the one
deployment large enough to care.

The **uncovered-split detection is derived, not rostered**, and the test for
it uses a type pair that appears in no alias map at all. A test written only
against ``System``/``system`` would pass equally well against a hard-coded
list of known-bad pairs, which is the thing that rots.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from trellis.analyze.graph_shape import (
    READ_MARGIN,
    GraphShapeReport,
    analyze_graph_shape,
)
from trellis.stores.sqlite.graph import SQLiteGraphStore


@pytest.fixture
def graph_store(tmp_path: Path):
    store = SQLiteGraphStore(tmp_path / "graph.db")
    yield store
    store.close()


class _NoEdgeWalkStore:
    """Proxy that forbids the per-node read the sensor must never make."""

    def __init__(self, inner: SQLiteGraphStore) -> None:
        self._inner = inner

    def get_edges(self, *args: Any, **kwargs: Any) -> Any:
        pytest.fail(
            "analyze_graph_shape called get_edges: the degree distribution "
            "must be computed from the single edge read, not per node"
        )

    def get_subgraph(self, *args: Any, **kwargs: Any) -> Any:
        pytest.fail("analyze_graph_shape must not expand subgraphs")

    def get_aliases(self, *args: Any, **kwargs: Any) -> Any:
        pytest.fail("analyze_graph_shape must not read the alias table per entity")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _analyze(store: SQLiteGraphStore) -> GraphShapeReport:
    """Run the sensor through the proxy, so every test carries the N+1 guard."""
    return analyze_graph_shape(_NoEdgeWalkStore(store))  # type: ignore[arg-type]


class TestEmptyGraph:
    """A graph with nothing in it reports zeros, not errors."""

    def test_empty_graph_is_ok_and_has_no_rates(self, graph_store) -> None:
        report = _analyze(graph_store)

        assert report.status == "ok"
        assert report.nodes == 0
        assert report.edges == 0
        # Every share divides by a population that is zero. None of them may
        # raise, and none may report a misleading 1.0.
        assert report.isolation.share == 0.0
        assert report.referents.any_share == 0.0
        assert report.documents.share == 0.0
        assert report.degree.degree_max == 0
        assert report.collisions.canonical == 0


class TestSingleReadPass:
    """The spec's load-bearing cost property."""

    def test_degree_is_computed_without_per_node_edge_reads(self, graph_store) -> None:
        for index in range(5):
            graph_store.upsert_node(f"n{index}", "Person", {"name": f"p{index}"})
        graph_store.upsert_edge("n0", "n1", "wasAssociatedWith")
        graph_store.upsert_edge("n0", "n2", "wasAssociatedWith")

        # The proxy raises on get_edges; reaching an assertion at all proves
        # the whole pass ran off the two bulk queries.
        report = _analyze(graph_store)

        assert report.degree.degree_max == 2
        assert report.degree.edges == 2

    def test_read_limit_is_sized_from_the_count(self, graph_store) -> None:
        graph_store.upsert_node("n0", "Person", {"name": "p"})
        report = _analyze(graph_store)

        assert report.scan.nodes_counted == 1
        assert report.scan.node_limit == 1 + READ_MARGIN
        assert report.scan.nodes_read == 1
        assert report.scan.truncated is False


class _UndercountingStore:
    """A store whose counts lie low, as one racing a writer would.

    The sensor sizes its read from ``count_*`` and has no cursor to fall back
    on, so a count that under-reports is exactly the condition under which a
    returned row set is a prefix of the graph rather than the graph.
    """

    def __init__(self, inner: SQLiteGraphStore, *, claimed: int) -> None:
        self._inner = inner
        self._claimed = claimed

    def count_nodes(self) -> int:
        return self._claimed

    def count_edges(self) -> int:
        return self._claimed

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class TestTruncation:
    """A partial census is reported as partial, never served as the graph."""

    def test_a_read_at_its_limit_is_truncated(self, graph_store) -> None:
        # Claim -READ_MARGIN so the computed limit lands at 0 -> clamped to 1,
        # while the store actually holds three nodes.
        for index in range(3):
            graph_store.upsert_node(f"n{index}", "Person", {"name": f"p{index}"})
        store = _UndercountingStore(graph_store, claimed=-READ_MARGIN)

        report = analyze_graph_shape(store)  # type: ignore[arg-type]

        assert report.status == "truncated"
        assert report.scan.truncated is True
        assert "read limit" in report.scan.note
        assert "newest rows only" in report.scan.note

    def test_a_complete_read_is_not_truncated(self, graph_store) -> None:
        graph_store.upsert_node("n0", "Person", {"name": "p"})

        report = _analyze(graph_store)

        assert report.status == "ok"
        assert report.scan.note == ""


class TestTypeBucketing:
    """Raw spellings collapse through the shipped alias map, not a local one."""

    def test_legacy_lowercase_types_bucket_onto_their_canonical(
        self, graph_store
    ) -> None:
        graph_store.upsert_node("a", "system", {"name": "alpha"})
        graph_store.upsert_node("b", "SoftwareApplication", {"name": "beta"})
        graph_store.upsert_node("c", "person", {"name": "gamma"})

        report = _analyze(graph_store)
        buckets = {bucket.canonical: bucket for bucket in report.type_buckets}

        assert buckets["SoftwareApplication"].total == 2
        assert buckets["SoftwareApplication"].raw_types == {
            "system": 1,
            "SoftwareApplication": 1,
        }
        assert buckets["Person"].total == 1

    def test_shares_are_reported_against_the_node_population(self, graph_store) -> None:
        graph_store.upsert_node("a", "Person", {"name": "a"})
        graph_store.upsert_node("b", "Person", {"name": "b"})
        graph_store.upsert_node("c", "Project", {"name": "c"})

        report = _analyze(graph_store)
        buckets = {bucket.canonical: bucket for bucket in report.type_buckets}

        assert buckets["Person"].share == pytest.approx(2 / 3)
        assert buckets["Project"].share == pytest.approx(1 / 3)


class TestUncoveredSplits:
    """The K3 gate: which case splits the alias map does *not* resolve."""

    def test_a_pascalcase_spelling_missing_from_the_alias_map_is_a_split(
        self, graph_store
    ) -> None:
        # 'system' -> SoftwareApplication; 'System' is not a key, so it stays.
        graph_store.upsert_node("a", "system", {"name": "alpha"})
        graph_store.upsert_node("b", "System", {"name": "beta"})

        report = _analyze(graph_store)

        assert len(report.uncovered_splits) == 1
        split = report.uncovered_splits[0]
        assert split.lowercase_key == "SoftwareApplication"
        assert split.buckets == {"SoftwareApplication": 1, "System": 1}
        assert split.nodes == 2
        assert split.share == pytest.approx(1.0)

    def test_a_casing_pair_the_alias_map_already_merges_is_not_a_split(
        self, graph_store
    ) -> None:
        # 'concept' -> Concept, and 'Concept' is already the canonical form,
        # so both land in one bucket and there is nothing to report.
        graph_store.upsert_node("a", "concept", {"name": "alpha"})
        graph_store.upsert_node("b", "Concept", {"name": "beta"})

        report = _analyze(graph_store)

        assert report.uncovered_splits == []
        buckets = {bucket.canonical: bucket for bucket in report.type_buckets}
        assert buckets["Concept"].total == 2

    def test_a_pair_in_no_alias_map_at_all_is_still_caught(self, graph_store) -> None:
        """The property is derived, so an unknown vocabulary is covered too.

        Neither spelling appears in ``ENTITY_TYPE_ALIASES``. A roster of
        known-bad pairs would miss this; asking whether a case-insensitive
        group canonicalizes to more than one bucket does not.
        """
        graph_store.upsert_node("a", "gotcha", {"name": "alpha"})
        graph_store.upsert_node("b", "Gotcha", {"name": "beta"})

        report = _analyze(graph_store)

        assert len(report.uncovered_splits) == 1
        assert report.uncovered_splits[0].buckets == {"gotcha": 1, "Gotcha": 1}

    def test_one_spelling_alone_is_never_a_split(self, graph_store) -> None:
        graph_store.upsert_node("a", "System", {"name": "alpha"})
        graph_store.upsert_node("b", "System", {"name": "beta"})

        report = _analyze(graph_store)

        assert report.uncovered_splits == []


class TestNameCollisions:
    """Counted on both keys, because the difference is the answer."""

    def test_a_genuine_type_disagreement_is_a_canonical_collision(
        self, graph_store
    ) -> None:
        graph_store.upsert_node("a", "Project", {"name": "hermes"})
        graph_store.upsert_node("b", "System", {"name": "hermes"})

        report = _analyze(graph_store)

        assert report.collisions.canonical == 1
        assert report.collisions.raw_type == 1
        assert report.collisions.explained_by_alias_map == 0
        group = report.collisions.groups[0]
        assert group.name == "hermes"
        assert group.canonical_types == {"Project": 1, "System": 1}
        assert sorted(group.node_ids) == ["a", "b"]

    def test_a_collision_the_alias_map_resolves_is_counted_but_not_listed(
        self, graph_store
    ) -> None:
        """This is the number #594 inferred and got wrong.

        Two raw types, one canonical bucket: the raw count sees a collision
        and the canonical count does not. Reporting only one of the two would
        leave the reader to guess the other.
        """
        graph_store.upsert_node("a", "system", {"name": "hermes"})
        graph_store.upsert_node("b", "SoftwareApplication", {"name": "hermes"})

        report = _analyze(graph_store)

        assert report.collisions.raw_type == 1
        assert report.collisions.canonical == 0
        assert report.collisions.explained_by_alias_map == 1
        assert report.collisions.groups == []
        assert report.collisions.same_type_duplicates == 1

    def test_names_group_case_insensitively(self, graph_store) -> None:
        graph_store.upsert_node("a", "Project", {"name": "Hermes"})
        graph_store.upsert_node("b", "System", {"name": "hermes"})

        report = _analyze(graph_store)

        assert report.collisions.canonical == 1
        assert report.collisions.distinct_names == 1

    def test_unnamed_nodes_do_not_collide_with_each_other(self, graph_store) -> None:
        graph_store.upsert_node("a", "Project", {})
        graph_store.upsert_node("b", "System", {"name": "   "})

        report = _analyze(graph_store)

        assert report.collisions.named_nodes == 0
        assert report.collisions.canonical == 0


class TestDegree:
    """Both endpoints earn a degree; missing endpoints are reported."""

    def test_both_endpoints_of_an_edge_earn_degree(self, graph_store) -> None:
        graph_store.upsert_node("a", "Person", {"name": "a"})
        graph_store.upsert_node("b", "Person", {"name": "b"})
        graph_store.upsert_edge("a", "b", "wasAssociatedWith")

        report = _analyze(graph_store)

        assert report.degree.degree_min == 1
        assert report.degree.degree_max == 1
        assert report.degree.mean == pytest.approx(1.0)
        assert report.degree.histogram["1"] == 2

    def test_a_hub_is_named_with_its_type(self, graph_store) -> None:
        """#594 read a hub as a defect. The name and type are what refute that."""
        graph_store.upsert_node("agent", "Agent", {"name": "trellis_meta_cli"})
        for index in range(4):
            graph_store.upsert_node(f"act{index}", "Activity", {"name": f"a{index}"})
            graph_store.upsert_edge("agent", f"act{index}", "wasAssociatedWith")

        report = _analyze(graph_store)

        top = report.degree.top_hubs[0]
        assert top.node_id == "agent"
        assert top.degree == 4
        assert top.name == "trellis_meta_cli"
        assert top.canonical_type == "Agent"

    def test_isolated_nodes_never_appear_as_hubs(self, graph_store) -> None:
        graph_store.upsert_node("a", "Person", {"name": "a"})

        report = _analyze(graph_store)

        assert report.degree.top_hubs == []
        assert report.degree.histogram["0"] == 1

    def test_percentiles_span_a_skewed_distribution(self, graph_store) -> None:
        graph_store.upsert_node("hub", "Agent", {"name": "hub"})
        for index in range(9):
            graph_store.upsert_node(f"n{index}", "Activity", {"name": f"n{index}"})
            graph_store.upsert_edge("hub", f"n{index}", "wasAssociatedWith")

        report = _analyze(graph_store)

        assert report.degree.degree_max == 9
        assert report.degree.degree_p50 == 1
        assert report.degree.histogram["8-15"] == 1


class TestIsolation:
    """The base rate #594 was built on."""

    def test_isolated_share_is_reported_overall_and_per_type(self, graph_store) -> None:
        graph_store.upsert_node("a", "Person", {"name": "a"})
        graph_store.upsert_node("b", "Person", {"name": "b"})
        graph_store.upsert_node("g1", "gotcha", {"name": "g1"})
        graph_store.upsert_node("g2", "gotcha", {"name": "g2"})
        graph_store.upsert_edge("a", "b", "wasAssociatedWith")

        report = _analyze(graph_store)

        assert report.isolation.isolated == 2
        assert report.isolation.share == pytest.approx(0.5)
        by_type = {row.canonical: row for row in report.isolation.by_type}
        assert by_type["gotcha"].isolated == 2
        assert by_type["gotcha"].share == pytest.approx(1.0)
        assert by_type["Person"].isolated == 0

    def test_isolation_buckets_through_the_alias_map(self, graph_store) -> None:
        graph_store.upsert_node("a", "system", {"name": "a"})
        graph_store.upsert_node("b", "SoftwareApplication", {"name": "b"})

        report = _analyze(graph_store)
        by_type = {row.canonical: row for row in report.isolation.by_type}

        assert by_type["SoftwareApplication"].total == 2
        assert by_type["SoftwareApplication"].isolated == 2


class TestReferents:
    """Two measures, reported apart and unioned — never summed."""

    def test_a_well_known_dataset_property_counts(self, graph_store) -> None:
        graph_store.upsert_node(
            "a", "Dataset", {"name": "orders", "source_system": "postgres"}
        )
        graph_store.upsert_node("b", "Person", {"name": "b"})

        report = _analyze(graph_store)

        assert report.referents.well_known == 1
        assert report.referents.locator_shape == 0
        assert report.referents.any_referent == 1
        assert report.referents.by_property == {"source_system": 1}

    @pytest.mark.parametrize(
        "value",
        [
            "https://github.com/ronsse/trellis-ai",
            "s3://bucket/key",
            "postgres://host/db",
            "file:///var/lib/thing",
            "/home/nronsse/projects/trellis-ai",
            "~/projects/trellis-ai",
        ],
    )
    def test_a_locator_shaped_value_counts_whatever_key_it_sits_under(
        self, graph_store, value: str
    ) -> None:
        graph_store.upsert_node("a", "File", {"name": "thing", "wherever": value})

        report = _analyze(graph_store)

        assert report.referents.locator_shape == 1
        assert report.referents.by_property == {"wherever": 1}

    @pytest.mark.parametrize(
        "value",
        ["just a sentence", "not/a/path", "", "1.2.3", "a:b"],
    )
    def test_an_ordinary_value_is_not_a_locator(self, graph_store, value: str) -> None:
        graph_store.upsert_node("a", "Person", {"name": "thing", "field": value})

        report = _analyze(graph_store)

        assert report.referents.locator_shape == 0

    def test_a_node_matching_both_measures_is_counted_once_in_the_union(
        self, graph_store
    ) -> None:
        graph_store.upsert_node(
            "a",
            "Dataset",
            {
                "name": "orders",
                "source_system": "postgres",
                "physical_uri": "postgres://host/db",
            },
        )

        report = _analyze(graph_store)

        assert report.referents.well_known == 1
        assert report.referents.any_referent == 1
        # The two measures overlap, so their sum overstates coverage. The
        # union is the number an actuator's base rate needs.
        assert report.referents.any_referent < (
            report.referents.well_known + report.referents.locator_shape + 1
        )

    def test_the_alias_table_is_reported_as_unread(self, graph_store) -> None:
        graph_store.upsert_node("a", "Person", {"name": "a"})

        report = _analyze(graph_store)

        assert report.referents.alias_table_read is False


class TestDocumentLinkage:
    """The graph-to-memory join, including the claim #594 made about it."""

    def test_linked_share_and_max_per_node(self, graph_store) -> None:
        graph_store.upsert_node("a", "Person", {"name": "a"}, document_ids=["doc1"])
        graph_store.upsert_node(
            "b", "Person", {"name": "b"}, document_ids=["doc1", "doc2"]
        )
        graph_store.upsert_node("c", "Person", {"name": "c"})

        report = _analyze(graph_store)

        assert report.documents.linked == 2
        assert report.documents.share == pytest.approx(2 / 3)
        assert report.documents.total_links == 3
        # Directly re-derives #594's "none carries more than one", instead of
        # leaving it to be quoted from prose.
        assert report.documents.max_per_node == 2
        assert report.documents.histogram == {"0": 1, "1": 1, "2": 1, "3+": 0}

    def test_linked_nodes_are_attributed_to_their_canonical_type(
        self, graph_store
    ) -> None:
        graph_store.upsert_node("a", "system", {"name": "a"}, document_ids=["doc1"])

        report = _analyze(graph_store)

        assert report.documents.by_type == {"SoftwareApplication": 1}


class TestReadOnly:
    """Running the sensor must not change what it measures."""

    def test_the_pass_writes_nothing(self, graph_store) -> None:
        graph_store.upsert_node("a", "Person", {"name": "a"})
        graph_store.upsert_node("b", "Person", {"name": "b"})
        graph_store.upsert_edge("a", "b", "wasAssociatedWith")
        before_nodes = graph_store.count_nodes()
        before_edges = graph_store.count_edges()
        history_before = graph_store.get_node_history("a")

        _analyze(graph_store)

        assert graph_store.count_nodes() == before_nodes
        assert graph_store.count_edges() == before_edges
        # A write would land as a new SCD-2 version rather than a count change.
        assert len(graph_store.get_node_history("a")) == len(history_before)
