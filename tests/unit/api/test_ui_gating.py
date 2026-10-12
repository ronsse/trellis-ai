"""Tests for TRELLIS_UI_ENABLED — static UI mount + root redirect gating.

Also covers read-only-mode gating within the mounted page itself: a
read-scoped key used to hard-block on every admin-scoped tab with no
warning, and ``throwIfAuthRefused`` told every 403 caller "this UI needs
an admin-scoped key" even though most of the UI's tabs are read-scoped
(Traces, Memories, Events, Packs, Graph, Precedents, Reports, and
Policies for viewing — see app.py's router scope map). The page now
badges the admin-only tabs once a 403 proves the stored key lacks admin
scope, instead of claiming the whole UI needs one.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import trellis_api
import trellis_api.app as app_module
from trellis.errors import ConfigError
from trellis.stores.registry import StoreRegistry
from trellis_api.app import create_app, resolve_ui_enabled

INDEX_HTML = Path(trellis_api.__file__).parent / "static" / "index.html"


def _page() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test with no exposure / auth configuration."""
    for var in (
        "TRELLIS_UI_ENABLED",
        "TRELLIS_OPS_DETAIL",
        "TRELLIS_METRICS_PUBLIC",
        "TRELLIS_AUTH_MODE",
        "TRELLIS_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def registry(tmp_path):
    """Fresh registry bound to the app module for each test."""
    reg = StoreRegistry(stores_dir=tmp_path / "stores")
    app_module._registry = reg
    yield reg
    reg.close()
    app_module._registry = None


def _client(registry) -> TestClient:
    """App over the fixture registry, without running the lifespan
    (which would re-init ``_registry`` from the real config dir)."""
    return TestClient(create_app())


class TestResolveUiEnabled:
    def test_unset_defaults_true(self) -> None:
        assert resolve_ui_enabled() is True

    @pytest.mark.parametrize("raw", ["true", "True", "TRUE", " true "])
    def test_true_case_insensitive(
        self, raw: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_UI_ENABLED", raw)
        assert resolve_ui_enabled() is True

    @pytest.mark.parametrize("raw", ["false", "False", "FALSE", " false "])
    def test_false_case_insensitive(
        self, raw: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_UI_ENABLED", raw)
        assert resolve_ui_enabled() is False

    @pytest.mark.parametrize("raw", ["1", "0", "yes", "no", "on", "off", "bogus"])
    def test_anything_else_raises_loudly(
        self, raw: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_UI_ENABLED", raw)
        with pytest.raises(ConfigError, match="TRELLIS_UI_ENABLED"):
            resolve_ui_enabled()


class TestUiEnabled:
    """Default posture — back-compat, nothing changes."""

    def test_root_redirects_to_ui(self, registry) -> None:
        resp = _client(registry).get("/", follow_redirects=False)
        assert resp.status_code == 307
        assert resp.headers["location"] == "/ui/"

    def test_ui_index_served(self, registry) -> None:
        resp = _client(registry).get("/ui/")
        assert resp.status_code == 200
        assert "Trellis" in resp.text

    def test_explicit_true_same_as_default(
        self, registry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_UI_ENABLED", "true")
        client = _client(registry)
        assert client.get("/", follow_redirects=False).headers["location"] == "/ui/"
        assert client.get("/ui/").status_code == 200


class TestUiDisabled:
    @pytest.fixture(autouse=True)
    def _disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TRELLIS_UI_ENABLED", "false")

    def test_ui_not_mounted(self, registry) -> None:
        assert _client(registry).get("/ui/").status_code == 404

    def test_root_redirects_to_version(self, registry) -> None:
        resp = _client(registry).get("/", follow_redirects=False)
        assert resp.status_code == 307
        assert resp.headers["location"] == "/api/version"

    def test_root_redirect_lands_on_version_handshake(self, registry) -> None:
        resp = _client(registry).get("/")
        assert resp.status_code == 200
        assert "api_version" in resp.json()

    def test_api_routes_unaffected(self, registry) -> None:
        # Auth defaults to "off" here — the UI toggle must not change
        # the API surface, only the static page.
        assert _client(registry).get("/api/v1/stats").status_code == 200


class TestStartupValidation:
    """Bad exposure-env values must crash create_app, not lurk."""

    def test_invalid_ui_enabled_crashes_create_app(
        self, registry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_UI_ENABLED", "maybe")
        with pytest.raises(ConfigError, match="TRELLIS_UI_ENABLED"):
            create_app()

    def test_invalid_ops_detail_crashes_create_app(
        self, registry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_OPS_DETAIL", "everyone")
        with pytest.raises(ConfigError, match="TRELLIS_OPS_DETAIL"):
            create_app()

    def test_invalid_metrics_public_crashes_create_app(
        self, registry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_METRICS_PUBLIC", "totally")
        with pytest.raises(ConfigError, match="TRELLIS_METRICS_PUBLIC"):
            create_app()


# Routes that back each nav tab, cross-referenced against app.py's router ->
# scope map (admin.py and settings.py routers require "admin"; retrieve.py,
# explore.py, analyze.py and the default policies.py route require "read").
_READ_SCOPE_VIEWS = {
    "graph",
    "traces",
    "precedents",
    "memories",
    "events",
    "packs",
    "reports",
    "policies",
}
_ADMIN_SCOPE_VIEWS = {
    "dashboard",
    "metrics",
    "review",
    "effectiveness",
    "loops",
    "advisories",
    "settings",
}


def _admin_only_views_literal(page: str) -> list[str]:
    found = re.search(r"const ADMIN_ONLY_VIEWS = \[(.*?)\];", page)
    assert found is not None, "ADMIN_ONLY_VIEWS literal not found in index.html"
    return re.findall(r"'([^']+)'", found.group(1))


class TestReadOnlyModeGating:
    """The nav/key-banner behaviour for a key that lacks admin scope."""

    def test_admin_only_views_matches_route_scopes(self) -> None:
        """Every view in the client-side admin gate really is admin-scoped,
        and no read-scoped view is wrongly included."""
        gated = set(_admin_only_views_literal(_page()))
        assert gated == _ADMIN_SCOPE_VIEWS
        assert gated.isdisjoint(_READ_SCOPE_VIEWS)

    def test_apply_scope_badges_only_targets_admin_views(self) -> None:
        page = _page()
        assert "function applyScopeBadges()" in page
        assert "ADMIN_ONLY_VIEWS.includes(link.dataset.view)" in page

    def test_mark_key_non_admin_exists_and_is_idempotent(self) -> None:
        page = _page()
        found = re.search(
            r"function markKeyNonAdmin\(\) \{(.*?)\n        \}", page, re.DOTALL
        )
        assert found is not None
        body = found.group(1)
        # Guards against re-running applyScopeBadges() on every subsequent
        # 403 once the key is already known to be non-admin.
        assert "if (keyIsNonAdmin) return;" in body

    def test_throw_if_auth_refused_marks_key_non_admin_on_403(self) -> None:
        page = _page()
        found = re.search(
            r"if \(res\.status === 403\) \{(.*?)\n            \}", page, re.DOTALL
        )
        assert found is not None, "403 branch of throwIfAuthRefused not found"
        assert "markKeyNonAdmin();" in found.group(1)

    def test_403_message_no_longer_overclaims_whole_ui_needs_admin(self) -> None:
        """Defect: every 403 (e.g. a read-scoped key hitting one admin-only
        tab) used to tell the user the whole UI needs an admin-scoped key,
        which is false for the read-scoped majority of tabs."""
        page = _page()
        assert "This UI needs an admin-scoped key" not in page

    def test_dashboard_403_message_names_working_tabs(self) -> None:
        """The dashboard's own 403 catch (it calls two admin-only routes)
        now points at the tabs that still work instead of reading as a
        dead end."""
        page = _page()
        found = re.search(
            r"async function init\(\) \{(.*?)\n        \}", page, re.DOTALL
        )
        assert found is not None
        body = found.group(1)
        assert "Traces, Memories, Events, Packs, Graph, Precedents, Reports" in body

    def test_nav_scope_badge_css_class_defined(self) -> None:
        assert ".sidebar-nav a .nav-scope-badge {" in _page()
