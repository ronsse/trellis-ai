"""Review nav entry shows a pending-items count badge, no polling.

The Review tab already has four per-section counts
(``review-proposals-count`` and siblings), but nothing on the nav entry
itself told an operator there was work waiting before they opened the
tab. ``refreshReviewBadge`` reuses ``/proposals`` -- the same endpoint
``loadReviewProposals`` already calls -- rather than adding a second
count route, and is wired to three triggers only (page load, the Review
view opening, a proposal being promoted/rejected), never a ``setInterval``
poll.

Two states must never render as each other:

- A genuinely empty queue (``count === 0``) hides the badge, not "0".
- No admin-scoped key held (or one the API refuses) also hides the badge,
  never "0" and never an error glyph -- a background nav-badge refresh has
  no place to show a failure the way ``setCountError`` does for an
  in-view section.
"""

from __future__ import annotations

import re
from pathlib import Path

import trellis_api

INDEX_HTML = Path(trellis_api.__file__).parent / "static" / "index.html"


def _page() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


def _between(page: str, start: str, end: str) -> str:
    found = re.search(re.escape(start) + r"(.*?)" + re.escape(end), page, re.DOTALL)
    assert found is not None, f"could not find region between {start!r} and {end!r}"
    return found.group(1)


def _function_body(page: str, signature: str, next_signature: str) -> str:
    return _between(page, signature, next_signature)


# ---------------------------------------------------------------------------
# 1. The nav entry carries a badge element, hidden by default
# ---------------------------------------------------------------------------


def test_review_nav_entry_has_a_hidden_badge_span() -> None:
    page = _page()
    nav_link = _between(
        page, '<a href="#" data-view="review"', '<a href="#" data-view="effectiveness"'
    )
    assert '<span class="nav-badge" id="nav-badge-review" hidden>' in nav_link


def test_nav_badge_is_toggled_with_the_hidden_property_not_display() -> None:
    # CLAUDE.md / the reset: `[hidden]` is `display:none !important`, so
    # visibility is toggled with `el.hidden`, never `el.style.display`.
    page = _page()
    body = _function_body(
        page,
        "async function refreshReviewBadge() {",
        "function reachabilityHtml(p) {",
    )
    assert "el.hidden = true" in body
    assert "el.hidden = false" in body
    assert "style.display" not in body


# ---------------------------------------------------------------------------
# 2. Badge hidden at 0 and on any fetch failure; shown only for a real count
# ---------------------------------------------------------------------------


def test_badge_hides_rather_than_shows_zero() -> None:
    page = _page()
    body = _function_body(
        page,
        "async function refreshReviewBadge() {",
        "function reachabilityHtml(p) {",
    )
    assert "if (n > 0) {" in body
    # Both branches of the n>0 test must end in a hide, not a "0" render --
    # the `else` covers n === 0, the catch covers a fetch failure.
    else_branch = _between(body, "if (n > 0) {", "} catch")
    assert "el.hidden = true" in _between(else_branch, "} else {", "}")


def test_no_key_held_skips_the_fetch_and_hides_instead_of_erroring() -> None:
    page = _page()
    body = _function_body(
        page,
        "async function refreshReviewBadge() {",
        "function reachabilityHtml(p) {",
    )
    assert "if (!getApiKey()) { el.hidden = true; return; }" in body


def test_a_fetch_failure_hides_the_badge_not_an_error_glyph() -> None:
    page = _page()
    body = _function_body(
        page,
        "async function refreshReviewBadge() {",
        "function reachabilityHtml(p) {",
    )
    catch_block = _between(body, "} catch {", "}")
    assert "el.hidden = true" in catch_block
    # Unlike setCountError's '!' glyph for an in-view section, the nav
    # badge has no such channel -- it must never try to borrow one.
    assert "'!'" not in catch_block


def test_badge_renders_100_plus_at_the_proposals_default_limit() -> None:
    # `/proposals` is called with no explicit `limit`, so the server's
    # `Query(100, ...)` default caps `count` at 100 -- that cap is a
    # floor on the real total, not the total itself, so it must never
    # render as a bare "100".
    page = _page()
    body = _function_body(
        page,
        "async function refreshReviewBadge() {",
        "function reachabilityHtml(p) {",
    )
    assert "REVIEW_BADGE_CAP" in page
    assert "n >= REVIEW_BADGE_CAP ? `${REVIEW_BADGE_CAP}+`" in body


def test_badge_reuses_the_proposals_endpoint_already_called_by_the_tab() -> None:
    page = _page()
    body = _function_body(
        page,
        "async function refreshReviewBadge() {",
        "function reachabilityHtml(p) {",
    )
    assert "fetchJSON('/proposals')" in body
    # No second, dedicated count route was added for this.
    assert "proposals/count" not in page
    assert "/review/pending" not in page


# ---------------------------------------------------------------------------
# 3. Wired to three triggers -- load, view-open, resolve -- never a poll
# ---------------------------------------------------------------------------


def test_badge_refreshed_on_initial_load() -> None:
    page = _page()
    tail = page[-400:]
    assert "refreshReviewBadge();" in tail


def test_badge_refreshed_every_time_the_review_view_opens() -> None:
    # Distinct from the one-time `viewLoaded` gate every other tab's
    # initial load uses: a proposal can land between visits with no
    # polling to have caught it, so this call sits outside that gate.
    page = _page()
    body = _function_body(
        page, "function switchView(name) {", "// -- Keyboard shortcuts --"
    )
    assert "if (name === 'review') refreshReviewBadge();" in body
    gated_block = _between(body, "if (!viewLoaded[name]) {", "\n            }")
    assert "refreshReviewBadge" not in gated_block


def test_badge_refreshed_after_a_proposal_is_promoted_or_rejected() -> None:
    page = _page()
    promote_body = _function_body(
        page,
        "async function promoteProposal(id) {",
        "function confirmRejectProposal(id) {",
    )
    assert "refreshReviewBadge();" in promote_body
    reject_body = _function_body(
        page, "async function rejectProposal(id) {", "function toastReview(msg, ok) {"
    )
    assert "refreshReviewBadge();" in reject_body


def test_no_polling_interval_was_added_for_the_badge() -> None:
    page = _page()
    body = _function_body(
        page,
        "async function refreshReviewBadge() {",
        "function reachabilityHtml(p) {",
    )
    assert "setInterval" not in body
    assert "setTimeout" not in body


def test_each_pattern_catches_a_deliberately_defective_subject() -> None:
    """Proves the substring checks above would actually fail on a
    regression, not just pass on anything."""
    defective = (
        "async function refreshReviewBadge() {\n"
        "    const el = document.getElementById('nav-badge-review');\n"
        "    if (!el) return;\n"
        "    const data = await fetchJSON('/proposals');\n"
        "    const n = (data.proposals || []).length;\n"
        "    el.textContent = String(n);\n"
        "    el.hidden = false;\n"
        "}\n"
    )
    assert "if (n > 0) {" not in defective
    assert "if (!getApiKey())" not in defective
    assert "REVIEW_BADGE_CAP" not in defective
