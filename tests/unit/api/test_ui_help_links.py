"""Every nav tab carries a "?" help link to the matching operator-docs
section on GitHub, and every one of those links resolves.

Before this, an operator who did not already know the CLI/REST surface
behind a tab had no path from the UI into
``docs/agent-guide/operations.md`` -- the doc existed, but nothing pointed
at it from the page an operator actually uses. ``installHelpLinks()`` now
injects one ``helpLinkHtml(anchor, label)`` link into each ``#view-<tab>``
container from a single ``TAB_HELP_ANCHORS`` map, read at page load.

The risk specific to this feature is a silently dead link: GitHub slugifies
a heading into its anchor (lowercase, punctuation stripped, spaces to
hyphens), and a hand-typed anchor that drifts from that slug -- because the
heading text changed, or because the anchor was mistyped -- renders fine
and opens the file at the top, not the section an operator is sent to look
for. This file reimplements GitHub's heading-to-anchor slug rule and checks
every anchor in ``TAB_HELP_ANCHORS`` against the real headings in
``docs/agent-guide/operations.md`` on this branch, not against a hand-typed
expectation that could drift the same way the anchors themselves could.

Static assertions on ``index.html``'s own markup/script and on the doc
file's headings, the same style as ``test_ui_graph_sample_error.py`` -- no
browser, no JS engine, no network fetch of the GitHub page itself.
"""

from __future__ import annotations

import re
from pathlib import Path

import trellis_api

INDEX_HTML = Path(trellis_api.__file__).parent / "static" / "index.html"
REPO_ROOT = Path(trellis_api.__file__).resolve().parents[2]
OPERATIONS_DOC = REPO_ROOT / "docs" / "agent-guide" / "operations.md"

OPERATOR_DOCS_URL = (
    "https://github.com/ronsse/trellis-ai/blob/main/docs/agent-guide/operations.md"
)

# The complete set of real sidebar nav tabs (`data-view="<name>"` entries in
# the `<nav>` list) -- excludes the unrelated `data-view="grouped"` /
# `data-view="flat"` search-view-toggle buttons inside the Traces search bar,
# which share the attribute name but are not tabs.
EXPECTED_TABS = {
    "dashboard",
    "metrics",
    "review",
    "effectiveness",
    "graph",
    "traces",
    "precedents",
    "memories",
    "events",
    "packs",
    "loops",
    "reports",
    "advisories",
    "policies",
    "schedules",
    "tagrules",
    "settings",
}


def _page() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


def _function_body(page: str, name: str) -> str:
    found = re.search(
        r"function " + re.escape(name) + r"\([^)]*\) \{(.*?)\n        \}",
        page,
        re.DOTALL,
    )
    assert found is not None, f"function {name}() not found"
    return found.group(1)


def _tab_help_anchors() -> dict[str, tuple[str, str]]:
    page = _page()
    found = re.search(r"const TAB_HELP_ANCHORS = \{(.*?)\n        \};", page, re.DOTALL)
    assert found is not None, "TAB_HELP_ANCHORS map not found"
    entries = re.findall(r"(\w+): \['([^']+)', '([^']+)'\]", found.group(1))
    assert entries, "TAB_HELP_ANCHORS had no parseable entries"
    return {tab: (anchor, label) for tab, anchor, label in entries}


def _github_slug(heading_text: str) -> str:
    """Reimplements GitHub's heading -> anchor slug rule: lowercase,
    strip everything but word characters/spaces/hyphens, then turn spaces
    into hyphens. Punctuation (backticks, slashes, braces, em dashes,
    parens, ...) is removed outright rather than replaced with a hyphen,
    which is why e.g. a heading using " -- " around removed punctuation
    collapses to a doubled hyphen in its anchor.
    """
    text = heading_text.strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def _doc_heading_anchors() -> set[str]:
    text = OPERATIONS_DOC.read_text(encoding="utf-8")
    headings = re.findall(r"^#{1,4} (.+)$", text, re.MULTILINE)
    assert headings, "no markdown headings found in operations.md"
    return {_github_slug(h) for h in headings}


# ---------------------------------------------------------------------------
# Every real nav tab has exactly one help-link entry, and vice versa.
# ---------------------------------------------------------------------------


def test_every_nav_tab_has_a_help_link_and_no_extra_entries() -> None:
    anchors = _tab_help_anchors()
    assert set(anchors) == EXPECTED_TABS


# ---------------------------------------------------------------------------
# Every anchor resolves to a real heading in the doc, on this branch.
# ---------------------------------------------------------------------------


def test_every_help_link_anchor_resolves_to_a_heading_in_the_doc() -> None:
    doc_anchors = _doc_heading_anchors()
    anchors = _tab_help_anchors()
    dead = {
        tab: anchor
        for tab, (anchor, _label) in anchors.items()
        if anchor not in doc_anchors
    }
    assert not dead, f"anchors with no matching heading: {dead}"


def test_github_slug_matches_known_heading_shapes() -> None:
    # Pins the slug rule itself against headings that each exercise a
    # different kind of punctuation GitHub strips (backticks + slashes,
    # braces, an em dash, parens) -- a regression in `_github_slug` that
    # still happened to pass the two tests above (because, say, it over-
    # strips) would slip through if only the live doc were checked.
    assert _github_slug("`GET /api/v1/loops`") == "get-apiv1loops"
    assert (
        _github_slug("`GET /api/v1/analyze` and `GET /api/v1/analyze/{report}`")
        == "get-apiv1analyze-and-get-apiv1analyzereport"
    )
    assert _github_slug("Review queue (admin scope)") == "review-queue-admin-scope"
    assert (
        _github_slug("Classify rule review — Apply / Revoke ")
        == "classify-rule-review--apply--revoke"
    )


# ---------------------------------------------------------------------------
# Wiring: the map is consumed, the links point at the right file, and open
# in a new tab rather than navigating the operator away from the console.
# ---------------------------------------------------------------------------


def test_install_help_links_reads_the_map_and_runs_before_init() -> None:
    page = _page()
    body = _function_body(page, "installHelpLinks")
    assert "Object.entries(TAB_HELP_ANCHORS)" in body
    assert "helpLinkHtml(anchor, label)" in body
    assert "insertAdjacentHTML('afterbegin'" in body
    tail = page[page.rindex("installHelpLinks();") :]
    assert "init();" in tail


def test_help_link_html_points_at_the_operations_doc_and_opens_a_new_tab() -> None:
    body = _function_body(_page(), "helpLinkHtml")
    assert "OPERATOR_DOCS_URL" in body
    assert 'target="_blank"' in body
    assert 'rel="noopener"' in body
    assert 'class="help-link"' in body


def test_operator_docs_url_points_at_the_real_github_blob_path() -> None:
    page = _page()
    found = re.search(r"const OPERATOR_DOCS_URL =\s*\n?\s*'([^']+)'", page)
    assert found is not None
    assert found.group(1) == OPERATOR_DOCS_URL


def test_help_link_css_class_is_defined() -> None:
    assert re.search(r"\.help-link\s*\{", _page()) is not None
