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


class _CountingStore:
    """Proxy that records how often the expensive scan is reached."""

    def __init__(self, inner: SQLiteGraphStore) -> None:
        self._inner = inner
        self.query_calls = 0
        self.resolve_alias_calls = 0

    def query(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        self.query_calls += 1
        return self._inner.query(*args, **kwargs)

    def resolve_alias(self, *args: Any, **kwargs: Any) -> dict[str, Any] | None:
        self.resolve_alias_calls += 1
        return self._inner.resolve_alias(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _add_entity(store: SQLiteGraphStore, node_id: str, name: str) -> str:
    return store.upsert_node(
        node_id=node_id,
        node_type="Person",
        properties={"name": name},
    )


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
        counting = _CountingStore(store)
        resolve = build_name_alias_resolver(counting)

        assert resolve("   ") == []
        assert counting.query_calls == 0
        assert counting.resolve_alias_calls == 0


class TestAliasMinting:
    def test_alias_minted_on_first_resolve(self, store) -> None:
        _add_entity(store, "ent-alice", "Alice")
        resolve = build_name_alias_resolver(store)

        assert resolve("Alice") == ["ent-alice"]

        row = store.resolve_alias(NAME_ALIAS_SOURCE_SYSTEM, "alice")
        assert row is not None
        assert row["entity_id"] == "ent-alice"
        assert row["raw_name"] == "Alice"

    def test_second_resolution_uses_the_index_not_the_scan(self, store) -> None:
        _add_entity(store, "ent-alice", "Alice")
        counting = _CountingStore(store)
        resolve = build_name_alias_resolver(counting)

        assert resolve("Alice") == ["ent-alice"]
        assert counting.query_calls == 1  # bootstrap scan

        assert resolve("alice") == ["ent-alice"]
        assert counting.query_calls == 1  # index answered — no second scan

    def test_minted_alias_serves_every_case_variant(self, store) -> None:
        _add_entity(store, "ent-alice", "Alice")
        counting = _CountingStore(store)
        resolve = build_name_alias_resolver(counting)

        resolve("Alice")
        for variant in ("ALICE", " alice ", "aLiCe"):
            assert resolve(variant) == ["ent-alice"]
        assert counting.query_calls == 1

    def test_mint_disabled_keeps_the_index_empty(self, store) -> None:
        _add_entity(store, "ent-alice", "Alice")
        counting = _CountingStore(store)
        resolve = build_name_alias_resolver(counting, mint=False)

        assert resolve("Alice") == ["ent-alice"]
        assert resolve("Alice") == ["ent-alice"]
        assert counting.query_calls == 2
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
        counting = _CountingStore(store)
        resolve = build_name_alias_resolver(counting)

        resolve("Hermes")
        resolve("Hermes")

        assert counting.query_calls == 2

    def test_distinct_names_get_distinct_aliases(self, store) -> None:
        _add_entity(store, "ent-alice", "Alice")
        _add_entity(store, "ent-alice-b", "Alice-B")
        resolve = build_name_alias_resolver(store)

        assert resolve("Alice") == ["ent-alice"]
        assert resolve("Alice-B") == ["ent-alice-b"]


class _TruncatingStore:
    """Graph-store double whose scan always returns a full page.

    Models the state the old resolvers degraded into silently: the graph
    is bigger than the cap, so the requested name may live in the tail
    that was never examined.
    """

    def __init__(self, page: list[dict[str, Any]]) -> None:
        self._page = page
        self.minted: list[tuple[str, str]] = []

    def resolve_alias(self, source_system: str, raw_id: str, as_of: Any = None):
        return None

    def query(self, *, limit: int, **kwargs: Any) -> list[dict[str, Any]]:
        return self._page[:limit]

    def upsert_alias(self, *, entity_id: str, raw_id: str, **kwargs: Any) -> str:
        self.minted.append((raw_id, entity_id))
        return "alias-1"


def _node(node_id: str, name: str) -> dict[str, Any]:
    return {"node_id": node_id, "node_type": "Person", "properties": {"name": name}}


class TestScanCap:
    """Behaviour at and past the scan cap the old resolvers stopped at."""

    def test_miss_past_the_cap_warns_instead_of_reporting_absence(
        self, log_output
    ) -> None:
        graph = _TruncatingStore([_node(f"ent-{i}", f"Filler {i}") for i in range(5)])
        resolve = build_name_alias_resolver(graph, scan_limit=3)

        assert resolve("Target") == []

        warnings = [
            e for e in log_output if e["event"] == "entity_resolution_scan_truncated"
        ]
        assert len(warnings) == 1
        assert warnings[0]["log_level"] == "warning"
        assert warnings[0]["scan_limit"] == 3

    def test_untruncated_miss_does_not_warn(self, log_output) -> None:
        graph = _TruncatingStore([_node("ent-0", "Filler")])
        resolve = build_name_alias_resolver(graph, scan_limit=10)

        assert resolve("Target") == []
        assert not [
            e for e in log_output if e["event"] == "entity_resolution_scan_truncated"
        ]

    def test_truncated_scan_does_not_mint(self) -> None:
        graph = _TruncatingStore([_node("ent-target", "Target"), _node("ent-1", "F")])
        resolve = build_name_alias_resolver(graph, scan_limit=2)

        assert resolve("Target") == ["ent-target"]
        assert graph.minted == []

    def test_indexed_name_resolves_past_the_cap(self, store) -> None:
        """Once minted, graph size is irrelevant — the lookup is a single
        indexed row read, not a scan."""
        _add_entity(store, "ent-target", "Target")
        counting = _CountingStore(store)

        # Mint while the graph is small enough for the scan to see it.
        build_name_alias_resolver(counting, scan_limit=10)("Target")
        assert counting.query_calls == 1

        for i in range(20):
            _add_entity(store, f"ent-{i}", f"Filler {i}")

        # A cap of 1 would defeat any scan-based resolver.
        resolve = build_name_alias_resolver(counting, scan_limit=1)
        assert resolve("Target") == ["ent-target"]
        assert counting.query_calls == 1  # unchanged: no scan ran


class TestStoreFailures:
    def test_scan_failure_is_soft_by_default(self, store, monkeypatch) -> None:
        def _boom(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            msg = "graph store down"
            raise RuntimeError(msg)

        monkeypatch.setattr(store, "query", _boom)
        resolve = build_name_alias_resolver(store)

        assert resolve("Alice") == []

    def test_scan_failure_can_be_made_loud(self, store, monkeypatch) -> None:
        def _boom(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            msg = "graph store down"
            raise RuntimeError(msg)

        monkeypatch.setattr(store, "query", _boom)
        seen: list[str] = []

        def _on_error(exc: Exception, mention: str) -> None:
            seen.append(mention)
            raise exc

        resolve = build_name_alias_resolver(store, on_scan_error=_on_error)

        with pytest.raises(RuntimeError, match="graph store down"):
            resolve("Alice")
        assert seen == ["Alice"]

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
