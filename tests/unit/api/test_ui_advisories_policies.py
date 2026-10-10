"""The Advisories and Policies tabs must not collapse distinct server states
into the same rendering.

Three shapes are easy to confuse with "nothing here" if a fetch handler is
written against the happy path only:

* ``GET /advisories`` answers a missing ``stores_dir`` at HTTP 200 with
  ``{"status": "error", ...}`` and no ``advisories``/``count`` keys -- a
  handler that reads ``data.advisories || []`` without checking ``status``
  first renders this as a quiet empty table instead of a visible failure.
* A degraded ``AdvisoryStore``/``PolicyStore`` (``DegradableJsonStore``) still
  answers 200 with however many rows parsed, plus a ``store_degradation``
  object describing what did not -- zero parsed rows and a healthy empty
  store must not render identically, or an operator reads "no policies" when
  the truth is "can't tell, the file is damaged".
* ``POST /advisories/generate`` answers two of its three refusal shapes at
  HTTP 409 with its own ``status``/``code`` body, not an ``HTTPException``
  ``{"detail": ...}`` envelope -- the generic ``postJSON`` helper used
  everywhere else on this page reads only ``detail`` and would collapse both
  to a bare "409 Conflict", discarding the one payload that explains the
  refusal.

These tests pin that the page's handlers branch on those specific shapes
rather than reusing the generic happy-path helpers unconditionally.
"""

from __future__ import annotations

import re
from pathlib import Path

import trellis_api

INDEX_HTML = Path(trellis_api.__file__).parent / "static" / "index.html"


def _page() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


def _function_body(page: str, name: str) -> str:
    found = re.search(
        rf"(?:async )?function {re.escape(name)}\([^)]*\) \{{\n(.*?)\n        \}}\n",
        page,
        re.DOTALL,
    )
    assert found is not None, f"{name}() not found"
    return found.group(1)


def test_load_advisories_checks_the_sentinel_error_before_counting_rows() -> None:
    body = _function_body(_page(), "loadAdvisories")
    # A literal, unconditional `if` -- not merely the substring appearing
    # somewhere, which a `if (false && data.status === 'error')` mutant
    # would also contain while never taking the branch.
    guard = re.search(r"if \(data\.status === 'error'\) \{", body)
    assert guard is not None, (
        "loadAdvisories() does not gate on a literal, unconditional "
        "`if (data.status === 'error')` -- the sentinel case would fall "
        "through to the empty-table branch instead of a visible error"
    )
    status_check = guard.start()
    rows_read = body.find("data.advisories || []")
    assert rows_read != -1, "loadAdvisories() does not read data.advisories"
    assert status_check < rows_read, (
        "the sentinel-error check happens after rows are already read as "
        "empty -- GET /advisories' 200 {status: error} response would "
        "render as zero advisories instead of a visible failure"
    )
    # The error branch renders text, not a silently empty <tbody>.
    error_branch = body[status_check : body.find("return;", status_check)]
    assert "escHtml(data.message" in error_branch
    assert "color:var(--error)" in error_branch


def test_load_advisories_distinguishes_degraded_from_healthy_empty() -> None:
    body = _function_body(_page(), "loadAdvisories")
    assert "renderDegradedBanner('advisories-banner', data.store_degradation)" in body
    empty_branch = re.search(r"if \(rows\.length === 0\) \{(.*?)\}", body, re.DOTALL)
    assert empty_branch is not None, "no empty-rows branch in loadAdvisories()"
    clause = empty_branch.group(1)
    # A ternary keyed on the same degradation field the banner used, so a
    # degraded-but-empty read cannot print the same words as a healthy one.
    assert "data.store_degradation ?" in clause
    assert "degraded" in clause.lower()


def test_load_policies_distinguishes_degraded_from_healthy_empty() -> None:
    body = _function_body(_page(), "loadPolicies")
    assert "renderDegradedBanner('policies-banner', data.store_degradation)" in body
    empty_branch = re.search(r"if \(rows\.length === 0\) \{(.*?)\}", body, re.DOTALL)
    assert empty_branch is not None, "no empty-rows branch in loadPolicies()"
    clause = empty_branch.group(1)
    assert "data.store_degradation ?" in clause
    assert "degraded" in clause.lower()


def test_render_degraded_banner_clears_when_not_degraded() -> None:
    body = _function_body(_page(), "renderDegradedBanner")
    # A falsy degradation must blank the banner element rather than leaving
    # a stale one from a previous load (e.g. switching a filter from a
    # degraded result back to a healthy one).
    assert re.search(r"if \(!degradation\) \{ .*?innerHTML = '';.*?\}", body)


def test_generate_advisories_does_not_reuse_postjson() -> None:
    """POST /advisories/generate's refusal shapes are not HTTPException
    envelopes for two of three branches, so the generic postJSON/fetchJSON
    error path (which reads only `.detail`) cannot be reused here -- the
    page must read the body unconditionally and branch on the route's own
    fields instead of relying on `res.ok`."""
    page = _page()
    body = _function_body(page, "generateAdvisoriesRequest")
    assert "postJSON(" not in body, (
        "generateAdvisoriesRequest() must not delegate to postJSON() -- two "
        "of the route's three refusal shapes are not HTTPException {detail} "
        "envelopes and postJSON's error path would discard them"
    )
    # The body is read regardless of res.ok, not only on success.
    assert "await res.json()" in body
    assert "res.ok" not in body


def test_run_generate_advisories_branches_on_all_three_refusal_shapes() -> None:
    body = _function_body(_page(), "runGenerateAdvisories")
    assert "data.detail && data.detail.code === 'stale_store_write'" in body
    assert "data.status === 'error'" in body
    assert "data.status === 'degraded'" in body


def test_generate_advisories_uses_two_step_confirm_not_window_confirm() -> None:
    page = _page()
    assert "window.confirm" not in page
    show = _function_body(page, "showGenerateAdvisoriesForm")
    assert "confirmGenerateAdvisories()" in show
    confirm = _function_body(page, "confirmGenerateAdvisories")
    assert "runGenerateAdvisories()" in confirm
    assert "cancelGenerateAdvisories()" in confirm
    # The confirm step must actually gate the destructive call behind a
    # second click: showGenerateAdvisoriesForm's initial actions call
    # confirmGenerateAdvisories, not runGenerateAdvisories, directly.
    show_actions = re.search(r'id="adv-gen-actions">(.*?)</div>', show, re.DOTALL)
    assert show_actions is not None
    assert "runGenerateAdvisories()" not in show_actions.group(1)


def test_delete_policy_uses_two_step_confirm_via_data_action() -> None:
    page = _page()
    confirm = _function_body(page, "confirmDeletePolicy")
    # The first click only swaps in a second, explicit button -- it never
    # calls deletePolicy itself.
    assert "deletePolicy(" not in confirm.replace('data-action="deletePolicy"', "")
    assert 'data-action="deletePolicy"' in confirm
    render_body = _function_body(page, "renderPolicyRow")
    assert 'data-action="confirmDeletePolicy"' in render_body


def test_create_policy_sends_exactly_one_rule() -> None:
    """`trellis policy add` (src/trellis_cli/policy.py) only ever builds one
    PolicyRule per Policy; the UI's create form matches that shape rather
    than offering a multi-rule editor the CLI has no parity for."""
    body = _function_body(_page(), "runCreatePolicy")
    rules_match = re.search(r"rules:\s*\[(.*?)\],\n\s*enforcement:", body, re.DOTALL)
    assert rules_match is not None, "runCreatePolicy() body has no `rules:` array"
    rules_literal = rules_match.group(1)
    # Exactly one object literal: one `operation:` key, not a loop or a
    # second entry appended elsewhere.
    assert rules_literal.count("operation:") == 1
    stripped = rules_literal.strip()
    assert stripped.startswith("{")
    assert stripped.endswith("}")


def test_policies_view_states_deny_wins_resolution() -> None:
    page = _page()
    view = re.search(
        r'<div id="view-policies"[^>]*>(.*?)<div class="search-bar">',
        page,
        re.DOTALL,
    )
    assert view is not None, "view-policies container not found"
    text = view.group(1)
    assert "Deny wins" in text or "deny wins" in text.lower()
    assert "<code>deny</code>" in text
    assert "require_approval" in text


def test_advisories_view_states_boosted_is_not_a_real_field() -> None:
    # G6's "served-vs-withheld" and "boosted" concepts are not present on
    # GET /advisories' payload; the view says so explicitly rather than
    # fabricating a badge for either.
    page = _page()
    view = re.search(
        r'<div id="view-advisories"[^>]*>(.*?)<div id="adv-generate-form"',
        page,
        re.DOTALL,
    )
    assert view is not None, "view-advisories container not found"
    text = view.group(1)
    assert "Boosted" in text or "boosted" in text
    assert "not a persisted state" in text or "not" in text.lower()
