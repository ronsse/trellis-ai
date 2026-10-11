"""The Tag rules tab renders mined candidates and effective rules correctly (e181).

Static assertions on ``index.html``'s own markup/script, mirroring the
pattern in ``test_loop_health_ui.py``: no browser, no JS engine -- pin the
exact code shape so a mutant that swaps a field name or drops a filter is
caught, not just "the right words appear somewhere in the file." The
governed Apply/Revoke behaviour itself (policy gates, precedence, audit
events) is covered end to end by ``test_classify_rules_routes.py`` and
``test_classify_rules_cli.py`` -- this file proves only the UI's wiring to
those same REST routes.

Four claims under test:

1. Nav link, view container and ``switchView`` dispatch are wired for the
   ``tagrules`` view, the same shape ``test_loop_health_ui.py`` pins for
   ``loops``.
2. The candidates table shows only ``status === "open"`` rows (an applied
   or dismissed candidate must not also offer an Apply button), and Apply
   submits the candidate's own ``source_event_id`` -- not its
   ``candidate_id`` -- as ``ClassifyRuleApplyRequest.source_event_id``,
   because the two are different fields on ``ClassifyCandidate``.
3. The effective-rules table offers Revoke only on ``source === "overlay"``
   rows: a ``default``/``config`` row was never written by the overlay
   store and the DELETE route refuses it.
4. A fetch failure on either table renders a visible error in its own
   tbody, not a silent empty state, and ``mineTagCandidates`` reads the
   mine response's actual field names (``tag_keyword_count`` /
   ``domain_alias_count``), not a count field that response never had.
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
# 1. Nav + view + switchView wiring.
# ---------------------------------------------------------------------------


def test_tagrules_view_is_wired_into_nav_and_switch_view() -> None:
    page = _page()
    assert 'data-view="tagrules" data-label="Tag rules"' in page
    assert 'id="view-tagrules"' in page
    assert re.search(r"views\s*=\s*\[[^\]]*'tagrules'[^\]]*\]", page)
    assert re.search(r"if\s*\(name === 'tagrules'\)\s*loadTagRules\(\);", page)


def test_tagrules_view_has_both_tables_and_their_tbodies() -> None:
    page = _page()
    view = _between(page, 'id="view-tagrules"', 'id="tagrules-rules-total"')
    assert 'id="tagrules-candidates-body"' in view
    assert 'id="tagrules-rules-body"' in view


# ---------------------------------------------------------------------------
# 2. Candidates: open-only listing, and apply carries source_event_id.
# ---------------------------------------------------------------------------


def test_candidates_list_is_filtered_to_open_status() -> None:
    page = _page()
    body = _function_body(
        page,
        "async function loadTagRuleCandidates() {",
        "\n        function renderTagCandidateRow(c) {",
    )
    assert re.search(r"\.filter\(c => c\.status === 'open'\)", body)


def test_open_candidate_row_offers_apply_and_dismiss_unconditionally() -> None:
    # renderTagCandidateRow is only ever called on the already-filtered
    # "open" rows (previous test), so it does not re-check status itself --
    # pin that every row it renders gets both actions, not a conditional
    # that could silently withhold one.
    page = _page()
    body = _function_body(
        page,
        "function renderTagCandidateRow(c) {",
        "\n        async function applyTagCandidate",
    )
    assert 'data-action="applyTagCandidate"' in body
    assert 'data-action="dismissTagCandidate"' in body


def test_apply_tag_candidate_sends_source_event_id_from_the_candidate_not_its_id() -> (
    None
):
    """``ClassifyCandidate.candidate_id`` and ``.source_event_id`` are two
    different fields (``dtos.py::ClassifyCandidate``) -- the apply request
    must carry the latter, or every applied rule's audit trail points at
    nothing.
    """
    page = _page()
    body = _function_body(
        page,
        "async function applyTagCandidate(candidateId) {",
        "\n        async function dismissTagCandidate",
    )
    assert "const c = tagCandidatesCache[candidateId];" in body
    post_call = _between(body, "await postJSON('/classify/rules', {", "});")
    assert "source_event_id: c.source_event_id," in post_call
    assert "candidateId" not in post_call
    assert "kind: c.kind," in post_call
    assert "key: c.key," in post_call
    assert "target: c.target," in post_call


def test_apply_tag_candidate_falls_back_to_candidates_cache_not_dom_attributes() -> (
    None
):
    # ID_ACTIONS dispatches with only dataset.id (test_ui_inline_handlers.py
    # ::test_every_data_action_reaches_a_function_with_its_id), so the row's
    # kind/key/target/metrics must come from a lookup, not extra data-*
    # attributes no dispatcher reads.
    page = _page()
    row_body = _function_body(
        page,
        "function renderTagCandidateRow(c) {",
        "\n        async function applyTagCandidate",
    )
    apply_button = _between(row_body, '<td id="tagcand-actions-', "</td>")
    assert "data-kind=" not in apply_button
    assert "data-key=" not in apply_button
    assert "data-target=" not in apply_button


def test_dismiss_candidate_calls_the_dismiss_route_with_no_body() -> None:
    page = _page()
    body = _function_body(
        page,
        "async function dismissTagCandidate(candidateId) {",
        "\n        async function mineTagCandidates",
    )
    assert "/classify/candidates/${encodeURIComponent(candidateId)}/dismiss`" in body
    assert "postJSON(`/classify/candidates/" in body


# ---------------------------------------------------------------------------
# 3. Effective rules: revoke only on overlay-sourced rows.
# ---------------------------------------------------------------------------


def test_revoke_button_is_gated_on_overlay_source() -> None:
    page = _page()
    body = _function_body(
        page,
        "function renderEffectiveRuleRow(r) {",
        "\n        function confirmRevokeTagRule",
    )
    assert re.search(r"r\.source === 'overlay'\s*\n?\s*\?", body)
    # The non-overlay branch of the ternary renders no revoke action.
    overlay_branch = _between(body, "? `", "`\n                : '';")
    assert "confirmRevokeTagRule" in overlay_branch


def test_revoke_tag_rule_reads_kind_and_key_from_the_rules_cache() -> None:
    page = _page()
    body = _function_body(
        page, "async function revokeTagRule(id) {", "\n        initSidebar();"
    )
    assert "const r = effectiveRulesCache[id];" in body
    assert "deleteJSON(`/classify/rules/${encodeURIComponent(r.kind)}/" in body
    assert "${encodeURIComponent(r.key)}`)" in body


# ---------------------------------------------------------------------------
# 4. Fetch failures are visible, and mine reads the real response fields.
# ---------------------------------------------------------------------------


def test_candidates_fetch_failure_renders_visible_error() -> None:
    page = _page()
    body = _function_body(
        page,
        "async function loadTagRuleCandidates() {",
        "\n        function renderTagCandidateRow(c) {",
    )
    catch_block = _between(body, "} catch (err) {", "\n            }\n        }")
    assert "tbody.innerHTML" in catch_block
    assert "err.message" in catch_block
    assert "color:var(--error)" in catch_block


def test_rules_fetch_failure_renders_visible_error() -> None:
    page = _page()
    body = _function_body(
        page,
        "async function loadEffectiveRules() {",
        "\n        function renderEffectiveRuleRow(r) {",
    )
    catch_block = _between(body, "} catch (err) {", "\n            }\n        }")
    assert "tbody.innerHTML" in catch_block
    assert "err.message" in catch_block
    assert "color:var(--error)" in catch_block


def test_mine_reads_the_responses_actual_count_field_names() -> None:
    """``ClassifyMineResponse`` (``dtos.py``) is
    ``{status, tag_keyword_count, domain_alias_count,
    used_recommended_defaults, candidates}`` -- a mismatched field name
    reads ``undefined`` and renders "Mined undefined ... candidate(s)"
    forever, silently.
    """
    page = _page()
    body = _function_body(
        page,
        "async function mineTagCandidates() {",
        "\n        async function loadEffectiveRules",
    )
    assert "r.tag_keyword_count" in body
    assert "r.domain_alias_count" in body
    assert "tag_keyword_candidate_count" not in body
    assert "domain_alias_candidate_count" not in body
