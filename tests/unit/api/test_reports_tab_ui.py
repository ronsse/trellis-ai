"""The Reports tab (G15) and the readiness pill (G14) render truthfully.

Static assertions on ``index.html``'s own markup/script, mirroring the
pattern in ``test_loop_health_ui.py``: no browser, no JS engine -- pin the
exact code shape so a mutant that drops a branch or an escape call is
caught, not just "the right words appear somewhere in the file."

Claims under test:

1. The Reports tab is wired into the sidebar nav, the ``views`` array and
   ``switchView``'s lazy-load dispatch, same as every other tab.
2. A fetch failure on ``GET /api/v1/analyze`` (the report list) or on
   ``GET /api/v1/analyze/{report}`` (running one) renders a visible error,
   not a silently empty picker or output panel.
3. Every value ``renderReportData`` places into the DOM goes through
   ``escHtml`` -- report keys, scalar values and the degraded-store detail
   blob are all agent/operator-influenced strings.
4. A degraded advisory store's payload (``{"status": "degraded", ...}``) is
   rendered as a visible refusal, not folded into the generic scalar-card
   path where ``"degraded"`` would just look like a normal value.
5. The readiness pill (G14) is fed by the same ``/readyz`` fetch
   ``init()`` already makes (no second request), and distinguishes
   ready / degraded / unknown with a hover title -- never defaulting
   silently to "ready".
6. The picker's ``<option>`` text is each report's human ``title``
   (B15), with the raw report ``name`` demoted to secondary text next
   to the description -- never the primary label.
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


# ---------------------------------------------------------------------------
# 1. Nav / views / switchView wiring.
# ---------------------------------------------------------------------------


def test_reports_view_is_wired_into_nav_and_switch_view() -> None:
    page = _page()
    assert 'data-view="reports" data-label="Reports"' in page
    assert 'id="view-reports"' in page
    # `views` is derived from the nav's own DOM order (see
    # test_ui_keyboard_shortcuts.py), not a hand-maintained literal, so the
    # nav tag asserted above is what puts "reports" in the derived list --
    # pin the derivation mechanism itself rather than a stale array shape.
    assert "const views = navLinks.map(a => a.dataset.view);" in page
    assert re.search(r"if\s*\(name === 'reports'\)\s*loadReportsTab\(\);", page)


# ---------------------------------------------------------------------------
# 2. Fetch failures are visible, never swallowed.
# ---------------------------------------------------------------------------


def test_load_reports_tab_does_not_auto_run_a_report() -> None:
    """Opening the tab populates the picker and the param form but does not
    call ``runReport()`` itself -- the operator picks a report and clicks
    Run. (A report can be expensive; running the first one in the list on
    every visit, including visits that only wanted a different report, was
    unasked-for work.) The empty output panel says so instead of reading
    as blank/broken."""
    page = _page()
    body = _between(
        page,
        "async function loadReportsTab() {",
        "\n        function onReportPickerChange() {",
    )
    try_block = _between(body, "onReportPickerChange();", "} catch (err) {")
    assert "runReport()" not in try_block
    # Pass 5 moved every empty-state site onto the shared
    # ``emptyStateHtml()`` component (brief row 3); this call site's own
    # source no longer spells the literal class name.
    assert "emptyStateHtml(" in try_block


def test_load_reports_tab_fetch_failure_renders_visible_error() -> None:
    page = _page()
    body = _between(
        page,
        "async function loadReportsTab() {",
        "\n        function onReportPickerChange() {",
    )
    catch_block = _between(body, "} catch (err) {", "\n            }\n        }")
    assert "outputEl.innerHTML" in catch_block
    # Reports is admin-only: loading the list goes through the shared
    # helpers so a merely-under-scoped key dims instead of reading as the
    # same red "Failed to load" text a genuine error gets (W1D ui-pass4,
    # see test_ui_gating.py's TestAdminScopeErrorRenderingConsistency).
    # Running one specific report (below) is a secondary action, not a
    # panel load, and is unaffected -- it still renders a plain error.
    assert "loadFailureText(err, " in catch_block
    assert "loadFailureColor(err)" in catch_block


def test_run_report_fetch_failure_renders_visible_error() -> None:
    page = _page()
    body = _between(
        page,
        "async function runReport() {",
        "\n        function renderReportOutput() {",
    )
    catch_block = _between(body, "} catch (err) {", "\n            }\n        }")
    assert "outputEl.innerHTML" in catch_block
    assert "err.message" in catch_block
    assert "color:var(--danger)" in catch_block


# ---------------------------------------------------------------------------
# 3. escHtml on every value renderReportData places into the DOM.
# ---------------------------------------------------------------------------


def _render_report_data_body(page: str) -> str:
    sidebar_rail_marker = "\n        // " + "=" * 60 + "\n        //  SIDEBAR RAIL"
    return _between(
        page,
        "function renderReportData(data) {",
        sidebar_rail_marker,
    )


def test_render_report_data_escapes_keys_and_scalar_values() -> None:
    body = _render_report_data_body(_page())
    assert "escHtml(key)" in body
    # The scalar-card branch escapes the value when it isn't null.
    assert re.search(r"escHtml\(val\)", body)


def test_render_report_data_escapes_degraded_detail_blob() -> None:
    body = _render_report_data_body(_page())
    degraded_branch = _between(body, "=== 'degraded') {", "\n            }")
    assert "escHtml(JSON.stringify(data.store_degradation" in degraded_branch


# ---------------------------------------------------------------------------
# 4. Degraded status is a distinct, visible branch -- not a generic card.
# ---------------------------------------------------------------------------


def test_degraded_status_is_checked_before_the_generic_object_path() -> None:
    page = _page()
    body = _render_report_data_body(page)
    degraded_idx = body.index("data.status === 'degraded'")
    generic_object_idx = body.index("const scalarEntries = []")
    assert degraded_idx < generic_object_idx
    degraded_branch = _between(body, "=== 'degraded') {", "\n            }")
    assert "color:var(--danger)" in degraded_branch


# ---------------------------------------------------------------------------
# 5. Readiness pill: fed by the existing /readyz fetch, three distinct
#    states, no silent default to "ready".
# ---------------------------------------------------------------------------


def _render_readiness_pill_body(page: str) -> str:
    return _between(
        page,
        "function renderReadinessPill(ready) {",
        "\n        async function init() {",
    )


def test_readiness_pill_reuses_the_existing_readyz_fetch() -> None:
    page = _page()
    # Exactly one /readyz fetch in the whole file -- the pill must consume
    # init()'s existing probe, not issue a second request.
    assert page.count("fetch('/readyz'") == 1
    assert re.search(r"renderReadinessPill\(ready\);", page)


def test_readiness_pill_distinguishes_ready_degraded_unknown() -> None:
    body = _render_readiness_pill_body(_page())
    assert "statusVal === 'ready'" in body
    assert "el.classList.add('is-ready')" in body
    assert "statusVal === 'degraded' || statusVal === 'initializing'" in body
    assert "el.classList.add('is-degraded')" in body
    # The fallback (unreachable probe, or an unrecognised status string)
    # is its own branch, not folded into "ready".
    assert "el.classList.add('is-unknown')" in body


def test_readiness_pill_degraded_branch_names_failing_backends_when_known() -> None:
    body = _render_readiness_pill_body(_page())
    degraded_branch = _between(
        body,
        "statusVal === 'degraded' || statusVal === 'initializing') {",
        "\n                return;",
    )
    assert re.search(r"b\.status\s*!==\s*'ok'", degraded_branch)
    assert "escHtml" in degraded_branch


def test_readiness_pill_css_classes_exist() -> None:
    # Pass 5 (topbar polish) turned the pill from a solid colour swatch
    # into a dot-plus-text pill (brief row 1): the status colour now
    # lives on the ``::before`` dot, not the pill's own background, so
    # the status word stays legible against ``--text-dim``.
    page = _page()
    assert ".readiness-pill.is-ready::before { background: var(--ok); }" in page
    assert ".readiness-pill.is-degraded::before { background: var(--danger); }" in page
    assert ".readiness-pill.is-unknown::before { background: var(--text-dim); }" in page


# ---------------------------------------------------------------------------
# 6. Picker option text is the title; the raw name is secondary text.
# ---------------------------------------------------------------------------


def test_picker_option_text_is_the_title_not_the_raw_name() -> None:
    page = _page()
    found = re.search(r"picker\.innerHTML = reports\.map\(r =>\s*\n\s*`([^`]*)`", page)
    assert found is not None, "picker option template literal not found"
    option_template = found.group(1)
    assert 'value="${escHtml(r.name)}"' in option_template
    assert ">${escHtml(r.title || r.name)}</option>" in option_template


def test_report_description_shows_the_raw_name_as_secondary_text() -> None:
    body = _between(
        _page(),
        "function onReportPickerChange() {",
        "\n        async function runReport",
    )
    assert "report-description" in body
    assert "escHtml(name)" in body
    assert "<code" in body
