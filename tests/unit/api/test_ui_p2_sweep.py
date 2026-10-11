"""P2 UI sweep (docs/handoff review u2-ui-quality.md): tables, time, naming,
accessibility, build footer. Each test below pins one finding that was still
live on origin/main at 9868d712, re-verified against the current file (several
other P2 findings from the same review -- e.g. P0-2's Tags column -- were
already fixed by a merged PR and are not retested here).
"""

from __future__ import annotations

import re
from pathlib import Path

import trellis_api

INDEX_HTML = Path(trellis_api.__file__).parent / "static" / "index.html"


def _page() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Item 3 -- one UTC-labelled time-renderer family
# ---------------------------------------------------------------------------


def test_only_one_absolute_time_renderer_family_exists() -> None:
    """fmtDate (local, unlabelled) and fmtDateUtc ("not measured" variant)
    must be gone -- fmtTime() is the only absolute-time renderer now, and it
    always carries a zone label (see the dedicated zone-label test)."""
    page = _page()
    assert re.search(r"\bfunction fmtDate\(", page) is None
    assert re.search(r"\bfunction fmtUtc\(", page) is None
    assert re.search(r"\bfunction fmtDateUtc\(", page) is None
    assert re.search(r"\bfunction fmtTime\(", page) is not None
    # No call site references a renderer that no longer exists.
    assert re.search(r"[^\w]fmtDate\(", page) is None
    assert re.search(r"[^\w]fmtUtc\(", page) is None
    assert re.search(r"[^\w]fmtDateUtc\(", page) is None


def test_fmt_time_always_labels_utc() -> None:
    page = _page()
    found = re.search(
        r"function fmtTime\(iso[^)]*\)\s*\{(.*?)\n        \}\n", page, re.DOTALL
    )
    assert found is not None
    body = found.group(1)
    assert "toLocaleString" not in body
    assert "UTC" in body


def test_relative_time_cells_carry_the_raw_iso_instant_as_their_title() -> None:
    """fmtTimeCell() is the one helper that pairs a relative "3h ago" with
    an absolute tooltip; the tooltip is the raw ISO instant (already
    zone-explicit via its Z/offset suffix), not a second reformatted copy."""
    page = _page()
    found = re.search(
        r"function fmtTimeCell\(iso\)\s*\{(.*?)\n        \}\n", page, re.DOTALL
    )
    assert found is not None
    body = found.group(1)
    assert "title=" in body
    assert "escHtml(iso)" in body
    assert "timeAgo(iso)" in body


# ---------------------------------------------------------------------------
# Item 2 -- values: ?? instead of || where 0 is legitimate
# ---------------------------------------------------------------------------


def test_pack_budget_fields_use_nullish_coalescing_not_or() -> None:
    """A pack with budget_max_items=0 (an explicit, legitimate cap) must not
    render as "-"; `|| '-'` conflates 0 with null/undefined, `?? '-'` does
    not (P2, docs/handoff review u2-ui-quality.md)."""
    page = _page()
    assert "payload.budget_max_items ?? '-'" in page
    assert "payload.budget_max_tokens ?? '-'" in page
    assert "payload.budget_max_items || '-'" not in page
    assert "payload.budget_max_tokens || '-'" not in page


def test_feedback_rating_badge_reads_the_backend_success_field() -> None:
    """``PackFeedback.to_event_payload()`` always emits an explicit
    ``success`` (SUCCESS_RATING_THRESHOLD-derived server-side); the badge
    must prefer it over recomputing `rating > 0`, which disagreed with the
    backend's own threshold (0.5) for a rating in (0, 0.5)."""
    page = _page()
    found = re.search(r"fp\.success[^\n]*badge-ok[^\n]*badge-err", page)
    assert found is not None, "feedback badge does not branch on fp.success"


# ---------------------------------------------------------------------------
# Item 2 -- sectioned packs / holdout
# ---------------------------------------------------------------------------


def test_sectioned_pack_gets_an_unattributable_badge() -> None:
    page = _page()
    assert "unattributable (sectioned)" in page
    assert "pack.sectioned" in page


def test_holdout_blinded_renders_a_note() -> None:
    page = _page()
    assert "holdout_blinded" in page


# ---------------------------------------------------------------------------
# Item 4 -- build footer
# ---------------------------------------------------------------------------


def test_sidebar_footer_calls_it_api_build_not_backend() -> None:
    page = _page()
    assert "API build" in page
    assert 'title="Backend version"' not in page


def test_sidebar_footer_surfaces_stale_or_unidentified_build() -> None:
    """GET /api/version's write_provenance carries stamp_stale (a stale
    editable install) and commit: null + version "fallback-version" (an
    image built without make docker-build) -- both must read as an honest
    warning in the footer, not silently as a normal commit/version."""
    page = _page()
    assert "stamp_stale" in page
    assert "fallback-version" in page


# ---------------------------------------------------------------------------
# Item 5 -- toasts
# ---------------------------------------------------------------------------


def test_toast_region_is_a_dedicated_aria_live_element_not_error() -> None:
    """A toast must never reuse #error -- #error is a static connectivity
    banner with no live-region semantics, and the two collided whenever a
    toast fired while a connectivity error was showing."""
    page = _page()
    assert re.search(r'<div id="toast-region"[^>]*aria-live="polite"', page) is not None


def test_toast_review_does_not_touch_the_error_element() -> None:
    page = _page()
    found = re.search(
        r"function toastReview\(msg, ok\)\s*\{(.*?)\n        \}\n", page, re.DOTALL
    )
    assert found is not None
    body = found.group(1)
    assert "getElementById('error')" not in body
    assert "toast-region" in body


def test_toast_has_a_close_button_and_pauses_on_hover() -> None:
    """A terminal-action toast must not auto-hide before it can be read --
    it pauses its timer on hover/focus and offers an explicit dismiss."""
    page = _page()
    found = re.search(
        r"function toastReview\(msg, ok\)\s*\{(.*?)\n        \}\n", page, re.DOTALL
    )
    assert found is not None
    body = found.group(1)
    assert "mouseenter" in body
    assert "aria-label" in body


# ---------------------------------------------------------------------------
# Item 6 -- naming
# ---------------------------------------------------------------------------


def test_nav_calls_the_tab_effectiveness_not_evolution() -> None:
    page = _page()
    assert ">Effectiveness<" in page
    assert ">Evolution<" not in page


def test_self_learning_is_never_introduced() -> None:
    assert "self-learning" not in _page().lower()


# ---------------------------------------------------------------------------
# Item 9 -- welcome banner
# ---------------------------------------------------------------------------


def test_welcome_banner_requires_a_green_readyz_not_only_zero_counts() -> None:
    """Zero counts alone cannot tell "genuinely empty" apart from "a store
    is unreadable" -- the banner must also check /readyz before claiming
    the deployment is an empty, ready-to-load greenfield."""
    page = _page()
    found = re.search(
        r"async function init\(\)\s*\{(.*?)\n        \}\n", page, re.DOTALL
    )
    assert found is not None
    body = found.group(1)
    assert "/readyz" in body


# ---------------------------------------------------------------------------
# Item 1 -- tables: shared footer, real pagination, named not-found, server
# domain counts
# ---------------------------------------------------------------------------


def test_one_shared_cursor_pager_family_backs_traces_events_and_packs() -> None:
    """Traces, Events and Packs each get real "newer/older" pagination from
    one shared helper family, not three bespoke implementations."""
    page = _page()
    assert re.search(r"\bfunction makeCursorPager\(\)", page) is not None
    assert re.search(r"\bfunction renderCursorPagerFooter\(", page) is not None
    assert "const tracePager = makeCursorPager()" in page
    assert "const eventPager = makeCursorPager()" in page
    assert "const packPager = makeCursorPager()" in page
    for prefix in ("trace", "event", "pack"):
        assert f'id="{prefix}-pagination"' in page
        assert f"renderCursorPagerFooter('{prefix}', {prefix}Pager" in page


def test_not_found_detail_names_the_id_never_renders_blank() -> None:
    """A trace/document/pack detail view that can't find its id must say so
    by name, not render an empty container."""
    page = _page()
    assert 'Trace <span class="mono">${escHtml(traceId)}</span> was not found.' in page
    assert 'Document <span class="mono">${escHtml(docId)}</span> was not found.' in page
    assert re.search(r"\? `Pack \$\{packId\} was not found\.`", page) is not None


def test_event_not_found_names_the_loaded_window_not_just_the_id() -> None:
    """Events page by a cursor window, not an id lookup, so a miss must say
    which window was searched -- an id-shaped message alone would be
    misleading (the id might exist outside the loaded window)."""
    page = _page()
    found = re.search(
        r'Event <span class="mono">\$\{escHtml\(eventId\)\}</span>'
        r" was not found in \$\{windowDesc\}",
        page,
    )
    assert found is not None


def test_traces_domain_filter_is_populated_from_a_server_side_count() -> None:
    """The domain dropdown must reflect every domain in the whole store, not
    just the domains visible on the currently-loaded page of traces."""
    page = _page()
    assert "/traces/domains" in page
    assert re.search(r"\bfunction loadTraceDomains\(\)", page) is not None


# ---------------------------------------------------------------------------
# Item 7 -- help text for config-like controls
# ---------------------------------------------------------------------------


def test_metrics_days_and_group_by_controls_get_a_description() -> None:
    page = _page()
    found = re.search(
        r'<p class="field-hint">\s*Time window picks how far back the scan goes',
        page,
    )
    assert found is not None


def test_graph_depth_and_sort_controls_get_a_description() -> None:
    page = _page()
    found = re.search(
        r'<p class="field-hint">\s*Depth controls how many relationship hops',
        page,
    )
    assert found is not None


# ---------------------------------------------------------------------------
# Item 8 -- accessibility
# ---------------------------------------------------------------------------


def test_dashboard_stat_card_failure_renders_in_the_card_not_the_error_line() -> None:
    """A /stats fetch failure must not blank the whole Dashboard via #error
    -- it has to render inside #stats-cards, and a /health failure likewise
    inside #health-rows, independently of each other."""
    page = _page()
    found = re.search(
        r"async function init\(\)\s*\{(.*?)\n        \}\n", page, re.DOTALL
    )
    assert found is not None
    body = found.group(1)
    assert "statsOutcome.ok" in body
    assert "healthOutcome.ok" in body
    assert "Failed to load stats" in body
    assert "Failed to load health checks" in body


def test_clickable_rows_are_keyboard_focusable_and_activatable() -> None:
    """A clickable <tr> must be reachable and operable from the keyboard,
    not only the mouse: tabindex + role for focus, and a keydown handler
    for Enter/Space."""
    page = _page()
    assert (
        re.search(r'<tr class="clickable"[^>]*tabindex="0"[^>]*role="button"', page)
        is not None
    )
    found = re.search(
        r"document\.addEventListener\('keydown', e => \{(.*?)\}\);", page, re.DOTALL
    )
    assert found is not None
    body = found.group(1)
    assert "'Enter'" in body
    assert "' '" in body
    assert "tr.clickable[data-action]" in body


def test_why_no_approve_reason_is_visible_text_not_only_a_hover_title() -> None:
    """The "Why no approve?" explanation must be readable without hovering --
    a sighted mouse user and a keyboard/screen-reader user get the same
    information. Both reason strings already exist inside a hover-only
    ``title="..."`` attribute on origin/main; stripping every such attribute
    must still leave the text behind, proving it also lives in visible
    markup."""
    page = _page()
    without_titles = re.sub(r'title="[^"]*"', "", page)
    assert "deliberately no Approve control here" in without_titles
    assert "deliberately no approve button here" in without_titles


def test_graph_canvas_has_a_text_alternative() -> None:
    page = _page()
    assert 'id="cy" role="img"' in page
    assert 'aria-describedby="cy-text-alt"' in page
    assert 'id="cy-text-alt" class="sr-only"' in page


def test_icon_only_buttons_carry_an_aria_label() -> None:
    page = _page()
    assert re.search(r'id="api-key-toggle"[^>]*aria-label="', page) is not None
    for action in (
        "graphZoom(1.3)",
        "graphZoom(0.7)",
        "graphFit()",
        "graphCenter()",
        "graphClear()",
    ):
        found = re.search(
            r'onclick="' + re.escape(action) + r'"[^>]*aria-label="'
            r"|aria-label=\"[^\"]*\"[^>]*onclick=\"" + re.escape(action) + r'"',
            page,
        )
        assert found is not None, f"missing aria-label near {action}"


def test_search_results_close_control_is_a_real_button() -> None:
    """The search-results close control must be a focusable, keyboard
    activatable <button>, not a <span> with an onclick handler."""
    page = _page()
    assert re.search(r'<span class="srp-close"', page) is None
    assert (
        re.search(r'<button type="button" class="srp-close"[^>]*aria-label=', page)
        is not None
    )
