"""Tests for the plugin diagnostic report used by ``check-plugins``."""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import patch

from trellis.plugins.diagnostic import PluginEntry, PluginReport, collect_plugin_report


@dataclass
class _FakeDist:
    name: str | None = None
    version: str | None = None


@dataclass
class _FakeEntryPoint:
    name: str
    value: str
    dist: _FakeDist | None = None


class TestPluginReport:
    def test_exit_code_clean(self):
        r = PluginReport(plugins=[])
        assert r.exit_code == 0

    def test_exit_code_warn_on_shadow(self):
        r = PluginReport(
            plugins=[
                PluginEntry(
                    group="trellis.stores.graph",
                    name="sqlite",
                    value="evil:Store",
                    distribution=None,
                    distribution_version=None,
                    status="SHADOWED",
                    reason="builtin wins",
                ),
            ]
        )
        assert r.exit_code == 1

    def test_exit_code_blocked_wins_over_shadow(self):
        r = PluginReport(
            plugins=[
                PluginEntry(
                    group="trellis.stores.graph",
                    name="sqlite",
                    value="evil:Store",
                    distribution=None,
                    distribution_version=None,
                    status="SHADOWED",
                    reason="",
                ),
                PluginEntry(
                    group="trellis.stores.vector",
                    name="broken",
                    value="nope:NotFound",
                    distribution=None,
                    distribution_version=None,
                    status="BLOCKED",
                    reason="import failed",
                ),
            ]
        )
        assert r.exit_code == 2


class TestCollectPluginReport:
    def test_empty_report_when_no_plugins(self):
        def fake(*, group: str):
            return []

        with patch("trellis.plugins.loader.entry_points", side_effect=fake):
            report = collect_plugin_report()
        # No plugins discovered anywhere, but every group was checked.
        assert report.plugins == []
        assert len(report.groups_checked) > 0
        assert report.exit_code == 0

    def test_loaded_plugin(self):
        """A plugin pointing at a real class loads cleanly."""
        eps = {
            "trellis.classifiers": [
                _FakeEntryPoint(
                    name="probe",
                    value="trellis.core.base:TrellisModel",
                    dist=_FakeDist(name="test", version="0.1"),
                ),
            ],
        }

        def fake(*, group: str):
            return eps.get(group, [])

        with patch("trellis.plugins.loader.entry_points", side_effect=fake):
            report = collect_plugin_report()

        statuses = {
            p.name: p for p in report.plugins if p.group == "trellis.classifiers"
        }
        assert "probe" in statuses
        assert statuses["probe"].status == "LOADED"

    def test_shadowed_plugin(self):
        eps = {
            "trellis.stores.graph": [
                _FakeEntryPoint(
                    name="sqlite",
                    value="evil_pkg.stores:EvilStore",
                    dist=_FakeDist(name="evil-pkg", version="9.9"),
                ),
            ],
        }

        def fake(*, group: str):
            return eps.get(group, [])

        with patch("trellis.plugins.loader.entry_points", side_effect=fake):
            report = collect_plugin_report()

        entry = next(
            p
            for p in report.plugins
            if p.group == "trellis.stores.graph" and p.name == "sqlite"
        )
        assert entry.status == "SHADOWED"
        assert report.exit_code == 1

    def test_blocked_plugin(self):
        eps = {
            "trellis.rerankers": [
                _FakeEntryPoint(
                    name="broken",
                    value="pkg.not.real:NotAThing",
                    dist=_FakeDist(name="broken", version="0.1"),
                ),
            ],
        }

        def fake(*, group: str):
            return eps.get(group, [])

        with patch("trellis.plugins.loader.entry_points", side_effect=fake):
            report = collect_plugin_report()

        entry = next(
            p
            for p in report.plugins
            if p.group == "trellis.rerankers" and p.name == "broken"
        )
        assert entry.status == "BLOCKED"
        assert report.exit_code == 2
