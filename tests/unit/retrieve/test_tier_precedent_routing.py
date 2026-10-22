"""A graph precedent is routed to ``domain_knowledge`` by node type.

``TierMapper``'s default ``domain_knowledge`` rule names ``precedent`` (and
``owner`` / ``team``) under ``item_types``. ``GraphSearch`` never emits those
as a ``PackItem.item_type``: every graph node rides as ``item_type="entity"``
and carries its node type in ``metadata["node_type"]``. Matching on
``item_type`` alone therefore left the rule unreachable for graph candidates,
and a promoted precedent was absent from every ``domain_knowledge`` section
(``get_objective_context`` among them) while present in the flat pack.

The end-to-end class runs a real :class:`SQLiteGraphStore` through
:class:`GraphSearch` and :meth:`PackBuilder.build_sectioned`, so it fails on
the actual item shape rather than a hand-built one.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from trellis.retrieve.pack_builder import PackBuilder
from trellis.retrieve.strategies import GraphSearch
from trellis.retrieve.tier_mapping import TierMapper
from trellis.schemas.pack import PackItem, SectionRequest
from trellis.stores.sqlite.graph import SQLiteGraphStore

_DOMAIN = SectionRequest(
    name="domain",
    retrieval_affinities=["domain_knowledge"],
    max_items=10,
    max_tokens=20000,
)
_REFERENCE = SectionRequest(
    name="reference",
    retrieval_affinities=["reference"],
    max_items=10,
    max_tokens=20000,
)
_BODY = "always drain the write queue before the deploy checklist runs"


def _graph_item(
    item_id: str,
    node_type: str,
    *,
    canonical: str | None = None,
    **metadata: object,
) -> PackItem:
    """An item shaped the way ``GraphSearch`` emits one."""
    return PackItem(
        item_id=item_id,
        item_type="entity",
        excerpt=_BODY,
        relevance_score=0.5,
        metadata={
            "source_strategy": "graph",
            "node_type": node_type,
            "node_type_canonical": canonical if canonical is not None else node_type,
            "node_role": "semantic",
            **metadata,
        },
    )


class TestInferredAffinityFromNodeType:
    def test_a_graph_precedent_matches_domain_knowledge(self) -> None:
        item = _graph_item("p1", "precedent")
        assert TierMapper().matches_section(item, _DOMAIN) is True

    def test_a_graph_precedent_keeps_its_reference_routing(self) -> None:
        """It is still an ``entity``; the new match adds a bucket, drops none."""
        item = _graph_item("p1", "precedent")
        assert TierMapper().matches_section(item, _REFERENCE) is True

    @pytest.mark.parametrize("node_type", ["team", "Team"])
    def test_legacy_and_canonical_spellings_route_alike(self, node_type: str) -> None:
        """``team`` and ``Team`` are one bucket: routing must not hang on which
        spelling the row was written with."""
        item = _graph_item("t1", node_type, canonical="Team")
        assert TierMapper().matches_section(item, _DOMAIN) is True

    @pytest.mark.parametrize("key", ["node_type", "node_type_canonical"])
    def test_either_stamped_key_alone_is_enough(self, key: str) -> None:
        """``GraphSearch`` writes both, but a caller-built item may carry one."""
        item = PackItem(
            item_id="p1",
            item_type="entity",
            excerpt=_BODY,
            metadata={key: "precedent"},
        )
        assert TierMapper().matches_section(item, _DOMAIN) is True

    def test_a_plain_entity_is_not_routed_to_domain_knowledge(self) -> None:
        item = _graph_item("c1", "concept")
        assert TierMapper().matches_section(item, _DOMAIN) is False
        assert TierMapper().matches_section(item, _REFERENCE) is True

    def test_a_node_type_only_appears_in_the_rule_for_its_own_affinity(
        self,
    ) -> None:
        """A precedent does not become operational or technical-pattern."""
        item = _graph_item("p1", "precedent")
        for name in ("operational", "technical_pattern"):
            section = SectionRequest(name=name, retrieval_affinities=[name])
            assert TierMapper().matches_section(item, section) is False

    def test_explicit_affinity_still_wins_over_node_type(self) -> None:
        item = _graph_item(
            "p1",
            "precedent",
            content_tags={"retrieval_affinity": ["technical_pattern"]},
        )
        assert TierMapper().matches_section(item, _DOMAIN) is False

    def test_a_non_string_node_type_is_ignored_not_raised(self) -> None:
        item = _graph_item("x", "precedent")
        item.metadata["node_type"] = ["precedent"]
        item.metadata["node_type_canonical"] = None
        assert TierMapper().matches_section(item, _DOMAIN) is False

    def test_a_custom_rule_set_matches_its_own_node_types(self) -> None:
        mapper = TierMapper(
            {
                "domain_knowledge": {
                    "content_types": set(),
                    "scopes": set(),
                    "item_types": {"runbook"},
                    "require_scope": False,
                }
            }
        )
        assert mapper.matches_section(_graph_item("r", "runbook"), _DOMAIN) is True
        assert mapper.matches_section(_graph_item("p", "precedent"), _DOMAIN) is False


@pytest.fixture
def graph(tmp_path: Path) -> Iterator[SQLiteGraphStore]:
    store = SQLiteGraphStore(tmp_path / "graph.db")
    yield store
    store.close()


class TestSectionedPackEndToEnd:
    def _seed(self, graph: SQLiteGraphStore) -> None:
        graph.upsert_node(
            "prec-1",
            "precedent",
            {"name": "queue drain", "description": _BODY},
        )
        graph.upsert_node(
            "concept-1",
            "concept",
            {"name": "queue", "description": _BODY + " and flush the cache"},
        )

    def test_a_graph_precedent_lands_in_the_domain_knowledge_section(
        self, graph: SQLiteGraphStore
    ) -> None:
        self._seed(graph)
        builder = PackBuilder(strategies=[GraphSearch(graph)])

        pack = builder.build_sectioned("deploy", sections=[_DOMAIN])

        served = {i.item_id for s in pack.sections for i in s.items}
        assert "prec-1" in served
        assert "concept-1" not in served

    def test_the_flat_pack_and_the_sectioned_pack_agree_on_the_precedent(
        self, graph: SQLiteGraphStore
    ) -> None:
        self._seed(graph)
        builder = PackBuilder(strategies=[GraphSearch(graph)])

        flat = {i.item_id for i in builder.build("deploy").items}
        sectioned = {
            i.item_id
            for s in builder.build_sectioned("deploy", sections=[_DOMAIN]).sections
            for i in s.items
        }

        assert "prec-1" in flat
        assert "prec-1" in sectioned

    def test_with_both_sections_requested_the_precedent_is_served_once(
        self, graph: SQLiteGraphStore
    ) -> None:
        """Cross-section dedup keeps one copy; the plain entity stays in reference."""
        self._seed(graph)
        builder = PackBuilder(strategies=[GraphSearch(graph)])

        pack = builder.build_sectioned("deploy", sections=[_DOMAIN, _REFERENCE])

        by_section = {s.name: [i.item_id for i in s.items] for s in pack.sections}
        all_ids = [i for ids in by_section.values() for i in ids]
        assert all_ids.count("prec-1") == 1
        assert "prec-1" in by_section["domain"]
        assert by_section["reference"] == ["concept-1"]
