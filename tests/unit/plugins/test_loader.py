"""Tests for :mod:`trellis.plugins.loader`.

Mocks ``importlib.metadata.entry_points`` so we can exercise the
discovery + merge paths without installing real plugin packages.
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import patch

import pytest

from trellis.plugins.loader import (
    OVERRIDE_ENV,
    PluginSpec,
    _parse_ep_value,
    discover,
    load_class,
    merge_with_builtins,
    store_backend_groups,
)


@dataclass
class _FakeDist:
    name: str | None = None
    version: str | None = None


@dataclass
class _FakeEntryPoint:
    name: str
    value: str
    dist: _FakeDist | None = None


def _mock_eps(items_by_group: dict[str, list[_FakeEntryPoint]]):
    """Produce a context manager that patches entry_points()."""

    def fake(*, group: str):
        return items_by_group.get(group, [])

    return patch("trellis.plugins.loader.entry_points", side_effect=fake)


class TestParseEntryPointValue:
    def test_colon_form(self):
        assert _parse_ep_value("pkg.mod:Class") == ("pkg.mod", "Class")

    def test_dotted_form(self):
        assert _parse_ep_value("pkg.mod.Class") == ("pkg.mod", "Class")

    def test_no_separator_invalid(self):
        assert _parse_ep_value("justaname") is None

    def test_empty_module_invalid(self):
        assert _parse_ep_value(":Class") is None

    def test_empty_attr_invalid(self):
        assert _parse_ep_value("pkg.mod:") is None


class TestDiscover:
    def test_returns_empty_when_group_absent(self):
        with _mock_eps({}):
            assert discover("trellis.nonexistent") == []

    def test_returns_parsed_specs(self):
        with _mock_eps(
            {
                "trellis.stores.graph": [
                    _FakeEntryPoint(
                        name="custom",
                        value="my_pkg.stores:CustomGraphStore",
                        dist=_FakeDist(name="my-pkg", version="1.2.3"),
                    ),
                ],
            }
        ):
            specs = discover("trellis.stores.graph")
        assert len(specs) == 1
        spec = specs[0]
        assert spec.name == "custom"
        assert spec.module == "my_pkg.stores"
        assert spec.attr == "CustomGraphStore"
        assert spec.distribution == "my-pkg"
        assert spec.distribution_version == "1.2.3"

    def test_drops_malformed_entries(self):
        with _mock_eps(
            {
                "trellis.stores.graph": [
                    _FakeEntryPoint(name="good", value="pkg:Class"),
                    _FakeEntryPoint(name="bad", value="nope"),
                ],
            }
        ):
            specs = discover("trellis.stores.graph")
        assert len(specs) == 1
        assert specs[0].name == "good"

    def test_exception_returns_empty(self):
        """A broken metadata call must not take down the registry."""
        with patch(
            "trellis.plugins.loader.entry_points",
            side_effect=RuntimeError("boom"),
        ):
            assert discover("trellis.stores.graph") == []


class TestMergeWithBuiltins:
    def _spec(self, name: str, module: str = "plug.mod", attr: str = "Plug"):
        return PluginSpec(
            group="trellis.stores.graph",
            name=name,
            value=f"{module}:{attr}",
            module=module,
            attr=attr,
        )

    def test_adds_new_plugin(self):
        builtins = {"sqlite": ("trellis.stores.sqlite.graph", "SQLiteGraphStore")}
        merged, shadowed = merge_with_builtins(
            "trellis.stores.graph",
            builtins,
            specs=[self._spec("custom")],
        )
        assert "custom" in merged
        assert merged["custom"] == ("plug.mod", "Plug")
        assert shadowed == []

    def test_builtin_wins_by_default(self):
        builtins = {"sqlite": ("trellis.stores.sqlite.graph", "SQLiteGraphStore")}
        merged, shadowed = merge_with_builtins(
            "trellis.stores.graph",
            builtins,
            specs=[self._spec("sqlite", "evil.pkg", "EvilStore")],
        )
        # Built-in unchanged.
        assert merged["sqlite"] == ("trellis.stores.sqlite.graph", "SQLiteGraphStore")
        assert shadowed == ["sqlite"]

    def test_override_env_allows_shadowing(self, monkeypatch):
        monkeypatch.setenv(OVERRIDE_ENV, "1")
        builtins = {"sqlite": ("trellis.stores.sqlite.graph", "SQLiteGraphStore")}
        merged, shadowed = merge_with_builtins(
            "trellis.stores.graph",
            builtins,
            specs=[self._spec("sqlite", "evil.pkg", "EvilStore")],
        )
        assert merged["sqlite"] == ("evil.pkg", "EvilStore")
        assert shadowed == []  # not logged as shadow when override is on

    @pytest.mark.parametrize("val", ["0", "false", "no", "", "off"])
    def test_override_falsy_values_do_not_override(self, monkeypatch, val):
        monkeypatch.setenv(OVERRIDE_ENV, val)
        builtins = {"sqlite": ("trellis.stores.sqlite.graph", "SQLiteGraphStore")}
        merged, shadowed = merge_with_builtins(
            "trellis.stores.graph",
            builtins,
            specs=[self._spec("sqlite", "evil.pkg", "EvilStore")],
        )
        assert merged["sqlite"] == ("trellis.stores.sqlite.graph", "SQLiteGraphStore")
        assert shadowed == ["sqlite"]


class TestStoreBackendGroups:
    def test_returns_all_six(self):
        groups = store_backend_groups()
        assert groups == (
            "trellis.stores.trace",
            "trellis.stores.document",
            "trellis.stores.graph",
            "trellis.stores.vector",
            "trellis.stores.event_log",
            "trellis.stores.blob",
        )


class TestLoadClass:
    def test_resolves_real_class(self):
        # Use a module we know exists and can import.
        spec = PluginSpec(
            group="trellis.stores.graph",
            name="real",
            value="trellis.core.base:TrellisModel",
            module="trellis.core.base",
            attr="TrellisModel",
        )
        cls = load_class(spec)
        assert cls is not None
        assert cls.__name__ == "TrellisModel"

    def test_missing_module_returns_none(self):
        spec = PluginSpec(
            group="trellis.stores.graph",
            name="ghost",
            value="pkg.not.real:Thing",
            module="pkg.not.real",
            attr="Thing",
        )
        assert load_class(spec) is None

    def test_missing_attr_returns_none(self):
        spec = PluginSpec(
            group="trellis.stores.graph",
            name="ghost",
            value="trellis.core.base:NotATrellisClass",
            module="trellis.core.base",
            attr="NotATrellisClass",
        )
        assert load_class(spec) is None
