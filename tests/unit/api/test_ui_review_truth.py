"""The Review tab and its empty states stop hiding why.

Five defects, pinned one test group each:

1. A tuner proposal with ``reachable === false`` can never be promoted —
   ``promote_proposal()`` and ``preview_promotion()`` both re-check the same
   reachability before any policy gate runs. The card now shows ``status``,
   ``tool_name`` and the reachability verdict (with reasons, when
   unreachable), disables Approve on an unreachable row, and routes Reject
   — terminal, unlike a recoverable bootstrap refusal — through an in-page
   confirm step instead of ``window.confirm``, which this runtime cannot
   show.
2. A fetch failure on any of the four Review-queue sections used to write a
   literal ``0`` into that section's count badge via ``setCount``, reading
   identically to "nothing pending" even while the body below it shows an
   error. ``setCountError`` now renders a distinct "!" marker with the
   failure in ``title``.
3. ``GET /learning/candidates`` answers 200 with ``status: "error"`` (the
   artifact is missing or unreadable) rather than a fetch failure, so it
   never reached the ``catch`` block and rendered as "no candidates found".
   It now renders as an error with the server's ``hint``/``code``. The
   artifact's ``generated_at_utc`` is shown labelled UTC via a new
   ``fmtUtc()`` instead of silently relabelled to the browser's local zone
   by ``fmtDate()``.
4. The precedents empty state said "Promote traces to create precedents",
   implying any trace promotion suffices. A precedent is only created when
   a learning candidate is approved, and nothing schedules that approval
   automatically.
5. ``/effectiveness`` with zero feedback rendered a "0.0%" success rate,
   reading as "every pack failed" rather than "nothing was measured".
   ``noise_candidates`` is the usage-rule's *proposal* (see
   ``EffectivenessReport``'s docstring) — not a demotion — so the badge no
   longer says "noise", and the section now reports how many of those
   proposals the evidence gate (``demotion_screen.admitted``) actually
   cleared.
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


def _load_review_proposals_body(page: str) -> str:
    return _between(
        page,
        "async function loadReviewProposals() {",
        "async function confirmPromoteProposal(id) {",
    )


def _load_review_learning_body(page: str) -> str:
    return _between(
        page,
        "async function loadReviewLearning() {",
        "async function submitLearningPromotions() {",
    )


def _load_evolution_body(page: str) -> str:
    return _between(
        page,
        "async function loadEvolution() {",
        "function nodeTypeOf(x) {",
    )


# ---------------------------------------------------------------------------
# 1. Tuner proposals: status/tool_name/reachability, disabled Approve,
#    two-step Reject confirm.
# ---------------------------------------------------------------------------


def test_proposal_card_shows_status_and_tool_name() -> None:
    body = _load_review_proposals_body(_page())
    assert "status: ${escHtml(p.status)}" in body
    assert "p.tool_name ? `<span>tool: ${escHtml(p.tool_name)}</span>` : ''" in body


def test_reachability_html_distinguishes_true_false_and_null() -> None:
    page = _page()
    body = _between(page, "function reachabilityHtml(p) {", "\n        }\n")
    # false: rendered as an error badge with the reasons the server gave.
    assert "p.reachable === false" in body
    assert "badge-err" in body
    assert "unreachable" in body
    assert "p.unreachable_reasons" in body
    # true: a distinct ok badge, not the same markup as false.
    assert "p.reachable === true" in body
    assert "badge-ok" in body
    # null (not checked) falls through to a third, explicitly-labelled case
    # rather than being folded into either true or false.
    assert "not checked" in body
    assert "badge-warn" in body


def test_approve_button_is_disabled_when_unreachable() -> None:
    body = _load_review_proposals_body(_page())
    # The disabled ternary must sit on the Approve button's own tag (next to
    # its data-action), not merely exist somewhere in the card template —
    # otherwise a mutant could move it onto an unrelated element and leave
    # Approve always clickable.
    assert re.search(
        r'data-action="confirmPromoteProposal" '
        r'data-id="\$\{escHtml\(p\.proposal_id\)\}"'
        r"\$\{p\.reachable === false \? ' disabled' : ''\}>Approve</button>",
        body,
    ), "Approve button is not gated on p.reachable === false"


def test_reject_button_routes_through_a_two_step_confirm() -> None:
    body = _load_review_proposals_body(_page())
    # The card's Reject button now dispatches to the confirm step, not
    # straight to the terminal action.
    assert re.search(
        r'data-action="confirmRejectProposal" '
        r'data-id="\$\{escHtml\(p\.proposal_id\)\}">Reject</button>',
        body,
    )
    assert (
        'data-action="rejectProposal" data-id="${escHtml(p.proposal_id)}"' not in body
    )


def test_confirm_reject_proposal_shows_a_terminal_warning_and_a_cancel() -> None:
    page = _page()
    body = _between(
        page,
        "function confirmRejectProposal(id) {",
        "async function rejectProposal(id) {",
    )
    assert "Reject is terminal." in body
    # Confirming calls the real, terminal action; Cancel reloads the
    # unmodified list rather than leaving the card in the confirm state.
    assert (
        'data-action="rejectProposal" data-id="${escHtml(id)}">Confirm reject</button>'
        in body
    )
    assert 'onclick="loadReviewProposals()">Cancel</button>' in body


def test_confirm_reject_proposal_is_wired_into_id_actions() -> None:
    page = _page()
    block = re.search(r"const ID_ACTIONS = \{(.*?)\};", page, re.DOTALL)
    assert block is not None
    names = {name.strip() for name in block.group(1).split(",") if name.strip()}
    assert "confirmRejectProposal" in names


# ---------------------------------------------------------------------------
# 2. Review count badges: a catch block must not write a literal 0.
# ---------------------------------------------------------------------------


def test_no_review_count_badge_is_set_to_a_literal_zero() -> None:
    page = _page()
    assert not re.search(r"setCount\('review-[\w-]+-count',\s*0\)", page), (
        "a review count badge is still set to a literal 0 (reads as "
        "'nothing pending' on a fetch failure, indistinguishable from an "
        "honest empty result)"
    )


def test_every_review_section_reports_set_count_error_on_its_own_catch() -> None:
    page = _page()
    # The four Review-queue sections named in the fix, by their count-badge
    # element id.
    expected = {
        "review-proposals-count",
        "review-learning-count",
        "review-schema-count",
        "review-code-count",
    }
    found = set(re.findall(r"setCountError\('([\w-]+)'", page))
    assert found == expected, f"setCountError coverage changed: {found}"


def test_set_count_error_renders_a_distinct_marker_with_the_failure_in_title() -> None:
    page = _page()
    body = _between(page, "function setCountError(id, msg) {", "\n        }\n")
    assert "el.textContent = '!';" in body
    assert "el.title = msg;" in body
    assert "review-count-error" in body


def test_set_count_clears_any_stale_error_state() -> None:
    """A section that fails once and then loads cleanly must not keep
    showing the error marker/colour from the previous failure."""
    page = _page()
    body = _between(page, "function setCount(id, n) {", "\n        }\n")
    assert "review-count-error" in body
    assert "removeAttribute('title')" in body


# ---------------------------------------------------------------------------
# 3. Learning candidates: status:"error" renders as an error, and
#    generated_at_utc is labelled UTC rather than silently localised.
# ---------------------------------------------------------------------------


def test_status_error_renders_as_an_error_not_the_empty_state() -> None:
    body = _load_review_learning_body(_page())
    branch = _between(body, "if (data.status === 'error') {", "\n                }\n")
    assert "reviewError(" in branch
    assert "reviewEmpty(" not in branch
    assert "return;" in branch
    assert "setCountError(" in branch
    # The branch must actually surface the server's own explanation, not a
    # fixed generic string that would read the same for every cause.
    assert "data.hint" in branch


def test_generated_at_utc_goes_through_fmt_utc_not_fmt_date() -> None:
    body = _load_review_learning_body(_page())
    assert "fmtUtc(data.generated_at_utc)" in body
    assert "fmtDate(data.generated_at_utc)" not in body


def test_fmt_utc_labels_the_zone_instead_of_converting_to_local() -> None:
    page = _page()
    body = _between(page, "function fmtUtc(iso) {", "\n        }\n")
    # fmtDate's whole defect is toLocaleString() with no zone label; fmtUtc
    # must not just be an alias for it.
    assert "toLocaleString" not in body
    assert "UTC" in body


# ---------------------------------------------------------------------------
# 4. Precedents empty state names the actual promotion path, not "promote
#    traces", and makes no claim of automatic scheduling.
# ---------------------------------------------------------------------------


def test_precedents_empty_state_names_the_real_promotion_path() -> None:
    page = _page()
    body = _between(
        page,
        "async function loadPrecedents() {",
        "// Populate domain filter",
    )
    empty_branch = _between(
        body, "if (items.length === 0) {", "return;\n                }"
    )
    assert "trellis curate promote-learning" in empty_branch
    assert "Nothing schedules that promotion automatically today" in empty_branch
    # The old claim ("Promote traces to create precedents") implied any
    # trace promotion was sufficient; it must actually be gone, not merely
    # supplemented.
    assert "Promote traces to create precedents" not in empty_branch


# ---------------------------------------------------------------------------
# 5. Evolution: an unmeasured success rate must not read as 0.0%, and the
#    noise-candidates list must not claim to be a demotion.
# ---------------------------------------------------------------------------


def test_success_rate_renders_not_measured_when_there_is_no_feedback() -> None:
    body = _load_evolution_body(_page())
    # Pin the exact assignment, condition and both arms together — not just
    # that "data.total_feedback === 0" appears *somewhere* in the function
    # (it also gates successRateSub on the next line, so a substring check
    # alone would miss the condition being inverted on successRateText
    # specifically while successRateSub stayed correct).
    text_assign = re.search(r"const successRateText = (.*?);\n", body, re.DOTALL)
    assert text_assign is not None, "successRateText assignment not found"
    assert text_assign.group(1) == (
        "data.total_feedback === 0\n"
        "                    ? 'not measured' : "
        "(data.success_rate * 100).toFixed(1) + '%'"
    )
    sub_assign = re.search(r"const successRateSub = (.*?);\n", body, re.DOTALL)
    assert sub_assign is not None, "successRateSub assignment not found"
    assert sub_assign.group(1) == (
        "data.total_feedback === 0\n"
        "                    ? '0 feedback in this window' : 'overall effectiveness'"
    )
    assert "renderCard('Success Rate', successRateText, successRateSub)" in body
    # The old unconditional computation must be gone, not merely shadowed —
    # a mutant that left it reachable on some other branch would still
    # read "0.0%" whenever success_rate itself is literally 0.
    assert (
        "renderCard('Success Rate', (data.success_rate * 100).toFixed(1) + '%'"
        not in body
    )


def test_noise_section_heading_no_longer_calls_candidates_noise() -> None:
    page = _page()
    section = _between(
        page,
        '<div class="status-section" id="evo-noise-section" style="display:none">',
        '<div id="evo-noise"></div>',
    )
    assert "<h2>Noise Candidates</h2>" not in section
    assert "Usage-Rule Proposals" in section


def test_noise_candidate_rows_use_proposed_admitted_not_a_noise_badge() -> None:
    body = _load_evolution_body(_page())
    assert "data.demotion_screen" in body
    assert "'proposed'" in body
    assert "'admitted'" in body
    assert '<span class="badge badge-warn">noise</span>' not in body


def test_noise_section_reports_how_many_the_evidence_gate_admitted() -> None:
    body = _load_evolution_body(_page())
    assert "admittedIds.length" in body
    assert "admitted by the evidence gate" in body
    # The computed summaryLine must actually reach noise.innerHTML, not
    # merely be computed and discarded — a mutant that reverted only the
    # final render while leaving the dead computation above it in place
    # would otherwise pass this test.
    assert re.search(r"noise\.innerHTML = `[^`]*\$\{summaryLine\}", body)
