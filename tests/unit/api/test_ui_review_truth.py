"""The Review tab and its empty states stop hiding why.

Six defects, pinned one test group each:

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
   by ``fmtDate()``. Two of the route's four ``status: "error"`` answers
   mean only that nothing has been written yet (a fresh deployment): those
   render as an empty state, every other answer as an error, and the error
   branch now shows the server's hint instead of ``Failed to load undefined:
   undefined``.
4. The precedents empty state said "Promote traces to create precedents",
   implying any trace promotion suffices. A precedent is ordinarily
   created when a learning candidate is approved on the Review tab —
   nothing schedules that approval automatically — but the auto-promotion
   loop can also create one itself once an operator enables its commit
   mode, and the empty state now says so instead of claiming human
   approval is the only path.
5. ``/effectiveness`` with zero feedback rendered a "0.0%" success rate,
   reading as "every pack failed" rather than "nothing was measured".
   ``noise_candidates`` is the usage-rule's *proposal* (see
   ``EffectivenessReport``'s docstring) — not a demotion — so the badge no
   longer says "noise", and the section now reports how many of those
   proposals the evidence gate (``demotion_screen.admitted``) actually
   cleared.
6. That same section had no write action and no third figure for what
   actually got tagged — the summary line stopped at "N proposed ... M
   admitted" with no word on ``noise_tags_written``, and nothing in the
   UI called ``POST /effectiveness/apply-noise-tags``. The line now adds
   a third segment, a new "Apply noise tags…" action runs that route
   behind a two-step inline confirm (never ``window.confirm``), and the
   section states that demotion requires evidence of unhelpfulness so an
   admitted-vs-proposed gap is expected, not a bug.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import trellis_api
from tests.integration._live_server import repo_src_pythonpath

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
    assert "badge-danger" in body
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
    """The Ledger components pass moved this off a per-call inline-swap
    button pair onto the one shared confirmation dialog (``.dialog``,
    Cancel first in DOM order, typed-word gate) -- Cancel and the warning
    chrome now live in that shared component rather than being rebuilt
    here, so this test pins what confirmRejectProposal itself supplies:
    the terminal warning text and the real, gated call to the terminal
    action."""
    page = _page()
    body = _between(
        page,
        "function confirmRejectProposal(id) {",
        "async function rejectProposal(id) {",
    )
    assert "openConfirmDialog(" in body
    assert "Reject is terminal." in body
    assert "confirmWord: 'reject'" in body
    # Confirming calls the real, terminal action -- gated behind the
    # dialog's typed-word match, never off the row's first click.
    assert "onConfirm: () => rejectProposal(id)" in body


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


def test_generated_at_utc_goes_through_fmt_time_not_local() -> None:
    body = _load_review_learning_body(_page())
    assert "fmtTime(data.generated_at_utc)" in body


def test_fmt_time_labels_the_zone_instead_of_converting_to_local() -> None:
    page = _page()
    body = _between(
        page, "function fmtTime(iso, { empty = '' } = {}) {", "\n        }\n"
    )
    # The old fmtDate()'s whole defect was toLocaleString() with no zone
    # label; the collapsed fmtTime() must not just be an alias for it.
    assert "toLocaleString" not in body
    assert "UTC" in body


# ---------------------------------------------------------------------------
# 4. Precedents empty state names the actual promotion path, not "promote
#    traces", and makes no claim of automatic scheduling.
# ---------------------------------------------------------------------------


def _render_precedents_empty_state_body(page: str) -> str:
    return _between(
        page,
        "async function renderPrecedentsEmptyState() {",
        "\n        async function loadPrecedents() {",
    )


def test_precedents_empty_branch_delegates_to_the_truthful_renderer() -> None:
    page = _page()
    body = _between(
        page,
        "async function loadPrecedents() {",
        'container.innerHTML = `<div class="precedent-grid">',
    )
    empty_branch = _between(
        body, "if (items.length === 0) {", "return;\n                }"
    )
    assert "await renderPrecedentsEmptyState()" in empty_branch


def test_precedents_empty_state_names_the_real_promotion_path() -> None:
    """A precedent is ordinarily created when a human approves a learning
    candidate on the Review tab. The empty state must say so and link
    there, never repeat the old "Promote traces to create precedents"
    claim that implied any trace promotion was sufficient — but it must
    also not claim human approval is the *only* path, since the
    auto-promotion loop can commit a precedent itself once an operator
    enables its commit mode.
    """
    page = _page()
    body = _render_precedents_empty_state_body(page)
    assert "switchView('review')" in body
    assert "Promote traces to create precedents" not in body
    assert "commit mode" in body
    assert "only when a human approves" not in body


def test_precedents_empty_state_reads_the_same_candidate_count_review_does() -> None:
    """The "N awaiting review" figure must come from the exact same
    fetch and the exact same field the Review tab's own count badge
    uses (``loadReviewLearning``'s ``data.candidates.length``) — a
    second count derived independently of Review's could disagree with
    it.
    """
    page = _page()
    review_body = _load_review_learning_body(page)
    assert "fetchJSON('/learning/candidates')" in review_body
    empty_state_body = _render_precedents_empty_state_body(page)
    assert "fetchJSON('/learning/candidates')" in empty_state_body
    assert "candidatesRes.value.candidates.length" in empty_state_body


def test_precedents_empty_state_would_qualify_count_comes_from_the_loop_row() -> None:
    """The "M would qualify" figure is the ``learning_candidate_auto_promote``
    loop row's own ``last_run_would_promote`` counter (loop_health.py's
    read of the latest auto-promote-evaluation event) — not a value
    this page re-derives from a job name or a constant. Whether that run
    actually promoted anything itself comes from the same row's own
    ``actuates`` flag, never a hardcoded claim either way.
    """
    page = _page()
    body = _render_precedents_empty_state_body(page)
    assert "fetchJSON('/loops')" in body
    assert "l.name === 'learning_candidate_auto_promote'" in body
    assert "row.counters.last_run_would_promote" in body
    assert "row && row.actuates" in body
    assert "it never promotes anything itself" not in body


def test_precedents_empty_state_never_claims_nightly_unconditionally() -> None:
    """The old copy said "it reports nightly" whenever the job merely
    *could* be scheduled. The new copy must gate any scheduled-and-
    running claim on both the schedule row (stored + enabled) and the
    dispatcher-observed signal (``#870``'s own ``dispatcher_seen``) —
    and must never use the word "nightly" at all, since the cadence is
    not necessarily daily.
    """
    page = _page()
    body = _render_precedents_empty_state_body(page)
    assert "nightly" not in body.lower()
    assert "fetchJSON('/admin/jobs')" in body
    assert "jobsRes.value.dispatcher_seen" in body
    assert "job.stored && job.enabled" in body
    # No literal catalog job name or source-level constant in rendered
    # copy — the job is found by which loop it drives, not by name.
    assert "auto-promote-candidates" not in body


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
    # The ratio itself now renders through the shared fmtPct() helper (see
    # test_ui_number_formatting.py), which also guards against a non-finite
    # success_rate -- but the total_feedback === 0 gate asserted here is the
    # claim this test exists to pin: "not measured" wins over a computed
    # 0.0% when there is no feedback to compute it from.
    assert text_assign.group(1) == (
        "data.total_feedback === 0\n"
        "                    ? 'not measured' : fmtPct(data.success_rate, 1)"
    )
    sub_assign = re.search(r"const successRateSub = (.*?);\n", body, re.DOTALL)
    assert sub_assign is not None, "successRateSub assignment not found"
    assert sub_assign.group(1) == (
        "data.total_feedback === 0\n"
        "                    ? '0 feedback in this window' : 'overall effectiveness'"
    )
    # The Fonts pass wires a fourth (raw) argument through this call so
    # renderCard's .value.na/.value.inf modifiers can see the unformatted
    # value -- see test_ui_number_formatting.py::TestRenderCardRawArgument.
    assert (
        "renderCard('Success Rate', successRateText, successRateSub,"
        " successRateRaw)" in body
    )
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
        '<div class="status-section hide" id="evo-noise-section">',
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


def test_noise_summary_line_adds_a_third_segment_for_tags_written() -> None:
    # The two-segment line ("N proposed ... M admitted ...") said nothing
    # about what apply-noise-tags actually wrote, so an operator could not
    # tell a dry read (/effectiveness, noise_tags_written always null)
    # from a completed write pass (/effectiveness/apply-noise-tags) just
    # by looking at this section.
    body = _load_evolution_body(_page())
    assert "data.noise_tags_written" in body
    assert "noise tags written" in body


def test_evolution_section_states_demotion_requires_evidence_of_unhelpfulness() -> None:
    page = _page()
    section = _between(
        page,
        '<div class="status-section hide" id="evo-noise-section">',
        '<div id="evo-noise"></div>',
    )
    assert "evidence of unhelpfulness" in section
    # Operator-copy sweep: the module name this claim comes from
    # (demotion_gate.py) no longer appears in rendered text -- the prose
    # itself states the rule instead of naming the source file.
    assert "demotion_gate" not in section
    assert "working as intended, not under-performing" in section


def test_apply_noise_tags_button_exists_behind_a_two_step_inline_confirm() -> None:
    page = _page()
    assert 'onclick="showApplyNoiseTagsConfirm()"' in page
    assert "function showApplyNoiseTagsConfirm() {" in page
    assert "function cancelApplyNoiseTags() {" in page
    assert "async function runApplyNoiseTags() {" in page
    confirm_region = _between(
        page,
        "function showApplyNoiseTagsConfirm() {",
        "async function runApplyNoiseTags() {",
    )
    # window.confirm cannot render in this runtime (see the advisories
    # generate / proposal reject precedents) — the confirm step must be
    # an inline DOM swap instead.
    assert "window.confirm" not in confirm_region
    assert "btn-danger" in confirm_region


def test_apply_noise_tags_posts_to_the_existing_route_and_shows_counts() -> None:
    page = _page()
    body = _between(
        page,
        "async function runApplyNoiseTags() {",
        "\n        }\n",
    )
    assert "postJSON(`/effectiveness/apply-noise-tags" in body
    assert "noise_candidates_tagged" in body
    assert "noise_candidates_refused_not_document" in body
    # A successful apply re-renders straight from the POST response (which
    # carries the same report-shaped fields as GET /effectiveness, just
    # with noise_tags_written populated) rather than re-fetching GET
    # /effectiveness, which would revert noise_tags_written back to null.
    assert "renderNoiseSection(data)" in body


# ---------------------------------------------------------------------------
# 7. The learning-promotion toast names the rows it skipped
# ---------------------------------------------------------------------------


def _submit_learning_promotions_source(page: str) -> str:
    return "async function submitLearningPromotions() {" + _between(
        page,
        "async function submitLearningPromotions() {",
        "// -- Section 3: Schema-evolution candidates --",
    )


def _toast_for(response: dict[str, object], tmp_path: Path) -> tuple[str, bool]:
    """Run the page's own ``submitLearningPromotions`` against a stub response."""
    harness = f"""
const toasts = [];
const document = {{ getElementById: () => ({{ querySelectorAll: () => [] }}) }};
const postJSON = async () => ({json.dumps(response)});
const toastReview = (msg, ok) => toasts.push([msg, ok]);
const loadReviewLearning = () => {{}};
{_submit_learning_promotions_source(_page())}
submitLearningPromotions().then(() => console.log(JSON.stringify(toasts)));
"""
    script = tmp_path / "harness.js"
    script.write_text(harness, encoding="utf-8")
    done = subprocess.run(  # noqa: S603
        [NODE, str(script)],
        capture_output=True,
        text=True,
        check=True,
        cwd=tmp_path,
        # node never imports trellis; the pin is here because the repo rule
        # (test_subprocess_pythonpath_rule) covers every launch under tests/.
        env={**os.environ, "PYTHONPATH": repo_src_pythonpath()},
        timeout=30,
    )
    (toast,) = json.loads(done.stdout)
    return toast[0], toast[1]


NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


@needs_node
def test_a_run_where_every_approved_row_is_blank_says_they_were_skipped(
    tmp_path: Path,
) -> None:
    # The route answers ``refused`` for a blank-id row, so the toast is an
    # error that names the skip and the verdict, not a bare success.
    msg, ok = _toast_for(
        {
            "status": "refused",
            "approved_count": 2,
            "named_count": 0,
            "promoted_count": 0,
        },
        tmp_path,
    )

    assert msg.startswith("Promoted 0/0 approved candidate(s).")
    assert "2 approved row(s) name no candidate_id and were skipped." in msg
    assert msg.endswith(" Run refused.")
    assert ok is False


@needs_node
def test_a_partly_blank_run_counts_only_the_blank_rows(tmp_path: Path) -> None:
    # What promoted still landed, so the count says so; the run is refused.
    msg, ok = _toast_for(
        {
            "status": "refused",
            "approved_count": 5,
            "named_count": 3,
            "promoted_count": 3,
        },
        tmp_path,
    )

    assert msg.startswith("Promoted 3/3 approved candidate(s).")
    assert "2 approved row(s) name no candidate_id and were skipped." in msg
    assert msg.endswith(" Run refused.")
    assert ok is False


@needs_node
def test_a_run_that_skipped_nothing_adds_no_note(tmp_path: Path) -> None:
    msg, ok = _toast_for(
        {"status": "ok", "approved_count": 3, "named_count": 3, "promoted_count": 3},
        tmp_path,
    )

    assert msg == "Promoted 3/3 approved candidate(s)."
    assert ok is True


@needs_node
def test_the_skip_note_sits_beside_a_failed_verdict(tmp_path: Path) -> None:
    msg, ok = _toast_for(
        {
            "status": "failed",
            "approved_count": 4,
            "named_count": 3,
            "promoted_count": 1,
        },
        tmp_path,
    )

    assert msg.startswith("Promoted 1/3 approved candidate(s).")
    assert "1 approved row(s) name no candidate_id and were skipped." in msg
    assert msg.endswith(" Run failed.")
    assert ok is False


def test_the_toast_reads_the_difference_the_cli_reads() -> None:
    """Source pin, so the behaviour tests above cannot skip away the rule."""
    body = _submit_learning_promotions_source(_page())

    assert "r.approved_count - r.named_count" in body
    assert "name no candidate_id and were skipped." in body


# ---------------------------------------------------------------------------
# 8. The Review learning section: nothing written yet is empty, not an error
# ---------------------------------------------------------------------------

#: The route's answers that mean "no writer has produced the artifact yet".
NOT_WRITTEN_CODES = ("learning_artifacts_dir_missing", "learning_candidates_missing")


def _review_learning_source(page: str) -> str:
    """The page's own functions ``loadReviewLearning`` runs, in source order."""

    def fn(start: str) -> str:
        return start + _between(page, start, "\n        }\n") + "\n        }\n"

    def region(start: str, end: str) -> str:
        return start + _between(page, start, end)

    return "\n".join(
        [
            fn("function fmtTime(iso, { empty = '' } = {}) {"),
            fn("function escHtml(s) {"),
            fn("function loadingHtml(msg) {"),
            region("const EMPTY_STATE_ICON =", "</svg>';") + "</svg>';",
            fn("function emptyStateHtml("),
            fn("function isAdminScopeError(err) {"),
            fn("function loadFailureText(err, label) {"),
            fn("function loadFailureColor(err) {"),
            # setCount, setCountError, reviewEmpty and reviewError.
            region("function setCount(id, n) {", "async function loadReview() {"),
            region("const LEARNING_NOT_WRITTEN_CODES = [", "];") + "];",
            region(
                "async function loadReviewLearning() {",
                "async function submitLearningPromotions() {",
            ),
        ]
    )


def render_review_learning(response: dict[str, object], tmp_path: Path) -> dict:
    """Run the page's own ``loadReviewLearning`` against a stub response.

    Returns the count badge (``text``, ``title``, ``failed``) and the body
    markup the section ends up with.
    """
    harness = f"""
const els = {{}};
const mk = (id) => els[id] || (els[id] = {{
    innerHTML: '', textContent: '', title: '', failed: false,
    classList: {{
        add: (c) => {{ if (c === 'review-count-error') els[id].failed = true; }},
        remove: (c) => {{ if (c === 'review-count-error') els[id].failed = false; }},
    }},
    removeAttribute: () => {{ els[id].title = ''; }},
}});
const escape = (t) => t
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
const document = {{
    getElementById: mk,
    createElement: () => {{
        let text = '';
        return {{
            set textContent(v) {{ text = v; }},
            get innerHTML() {{ return escape(text); }},
        }};
    }},
}};
const fetchJSON = async () => ({json.dumps(response)});
{_review_learning_source(_page())}
loadReviewLearning().then(() => {{
    const count = els['review-learning-count'];
    console.log(JSON.stringify({{
        count: {{
            text: String(count.textContent), title: count.title, failed: count.failed,
        }},
        body: els['review-learning-body'].innerHTML,
    }}));
}});
"""
    script = tmp_path / "review_learning_harness.js"
    script.write_text(harness, encoding="utf-8")
    done = subprocess.run(  # noqa: S603
        [NODE, str(script)],
        capture_output=True,
        text=True,
        check=True,
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": repo_src_pythonpath()},
        timeout=30,
    )
    return json.loads(done.stdout)


def _unavailable(code: str | None, hint: str) -> dict[str, object]:
    return {
        "status": "error",
        "code": code,
        "artifacts_dir": "/data/learning",
        "hint": hint,
        "candidate_count": 0,
        "candidates": [],
    }


@needs_node
@pytest.mark.parametrize("code", NOT_WRITTEN_CODES)
def test_a_deployment_with_nothing_written_reads_as_an_empty_state(
    code: str, tmp_path: Path
) -> None:
    hint = "No learning candidates have been written for review: /data/learning."
    shown = render_review_learning(_unavailable(code, hint), tmp_path)

    # An honest zero, not the "!" a failed load gets.
    assert shown["count"] == {"text": "0", "title": "", "failed": False}
    assert "No learning candidates yet" in shown["body"]
    # The hint still names where the page looked and how to fill it.
    assert hint in shown["body"]
    assert "var(--danger)" not in shown["body"]
    assert "Failed to load" not in shown["body"]


@needs_node
@pytest.mark.parametrize(
    ("code", "hint"),
    [
        (
            "learning_candidates_unreadable",
            "Could not read learning candidates from /data/learning/x.json: bad.",
        ),
        (
            "stores_dir_unconfigured",
            "No learning-candidate directory: set TRELLIS_LEARNING_ARTIFACTS_DIR.",
        ),
        ("some_future_reason", "A reason this page has never heard of."),
        (None, "An error answer that carries no code."),
    ],
    ids=["unreadable", "unconfigured", "unknown-code", "no-code"],
)
def test_a_real_failure_still_reads_as_an_error_with_the_servers_reason(
    code: str | None, hint: str, tmp_path: Path
) -> None:
    shown = render_review_learning(_unavailable(code, hint), tmp_path)

    assert shown["count"]["text"] == "!"
    assert shown["count"]["failed"] is True
    assert hint in shown["count"]["title"]
    assert "var(--danger)" in shown["body"]
    # The reason is in the body too, not only in the badge's tooltip, and the
    # body is not the placeholder text a string passed as an Error produced.
    assert hint in shown["body"]
    assert "undefined" not in shown["body"]
    assert "No learning candidates yet" not in shown["body"]


@needs_node
def test_an_artifact_that_was_read_renders_as_before(tmp_path: Path) -> None:
    shown = render_review_learning(
        {
            "status": "ok",
            "artifacts_dir": "/data/learning",
            "generated_at_utc": "2026-01-02T03:04:05+00:00",
            "candidate_count": 0,
            "candidates": [],
        },
        tmp_path,
    )

    assert shown["count"]["text"] == "0"
    assert shown["count"]["failed"] is False
    assert "0 candidate(s)" in shown["body"]
    assert "No learning candidates found." in shown["body"]


def test_the_not_written_codes_are_exactly_the_two_nothing_written_answers() -> None:
    """Source pin, so the behaviour tests above cannot skip away the rule."""
    listed = _between(_page(), "const LEARNING_NOT_WRITTEN_CODES = [", "];")

    assert tuple(re.findall(r"'([a-z_]+)'", listed)) == NOT_WRITTEN_CODES


# ---------------------------------------------------------------------------
# 9. The Precedents empty state: the same two answers, the same reading
# ---------------------------------------------------------------------------


def _page_fn(page: str, start: str) -> str:
    return start + _between(page, start, "\n        }\n") + "\n        }\n"


def _page_region(page: str, start: str, end: str) -> str:
    return start + _between(page, start, end)


def render_precedents_empty_state(
    candidates: dict[str, object] | str, tmp_path: Path
) -> str:
    """Run the page's own ``renderPrecedentsEmptyState`` against stub routes.

    ``candidates`` is the body ``/learning/candidates`` answers, or a string
    for a request that fails with that message. The two other routes the
    state reads answer empty, so only the candidate line varies. Returns the
    markup of the empty state.
    """
    page = _page()
    source = "\n".join(
        [
            _page_fn(page, "function escHtml(s) {"),
            _page_region(page, "const EMPTY_STATE_ICON =", "</svg>';") + "</svg>';",
            _page_fn(page, "function emptyStateHtml("),
            _page_region(page, "const LEARNING_NOT_WRITTEN_CODES = [", "];") + "];",
            _page_region(
                page,
                "async function renderPrecedentsEmptyState() {",
                "\n        async function loadPrecedents() {",
            ),
        ]
    )
    harness = f"""
const document = {{
    createElement: () => {{
        let text = '';
        return {{
            set textContent(v) {{ text = v; }},
            get innerHTML() {{
                return text
                    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
            }},
        }};
    }},
}};
const candidates = {json.dumps(candidates)};
const fetchJSON = async (path) => {{
    if (path === '/learning/candidates') {{
        if (typeof candidates === 'string') throw new Error(candidates);
        return candidates;
    }}
    return path === '/loops' ? {{ loops: [] }} : {{ jobs: [] }};
}};
{source}
renderPrecedentsEmptyState().then((html) => console.log(JSON.stringify(html)));
"""
    script = tmp_path / "precedents_empty_state_harness.js"
    script.write_text(harness, encoding="utf-8")
    done = subprocess.run(  # noqa: S603
        [NODE, str(script)],
        capture_output=True,
        text=True,
        check=True,
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": repo_src_pythonpath()},
        timeout=30,
    )
    return json.loads(done.stdout)


_NOT_YET = "No learning candidates are currently awaiting a decision."
_UNCHECKED = "could not be checked"


def _line(shown: str) -> str:
    """The candidate sentence alone, so the two loop sentences cannot match.

    It sits between the fixed introduction and the sentence about the check
    that estimates what would qualify (the stub loops route reports none).
    """
    return shown.split("its commit mode. ")[1].split(
        " The automatic check", maxsplit=1
    )[0]


@needs_node
@pytest.mark.parametrize("code", NOT_WRITTEN_CODES)
def test_precedents_reads_nothing_written_as_an_empty_queue_with_the_hint(
    code: str, tmp_path: Path
) -> None:
    hint = "No learning candidates have been written for review: /data/learning."
    shown = render_precedents_empty_state(_unavailable(code, hint), tmp_path)

    assert _NOT_YET in shown
    assert hint in shown
    assert _UNCHECKED not in _line(shown)


@needs_node
@pytest.mark.parametrize(
    ("code", "hint"),
    [
        (
            "learning_candidates_unreadable",
            "Could not read learning candidates from /data/learning/x.json: bad.",
        ),
        (
            "stores_dir_unconfigured",
            "No learning-candidate directory: set TRELLIS_LEARNING_ARTIFACTS_DIR.",
        ),
        ("some_future_reason", "A reason this page has never heard of."),
        (None, "An error answer that carries no code."),
    ],
    ids=["unreadable", "unconfigured", "unknown-code", "no-code"],
)
def test_precedents_shows_the_servers_reason_for_a_real_failure(
    code: str | None, hint: str, tmp_path: Path
) -> None:
    sentence = _line(render_precedents_empty_state(_unavailable(code, hint), tmp_path))

    assert _UNCHECKED in sentence
    assert hint in sentence
    assert (f"({code})" in sentence) is (code is not None)
    assert _NOT_YET not in sentence


@needs_node
def test_precedents_shows_why_a_failed_request_could_not_be_checked(
    tmp_path: Path,
) -> None:
    sentence = _line(render_precedents_empty_state("HTTP 503 <down>", tmp_path))

    assert _UNCHECKED in sentence
    assert "HTTP 503 &lt;down&gt;" in sentence
    assert _NOT_YET not in sentence


@needs_node
@pytest.mark.parametrize(
    ("candidates", "expected"),
    [
        ([], _NOT_YET),
        ([{"candidate_id": "a"}, {"candidate_id": "b"}], "2 learning candidate(s)"),
    ],
    ids=["none", "two"],
)
def test_precedents_reads_an_artifact_that_was_read_as_before(
    candidates: list[dict[str, str]], expected: str, tmp_path: Path
) -> None:
    answer = {
        "status": "ok",
        "candidate_count": len(candidates),
        "candidates": candidates,
    }
    sentence = _line(render_precedents_empty_state(answer, tmp_path))

    assert expected in sentence
    assert _UNCHECKED not in sentence


def test_precedents_uses_the_same_nothing_written_list_review_does() -> None:
    """Source pin: one list of codes, so the two tabs cannot disagree."""
    body = _render_precedents_empty_state_body(_page())

    assert "LEARNING_NOT_WRITTEN_CODES.includes(" in body
    assert "learning_artifacts_dir_missing" not in body
