"""Tests for the read-only referent resolver behind ``save_knowledge``.

The behaviours pinned here are the ones the reference deployment measured.
Trace extraction mints ``domain:``/``team:``/``agent:``/``tool:`` ids with
:func:`~trellis.extract.trace.normalize_slug`, not with the name key, so a
resolver that derived ids from the name key would miss most of them. Ninety
older ``tool:`` nodes carry the raw tool name verbatim. And ``entity.create``
binds a name alias to the node it has just written, so a resolver that did
not exclude the caller's own node would resolve a note to itself.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from trellis.extract.entity_resolution import NAME_ALIAS_SOURCE_SYSTEM
from trellis.extract.referents import (
    DEFAULT_REFERENT_NAMESPACES,
    VIA_ID,
    VIA_NAME_ALIAS,
    ReferentStatus,
    namespace_candidates,
    resolve_referent,
    resolve_referents,
)
from trellis.extract.trace import TraceExtractor
from trellis.schemas.trace import Trace
from trellis.schemas.well_known import (
    CONCEPT,
    CREATIVE_WORK,
    SOFTWARE_APPLICATION,
    normalize_entity_name,
)
from trellis.stores.sqlite.graph import SQLiteGraphStore

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SQLiteGraphStore]:
    graph = SQLiteGraphStore(tmp_path / "graph.db")
    yield graph
    graph.close()


class _SpyStore:
    """Allow-list proxy: the two reads the resolver may make, and nothing else.

    Any other attribute — a write, or a read the design does not use such as
    a per-candidate ``get_node`` — fails the test, so "read-only, one batch
    read" is asserted rather than inferred from side effects.
    """

    def __init__(self, inner: SQLiteGraphStore) -> None:
        self._inner = inner
        self.alias_keys: list[str] = []
        self.bulk_reads: list[list[str]] = []

    def resolve_alias(
        self, source_system: str, raw_id: str, *args: Any, **kwargs: Any
    ) -> dict[str, Any] | None:
        self.alias_keys.append(raw_id)
        return self._inner.resolve_alias(source_system, raw_id, *args, **kwargs)

    def get_nodes_bulk(
        self, node_ids: list[str], *args: Any, **kwargs: Any
    ) -> list[dict[str, Any]]:
        self.bulk_reads.append(list(node_ids))
        return self._inner.get_nodes_bulk(node_ids, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        msg = f"resolver touched GraphStore.{name}"
        raise AssertionError(msg)


def _node(store: SQLiteGraphStore, node_id: str, node_type: str, name: str) -> str:
    return store.upsert_node(node_id, node_type, {"name": name})


def _bind_name(store: SQLiteGraphStore, node_id: str, name: str) -> None:
    """Bind the ``name`` alias the way ``entity.create`` does."""
    store.upsert_alias(
        node_id, NAME_ALIAS_SOURCE_SYSTEM, normalize_entity_name(name), raw_name=name
    )


# ---------------------------------------------------------------------------
# Candidate derivation
# ---------------------------------------------------------------------------


class TestNamespaceCandidates:
    def test_bare_name_derives_each_namespace_by_its_minting_rule(self) -> None:
        derived = namespace_candidates("MCP Trellis Search")
        assert [(form.via, candidate) for form, candidate in derived] == [
            ("domain:slug", "domain:mcp-trellis-search"),
            ("team:slug", "team:mcp-trellis-search"),
            ("agent:slug", "agent:mcp-trellis-search"),
            ("tool:slug", "tool:mcp-trellis-search"),
            ("tool:verbatim", "tool:MCP Trellis Search"),
            ("artifact:verbatim", "artifact:MCP Trellis Search"),
        ]

    def test_slug_is_the_minting_rule_not_the_name_key(self) -> None:
        # The name key leaves underscores alone, so an id derived from it
        # is not the id trace extraction minted for this tool.
        raw = "mcp__trellis__search"
        assert normalize_entity_name(raw) == raw
        candidates = {c for _, c in namespace_candidates(raw, namespaces=("tool",))}
        assert "tool:mcp-trellis-search" in candidates

    def test_prefixed_value_is_scoped_to_its_namespace(self) -> None:
        derived = namespace_candidates("domain:Trellis AI")
        assert [(form.via, candidate) for form, candidate in derived] == [
            ("domain:slug", "domain:trellis-ai")
        ]

    def test_prefix_match_ignores_case(self) -> None:
        derived = namespace_candidates("Tool:Bash")
        assert [candidate for _, candidate in derived] == ["tool:bash", "tool:Bash"]

    def test_prefix_outside_the_allowed_namespaces_derives_nothing(self) -> None:
        assert namespace_candidates("tool:bash", namespaces=("domain",)) == ()

    def test_unknown_prefix_is_part_of_the_name(self) -> None:
        candidates = [c for _, c in namespace_candidates("https://example.com/x")]
        assert "artifact:https://example.com/x" in candidates
        assert "domain:https-example-com-x" in candidates

    def test_blank_tail_derives_nothing(self) -> None:
        assert namespace_candidates("domain:") == ()
        assert namespace_candidates("tool:   ") == ()


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


class TestResolveReferent:
    def test_exact_id_matches(self, store: SQLiteGraphStore) -> None:
        _node(store, "01NOTE", CONCEPT, "anything")
        resolution = resolve_referent(store, "01NOTE")
        assert resolution.status is ReferentStatus.EXACT
        assert resolution.match is not None
        assert resolution.match.node_id == "01NOTE"
        assert resolution.match.via == VIA_ID

    def test_exact_id_wins_over_a_derived_match(self, store: SQLiteGraphStore) -> None:
        _node(store, "bash", CONCEPT, "a node whose id is literally bash")
        _node(store, "tool:bash", SOFTWARE_APPLICATION, "Bash")
        resolution = resolve_referent(store, "bash")
        assert resolution.status is ReferentStatus.EXACT
        assert [m.node_id for m in resolution.matches] == ["bash"]

    def test_name_resolves_through_the_slug(self, store: SQLiteGraphStore) -> None:
        _node(store, "tool:mcp-trellis-search", SOFTWARE_APPLICATION, "x")
        resolution = resolve_referent(store, "mcp__trellis__search")
        assert resolution.status is ReferentStatus.RESOLVED
        assert resolution.match is not None
        assert resolution.match.node_id == "tool:mcp-trellis-search"
        assert resolution.match.node_type == SOFTWARE_APPLICATION
        assert resolution.match.via == "tool:slug"

    def test_legacy_verbatim_tool_id_resolves(self, store: SQLiteGraphStore) -> None:
        _node(store, "tool:mcp__trellis__search", SOFTWARE_APPLICATION, "x")
        resolution = resolve_referent(store, "mcp__trellis__search")
        assert resolution.status is ReferentStatus.RESOLVED
        assert resolution.match is not None
        assert resolution.match.node_id == "tool:mcp__trellis__search"
        assert resolution.match.via == "tool:verbatim"

    def test_slug_is_preferred_when_both_tool_spellings_exist(
        self, store: SQLiteGraphStore
    ) -> None:
        _node(store, "tool:mcp-trellis-search", SOFTWARE_APPLICATION, "x")
        _node(store, "tool:mcp__trellis__search", SOFTWARE_APPLICATION, "x")
        resolution = resolve_referent(store, "mcp__trellis__search")
        # Two spellings of one tool are one referent, not an ambiguity.
        assert resolution.status is ReferentStatus.RESOLVED
        assert [m.node_id for m in resolution.matches] == ["tool:mcp-trellis-search"]

    def test_artifact_resolves_verbatim(self, store: SQLiteGraphStore) -> None:
        _node(store, "artifact:docs/PRD.md", CREATIVE_WORK, "docs/PRD.md")
        resolution = resolve_referent(store, "docs/PRD.md")
        assert resolution.status is ReferentStatus.RESOLVED
        assert resolution.match is not None
        assert resolution.match.node_id == "artifact:docs/PRD.md"
        assert resolution.match.via == "artifact:verbatim"

    def test_prefixed_value_is_canonicalised_within_its_namespace(
        self, store: SQLiteGraphStore
    ) -> None:
        _node(store, "domain:trellis-ai", CONCEPT, "trellis-ai")
        resolution = resolve_referent(store, "domain:Trellis AI")
        assert resolution.status is ReferentStatus.RESOLVED
        assert resolution.match is not None
        assert resolution.match.node_id == "domain:trellis-ai"
        assert resolution.match.via == "domain:slug"

    def test_one_tail_in_two_namespaces_is_ambiguous(
        self, store: SQLiteGraphStore
    ) -> None:
        _node(store, "domain:deploy", CONCEPT, "deploy")
        _node(store, "tool:deploy", SOFTWARE_APPLICATION, "deploy")
        resolution = resolve_referent(store, "deploy")
        assert resolution.status is ReferentStatus.AMBIGUOUS
        assert resolution.match is None
        assert {m.node_id for m in resolution.matches} == {
            "domain:deploy",
            "tool:deploy",
        }

    def test_a_prefix_settles_an_ambiguous_tail(self, store: SQLiteGraphStore) -> None:
        _node(store, "domain:deploy", CONCEPT, "deploy")
        _node(store, "tool:deploy", SOFTWARE_APPLICATION, "deploy")
        resolution = resolve_referent(store, "tool:Deploy")
        assert resolution.status is ReferentStatus.RESOLVED
        assert [m.node_id for m in resolution.matches] == ["tool:deploy"]

    def test_unknown_name_is_missing(self, store: SQLiteGraphStore) -> None:
        _node(store, "tool:bash", SOFTWARE_APPLICATION, "Bash")
        resolution = resolve_referent(store, "no such thing")
        assert resolution.status is ReferentStatus.MISSING
        assert resolution.matches == ()
        assert resolution.match is None

    def test_live_name_alias_resolves(self, store: SQLiteGraphStore) -> None:
        _node(store, "01NOTE", CONCEPT, "Release Checklist")
        _bind_name(store, "01NOTE", "Release Checklist")
        resolution = resolve_referent(store, "release   checklist")
        assert resolution.status is ReferentStatus.RESOLVED
        assert resolution.match is not None
        assert resolution.match.node_id == "01NOTE"
        assert resolution.match.via == VIA_NAME_ALIAS

    def test_alias_to_a_renamed_node_is_ignored(self, store: SQLiteGraphStore) -> None:
        _node(store, "01NOTE", CONCEPT, "Release Checklist")
        _bind_name(store, "01NOTE", "Release Checklist")
        store.upsert_node("01NOTE", CONCEPT, {"name": "Launch Runbook"})
        resolution = resolve_referent(store, "Release Checklist")
        assert resolution.status is ReferentStatus.MISSING

    def test_alias_and_id_naming_one_node_are_one_match(
        self, store: SQLiteGraphStore
    ) -> None:
        _node(store, "tool:bash", SOFTWARE_APPLICATION, "Bash")
        _bind_name(store, "tool:bash", "Bash")
        resolution = resolve_referent(store, "Bash")
        assert resolution.status is ReferentStatus.RESOLVED
        assert [m.node_id for m in resolution.matches] == ["tool:bash"]

    def test_alias_and_id_naming_two_nodes_are_ambiguous(
        self, store: SQLiteGraphStore
    ) -> None:
        _node(store, "tool:bash", SOFTWARE_APPLICATION, "Bash")
        _node(store, "01NOTE", CONCEPT, "bash")
        _bind_name(store, "01NOTE", "bash")
        resolution = resolve_referent(store, "bash")
        assert resolution.status is ReferentStatus.AMBIGUOUS
        assert {m.node_id for m in resolution.matches} == {"tool:bash", "01NOTE"}

    def test_exclude_ids_keeps_the_callers_node_out(
        self, store: SQLiteGraphStore
    ) -> None:
        # The shape entity.create leaves behind: the new note named "docker"
        # owns the name alias, and a tool node shares the name.
        _node(store, "tool:docker", SOFTWARE_APPLICATION, "docker")
        _node(store, "01SELF", CONCEPT, "docker")
        _bind_name(store, "01SELF", "docker")

        unexcluded = resolve_referent(store, "docker")
        assert unexcluded.status is ReferentStatus.AMBIGUOUS

        resolution = resolve_referent(store, "docker", exclude_ids=("01SELF",))
        assert resolution.status is ReferentStatus.RESOLVED
        assert [m.node_id for m in resolution.matches] == ["tool:docker"]

    def test_an_excluded_id_is_never_an_exact_match(
        self, store: SQLiteGraphStore
    ) -> None:
        _node(store, "01SELF", CONCEPT, "note")
        resolution = resolve_referent(store, "01SELF", exclude_ids=("01SELF",))
        assert resolution.status is ReferentStatus.MISSING

    def test_allow_exact_off_answers_only_from_the_namespaces(
        self, store: SQLiteGraphStore
    ) -> None:
        _node(store, "trellis", CONCEPT, "a node whose id is literally trellis")
        _node(store, "domain:trellis", CONCEPT, "trellis")

        with_exact = resolve_referent(store, "trellis", namespaces=("domain",))
        assert with_exact.status is ReferentStatus.EXACT

        resolution = resolve_referent(
            store, "trellis", namespaces=("domain",), allow_exact=False
        )
        assert resolution.status is ReferentStatus.RESOLVED
        assert [m.node_id for m in resolution.matches] == ["domain:trellis"]

    def test_namespaces_limit_what_is_derived(self, store: SQLiteGraphStore) -> None:
        _node(store, "tool:deploy", SOFTWARE_APPLICATION, "deploy")
        resolution = resolve_referent(
            store, "deploy", namespaces=("domain",), use_name_alias=False
        )
        assert resolution.status is ReferentStatus.MISSING

    def test_name_alias_off_skips_the_alias_index(
        self, store: SQLiteGraphStore
    ) -> None:
        _node(store, "01NOTE", CONCEPT, "Release Checklist")
        _bind_name(store, "01NOTE", "Release Checklist")
        spy = _SpyStore(store)
        resolution = resolve_referent(spy, "Release Checklist", use_name_alias=False)  # type: ignore[arg-type]
        assert resolution.status is ReferentStatus.MISSING
        assert spy.alias_keys == []

    def test_unknown_namespace_raises(self, store: SQLiteGraphStore) -> None:
        with pytest.raises(ValueError, match="widget"):
            resolve_referents(store, ["x"], namespaces=("widget",))

    def test_blank_value_is_missing_without_a_read(
        self, store: SQLiteGraphStore
    ) -> None:
        spy = _SpyStore(store)
        resolution = resolve_referent(spy, "   ")  # type: ignore[arg-type]
        assert resolution.status is ReferentStatus.MISSING
        assert spy.alias_keys == []
        assert spy.bulk_reads == []


class TestResolveReferents:
    def test_many_values_cost_one_bulk_read(self, store: SQLiteGraphStore) -> None:
        _node(store, "domain:trellis", CONCEPT, "trellis")
        _node(store, "tool:bash", SOFTWARE_APPLICATION, "Bash")
        spy = _SpyStore(store)
        values = ["trellis", "Bash", "nothing", "trellis"]

        resolutions = resolve_referents(spy, values)  # type: ignore[arg-type]

        assert [r.value for r in resolutions] == values
        assert [r.status for r in resolutions] == [
            ReferentStatus.RESOLVED,
            ReferentStatus.RESOLVED,
            ReferentStatus.MISSING,
            ReferentStatus.RESOLVED,
        ]
        assert len(spy.bulk_reads) == 1
        # One alias lookup per distinct name key, not per value.
        assert sorted(spy.alias_keys) == ["bash", "nothing", "trellis"]

    def test_resolution_writes_nothing(self, store: SQLiteGraphStore) -> None:
        _node(store, "tool:deploy", SOFTWARE_APPLICATION, "deploy")
        _node(store, "domain:deploy", CONCEPT, "deploy")
        nodes, edges = store.count_nodes(), store.count_edges()
        spy = _SpyStore(store)

        resolve_referents(spy, ["deploy", "tool:deploy", "unknown", ""])  # type: ignore[arg-type]

        assert (store.count_nodes(), store.count_edges()) == (nodes, edges)

    def test_a_bulk_read_failure_propagates(self, store: SQLiteGraphStore) -> None:
        class _Failing(_SpyStore):
            def get_nodes_bulk(self, *args: Any, **kwargs: Any) -> list[Any]:
                msg = "graph store unavailable"
                raise RuntimeError(msg)

        with pytest.raises(RuntimeError, match="unavailable"):
            resolve_referent(_Failing(store), "bash")  # type: ignore[arg-type]

    def test_an_alias_read_failure_propagates(self, store: SQLiteGraphStore) -> None:
        class _Failing(_SpyStore):
            def resolve_alias(self, *args: Any, **kwargs: Any) -> None:
                msg = "alias index unavailable"
                raise RuntimeError(msg)

        with pytest.raises(RuntimeError, match="unavailable"):
            resolve_referent(_Failing(store), "bash")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Round trip against the real minting code
# ---------------------------------------------------------------------------


class TestReversesTraceMinting:
    async def test_every_namespaced_id_resolves_from_its_own_name(
        self, store: SQLiteGraphStore
    ) -> None:
        """What trace extraction mints from a name, that name resolves back to.

        Built from :class:`TraceExtractor` itself rather than from literal
        ids, so a change to any namespace's minting rule fails here instead
        of silently stranding the ids it now mints.
        """
        trace = Trace.model_validate(
            {
                "source": "agent",
                "intent": "Ship the resolver",
                "steps": [
                    {"step_type": "tool_call", "name": "mcp__trellis__search"},
                    {"step_type": "tool_call", "name": "Web Fetch"},
                    {"step_type": "tool_call", "name": "Bash"},
                ],
                "artifacts_produced": [
                    {"artifact_id": "docs/plans/K1.md", "artifact_type": "file"}
                ],
                "context": {
                    "agent_id": "Code Orchestrator",
                    "team": "Platform_Infra",
                    "domain": "Trellis AI",
                },
            }
        )
        result = await TraceExtractor().extract(trace, source_hint="trace")
        for draft in result.entities:
            assert draft.entity_id is not None
            store.upsert_node(draft.entity_id, draft.entity_type, {"name": draft.name})

        namespaced = [
            draft
            for draft in result.entities
            if draft.entity_id is not None
            and draft.entity_id.partition(":")[0] in DEFAULT_REFERENT_NAMESPACES
        ]
        # agent, team, domain, three tools, one artifact.
        assert len(namespaced) == 7
        for draft, resolution in zip(
            namespaced,
            resolve_referents(store, [draft.name for draft in namespaced]),
            strict=True,
        ):
            assert resolution.match is not None, draft.entity_id
            assert resolution.match.node_id == draft.entity_id
