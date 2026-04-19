"""Tests for the ParameterRegistry facade."""

from __future__ import annotations

from pathlib import Path

import pytest

from trellis.ops import ParameterRegistry
from trellis.schemas.parameters import ParameterScope, ParameterSet
from trellis.stores.sqlite.parameter import SQLiteParameterStore


@pytest.fixture
def store(tmp_path: Path):
    s = SQLiteParameterStore(tmp_path / "parameters.db")
    yield s
    s.close()


def test_get_returns_default_when_store_is_none():
    reg = ParameterRegistry(store=None)
    scope = ParameterScope(component_id="c")
    assert reg.get(scope, "k", 42) == 42


def test_get_returns_default_when_no_snapshot(store: SQLiteParameterStore):
    reg = ParameterRegistry(store)
    scope = ParameterScope(component_id="c")
    assert reg.get(scope, "k", 42) == 42


def test_get_returns_override_when_snapshot_present(
    store: SQLiteParameterStore,
):
    scope = ParameterScope(component_id="c")
    store.put(ParameterSet(scope=scope, values={"k": 99}))
    reg = ParameterRegistry(store)
    assert reg.get(scope, "k", 42) == 99


def test_get_falls_back_for_unknown_key(store: SQLiteParameterStore):
    scope = ParameterScope(component_id="c")
    store.put(ParameterSet(scope=scope, values={"other": 1}))
    reg = ParameterRegistry(store)
    assert reg.get(scope, "k", 42) == 42


def test_precedence_chain_via_registry(store: SQLiteParameterStore):
    # component-only baseline + domain-specific override
    store.put(ParameterSet(scope=ParameterScope(component_id="c"), values={"k": 1}))
    store.put(
        ParameterSet(
            scope=ParameterScope(component_id="c", domain="d"),
            values={"k": 7},
        )
    )
    reg = ParameterRegistry(store)

    narrow = ParameterScope(component_id="c", domain="d", intent_family="plan")
    assert reg.get(narrow, "k", 99) == 7

    no_domain = ParameterScope(component_id="c", intent_family="plan")
    assert reg.get(no_domain, "k", 99) == 1


def test_cache_hit(store: SQLiteParameterStore):
    scope = ParameterScope(component_id="c")
    store.put(ParameterSet(scope=scope, values={"k": 1}))
    reg = ParameterRegistry(store)
    assert reg.get(scope, "k", 0) == 1

    # Mutate underlying store directly.  Registry should still return cached value.
    store.put(ParameterSet(scope=scope, values={"k": 99}))
    assert reg.get(scope, "k", 0) == 1

    reg.invalidate(scope)
    assert reg.get(scope, "k", 0) == 99


def test_invalidate_all(store: SQLiteParameterStore):
    scope = ParameterScope(component_id="c")
    store.put(ParameterSet(scope=scope, values={"k": 1}))
    reg = ParameterRegistry(store)
    reg.get(scope, "k", 0)

    store.put(ParameterSet(scope=scope, values={"k": 2}))
    reg.invalidate()  # no scope → clear everything
    assert reg.get(scope, "k", 0) == 2


def test_get_values_returns_full_dict(store: SQLiteParameterStore):
    scope = ParameterScope(component_id="c")
    store.put(
        ParameterSet(scope=scope, values={"a": 1, "b": "hi", "c": True})
    )
    reg = ParameterRegistry(store)
    values = reg.get_values(scope)
    assert values == {"a": 1, "b": "hi", "c": True}


def test_get_values_empty_when_missing(store: SQLiteParameterStore):
    reg = ParameterRegistry(store)
    assert reg.get_values(ParameterScope(component_id="c")) == {}


def test_params_version_returns_active_version(store: SQLiteParameterStore):
    scope = ParameterScope(component_id="c")
    stored = store.put(ParameterSet(scope=scope, values={"k": 1}))
    reg = ParameterRegistry(store)
    assert reg.params_version(scope) == stored.params_version


def test_params_version_returns_none_when_missing(store: SQLiteParameterStore):
    reg = ParameterRegistry(store)
    assert reg.params_version(ParameterScope(component_id="c")) is None


def test_resolve_swallows_store_errors():
    class BrokenStore:
        def resolve(self, _scope):
            msg = "boom"
            raise RuntimeError(msg)

    reg = ParameterRegistry(BrokenStore())  # type: ignore[arg-type]
    # Should log and fall back to default, not raise.
    assert reg.get(ParameterScope(component_id="c"), "k", 42) == 42
