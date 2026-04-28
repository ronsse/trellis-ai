"""Tests for SQLiteParameterStore including precedence chain resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from trellis.schemas.parameters import ParameterScope, ParameterSet
from trellis.stores.sqlite.parameter import SQLiteParameterStore


@pytest.fixture
def store(tmp_path: Path):
    s = SQLiteParameterStore(tmp_path / "parameters.db")
    yield s
    s.close()


def _set(component_id="c", *, domain=None, intent_family=None, tool_name=None, **kw):
    scope = ParameterScope(
        component_id=component_id,
        domain=domain,
        intent_family=intent_family,
        tool_name=tool_name,
    )
    return ParameterSet(scope=scope, **kw)


def test_put_and_get(store: SQLiteParameterStore):
    ps = _set(values={"k": 60})
    stored = store.put(ps)
    assert stored.params_version == ps.params_version

    got = store.get(ps.params_version)
    assert got is not None
    assert got.values == {"k": 60}


def test_get_missing_returns_none(store: SQLiteParameterStore):
    assert store.get("nonexistent") is None


def test_get_active_returns_latest(store: SQLiteParameterStore):
    scope = ParameterScope(component_id="c")
    store.put(ParameterSet(scope=scope, values={"v": 1}))
    latest = ParameterSet(scope=scope, values={"v": 2})
    store.put(latest)
    got = store.get_active(scope)
    assert got is not None
    assert got.values == {"v": 2}


def test_resolve_exact_scope(store: SQLiteParameterStore):
    store.put(_set(domain="d", intent_family="plan", tool_name="t", values={"v": 4}))
    result = store.resolve(
        ParameterScope(
            component_id="c",
            domain="d",
            intent_family="plan",
            tool_name="t",
        )
    )
    assert result is not None
    assert result.values == {"v": 4}


def test_resolve_backs_off_from_tool_to_domain_intent(store: SQLiteParameterStore):
    # Only a (domain, intent) snapshot exists — resolve from full scope.
    store.put(_set(domain="d", intent_family="plan", values={"v": 3}))
    result = store.resolve(
        ParameterScope(
            component_id="c",
            domain="d",
            intent_family="plan",
            tool_name="t",
        )
    )
    assert result is not None
    assert result.values == {"v": 3}


def test_resolve_backs_off_to_component_only(store: SQLiteParameterStore):
    store.put(_set(values={"v": 1}))  # component-only baseline
    result = store.resolve(
        ParameterScope(
            component_id="c",
            domain="d",
            intent_family="plan",
            tool_name="t",
        )
    )
    assert result is not None
    assert result.values == {"v": 1}


def test_resolve_prefers_narrower(store: SQLiteParameterStore):
    store.put(_set(values={"v": 1}))  # component
    store.put(_set(domain="d", values={"v": 2}))  # domain
    store.put(_set(domain="d", intent_family="plan", values={"v": 3}))

    result = store.resolve(
        ParameterScope(
            component_id="c",
            domain="d",
            intent_family="plan",
            tool_name="t",
        )
    )
    assert result is not None
    assert result.values == {"v": 3}


def test_resolve_missing_returns_none(store: SQLiteParameterStore):
    result = store.resolve(ParameterScope(component_id="c"))
    assert result is None


def test_resolve_intent_without_domain(store: SQLiteParameterStore):
    store.put(_set(intent_family="plan", values={"v": 5}))
    result = store.resolve(ParameterScope(component_id="c", intent_family="plan"))
    assert result is not None
    assert result.values == {"v": 5}


def test_list_versions_filtered_by_scope(store: SQLiteParameterStore):
    store.put(_set(values={"v": 1}))
    store.put(_set(values={"v": 2}))
    store.put(_set(domain="d", values={"v": 9}))

    all_versions = store.list_versions()
    assert len(all_versions) == 3

    component_only = store.list_versions(ParameterScope(component_id="c"))
    assert len(component_only) == 2
