"""Integration tests: plugin discovery inside StoreRegistry."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import pytest

from trellis.stores.registry import StoreRegistry, _reset_backend_cache


@dataclass
class _FakeEntryPoint:
    name: str
    value: str
    dist: object | None = None


@pytest.fixture(autouse=True)
def _clear_cache():
    """StoreRegistry caches merged backends per store_type — reset so
    each test sees a fresh view of the mocked entry points."""
    _reset_backend_cache()
    yield
    _reset_backend_cache()


class TestStoreRegistryPluginDiscovery:
    def test_unknown_backend_still_raises(self, tmp_path: Path):
        """Smoke test: no plugins installed → unknown backend fails."""
        registry = StoreRegistry(
            config={"graph": {"backend": "absent"}},
            stores_dir=tmp_path / "stores",
        )
        with pytest.raises(ValueError, match="Unknown backend 'absent'"):
            _ = registry.graph_store

    def test_builtin_unaffected_by_absent_plugins(self, tmp_path: Path):
        """Existing SQLite behavior is unchanged when no plugins are present."""
        registry = StoreRegistry(stores_dir=tmp_path / "stores")
        # Build the sqlite graph store — default config.
        store = registry.graph_store
        assert store is not None

    def test_plugin_backend_discoverable(self, tmp_path: Path, monkeypatch):
        """A plugin-advertised graph backend is reachable by name."""
        # Install a fake graph-store class on an importable module path.
        fake_module_name = "tests.unit.plugins._stub_graph_store"

        class StubGraphStore:
            def __init__(self, **params):
                self._params = params

            def close(self):
                pass

        import sys
        import types

        mod = types.ModuleType(fake_module_name)
        mod.StubGraphStore = StubGraphStore  # type: ignore[attr-defined]
        sys.modules[fake_module_name] = mod

        def fake_eps(*, group: str):
            if group == "trellis.stores.graph":
                return [
                    _FakeEntryPoint(
                        name="stub_backend",
                        value=f"{fake_module_name}:StubGraphStore",
                    ),
                ]
            return []

        with patch("trellis.plugins.loader.entry_points", side_effect=fake_eps):
            _reset_backend_cache()
            registry = StoreRegistry(
                config={"graph": {"backend": "stub_backend"}},
                stores_dir=tmp_path / "stores",
            )
            store = registry.graph_store
            assert isinstance(store, StubGraphStore)

    def test_plugin_cannot_shadow_builtin_by_default(self, tmp_path: Path):
        """Plugin named ``sqlite`` must not replace the built-in."""
        fake_module_name = "tests.unit.plugins._stub_sqlite_shadow"

        class ShadowStore:
            def __init__(self, **params):
                self.identity = "shadow"

            def close(self):
                pass

        import sys
        import types

        mod = types.ModuleType(fake_module_name)
        mod.ShadowStore = ShadowStore  # type: ignore[attr-defined]
        sys.modules[fake_module_name] = mod

        def fake_eps(*, group: str):
            if group == "trellis.stores.graph":
                return [
                    _FakeEntryPoint(
                        name="sqlite",  # colliding name
                        value=f"{fake_module_name}:ShadowStore",
                    ),
                ]
            return []

        with patch("trellis.plugins.loader.entry_points", side_effect=fake_eps):
            _reset_backend_cache()
            registry = StoreRegistry(stores_dir=tmp_path / "stores")
            store = registry.graph_store
            # Built-in SQLite class, not the shadow.
            assert not hasattr(store, "identity")

    def test_plugin_shadow_with_override_env(self, tmp_path: Path, monkeypatch):
        """Operator opt-in via TRELLIS_PLUGIN_OVERRIDE allows shadowing."""
        monkeypatch.setenv("TRELLIS_PLUGIN_OVERRIDE", "1")

        fake_module_name = "tests.unit.plugins._stub_sqlite_override"

        class OverrideStore:
            def __init__(self, **params):
                self.identity = "override"

            def close(self):
                pass

        import sys
        import types

        mod = types.ModuleType(fake_module_name)
        mod.OverrideStore = OverrideStore  # type: ignore[attr-defined]
        sys.modules[fake_module_name] = mod

        def fake_eps(*, group: str):
            if group == "trellis.stores.graph":
                return [
                    _FakeEntryPoint(
                        name="sqlite",
                        value=f"{fake_module_name}:OverrideStore",
                    ),
                ]
            return []

        with patch("trellis.plugins.loader.entry_points", side_effect=fake_eps):
            _reset_backend_cache()
            registry = StoreRegistry(stores_dir=tmp_path / "stores")
            store = registry.graph_store
            assert isinstance(store, OverrideStore)
            assert store.identity == "override"
