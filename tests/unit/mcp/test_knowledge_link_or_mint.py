"""``save_knowledge`` with ``TRELLIS_KNOWLEDGE_LINK_OR_MINT`` on.

The knob-off behaviour is pinned by the differential test next door; this
file covers what the knob adds: a missing ``domain:<slug>`` node is minted
through the executor, a note whose name already resolves is attached to
instead of duplicated, ``relates_to`` resolves raw ids through external
aliases, and an unresolvable reference is reported by count and position.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from trellis.core.write_config import KNOWLEDGE_LINK_OR_MINT_ENV
from trellis.extract.entity_resolution import NAME_ALIAS_SOURCE_SYSTEM
from trellis.mcp.server import save_knowledge
from trellis.mutate import build_curate_executor
from trellis.mutate.commands import Command, Operation
from trellis.mutate.policy_source import POLICY_FILENAME
from trellis.retrieve.strategies import NamespaceSeedExtractor
from trellis.schemas.enums import Enforcement, PolicyType
from trellis.schemas.policy import Policy, PolicyRule, PolicyScope
from trellis.schemas.well_known import CONCEPT
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry


@pytest.fixture
def knob_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(KNOWLEDGE_LINK_OR_MINT_ENV, "1")


def _graph(registry: StoreRegistry) -> Any:
    return registry.knowledge.graph_store


def _id_of(reply: str) -> str:
    return reply.split("\n", 1)[0].split(": ", 1)[1].split(" ", 1)[0]


def _domain_nodes(registry: StoreRegistry) -> list[dict[str, Any]]:
    return [
        n
        for n in _graph(registry).query(limit=1000)
        if str(n["node_id"]).startswith("domain:")
    ]


def _event_counts(registry: StoreRegistry) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in registry.operational.event_log.get_events(limit=1000):
        counts[event.event_type.value] = counts.get(event.event_type.value, 0) + 1
    return counts


class TestDomainMint:
    def test_missing_domain_is_minted_and_linked(
        self, knob_on: None, temp_registry: StoreRegistry
    ) -> None:
        reply = save_knowledge(name="Close checklist", properties={"domain": "Finance"})
        note_id = _id_of(reply)
        assert "Domain minted: domain:finance" in reply
        assert "Domain link: " in reply
        node = _graph(temp_registry).get_node("domain:finance")
        assert node is not None
        assert node["node_type"] == CONCEPT
        assert node["properties"]["name"] == "Finance"
        assert node["properties"]["minted_by"] == "mcp:save_knowledge"
        edges = _graph(temp_registry).get_edges(note_id, direction="outgoing")
        assert [(e["target_id"], e["edge_type"]) for e in edges] == [
            ("domain:finance", "appliesTo")
        ]

    def test_minted_once_and_reused(
        self, knob_on: None, temp_registry: StoreRegistry
    ) -> None:
        first = save_knowledge(name="One", properties={"domain": "Finance"})
        second = save_knowledge(name="Two", properties={"domain": "finance"})
        assert "Domain minted" in first
        assert "Domain minted" not in second
        assert "Domain link:" in second
        assert [n["node_id"] for n in _domain_nodes(temp_registry)] == [
            "domain:finance"
        ]
        created = [
            e
            for e in temp_registry.operational.event_log.get_events(limit=1000)
            if e.event_type == EventType.ENTITY_CREATED
            and e.entity_id == "domain:finance"
        ]
        assert len(created) == 1

    def test_two_spellings_in_one_call_mint_once(
        self, knob_on: None, temp_registry: StoreRegistry
    ) -> None:
        reply = save_knowledge(
            name="Both", properties={"domain": ["Data Science", "data-science"]}
        )
        assert reply.count("Domain minted") == 1
        assert [n["node_id"] for n in _domain_nodes(temp_registry)] == [
            "domain:data-science"
        ]
        assert "Unresolved" not in reply

    @pytest.mark.parametrize(
        ("raw", "slug"),
        [
            ("Finance", "finance"),
            ("  Data   Science ", "data-science"),
            ("R&D / Ops!", "r-d-ops"),
            ("Café Ünïcode", "café-ünïcode"),
            ("\uff26\uff29\uff2e\uff21\uff2e\uff23\uff25", "finance"),
        ],
    )
    def test_slug_normalisation(
        self, knob_on: None, temp_registry: StoreRegistry, raw: str, slug: str
    ) -> None:
        save_knowledge(name="Note", properties={"domain": raw})
        assert [n["node_id"] for n in _domain_nodes(temp_registry)] == [
            f"domain:{slug}"
        ]

    def test_a_domain_with_no_slug_is_not_minted(
        self, knob_on: None, temp_registry: StoreRegistry
    ) -> None:
        reply = save_knowledge(name="Note", properties={"domain": "!!!"})
        assert _domain_nodes(temp_registry) == []
        assert "Unresolved: 1 of 1 references (positions 1)" in reply
        assert "!!!" not in reply

    def test_an_existing_trace_minted_domain_is_reused_not_reminted(
        self, knob_on: None, temp_registry: StoreRegistry
    ) -> None:
        _graph(temp_registry).upsert_node(
            "domain:trellis", CONCEPT, {"name": "trellis"}
        )
        reply = save_knowledge(name="Note", properties={"domain": "Trellis"})
        assert "Domain minted" not in reply
        assert "Domain link:" in reply

    def test_knob_off_mints_nothing(
        self, monkeypatch: pytest.MonkeyPatch, temp_registry: StoreRegistry
    ) -> None:
        monkeypatch.delenv(KNOWLEDGE_LINK_OR_MINT_ENV, raising=False)
        reply = save_knowledge(name="Note", properties={"domain": "Finance"})
        assert _domain_nodes(temp_registry) == []
        assert "Domain minted" not in reply
        assert "has no domain node" in reply


class TestPolicyGate:
    def _deny_concept_creates(self, registry: StoreRegistry) -> None:
        policy = Policy(
            policy_type=PolicyType.MUTATION,
            scope=PolicyScope(level="entity_type", value=CONCEPT),
            rules=[PolicyRule(operation="entity.create", action="deny")],
            enforcement=Enforcement.ENFORCE,
        )
        stores_dir = registry.stores_dir
        assert stores_dir is not None
        (stores_dir / POLICY_FILENAME).write_text(
            json.dumps({"policies": [policy.model_dump(mode="json")]}),
            encoding="utf-8",
        )

    def test_a_deny_policy_blocks_the_mint(
        self, knob_on: None, temp_registry: StoreRegistry
    ) -> None:
        self._deny_concept_creates(temp_registry)
        reply = save_knowledge(
            name="Note", entity_type="person", properties={"domain": "Finance"}
        )
        assert reply.startswith("Entity created:")
        assert _graph(temp_registry).get_node("domain:finance") is None
        assert "Warning: domain node not minted" in reply
        assert "Unresolved: 1 of 1 references (positions 1)" in reply
        assert "Domain link" not in reply

    def test_the_mint_goes_through_the_executor_and_is_audited(
        self, knob_on: None, temp_registry: StoreRegistry
    ) -> None:
        save_knowledge(name="Note", properties={"domain": "Finance"})
        events = temp_registry.operational.event_log.get_events(limit=1000)
        minted = [
            e
            for e in events
            if e.event_type == EventType.ENTITY_CREATED
            and e.entity_id == "domain:finance"
        ]
        assert [e.source for e in minted] == ["mutation_executor"]


class TestAttach:
    def test_same_name_same_type_attaches_instead_of_duplicating(
        self, knob_on: None, temp_registry: StoreRegistry
    ) -> None:
        first = save_knowledge(
            name="Alpha gotcha", properties={"owner": "a"}, content="First body."
        )
        first_id = _id_of(first)
        graph = _graph(temp_registry)
        aliases_before = graph.get_aliases(first_id)
        before = _event_counts(temp_registry)

        second = save_knowledge(
            name=" alpha  GOTCHA ",
            properties={"owner": "b", "extra": "x"},
            content="Second body.",
        )

        assert second.startswith(f"Entity exists: {first_id} ")
        assert "attached, not created" in second
        notes = [
            n
            for n in graph.query(limit=1000)
            if not str(n["node_id"]).startswith("domain:")
        ]
        assert [n["node_id"] for n in notes] == [first_id]
        node = graph.get_node(first_id)
        assert node["properties"]["owner"] == "a"  # never overwritten
        assert node["properties"]["extra"] == "x"  # only what was missing
        assert len(node["document_ids"]) == 2  # both evidence documents
        assert graph.get_aliases(first_id) == aliases_before  # no new alias
        after = _event_counts(temp_registry)
        assert after.get("entity.created", 0) == before.get("entity.created", 0)
        assert after.get("entity.updated", 0) == before.get("entity.updated", 0) + 1

    def test_a_second_save_with_nothing_new_writes_nothing(
        self, knob_on: None, temp_registry: StoreRegistry
    ) -> None:
        save_knowledge(name="Alpha", properties={"owner": "a"})
        before = _event_counts(temp_registry)
        reply = save_knowledge(name="Alpha", properties={"owner": "a"})
        assert "attached, not created" in reply
        assert _event_counts(temp_registry) == before

    def test_domain_links_still_run_for_the_attached_note(
        self, knob_on: None, temp_registry: StoreRegistry
    ) -> None:
        first_id = _id_of(save_knowledge(name="Alpha"))
        reply = save_knowledge(name="Alpha", properties={"domain": "Finance"})
        assert "Domain minted: domain:finance" in reply
        edges = _graph(temp_registry).get_edges(first_id, direction="outgoing")
        assert [e["target_id"] for e in edges] == ["domain:finance"]

    def test_a_different_type_is_a_new_node_and_the_alias_is_not_rebound(
        self, knob_on: None, temp_registry: StoreRegistry
    ) -> None:
        note_id = _id_of(save_knowledge(name="Shared"))
        person = save_knowledge(name="Shared", entity_type="person")
        assert person.startswith("Entity created:")
        row = _graph(temp_registry).resolve_alias(NAME_ALIAS_SOURCE_SYSTEM, "shared")
        assert row is not None
        assert row["entity_id"] == note_id

    def test_a_namespaced_node_is_never_an_attach_target(
        self, knob_on: None, temp_registry: StoreRegistry
    ) -> None:
        save_knowledge(name="Seed", properties={"domain": "Finance"})
        reply = save_knowledge(name="Finance", entity_type="concept")
        assert reply.startswith("Entity created:")

    def test_supersedes_never_attaches(
        self, knob_on: None, temp_registry: StoreRegistry
    ) -> None:
        old_id = _id_of(save_knowledge(name="Policy", content="Old."))
        reply = save_knowledge(name="Policy", content="New.", supersedes=old_id)
        assert reply.startswith("Entity created:")

    def test_knob_off_creates_a_duplicate_as_before(
        self, monkeypatch: pytest.MonkeyPatch, temp_registry: StoreRegistry
    ) -> None:
        monkeypatch.delenv(KNOWLEDGE_LINK_OR_MINT_ENV, raising=False)
        save_knowledge(name="Alpha")
        again = save_knowledge(name="Alpha")
        assert again.startswith("Entity created:")


class TestRelatesTo:
    def _seed_dbt_node(self, registry: StoreRegistry) -> None:
        graph = _graph(registry)
        graph.upsert_node("01SEEDNOTE", CONCEPT, {"name": "Seed note"})
        graph.bind_alias_if_absent("01SEEDNOTE", "dbt", "model.shop.orders")

    def test_raw_id_resolves_through_an_external_alias(
        self, knob_on: None, temp_registry: StoreRegistry
    ) -> None:
        self._seed_dbt_node(temp_registry)
        reply = save_knowledge(name="Eps", relates_to="model.shop.orders")
        assert "Resolved relates_to 'model.shop.orders' -> 01SEEDNOTE" in reply
        assert "via alias:dbt" in reply
        assert "Unresolved" not in reply
        edges = _graph(temp_registry).get_edges(_id_of(reply), direction="outgoing")
        assert [e["target_id"] for e in edges] == ["01SEEDNOTE"]

    def test_a_system_prefixed_raw_id_reaches_any_system(
        self, knob_on: None, temp_registry: StoreRegistry
    ) -> None:
        graph = _graph(temp_registry)
        graph.upsert_node("t:orders", "table", {"name": "orders"})
        graph.bind_alias_if_absent("t:orders", "unity_catalog", "main.sales.orders")
        reply = save_knowledge(name="Eps", relates_to="unity_catalog:main.sales.orders")
        assert "-> t:orders" in reply
        assert "via alias:unity_catalog" in reply

    def test_knob_off_still_cannot_resolve_it(
        self, monkeypatch: pytest.MonkeyPatch, temp_registry: StoreRegistry
    ) -> None:
        monkeypatch.delenv(KNOWLEDGE_LINK_OR_MINT_ENV, raising=False)
        self._seed_dbt_node(temp_registry)
        reply = save_knowledge(name="Eps", relates_to="model.shop.orders")
        assert "target entity not found" in reply

    def test_a_raw_id_claimed_by_two_nodes_is_ambiguous_and_links_nothing(
        self, knob_on: None, temp_registry: StoreRegistry
    ) -> None:
        graph = _graph(temp_registry)
        graph.upsert_node("a:1", CONCEPT, {"name": "one"})
        graph.upsert_node("a:2", CONCEPT, {"name": "two"})
        graph.bind_alias_if_absent("a:1", "dbt", "model.dup")
        graph.bind_alias_if_absent("a:2", "openlineage_dataset", "model.dup")
        reply = save_knowledge(name="Eps", relates_to="model.dup")
        assert "names 2 nodes" in reply
        assert "Unresolved: 1 of 1 references (positions 1)" in reply
        assert graph.get_edges(_id_of(reply), direction="outgoing") == []

    def test_unresolvable_values_are_reported_by_count_and_position_only(
        self, knob_on: None, temp_registry: StoreRegistry
    ) -> None:
        reply = save_knowledge(
            name="Eps",
            relates_to="secret-token-xyz",
            properties={"domain": ["!!!", "Finance"]},
        )
        assert "Unresolved: 2 of 3 references (positions 1, 2) — not linked" in reply
        assert "secret-token-xyz" not in reply
        assert "!!!" not in reply
        assert "Domain minted: domain:finance" in reply

    def test_nothing_unresolved_adds_no_line(
        self, knob_on: None, temp_registry: StoreRegistry
    ) -> None:
        reply = save_knowledge(name="Plain")
        assert reply.count("\n") == 0
        assert "Unresolved" not in reply


class TestAliasNeverRebinds:
    def test_a_bound_raw_id_stays_with_its_first_node(
        self, knob_on: None, temp_registry: StoreRegistry
    ) -> None:
        graph = _graph(temp_registry)
        graph.upsert_node("a:1", CONCEPT, {"name": "one"})
        graph.upsert_node("a:2", CONCEPT, {"name": "two"})
        executor = build_curate_executor(temp_registry, clearance="restricted")
        for entity_id in ("a:1", "a:2"):
            executor.execute(
                Command(
                    operation=Operation.ALIAS_UPSERT,
                    args={
                        "entity_id": entity_id,
                        "source_system": "dbt",
                        "raw_id": "model.x",
                        "if_absent": True,
                    },
                    requested_by="test",
                )
            )
        row = graph.resolve_alias("dbt", "model.x")
        assert row is not None
        assert row["entity_id"] == "a:1"


class TestSeeds:
    INTENT = "finance reporting"

    def _save(self) -> None:
        save_knowledge(
            name="Quarterly close checklist", properties={"domain": "finance"}
        )

    def test_a_minted_domain_seeds_the_graph_axis_with_the_knob_on(
        self, knob_on: None, temp_registry: StoreRegistry
    ) -> None:
        self._save()
        seeds = NamespaceSeedExtractor(_graph(temp_registry)).extract(self.INTENT)
        assert "domain:finance" in seeds

    def test_the_same_intent_seeds_nothing_with_the_knob_off(
        self, monkeypatch: pytest.MonkeyPatch, temp_registry: StoreRegistry
    ) -> None:
        monkeypatch.delenv(KNOWLEDGE_LINK_OR_MINT_ENV, raising=False)
        self._save()
        seeds = NamespaceSeedExtractor(_graph(temp_registry)).extract(self.INTENT)
        assert seeds == []


def test_tmp_registry_has_a_stores_dir(temp_registry: StoreRegistry) -> None:
    assert isinstance(temp_registry.stores_dir, Path)
