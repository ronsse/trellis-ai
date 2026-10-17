"""Every ``<button>`` in the console carries a ``btn``-prefixed class.

Before this pass the button look was split across two rules at tied
specificity -- ``button.btn-danger`` (declared earlier) and
``.search-bar button, button.btn`` (declared later) -- so an element
carrying both ``btn`` and ``btn-danger`` had its background/colour
decided by which rule happened to be written second, not by which class
a reader would expect to win. ``.search-bar button`` was also a silent
safety net: any bare ``<button>`` dropped inside a ``.search-bar`` div
picked up button styling for free, with no class required.

This pass merges everything into plain, single-class selectors --
``.btn`` (base, also the primary look), ``.btn-primary`` (an explicit
alias of the same look), ``.btn-danger`` (now filled: ``--danger``
background, ``--text-inverse`` text), ``.btn-ghost``, ``.btn-sm`` --
declared base-then-modifiers so equal-specificity source order resolves
correctly, and removes the ``.search-bar button`` net. That net's
removal is exactly what this guard exists to protect: every button that
relied on it needed an explicit ``class="btn"`` added at its own site,
and a future bare ``<button>`` dropped into a ``.search-bar`` div would
otherwise render unstyled with no test ever noticing.

Not every ``<button>`` is a push-button in the ``.btn`` sense, so a
documented allowance list covers the genuine exceptions: two segmented
toggle groups (layout selector, search-results view toggle), one
dedicated icon-toolbar component (graph pan/zoom controls), one
dedicated icon close control, the topbar sidebar toggle (out of scope
this pass), and the settings page's real ``role="switch"`` control.
Each is governed by its own container selector or ARIA role, never by
``.btn``, and each line below names that selector/role as the reason.
"""

from __future__ import annotations

import re
from pathlib import Path

import trellis_api
from tests.ast_rules import assert_hand_read_floor

INDEX_HTML = Path(trellis_api.__file__).parent / "static" / "index.html"

_BUTTON_TAG_RE = re.compile(r"<button\b.*?>", re.DOTALL)
_CLASS_ATTR_RE = re.compile(r'class=([\'"])(.*?)\1', re.DOTALL)
_BTN_TOKEN_RE = re.compile(r"(?<![\w-])btn(-[\w-]+)?(?![\w-])")
_JS_LINE_COMMENT_RE = re.compile(r"^\s*//")

# Each marker is a substring unique to one <button> tag's opening markup.
# None of these sites is a push-button in the .btn sense, so none needs
# a btn-prefixed class -- each is governed by its own selector/role
# instead, named in the reason.
_ALLOWED_NO_BTN_CLASS = {
    'id="nav-toggle"': "topbar sidebar toggle -- out of scope (topbar) "
    "this pass; governed by .rail-btn.",
    'data-layout="cose"': "layout-selector segmented toggle, site 1/4; "
    "governed by .layout-selector button.",
    'data-layout="concentric"': "layout-selector segmented toggle, site "
    "2/4; governed by .layout-selector button.",
    'data-layout="breadthfirst"': "layout-selector segmented toggle, "
    "site 3/4; governed by .layout-selector button.",
    'data-layout="circle"': "layout-selector segmented toggle, site "
    "4/4; governed by .layout-selector button.",
    'data-view="grouped"': "search-results view toggle, site 1/2; "
    "governed by .view-toggle button.",
    'data-view="flat"': "search-results view toggle, site 2/2; "
    "governed by .view-toggle button.",
    'class="srp-close"': "search-results-panel close control; governed "
    "by .search-results-panel .srp-close.",
    "graphZoom(1.3)": "graph icon-toolbar control, site 1/5; governed "
    "by .graph-toolbar button.",
    "graphZoom(0.7)": "graph icon-toolbar control, site 2/5; governed "
    "by .graph-toolbar button.",
    "graphFit()": "graph icon-toolbar control, site 3/5; governed by "
    ".graph-toolbar button.",
    "graphCenter()": "graph icon-toolbar control, site 4/5; governed by "
    ".graph-toolbar button.",
    "graphClear()": "graph icon-toolbar control, site 5/5; governed by "
    ".graph-toolbar button.",
    'role="switch"': "a real switch control (role=switch + "
    "aria-checked), not a push-button -- the brief's own named "
    "exception for toggles.",
}


def _page() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


def _strip_js_line_comments(text: str) -> str:
    """Blank out ``//``-style JS comment lines.

    One of this file's own accessibility notes reads "...the way a real
    ``<button>`` does" -- literal text that would otherwise be mistaken
    for a live, unclassed tag by the regex below.
    """
    return "\n".join(
        "" if _JS_LINE_COMMENT_RE.match(line) else line for line in text.splitlines()
    )


def _button_tags(page: str) -> list[str]:
    return _BUTTON_TAG_RE.findall(_strip_js_line_comments(page))


def _has_btn_class(tag: str) -> bool:
    match = _CLASS_ATTR_RE.search(tag)
    if not match:
        return False
    return _BTN_TOKEN_RE.search(match.group(2)) is not None


def test_no_button_lacks_a_btn_class_or_a_documented_exception() -> None:
    unexplained = [
        tag[:160]
        for tag in _button_tags(_page())
        if not _has_btn_class(tag)
        and not any(marker in tag for marker in _ALLOWED_NO_BTN_CLASS)
    ]
    assert unexplained == [], (
        "<button> site(s) with neither a btn-prefixed class nor a "
        f"documented exception: {unexplained}"
    )


def test_allowance_list_sites_still_exist_exactly_once() -> None:
    """An allowance marker that stops matching is scope creep in the
    other direction: it would silently let a *different*, unrelated
    unclassed button through once the markup it used to pin moves on."""
    tags = _button_tags(_page())
    for marker in _ALLOWED_NO_BTN_CLASS:
        matches = [tag for tag in tags if marker in tag]
        assert len(matches) == 1, (
            f"allowance marker {marker!r} matched {len(matches)} "
            "<button> site(s), expected exactly 1 -- update the "
            "allowance list in this file"
        )


def test_btn_classed_button_population_is_above_a_hand_read_floor() -> None:
    n_btn = sum(1 for tag in _button_tags(_page()) if _has_btn_class(tag))
    assert_hand_read_floor(
        n_btn,
        95,
        subject="<button> site(s) carrying a btn-prefixed class",
        hint="hand count for this pass: 102 (81 pre-existing sites plus "
        "the 21 .search-bar buttons that needed an explicit class once "
        "the .search-bar button fallback selector was removed).",
    )


def test_each_pattern_catches_a_deliberately_defective_subject() -> None:
    defective = '<button onclick="doThing()">Do thing</button>'
    tag = _BUTTON_TAG_RE.search(defective)
    assert tag is not None
    assert _has_btn_class(tag.group(0)) is False

    fixed = '<button class="btn" onclick="doThing()">Do thing</button>'
    fixed_tag = _BUTTON_TAG_RE.search(fixed)
    assert fixed_tag is not None
    assert _has_btn_class(fixed_tag.group(0)) is True

    commented = "        // the way a real <button> does this\n"
    assert _button_tags(commented) == []
