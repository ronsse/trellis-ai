"""The Tag rules tab renders mined candidates and effective rules correctly.

Static assertions on ``index.html``'s own markup/script, mirroring the
pattern in ``test_loop_health_ui.py``: no browser, no JS engine -- pin the
exact code shape so a mutant that swaps a field name or drops a filter is
caught, not just "the right words appear somewhere in the file." The
governed Apply/Revoke behaviour itself (policy gates, precedence, audit
events) is covered end to end by ``test_classify_rules_routes.py`` and
``test_classify_rules_cli.py`` -- this file proves only the UI's wiring to
those same REST routes.

Five claims under test:

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
5. Apply, Dismiss, Mine and Revoke each go through an in-page confirmation
   step (no ``confirm()`` dialog) before the write fires; a degraded
   overlay store is rendered from both the GET responses and a write's own
   409 body (the generic ``postJSON``/``deleteJSON`` helpers drop that 409
   body, so the write paths use a local helper that keeps it -- see
   ``classifyRuleWriteJSON``); and the descriptive paragraph names no CLI
   command or config file, since this page ships in a public repo.
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
    assert 'data-action="confirmApplyTagCandidate"' in body
    assert 'data-action="confirmDismissTagCandidate"' in body


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
    post_call = _between(
        body, "await classifyRuleWriteJSON('POST', '/classify/rules', {", "});"
    )
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
    assert (
        "classifyRuleWriteJSON('DELETE', `/classify/rules/"
        "${encodeURIComponent(r.kind)}/" in body
    )
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


# ---------------------------------------------------------------------------
# 5. In-page confirmation, degraded-store banner wiring, no CLI names/paths.
# ---------------------------------------------------------------------------


def test_apply_and_dismiss_buttons_land_on_a_confirm_step_not_the_write() -> None:
    """The row's own buttons must name the confirm functions, and each
    confirm function must itself offer the real write action -- so the
    write is always one extra click behind the row, never direct.
    """
    page = _page()
    row_body = _function_body(
        page,
        "function renderTagCandidateRow(c) {",
        "\n        async function classifyRuleWriteJSON",
    )
    assert 'data-action="confirmApplyTagCandidate"' in row_body
    assert 'data-action="confirmDismissTagCandidate"' in row_body

    apply_confirm_body = _function_body(
        page,
        "function confirmApplyTagCandidate(candidateId) {",
        "\n        async function applyTagCandidate",
    )
    assert 'data-action="applyTagCandidate"' in apply_confirm_body
    assert "Confirm apply" in apply_confirm_body

    dismiss_confirm_body = _function_body(
        page,
        "function confirmDismissTagCandidate(candidateId) {",
        "\n        async function dismissTagCandidate",
    )
    assert 'data-action="dismissTagCandidate"' in dismiss_confirm_body
    assert "Confirm dismiss" in dismiss_confirm_body


def test_apply_confirm_copy_is_truthful_and_kind_specific() -> None:
    """The risk copy branches on ``c.kind``: a keyword rule's copy must
    name the classifier's real two-keyword threshold (``_MIN_HITS`` in
    ``keyword.py``) and say domain is never auto-assigned at write time;
    an alias rule's copy must say it only affects mining and listing.
    """
    page = _page()
    body = _function_body(
        page,
        "function confirmApplyTagCandidate(candidateId) {",
        "\n        async function applyTagCandidate",
    )
    assert "c.kind === 'domain_alias'" in body
    assert "at least two matching keywords" in body
    assert "never auto-tagged with domain at write time" in body
    assert "This only changes" in body
    assert "how future keyword/alias candidates are mined" in body


def test_dismiss_confirm_copy_states_dismissal_is_not_permanent() -> None:
    page = _page()
    body = _function_body(
        page,
        "function confirmDismissTagCandidate(candidateId) {",
        "\n        async function dismissTagCandidate",
    )
    assert "resurface the same candidate as open" in body
    assert "again &mdash; this only drops it from today's open list" in body


def test_mine_button_lands_on_a_confirm_step() -> None:
    page = _page()
    assert 'onclick="confirmMineTagCandidates()"' in page
    assert 'onclick="mineTagCandidates()"' in page
    body = _function_body(
        page,
        "function confirmMineTagCandidates() {",
        "\n        function cancelMineTagCandidates",
    )
    assert "Confirm mine" in body
    assert "only proposes" in body


def test_revoke_confirm_copy_states_no_retag_and_branches_on_kind() -> None:
    page = _page()
    body = _function_body(
        page,
        "function confirmRevokeTagRule(id) {",
        "\n        async function revokeTagRule",
    )
    assert "r && r.kind === 'domain_alias'" in body
    assert "does not retag any content" in body


def test_no_browser_confirm_or_alert_dialog_in_tag_rules_section() -> None:
    page = _page()
    section = _between(
        page,
        "// -- Tag rules: mined candidate review (Apply/Revoke) --",
        "// === Settings (e179) ===",
    )
    assert not re.search(r"(?<![A-Za-z0-9_])confirm\(", section)
    assert not re.search(r"(?<![A-Za-z0-9_])alert\(", section)


def test_classify_rule_write_json_attaches_payload_for_the_degraded_banner() -> None:
    """``postJSON``/``deleteJSON`` build only a flattened string message on
    a non-OK response and discard the parsed body, so a 409's
    ``store_degradation`` never reaches a caller through them (proven by
    ``writeSettingJSON`` needing its own copy of this for the Settings
    tab). Apply/Revoke write through the overlay store and can 409 the
    same way, so they need the same ``err.payload`` attachment.
    """
    page = _page()
    body = _function_body(
        page,
        "async function classifyRuleWriteJSON(method, path, body) {",
        "\n        // Risk copy here is traced",
    )
    assert "err.payload = payload;" in body
    assert "err.status = res.status;" in body


def test_apply_and_revoke_render_the_degraded_banner_from_a_409() -> None:
    page = _page()
    apply_body = _function_body(
        page,
        "async function applyTagCandidate(candidateId) {",
        "\n        // Dismissal is not a block",
    )
    assert (
        "err.payload && err.payload.detail && err.payload.detail.store_degradation"
        in apply_body
    )
    assert (
        "renderDegradedBanner('tagrules-candidates-banner', degradation)" in apply_body
    )

    revoke_body = _function_body(
        page, "async function revokeTagRule(id) {", "\n        initSidebar();"
    )
    assert (
        "err.payload && err.payload.detail && err.payload.detail.store_degradation"
        in revoke_body
    )
    assert (
        "renderDegradedBanner('tagrules-candidates-banner', degradation)" in revoke_body
    )


def test_both_list_loaders_render_the_degraded_banner_from_the_get_response() -> None:
    page = _page()
    candidates_body = _function_body(
        page,
        "async function loadTagRuleCandidates() {",
        "\n        function renderTagCandidateRow(c) {",
    )
    assert (
        "renderDegradedBanner('tagrules-candidates-banner', data.store_degradation);"
        in candidates_body
    )

    rules_body = _function_body(
        page,
        "async function loadEffectiveRules() {",
        "\n        function renderEffectiveRuleRow(r) {",
    )
    assert (
        "renderDegradedBanner('tagrules-candidates-banner', data.store_degradation);"
        in rules_body
    )


def test_tagrules_description_names_no_cli_command_or_config_file() -> None:
    """Operator-facing copy in a public repo must not leak a CLI command
    name or a specific config file path (B6) -- it can describe what a
    rule's source badge means without naming ``trellis classify
    tag-candidates``/``apply-rule`` or ``config.yaml`` by name.
    """
    page = _page()
    description = _between(
        page, 'id="view-tagrules"', 'id="tagrules-candidates-banner"'
    )
    assert "trellis classify" not in description
    assert "config.yaml" not in description
