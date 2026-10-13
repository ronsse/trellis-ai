"""Graph tab no longer depends on a third-party CDN being reachable.

The page used to load cytoscape.js from ``unpkg.com`` at a pinned version;
an outage, a corporate proxy block, or ad/script blocking there (common on
a CDN host whose name reads as a tracker to some blockers) left the Graph
tab's canvas uninitialised and `initGraph()` threw an uncaught
``ReferenceError`` the first time it called the (undefined) `cytoscape`
global, with no visible message. The library is now vendored under
``static/vendor/cytoscape/`` (MIT-licensed, version pinned, licence file
included) and a load-failure is now a visible banner, not a silent dead
tab.
"""

from __future__ import annotations

import re
from pathlib import Path

import trellis_api

STATIC_DIR = Path(trellis_api.__file__).parent / "static"
INDEX_HTML = STATIC_DIR / "index.html"
VENDOR_DIR = STATIC_DIR / "vendor" / "cytoscape"


def _page() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


class TestVendoredAssetsOnDisk:
    def test_cytoscape_bundle_present(self) -> None:
        bundle = VENDOR_DIR / "cytoscape.min.js"
        assert bundle.is_file()
        assert bundle.stat().st_size > 1000

    def test_license_present(self) -> None:
        license_file = VENDOR_DIR / "LICENSE"
        assert license_file.is_file()
        text = license_file.read_text(encoding="utf-8")
        assert "MIT" in text

    def test_license_permits_vendoring(self) -> None:
        text = (VENDOR_DIR / "LICENSE").read_text(encoding="utf-8")
        # MIT grants redistribution rights unconditionally (no copyleft,
        # no field-of-use restriction) as long as the notice ships with it,
        # which it does -- this directory.
        assert "Permission is hereby granted" in text


_CSS_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def _page_without_comments() -> str:
    """The page with CSS/HTML comments stripped.

    The Fonts pass's ``@font-face`` block cites its vendored source --
    ``@fontsource/...`` served from jsdelivr -- in a CSS comment beside
    it, for provenance. That is documentation, not a live reference the
    browser fetches, and is a different claim than this test's: that no
    page asset is ever *loaded* from a third-party CDN at runtime.
    """
    return _HTML_COMMENT_RE.sub("", _CSS_COMMENT_RE.sub("", _page()))


class TestScriptTagPointsLocal:
    def test_no_cdn_reference_for_cytoscape(self) -> None:
        page = _page_without_comments()
        assert "unpkg.com" not in page
        assert "cdn.jsdelivr.net" not in page

    def test_script_tag_loads_vendored_path(self) -> None:
        found = re.search(r'<script src="([^"]*cytoscape[^"]*)"', _page())
        assert found is not None, "no cytoscape <script> tag found"
        src = found.group(1)
        assert src == "vendor/cytoscape/cytoscape.min.js"


class TestLoadFailureIsVisible:
    def test_error_banner_markup_exists(self) -> None:
        page = _page()
        assert 'id="cy-load-error"' in page

    def test_init_graph_guards_missing_global(self) -> None:
        page = _page()
        found = re.search(
            r"function initGraph\(\) \{(.*?)\n        \}", page, re.DOTALL
        )
        assert found is not None
        body = found.group(1)
        assert "typeof cytoscape === 'undefined'" in body
        # The guard must come before the call that would otherwise throw.
        guard_pos = body.index("typeof cytoscape === 'undefined'")
        call_pos = body.index("cy = cytoscape({")
        assert guard_pos < call_pos

    def test_init_graph_shows_banner_and_hides_broken_canvas(self) -> None:
        page = _page()
        found = re.search(
            r"function initGraph\(\) \{(.*?)\n        \}", page, re.DOTALL
        )
        assert found is not None
        body = found.group(1)
        assert "cy-load-error" in body
        assert "return;" in body
