"""Review nav entry shows a pending-items count badge, no polling.

The Review tab already has four per-section counts
(``review-proposals-count`` and siblings), but nothing on the nav entry
itself told an operator there was work waiting before they opened the
tab. ``refreshReviewBadge`` totals ``GET /review/backlog``, which sizes
only the kinds a reviewer can resolve in the view (tuner proposals and
learning candidates), and is wired to a fixed set of triggers (page load,
the Review view opening, a decision being recorded), never a
``setInterval`` poll.

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

import pytest

import trellis_api
from tests.unit.api.test_ui_review_truth import NODE as _HARNESS_NODE

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


def _badge_for(rows: list[dict], tmp_path: Path) -> dict:
    """Run the page's own ``refreshReviewBadge`` against a stub response."""
    import json
    import os
    import subprocess

    from tests.integration._live_server import repo_src_pythonpath
    from tests.unit.api.test_ui_review_truth import NODE

    fn = (
        "async function refreshReviewBadge() {"
        + _between(_page(), "async function refreshReviewBadge() {", "\n        }\n")
        + "\n        }\n"
    )
    harness = f"""
const el = {{ hidden: true, textContent: '' }};
const urls = [];
const document = {{ getElementById: () => el }};
const getApiKey = () => 'k';
const fetchJSON = async (u) => {{
    urls.push(u);
    return {{ review_backlog: {json.dumps(rows)} }};
}};
{fn}
refreshReviewBadge().then(() => console.log(JSON.stringify(
    {{ hidden: el.hidden, text: String(el.textContent), urls }})));
"""
    script = tmp_path / "badge_harness.js"
    script.write_text(harness, encoding="utf-8")
    done = subprocess.run(  # noqa: S603
        [str(NODE), str(script)],
        capture_output=True,
        text=True,
        check=True,
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": repo_src_pythonpath()},
        timeout=30,
    )
    return json.loads(done.stdout.strip().splitlines()[-1])


def _row(kind, *, count, status="ok", truncated=False, actionable=True):
    return {
        "kind": kind,
        "status": status,
        "pending_count": count,
        "truncated": truncated,
        "actionable": actionable,
    }


# The same binary the harness runs, so the guard and the run cannot disagree.
_needs_node = pytest.mark.skipif(_HARNESS_NODE is None, reason="no node")


@_needs_node
def test_badge_renders_a_floor_when_a_kind_is_unreadable_or_capped(tmp_path) -> None:
    # Only an unreadable source or a capped read makes the total a floor.
    unreadable = _badge_for(
        [
            _row("tuner_proposal", count=3),
            _row("learning_candidate", count=None, status="unreadable"),
        ],
        tmp_path,
    )
    assert (unreadable["hidden"], unreadable["text"]) == (False, "3+")
    capped = _badge_for([_row("tuner_proposal", count=4, truncated=True)], tmp_path)
    assert (capped["hidden"], capped["text"]) == (False, "4+")
    # A capped decision read on the learning row marks the total the same way.
    learning = _badge_for(
        [
            _row("tuner_proposal", count=1),
            _row("learning_candidate", count=2, truncated=True),
        ],
        tmp_path,
    )
    assert (learning["hidden"], learning["text"]) == (False, "3+")


@_needs_node
def test_a_not_written_learning_artifact_adds_zero_not_a_floor(tmp_path) -> None:
    shown = _badge_for(
        [
            _row("tuner_proposal", count=2),
            _row("learning_candidate", count=None, status="not_written"),
        ],
        tmp_path,
    )
    assert (shown["hidden"], shown["text"]) == (False, "2")
    nothing = _badge_for(
        [_row("learning_candidate", count=None, status="not_written")], tmp_path
    )
    assert nothing["hidden"] is True


@_needs_node
def test_badge_totals_only_actionable_rows_from_the_backlog_route(tmp_path) -> None:
    shown = _badge_for(
        [
            _row("tuner_proposal", count=1),
            _row("learning_candidate", count=1),
            _row("schema_evolution", count=40, actionable=False),
            _row("code_proposal", count=None, status="unreadable", actionable=False),
        ],
        tmp_path,
    )
    assert (shown["hidden"], shown["text"]) == (False, "2")
    assert shown["urls"] == ["/review/backlog"]


def test_badge_reads_the_backlog_route_not_loop_health() -> None:
    page = _page()
    body = _function_body(
        page,
        "async function refreshReviewBadge() {",
        "function reachabilityHtml(p) {",
    )
    assert "fetchJSON('/review/backlog')" in body
    assert "fetchJSON('/loops')" not in body
    assert "fetchJSON('/proposals')" not in body
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


def test_badge_refreshed_after_a_learning_decision() -> None:
    page = _page()
    submit_body = _function_body(
        page,
        "async function submitLearningPromotions() {",
        "async function declineLearningCandidate(cid) {",
    )
    assert "refreshReviewBadge();" in submit_body
    decline_body = _function_body(
        page,
        "async function declineLearningCandidate(cid) {",
        "// -- Section 3: Schema-evolution candidates --",
    )
    assert "refreshReviewBadge();" in decline_body


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
    assert "review_backlog" not in defective
    assert "floor" not in defective
