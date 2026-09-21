"""The extractor that gives the graph axis a query-relevant seed (#375).

`GraphSearch`'s unseeded branch is ``ORDER BY created_at DESC LIMIT n``, so
until something resolves intent words to node ids the axis cannot consult
what was asked. #375 refuses the obvious mechanism in as many words —
*"scan the whole node table per pack and match names client-side… O(N) per
pack, buys time, fixes nothing"* — so the property under test here is not
only that seeds come back, but **how**: indexed reads, one batch, bounded.

The refusal is pinned first and deliberately: a later change that quietly
reintroduces the scan would still satisfy every yield test in this file.
"""

from __future__ import annotations

from typing import Any

import pytest

from trellis.extract.entity_resolution import NAME_ALIAS_SOURCE_SYSTEM
from trellis.retrieve.strategies import (
    DEFAULT_SEED_NAMESPACES,
    EXTRACTED_SEED_DEPTH,
    GRAPH_SELECTION_SEEDED,
    GraphSearch,
    GraphSeedExtractor,
    NamespaceSeedExtractor,
)


class _SeedStore:
    """Graph store stand-in that records every call it was asked to make.

    Holds a set of node ids it considers live plus an alias table, and
    answers ``get_subgraph(ids, depth=0)`` the way the contract suite
    requires — exactly the seed nodes that exist, nothing else.
    """

    def __init__(
        self,
        live: dict[str, dict[str, Any]] | None = None,
        aliases: dict[str, str] | None = None,
        *,
        alias_raises: bool = False,
        subgraph_raises: bool = False,
    ) -> None:
        self.live = live or {}
        self.aliases = aliases or {}
        self.alias_raises = alias_raises
        self.subgraph_raises = subgraph_raises
        self.alias_calls: list[tuple[str, str]] = []
        self.subgraph_calls: list[dict[str, Any]] = []
        self.scan_calls: list[Any] = []

    # --- the two reads the extractor is allowed to make ---

    def resolve_alias(
        self, source_system: str, raw_id: str, as_of: Any = None
    ) -> dict[str, Any] | None:
        self.alias_calls.append((source_system, raw_id))
        if self.alias_raises:
            msg = "alias index unavailable"
            raise RuntimeError(msg)
        entity_id = self.aliases.get(raw_id)
        return {"entity_id": entity_id} if entity_id else None

    def get_subgraph(
        self,
        seed_ids: list[str],
        depth: int = 2,
        edge_types: list[str] | None = None,
    ) -> dict[str, Any]:
        self.subgraph_calls.append(
            {"seed_ids": list(seed_ids), "depth": depth, "edge_types": edge_types}
        )
        if self.subgraph_raises:
            msg = "graph unavailable"
            raise RuntimeError(msg)
        return {
            "nodes": [self.live[i] for i in seed_ids if i in self.live],
            "edges": [],
        }

    # --- the reads it must never make ---

    def query(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.scan_calls.append(kwargs)
        return []

    def execute_node_query(self, query: Any) -> list[dict[str, Any]]:
        self.scan_calls.append(query)
        return []


def _node(node_id: str, name: str) -> dict[str, Any]:
    return {
        "node_id": node_id,
        "node_type": "concept",
        "properties": {"name": name},
    }


class TestNoScan:
    """The mechanism #375 refuses, pinned as absent."""

    def test_resolution_never_scans_the_node_table(self) -> None:
        store = _SeedStore({"domain:fincore": _node("domain:fincore", "fincore")})
        assert NamespaceSeedExtractor(store).extract("fincore forecast") == [
            "domain:fincore"
        ]
        assert store.scan_calls == []

    def test_a_miss_never_falls_back_to_a_scan(self) -> None:
        """The expensive failure mode: degrade to the refused mechanism."""
        store = _SeedStore()
        assert NamespaceSeedExtractor(store).extract("nothing here resolves") == []
        assert store.scan_calls == []

    def test_confirmation_is_one_batched_read_whatever_the_intent_size(self) -> None:
        """The property that makes the cost independent of graph size.

        ``get_subgraph(ids, depth=0)`` returns exactly the live seeds and is
        pinned on every backend by
        ``GraphStoreContractTests.test_subgraph_seed_only_at_depth_zero``.
        Measured on the reference deployment it is a median 1.3 ms for a
        median 100 candidate ids, against 29.8 ms to fetch the same 100 one
        at a time and 24.4 ms for the refused whole-table scan — so a
        regression to per-id reads is a real cost, not a style question.
        """
        store = _SeedStore()
        NamespaceSeedExtractor(store).extract(" ".join(f"word{i}" for i in range(30)))
        assert len(store.subgraph_calls) == 1
        assert store.subgraph_calls[0]["depth"] == 0
        # 30 keys x 5 namespaces, in one call.
        assert len(store.subgraph_calls[0]["seed_ids"]) == 30 * len(
            DEFAULT_SEED_NAMESPACES
        )

    def test_no_read_at_all_when_the_intent_has_no_keys(self) -> None:
        store = _SeedStore()
        assert NamespaceSeedExtractor(store).extract("   ") == []
        assert store.subgraph_calls == []
        assert store.alias_calls == []
        assert store.scan_calls == []


class TestNamespaceResolution:
    def test_every_namespace_is_probed_for_every_key(self) -> None:
        store = _SeedStore()
        NamespaceSeedExtractor(store).extract("alpha beta")
        assert store.subgraph_calls[0]["seed_ids"] == [
            f"{ns}:{key}" for key in ("alpha", "beta") for ns in DEFAULT_SEED_NAMESPACES
        ]

    def test_only_ids_the_store_confirms_come_back(self) -> None:
        """Existence is checked, not assumed from the id scheme."""
        store = _SeedStore({"tool:verify": _node("tool:verify", "verify")})
        assert NamespaceSeedExtractor(store).extract("verify the plan") == [
            "tool:verify"
        ]

    def test_keys_are_normalized_the_way_the_alias_index_normalizes(self) -> None:
        """Case and whitespace only — the same three transforms, no more."""
        store = _SeedStore({"domain:fincore": _node("domain:fincore", "FinCore")})
        assert NamespaceSeedExtractor(store).extract("FinCore") == ["domain:fincore"]

    def test_a_namespace_hit_needs_no_name_match(self) -> None:
        """Trellis slugifies some ids, so a name check here would be wrong.

        ``merge_prs`` mints ``tool:merge-prs``; the node's name is the
        un-slugified form. The id itself is the evidence on this path — it
        was constructed from the key — so requiring the name to agree would
        reject a correct seed. Measured: 129 of 708 ``tool:`` nodes are
        name-reachable at all, and slug-matching them was separately
        measured at zero marginal yield, so this asymmetry is the whole of
        what makes the namespace path work.
        """
        store = _SeedStore({"tool:merge": _node("tool:merge", "merge_prs")})
        assert NamespaceSeedExtractor(store).extract("merge") == ["tool:merge"]

    def test_the_namespace_roster_is_configurable(self) -> None:
        store = _SeedStore({"custom:thing": _node("custom:thing", "thing")})
        extractor = NamespaceSeedExtractor(store, namespaces=("custom",))
        assert extractor.extract("thing") == ["custom:thing"]
        assert store.subgraph_calls[0]["seed_ids"] == ["custom:thing"]

    def test_an_empty_namespace_probes_the_bare_key(self) -> None:
        """For a deployment whose node ids are not prefixed at all."""
        store = _SeedStore({"thing": _node("thing", "thing")})
        extractor = NamespaceSeedExtractor(store, namespaces=("",))
        assert extractor.extract("thing") == ["thing"]

    def test_seeds_follow_the_order_the_entities_were_named(self) -> None:
        store = _SeedStore(
            {
                "domain:beta": _node("domain:beta", "beta"),
                "domain:alpha": _node("domain:alpha", "alpha"),
            }
        )
        assert NamespaceSeedExtractor(store).extract("beta then alpha") == [
            "domain:beta",
            "domain:alpha",
        ]

    def test_a_repeated_word_is_probed_once(self) -> None:
        store = _SeedStore()
        NamespaceSeedExtractor(store).extract("fincore fincore FINCORE")
        assert store.alias_calls == [(NAME_ALIAS_SOURCE_SYSTEM, "fincore")]


class TestAliasResolution:
    def test_an_alias_binding_resolves_an_id_the_scheme_cannot_reach(self) -> None:
        """The durable half: an id with no derivable prefix form.

        This is why the alias path is wired even though it resolves nothing
        on the reference deployment today — ``entity_aliases`` holds 0 rows
        there because #530 is merged but not yet deployed and the #369
        backfill has not run.
        """
        ulid = "01M0M2NYZC8DHVMFBC3YFJ5SRQ"
        store = _SeedStore({ulid: _node(ulid, "fincore")}, {"fincore": ulid})
        assert NamespaceSeedExtractor(store).extract("fincore") == [ulid]
        assert store.alias_calls == [(NAME_ALIAS_SOURCE_SYSTEM, "fincore")]

    def test_the_alias_binding_wins_over_the_namespace_guess(self) -> None:
        store = _SeedStore(
            {
                "e-1": _node("e-1", "fincore"),
                "domain:fincore": _node("domain:fincore", "fincore"),
            },
            {"fincore": "e-1"},
        )
        seeds = NamespaceSeedExtractor(store).extract("fincore")
        assert seeds == ["e-1", "domain:fincore"]

    def test_a_renamed_binding_is_dropped_rather_than_expanded(self) -> None:
        """Parity with ``entity_resolution._binding_is_live``.

        An alias row binds a normalized *name* to an id, so a renamed node
        leaves a binding pointing somewhere real and wrong. Seeding on it
        would hand the caller a neighbourhood for an entity they did not
        name. The re-check costs nothing: the name comes back on the
        confirm call that was already made.
        """
        store = _SeedStore({"e-1": _node("e-1", "something else")}, {"fincore": "e-1"})
        assert NamespaceSeedExtractor(store).extract("fincore") == []

    def test_a_binding_to_a_deleted_node_is_dropped(self) -> None:
        store = _SeedStore({}, {"fincore": "e-1"})
        assert NamespaceSeedExtractor(store).extract("fincore") == []


class TestBounds:
    def test_keys_are_capped(self) -> None:
        """The cost bound, and the measured saturation point.

        Over the 85 real intents this deployment has assembled packs for, a
        cap of 16 seeds 46, 24 seeds 49, and 32 seeds 51 — identical to no
        cap at all, down to the same 18 distinct seed ids.
        """
        store = _SeedStore()
        extractor = NamespaceSeedExtractor(store, max_keys=3)
        extractor.extract("one two three four five")
        assert [c[1] for c in store.alias_calls] == ["one", "two", "three"]
        assert len(store.subgraph_calls[0]["seed_ids"]) == 3 * len(
            DEFAULT_SEED_NAMESPACES
        )

    def test_seeds_are_capped(self) -> None:
        """Every seed is a BFS root, so this bounds the expansion."""
        live = {f"domain:w{i}": _node(f"domain:w{i}", f"w{i}") for i in range(5)}
        store = _SeedStore(live)
        extractor = NamespaceSeedExtractor(store, max_seeds=2)
        assert extractor.extract("w0 w1 w2 w3 w4") == ["domain:w0", "domain:w1"]

    def test_the_default_bounds_are_the_measured_ones(self) -> None:
        """Pinned as values, because both are claims about measurements.

        32 keys is where yield saturates; 8 seeds is a tail guard against a
        future corpus whose names collide with common words (the maximum
        observed on real intents is 3).
        """
        from trellis.retrieve.strategies import (
            DEFAULT_MAX_SEED_KEYS,
            DEFAULT_MAX_SEEDS,
        )

        assert DEFAULT_MAX_SEED_KEYS == 32
        assert DEFAULT_MAX_SEEDS == 8
        assert DEFAULT_SEED_NAMESPACES == (
            "domain",
            "team",
            "artifact",
            "agent",
            "tool",
        )
        assert "trace" not in DEFAULT_SEED_NAMESPACES


class TestTotality:
    """``extract`` runs inline on the pack path and must never raise."""

    def test_an_alias_outage_costs_the_alias_path_and_nothing_else(self) -> None:
        store = _SeedStore(
            {"domain:fincore": _node("domain:fincore", "fincore")},
            alias_raises=True,
        )
        assert NamespaceSeedExtractor(store).extract("fincore") == ["domain:fincore"]

    def test_a_confirm_outage_yields_no_seeds_rather_than_an_error(self) -> None:
        store = _SeedStore(
            {"domain:fincore": _node("domain:fincore", "fincore")},
            subgraph_raises=True,
        )
        assert NamespaceSeedExtractor(store).extract("fincore") == []

    def test_a_store_missing_the_methods_entirely_still_returns(self) -> None:
        assert NamespaceSeedExtractor(object()).extract("fincore") == []

    def test_a_malformed_subgraph_payload_is_survived(self) -> None:
        class _Bad(_SeedStore):
            def get_subgraph(self, *a: Any, **k: Any) -> Any:
                return "not a mapping"

        assert NamespaceSeedExtractor(_Bad()).extract("fincore") == []

    def test_it_satisfies_the_protocol(self) -> None:
        assert isinstance(NamespaceSeedExtractor(_SeedStore()), GraphSeedExtractor)


class TestThroughGraphSearch:
    """The extractor and the axis, wired together."""

    def test_a_resolved_intent_takes_the_seeded_branch(self) -> None:
        store = _SeedStore({"domain:fincore": _node("domain:fincore", "fincore")})
        items = GraphSearch(store, seed_extractor=NamespaceSeedExtractor(store)).search(
            "fincore forecast"
        )
        assert items
        assert all(
            i.metadata["graph_selection"] == GRAPH_SELECTION_SEEDED for i in items
        )
        assert store.scan_calls == []

    def test_an_unresolvable_intent_still_gets_the_recency_window(self) -> None:
        """Seeding is additive; a miss must not cost the caller an axis."""
        store = _SeedStore()
        GraphSearch(store, seed_extractor=NamespaceSeedExtractor(store)).search("q")
        assert store.scan_calls

    @pytest.mark.parametrize("depth", [1, 3])
    def test_an_explicit_depth_wins_on_the_derived_path(self, depth: int) -> None:
        """``setdefault``, not an assignment.

        Parametrized past the default on purpose: a test that only passes
        ``depth=1`` cannot tell a default from an override.
        """
        store = _SeedStore({"domain:fincore": _node("domain:fincore", "fincore")})
        GraphSearch(store, seed_extractor=NamespaceSeedExtractor(store)).search(
            "fincore", filters={"depth": depth}
        )
        assert store.subgraph_calls[-1]["depth"] == depth

    def test_a_derived_seed_expands_one_hop(self) -> None:
        """Measured: depth 1 yields 90 non-structural nodes across the 18
        real seeds, depth 2 yields 344 — and ``search`` finishes with
        ``nodes[:limit]`` over an *unordered* subgraph, so the extra 254
        displace the on-topic neighbours rather than ranking below them.
        """
        store = _SeedStore({"domain:fincore": _node("domain:fincore", "fincore")})
        GraphSearch(store, seed_extractor=NamespaceSeedExtractor(store)).search(
            "fincore"
        )
        assert store.subgraph_calls[-1]["depth"] == EXTRACTED_SEED_DEPTH
        assert EXTRACTED_SEED_DEPTH == 1

    def test_a_named_seed_keeps_the_historical_depth(self) -> None:
        """A caller who names a seed made a claim; a derived one is a guess."""
        store = _SeedStore()
        GraphSearch(store, seed_extractor=NamespaceSeedExtractor(store)).search(
            "fincore", filters={"seed_ids": ["explicit"]}
        )
        assert store.subgraph_calls[-1]["depth"] == 2
