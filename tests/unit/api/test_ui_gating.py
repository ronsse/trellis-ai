"""Tests for TRELLIS_UI_ENABLED — static UI mount + root redirect gating.

Also covers read-only-mode gating within the mounted page itself: a
read-scoped key used to hard-block on every admin-scoped tab with no
warning, and ``throwIfAuthRefused`` told every 403 caller "this UI needs
an admin-scoped key" even though most of the UI's tabs are read-scoped
(Traces, Memories, Graph, Precedents, Policies and Tag rules for
viewing — see app.py's router scope map). The page now badges the admin-only tabs
once a 403 proves the stored key lacks admin scope, instead of claiming
the whole UI needs one.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import trellis_api
import trellis_api.app as app_module
from tests.ast_rules import assert_hand_read_floor
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


class TestFontsServed:
    """The vendored IBM Plex woff2 files sit under the same ``/ui`` mount
    as the page itself, so they share its posture: unauthenticated, no
    TRELLIS_AUTH_MODE gate. The console's @font-face rules fetch them
    before any API key exists in localStorage, so a gate here would be
    a silent render regression (fallback system font), not an error the
    page could surface."""

    def test_one_font_file_resolves_with_a_font_content_type(self, registry) -> None:
        resp = _client(registry).get("/ui/fonts/ibm-plex-sans-latin-400-normal.woff2")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "font/woff2"

    def test_font_mount_needs_no_api_key(
        self, registry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")
        monkeypatch.setenv("TRELLIS_API_KEY", "does-not-matter")
        resp = _client(registry).get("/ui/fonts/ibm-plex-mono-latin-400-normal.woff2")
        assert resp.status_code == 200


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
# scope map (admin.py, settings.py, api_keys.py and analyze.py require "admin"; the
# default retrieve.py and policies.py routes require "read"; explore.py is
# split — documents and graph history stay "read", its pack-telemetry
# routes (events, packs) carry their own route-level "admin" requirement).
_READ_SCOPE_VIEWS = {
    "graph",
    "traces",
    "precedents",
    "memories",
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
    "events",
    "packs",
    "reports",
    "schedules",
    "keys",
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
        assert "Traces, Memories, Graph, Precedents, Policies and Tag rules" in body

    def test_nav_scope_badge_css_class_defined(self) -> None:
        assert ".sidebar-nav a .nav-scope-badge {" in _page()


class TestAdminScopeErrorRenderingConsistency:
    """Every admin-only panel's catch block dims a 403 (isAdminScopeError)
    instead of reporting it in the same red "Failed to load" text a real
    error gets -- loadFailureColor/loadFailureText already drew this
    distinction for Metrics, Review, Events, Packs, Loops, Reports,
    Advisories, Schedules and Settings. Effectiveness was the one
    holdout: its catch block built its own fixed "Could not load
    effectiveness data: <message>" string, coloured the same way
    whether the key merely lacked admin scope or the API was genuinely
    down (W1D ui-pass4)."""

    def test_effectiveness_panel_dims_a_403_through_the_shared_helpers(self) -> None:
        page = _page()
        found = re.search(
            r"async function loadEvolution\(\) \{(.*?)\n        \}", page, re.DOTALL
        )
        assert found is not None, "loadEvolution() not found in index.html"
        body = found.group(1)
        assert "loadFailureColor(err)" in body
        assert "loadFailureText(err, " in body
        # The regression this closes: a fixed string with no isAdminScopeError
        # branch, read the same for a 403 as for a genuine failure.
        assert "Could not load effectiveness data: ${escHtml(err.message)}" not in body

    def test_shared_failure_helper_population_is_above_a_hand_read_floor(self) -> None:
        # Vacuous-sweep guard for the test above: pins that the shared
        # helpers still have real call sites, not just the one this pass
        # touched. Hand count for this pass: 10 loadFailureColor(err) +
        # 14 loadFailureText(err, ...) sites (Metrics x2 loadFailureText-only
        # via chartEmpty, Review x4 loadFailureText-only via setCountError,
        # plus Events/Packs/Loops/Reports/Advisories/Schedules/Settings/
        # Effectiveness carrying both).
        page = _page()
        assert_hand_read_floor(
            page.count("loadFailureColor(err)"),
            9,
            subject="loadFailureColor(err) call sites",
            hint="hand count for this pass: 10.",
        )
        assert_hand_read_floor(
            page.count("loadFailureText(err, "),
            13,
            subject="loadFailureText(err, ...) call sites",
            hint="hand count for this pass: 14.",
        )

    def test_each_pattern_catches_a_deliberately_defective_subject(self) -> None:
        defective = (
            "async function loadEvolution() {\n"
            "            } catch (err) {\n"
            "                summary.innerHTML = `<p>Could not load effectiveness"
            " data: ${escHtml(err.message)}</p>`;\n"
            "            }\n"
            "        }"
        )
        found = re.search(
            r"async function loadEvolution\(\) \{(.*?)\n        \}",
            defective,
            re.DOTALL,
        )
        assert found is not None
        assert "loadFailureColor(err)" not in found.group(1)
