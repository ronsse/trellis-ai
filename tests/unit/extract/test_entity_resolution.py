"""Tests for the indexed name → entity resolver used by the write paths.

The behaviours worth pinning are the ones that cost the live graph seven
duplicate ``hermes`` nodes: the resolver must actually read the display
name where the store puts it, must write what it learns into
``entity_aliases`` so the next lookup is indexed, and must never collapse
two same-named entities into one.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import structlog
from structlog.testing import capture_logs

from trellis.extract.entity_resolution import (
    NAME_ALIAS_SOURCE_SYSTEM,
    build_name_alias_resolver,
)
from trellis.stores.sqlite.graph import SQLiteGraphStore

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture
def store(tmp_path: Path):
    graph = SQLiteGraphStore(tmp_path / "graph.db")
    yield graph
    graph.close()


@pytest.fixture
def log_output() -> Iterator[list[dict]]:
    saved = structlog.get_config()
    structlog.configure(
        wrapper_class=structlog.BoundLogger,
        processors=saved.get("processors", []),
    )
    try:
        with capture_logs() as cap:
            yield cap
    finally:
        structlog.configure(**saved)


class _SpyStore:
    """Proxy over a real store that counts the calls the design turns on.

    Everything the resolver does not care about forwards untouched; the
    three methods it *does* care about are counted, so a test can assert
    "the scan ran once and never again" rather than inferring it from
    side effects.
    """

    def __init__(self, inner: SQLiteGraphStore) -> None:
        self._inner = inner
        self.query_calls = 0
        self.resolve_alias_calls = 0
        self.minted: list[tuple[str, str]] = []

    def query(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        self.query_calls += 1
        return self._inner.query(*args, **kwargs)

    def resolve_alias(self, *args: Any, **kwargs: Any) -> dict[str, Any] | None:
        self.resolve_alias_calls += 1
        return self._inner.resolve_alias(*args, **kwargs)

    def upsert_alias(self, *, entity_id: str, raw_id: str, **kwargs: Any) -> str:
        self.minted.append((raw_id, entity_id))
        return self._inner.upsert_alias(entity_id=entity_id, raw_id=raw_id, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _add_entity(store: SQLiteGraphStore, node_id: str, name: str) -> str:
    return store.upsert_node(
        node_id=node_id,
        node_type="Person",
        properties={"name": name},
    )


def _truncations(log_output: list[dict]) -> list[dict]:
    return [e for e in log_output if e["event"] == "entity_resolution_scan_truncated"]


class TestResolution:
    def test_resolves_name_from_node_properties(self, store) -> None:
        """The display name lives in properties — the old scan read the
        non-existent top-level ``name`` key and so never matched."""
        _add_entity(store, "ent-alice", "Alice")

        resolve = build_name_alias_resolver(store)

        assert resolve("Alice") == ["ent-alice"]

    def test_unknown_name_returns_no_match(self, store) -> None:
        _add_entity(store, "ent-alice", "Alice")
        resolve = build_name_alias_resolver(store)

        assert resolve("Bob") == []

    @pytest.mark.parametrize(
        "mention",
        ["hermes", "HERMES", "Hermes", "  Hermes  ", "Hermes\tThree"],
    )
    def test_case_and_whitespace_variants_resolve_to_one_entity(
        self, store, mention: str
    ) -> None:
        _add_entity(store, "ent-hermes", "Hermes Three")
        _add_entity(store, "ent-hermes-model", "hermes")
        resolve = build_name_alias_resolver(store)

        expected = "ent-hermes" if "Three" in mention else "ent-hermes-model"
        assert resolve(mention) == [expected]

    def test_blank_mention_is_not_resolvable(self, store) -> None:
        _add_entity(store, "ent-alice", "Alice")
        spy = _SpyStore(store)
        resolve = build_name_alias_resolver(spy)

        assert resolve("   ") == []
        assert spy.query_calls == 0
        assert spy.resolve_alias_calls == 0


class TestAliasMinting:
    def test_alias_minted_on_first_resolve(self, store) -> None:
        _add_entity(store, "ent-alice", "Alice")
        spy = _SpyStore(store)
        resolve = build_name_alias_resolver(spy)

        assert resolve("Alice") == ["ent-alice"]
        assert spy.minted == [("alice", "ent-alice")]

        row = store.resolve_alias(NAME_ALIAS_SOURCE_SYSTEM, "alice")
        assert row is not None
        assert row["entity_id"] == "ent-alice"
        assert row["raw_name"] == "Alice"

    def test_second_resolution_uses_the_index_not_the_scan(self, store) -> None:
        _add_entity(store, "ent-alice", "Alice")
        spy = _SpyStore(store)
        resolve = build_name_alias_resolver(spy)

        assert resolve("Alice") == ["ent-alice"]
        assert spy.query_calls == 1  # bootstrap scan

        assert resolve("alice") == ["ent-alice"]
        assert spy.query_calls == 1  # index answered — no second scan
        assert len(spy.minted) == 1  # and no second mint

    def test_minted_alias_serves_every_case_variant(self, store) -> None:
        _add_entity(store, "ent-alice", "Alice")
        spy = _SpyStore(store)
        resolve = build_name_alias_resolver(spy)

        resolve("Alice")
        for variant in ("ALICE", " alice ", "aLiCe"):
            assert resolve(variant) == ["ent-alice"]
        assert spy.query_calls == 1

    def test_mint_disabled_keeps_the_index_empty(self, store) -> None:
        _add_entity(store, "ent-alice", "Alice")
        spy = _SpyStore(store)
        resolve = build_name_alias_resolver(spy, mint=False)

        assert resolve("Alice") == ["ent-alice"]
        assert resolve("Alice") == ["ent-alice"]
        assert spy.query_calls == 2
        assert spy.minted == []
        assert store.resolve_alias(NAME_ALIAS_SOURCE_SYSTEM, "alice") is None

    def test_mint_failure_does_not_break_resolution(self, store, monkeypatch) -> None:
        _add_entity(store, "ent-alice", "Alice")

        def _boom(*args: Any, **kwargs: Any) -> str:
            msg = "alias table unavailable"
            raise RuntimeError(msg)

        monkeypatch.setattr(store, "upsert_alias", _boom)
        resolve = build_name_alias_resolver(store)

        assert resolve("Alice") == ["ent-alice"]


class TestAmbiguityIsNeverMerged:
    def test_two_entities_sharing_a_name_are_not_merged(self, store) -> None:
        """The destructive case. Both ids come back, the caller treats
        anything but a single hit as unresolved, and nothing is cached."""
        _add_entity(store, "ent-hermes-model", "Hermes")
        _add_entity(store, "ent-hermes-person", "hermes")

        resolve = build_name_alias_resolver(store)
        matches = resolve("Hermes")

        assert sorted(matches) == ["ent-hermes-model", "ent-hermes-person"]
        assert store.resolve_alias(NAME_ALIAS_SOURCE_SYSTEM, "hermes") is None

    def test_ambiguity_is_rescanned_every_time(self, store) -> None:
        _add_entity(store, "ent-a", "Hermes")
        _add_entity(store, "ent-b", "hermes")
        spy = _SpyStore(store)
        resolve = build_name_alias_resolver(spy)

        resolve("Hermes")
        resolve("Hermes")

        assert spy.query_calls == 2

    def test_distinct_names_get_distinct_aliases(self, store) -> None:
        _add_entity(store, "ent-alice", "Alice")
        _add_entity(store, "ent-alice-b", "Alice-B")
        resolve = build_name_alias_resolver(store)

        assert resolve("Alice") == ["ent-alice"]
        assert resolve("Alice-B") == ["ent-alice-b"]


class TestBindingLifecycle:
    """What a minted binding does when the graph moves underneath it."""

    def test_a_later_same_named_entity_does_not_reopen_the_ambiguity(
        self, store
    ) -> None:
        """ACCEPTED FAILURE MODE, pinned so it stays a decision.

        Once ``hermes`` is bound, a second ``Hermes`` created afterwards
        is invisible to the resolver: the binding still points at a live,
        correctly-named node, so nothing re-scans and mentions keep
        resolving to the first one. Detecting it would cost a full scan on
        every hit — the O(n) cost this module exists to remove. The blast
        radius is a ``mentions`` edge on the wrong one of two same-named
        entities, which is deletable; no node is merged or rewritten.
        """
        _add_entity(store, "ent-first", "Hermes")
        spy = _SpyStore(store)
        resolve = build_name_alias_resolver(spy)
        assert resolve("Hermes") == ["ent-first"]

        _add_entity(store, "ent-second", "Hermes")

        assert resolve("Hermes") == ["ent-first"]
        assert spy.query_calls == 1  # never looked again

    def test_binding_to_a_missing_entity_is_dropped_and_rescanned(
        self, store, log_output
    ) -> None:
        """Without this the binding would outlive its node and every
        mention would emit an edge the FK pre-flight rejects.

        The alias table has no foreign key, and only SQLite's
        ``delete_node`` happens to cascade to it — so a row pointing at a
        node that is not there is reachable on any backend, and via the
        bulk-ingest alias route on all of them.
        """
        store.upsert_alias(
            entity_id="ent-gone",
            source_system=NAME_ALIAS_SOURCE_SYSTEM,
            raw_id="hermes",
            raw_name="Hermes",
        )
        _add_entity(store, "ent-new", "Hermes")
        resolve = build_name_alias_resolver(store)

        assert resolve("Hermes") == ["ent-new"]
        rebound = store.resolve_alias(NAME_ALIAS_SOURCE_SYSTEM, "hermes")
        assert rebound is not None
        assert rebound["entity_id"] == "ent-new"
        dropped = [
            e
            for e in log_output
            if e["event"] == "entity_resolution_stale_binding_dropped"
        ]
        assert [e["reason"] for e in dropped] == ["node_missing"]

    def test_renamed_entity_releases_its_binding(self, store, log_output) -> None:
        _add_entity(store, "ent-a", "Hermes")
        resolve = build_name_alias_resolver(store)
        assert resolve("Hermes") == ["ent-a"]

        _add_entity(store, "ent-a", "Hermes Two")  # SCD-2 rename

        assert resolve("Hermes") == []
        dropped = [
            e
            for e in log_output
            if e["event"] == "entity_resolution_stale_binding_dropped"
        ]
        assert [e["reason"] for e in dropped] == ["name_changed"]

    def test_binding_check_failure_keeps_the_binding(self, store, monkeypatch) -> None:
        """An outage on the validation read must not discard good data."""
        _add_entity(store, "ent-alice", "Alice")
        resolve = build_name_alias_resolver(store)
        assert resolve("Alice") == ["ent-alice"]

        def _boom(*args: Any, **kwargs: Any) -> dict[str, Any] | None:
            msg = "graph store down"
            raise RuntimeError(msg)

        monkeypatch.setattr(store, "get_node", _boom)
        assert resolve("Alice") == ["ent-alice"]


class TestScanCap:
    """Behaviour at and past the scan cap the old resolvers stopped at."""

    def test_miss_past_the_cap_warns_instead_of_reporting_absence(
        self, store, log_output
    ) -> None:
        for i in range(5):
            _add_entity(store, f"ent-{i}", f"Filler {i}")
        resolve = build_name_alias_resolver(store, scan_limit=3)

        assert resolve("Target") == []

        warnings = _truncations(log_output)
        assert len(warnings) == 1
        assert warnings[0]["log_level"] == "warning"
        assert warnings[0]["scan_limit"] == 3

    def test_single_match_past_the_cap_still_warns(self, store, log_output) -> None:
        """The dangerous case: the scan found exactly one ``Target`` but
        never looked at the tail, where a second one may live. Acting on
        it is the only outcome that can bind the *wrong* entity, so it
        must not be the quiet one."""
        for i in range(4):
            _add_entity(store, f"ent-{i}", f"Filler {i}")
        _add_entity(store, "ent-target", "Target")  # newest — inside page 1
        resolve = build_name_alias_resolver(store, scan_limit=2)

        assert resolve("Target") == ["ent-target"]

        warnings = _truncations(log_output)
        assert len(warnings) == 1
        assert warnings[0]["matches"] == 1

    def test_untruncated_miss_does_not_warn(self, store, log_output) -> None:
        _add_entity(store, "ent-0", "Filler")
        resolve = build_name_alias_resolver(store, scan_limit=10)

        assert resolve("Target") == []
        assert _truncations(log_output) == []

    def test_truncated_scan_does_not_mint(self, store) -> None:
        _add_entity(store, "ent-target", "Target")
        _add_entity(store, "ent-1", "Filler")
        spy = _SpyStore(store)
        resolve = build_name_alias_resolver(spy, scan_limit=2)

        assert resolve("Target") == ["ent-target"]
        assert spy.minted == []

    def test_indexed_name_resolves_past_the_cap(self, store) -> None:
        """Once minted, graph size is irrelevant — the lookup is a single
        indexed row read, not a scan."""
        _add_entity(store, "ent-target", "Target")
        spy = _SpyStore(store)

        # Mint while the graph is small enough for the scan to see it.
        build_name_alias_resolver(spy, scan_limit=10)("Target")
        assert spy.query_calls == 1

        for i in range(20):
            _add_entity(store, f"ent-{i}", f"Filler {i}")

        # A cap of 1 would defeat any scan-based resolver.
        resolve = build_name_alias_resolver(spy, scan_limit=1)
        assert resolve("Target") == ["ent-target"]
        assert spy.query_calls == 1  # unchanged: no scan ran


class TestStoreFailures:
    """No store outage may fail the ingest that triggered the resolution."""

    def test_scan_failure_yields_no_match(self, store, monkeypatch) -> None:
        def _boom(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            msg = "graph store down"
            raise RuntimeError(msg)

        monkeypatch.setattr(store, "query", _boom)
        resolve = build_name_alias_resolver(store)

        assert resolve("Alice") == []

    def test_index_failure_falls_back_to_the_scan(self, store, monkeypatch) -> None:
        _add_entity(store, "ent-alice", "Alice")

        def _boom(*args: Any, **kwargs: Any) -> dict[str, Any] | None:
            msg = "alias index unavailable"
            raise RuntimeError(msg)

        monkeypatch.setattr(store, "resolve_alias", _boom)
        resolve = build_name_alias_resolver(store, mint=False)

        assert resolve("Alice") == ["ent-alice"]


class TestWritePathWiring:
    """Both write paths must share this builder, not re-derive it."""

    def test_ingest_hook_resolver_mints(self, tmp_path: Path) -> None:
        from trellis.extract.memory_ingest_hook import _graph_alias_resolver
        from trellis.stores.registry import StoreRegistry

        stores = tmp_path / "stores"
        stores.mkdir()
        registry = StoreRegistry(stores_dir=stores)
        graph = registry.knowledge.graph_store
        _add_entity(graph, "ent-alice", "Alice")

        resolve = _graph_alias_resolver(registry)

        assert resolve("alice") == ["ent-alice"]
        assert graph.resolve_alias(NAME_ALIAS_SOURCE_SYSTEM, "alice") is not None

    def test_mcp_resolver_mints(self, tmp_path: Path) -> None:
        from trellis.mcp.server import _build_alias_resolver
        from trellis.stores.registry import StoreRegistry

        stores = tmp_path / "stores"
        stores.mkdir()
        registry = StoreRegistry(stores_dir=stores)
        graph = registry.knowledge.graph_store
        _add_entity(graph, "ent-alice", "Alice")

        resolve = _build_alias_resolver(registry)

        assert resolve("ALICE") == ["ent-alice"]
        assert graph.resolve_alias(NAME_ALIAS_SOURCE_SYSTEM, "alice") is not None

    def test_mcp_scan_failure_is_soft_like_the_ingest_path(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Both paths share one behaviour on a store outage. The MCP path
        used to raise, which made no difference — ``_run_memory_extraction``
        swallows it — but did abandon the whole extraction pass."""
        from trellis.mcp.server import _build_alias_resolver
        from trellis.stores.registry import StoreRegistry

        stores = tmp_path / "stores"
        stores.mkdir()
        registry = StoreRegistry(stores_dir=stores)

        def _boom(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            msg = "graph store down"
            raise RuntimeError(msg)

        monkeypatch.setattr(registry.knowledge.graph_store, "query", _boom)

        assert _build_alias_resolver(registry)("Alice") == []
