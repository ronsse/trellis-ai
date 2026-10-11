"""The keyboard-shortcut hint must match what the keys actually do.

``views`` used to be a hand-maintained array that duplicated the nav's
``data-view`` order; past the tenth entry it had drifted (``loops`` and
``reports`` traded places with ``advisories``/``policies``/``settings``
relative to the actual sidebar). No live shortcut depended on that tail
--  digits only ever covered the first ten views -- so the drift was
silent, but it meant any future shortcut keyed by index past 9 would have
opened the wrong tab. ``views`` is now derived from the nav's DOM order
directly, the on-screen key hints are generated from that same order (so
hint and behaviour cannot diverge again), and five more tabs
(Loops/Reports/Advisories/Policies/Settings) now have an ``Alt+<n>``
shortcut and a matching hint.
"""

from __future__ import annotations

import re
from pathlib import Path

import trellis_api

INDEX_HTML = Path(trellis_api.__file__).parent / "static" / "index.html"

# The nav's actual DOM order (verified against the <aside class="sidebar">
# markup) -- used only to sanity-check that the page still has exactly
# this many distinct views wired up; the page itself derives its own
# `views` from the DOM rather than from this list. 16, not 15: the
# governed tag/domain candidate review tab (`tagrules`) is a 16th nav
# entry added after this count was first set.
_EXPECTED_VIEW_COUNT = 16


def _page() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


def _nav_data_views(page: str) -> list[str]:
    nav_section = re.search(r'<nav class="sidebar-nav">(.*?)</nav>', page, re.DOTALL)
    assert nav_section is not None
    return re.findall(r'data-view="([^"]+)"', nav_section.group(1))


class TestViewsDerivedFromNav:
    def test_no_hand_maintained_views_array(self) -> None:
        """Regression guard: the old defect was a literal array of view
        names that could silently drift from the nav's real order."""
        assert "const views = ['dashboard'" not in _page()
        assert not re.search(r"const views = \[\s*'", _page())

    def test_views_is_derived_from_nav_links(self) -> None:
        page = _page()
        assert (
            "const navLinks = Array.from("
            "document.querySelectorAll('nav a[data-view]'));"
        ) in page
        assert "const views = navLinks.map(a => a.dataset.view);" in page

    def test_nav_has_sixteen_distinct_views_with_no_duplicates(self) -> None:
        names = _nav_data_views(_page())
        assert len(names) == _EXPECTED_VIEW_COUNT
        assert len(set(names)) == _EXPECTED_VIEW_COUNT


class TestKeyHintsGeneratedFromSameOrder:
    def test_hint_generation_loop_present(self) -> None:
        page = _page()
        assert (
            "idx < 9 ? String(idx + 1) : idx === 9 ? '0' : 'Alt+' + (idx - 9)"
        ) in page

    def test_alt_digit_handler_uses_matching_offset(self) -> None:
        page = _page()
        found = re.search(
            r"if \(e\.altKey && e\.key >= '1' && e\.key <= '9'\) \{"
            r"(.*?)\n            \}",
            page,
            re.DOTALL,
        )
        assert found is not None, "Alt+digit keydown branch not found"
        body = found.group(1)
        assert "9 + parseInt(e.key, 10)" in body
        assert "views[idx]" in body

    def test_shortcuts_hint_advertises_alt_range(self) -> None:
        page = _page()
        assert "<kbd>Alt</kbd>+<kbd>1</kbd>-<kbd>5</kbd>" in page
