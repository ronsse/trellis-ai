"""The Schedules tab's UI-level fixes from gate #860 stay fixed.

Three defects, pinned here because none of them is reachable from a
Python-level API test — they are properties of the generated JavaScript
source itself, following the same source-regex convention as
``test_ui_review_truth.py`` and ``test_ui_p2_sweep.py``:

1. Blocker 3: the Edit form read the raw cadence back out of a table
   cell's rendered ``textContent``, which also carries the human cadence
   description and, for a customized job, a "catalog default: ..." line
   — so an unchanged Save on a customized job silently rewrote the
   cadence to that second line's text. It now reads a dedicated
   ``data-cadence`` attribute the cell carries alongside its display
   text, and ``confirmEditJob`` never re-derives cadence from the row at
   all — only from the input the operator sees.
2. Blocker 4 (UI half): due-jobs evaluates every cadence and last-run
   timestamp in UTC. ``describeCron()`` labels every cadence form "UTC"
   rather than leaving the viewer to assume their own browser zone.
3. Blocker 5: the dispatcher banner used to read "nothing will pick it
   up" when no job-run had been recorded recently, even though a healthy
   dispatcher's own ``due-jobs`` polls leave no record at all — so the
   claim was false exactly when nothing was wrong. It now states only
   what the EventLog actually shows, and renders nothing at all once a
   run is seen.

Gate #860b, follow-ups 7 and 8 add two more defects of the same kind:

7. The Edit button had no ``disabledAttr`` guard, unlike the checkbox
   and "Run now" button on the same row, so clicking Edit then Save on
   an unstored row silently enrolled it — the same bug blocker-pattern
   already fixed for "Run now", just on a control that was missed.
8. The timeout field is pre-filled with the row's *effective* timeout
   (the catalog default when no override is stored), not its raw
   stored value, so an unchanged Save always re-sent that number as
   an explicit override — pinning a null override to the shown
   default. ``confirmEditJob`` now compares the field's current value
   against the value it was pre-filled with and omits
   ``timeout_seconds`` from the PATCH body entirely when they match.
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


def _show_edit_job_form_body(page: str) -> str:
    return _between(
        page,
        "function showEditJobForm(name) {",
        "\n        function cancelEditJobForm() {",
    )


def _confirm_edit_job_body(page: str) -> str:
    return _between(
        page,
        "async function confirmEditJob(name) {",
        "\n        function confirmRunJob(name) {",
    )


def _render_schedule_row_body(page: str) -> str:
    return _between(
        page,
        "function renderScheduleRow(job, storeDegraded, dispatcherSeen) {",
        "\n        function toggleJobEnabled(name) {",
    )


# ---------------------------------------------------------------------------
# Blocker 3: cadence comes from a data attribute, never textContent.
# ---------------------------------------------------------------------------


def test_edit_form_reads_cadence_from_data_attribute_not_textcontent() -> None:
    body = _show_edit_job_form_body(_page())
    assert "cells[2].dataset.cadence" in body
    # The cadence cell's own textContent also carries the human
    # description and an optional "catalog default: ..." line -- reading
    # that back as the cadence is exactly the bug gate #860 found, so its
    # absence here is the regression this test exists to catch.
    assert "cells[2].textContent" not in body


def test_cadence_cell_carries_a_data_cadence_attribute() -> None:
    page = _page()
    assert "data-cadence=\"${escHtml(job.cadence || '')}\"" in page


# ---------------------------------------------------------------------------
# Blocker 4 (UI half): every cadence form is explicitly labelled UTC.
# ---------------------------------------------------------------------------


def test_describe_cron_labels_every_cadence_form_utc() -> None:
    page = _page()
    body = _between(page, "function describeCron(cadence) {", "\n        }\n")
    assert "Daily at" in body
    assert "Weekly on" in body
    assert "Monthly on day" in body
    for line in body.splitlines():
        if "Daily at" in line or "Weekly on" in line or "Monthly on day" in line:
            assert "UTC" in line, f"cadence description not labelled UTC: {line!r}"
    # No wording anywhere in the page claims a cadence or run time is the
    # host's own local clock -- due-jobs evaluates UTC unconditionally
    # (trellis.core.cron), so any such claim would be false.
    assert "local time" not in page.lower()
    assert "local clock" not in page.lower()


# ---------------------------------------------------------------------------
# Blocker 5: the dispatcher banner never claims "nothing will pick it up".
# ---------------------------------------------------------------------------


def test_dispatcher_banner_never_claims_nothing_will_pick_it_up() -> None:
    page = _page()
    body = _between(
        page,
        "function renderDispatcherBanner(seen, lastSeenAt) {",
        "\n        }\n",
    )
    # Only the *rendered* HTML (the el.innerHTML template literal) can
    # ever reach an operator -- a surrounding code comment is allowed to
    # name the retired phrase while explaining why it was removed, so
    # scope the check to the template literal itself, not the whole
    # function body.
    rendered = _between(body, "el.innerHTML = `", "`;")
    assert "nothing will pick" not in rendered.lower()
    # A missing poll record is not evidence of a dead dispatcher -- the
    # banner must say so rather than asserting the dispatcher is gone.
    assert "not proof" in rendered.lower() or "leave no record" in rendered.lower()


def test_dispatcher_banner_renders_nothing_once_a_run_is_seen() -> None:
    body = _between(
        _page(),
        "function renderDispatcherBanner(seen, lastSeenAt) {",
        "\n        }\n",
    )
    assert "if (seen) { el.innerHTML = ''; return; }" in body


# ---------------------------------------------------------------------------
# Gate #860b, follow-up 7: Edit is gated the same way Run now already is.
# ---------------------------------------------------------------------------


def test_edit_button_is_disabled_on_an_unstored_or_degraded_row() -> None:
    body = _render_schedule_row_body(_page())
    edit_button = _between(body, 'data-action="showEditJobForm"', "</button>")
    run_now_button = _between(body, 'data-action="confirmRunJob"', "</button>")
    # Both controls gate on the same computed attribute as the checkbox --
    # Edit+Save on an unstored row must not silently enroll it, exactly
    # like Run now already refuses one (gate #860b, follow-up 7).
    edit_prefix = body[: body.index('data-action="showEditJobForm"')]
    run_now_prefix = body[: body.index('data-action="confirmRunJob"')]
    assert "${disabledAttr}" in edit_prefix.splitlines()[-1]
    assert "${disabledAttr}" in run_now_prefix.splitlines()[-1]
    del edit_button, run_now_button  # only the gating prefix matters here


# ---------------------------------------------------------------------------
# Gate #860b, follow-up 8: an unchanged Save never pins timeout_seconds.
# ---------------------------------------------------------------------------


def test_timeout_field_remembers_its_prefilled_effective_value() -> None:
    body = _show_edit_job_form_body(_page())
    # The field is pre-filled from the row's *effective* (never-null)
    # timeout, so confirmEditJob needs a separate record of that
    # pre-filled value to detect "unchanged" -- stashed in a data
    # attribute, the same pattern data-cadence already uses for blocker 3.
    assert 'id="sched-edit-timeout"' in body
    assert "data-initial-value=" in body


def test_confirm_edit_job_omits_timeout_when_the_field_is_unchanged() -> None:
    body = _confirm_edit_job_body(_page())
    # confirmEditJob must compare the live field value against the
    # pre-filled one and gate the body.timeout_seconds assignment on that
    # comparison -- never assign it unconditionally whenever the field is
    # merely non-empty (the pre-filled field is *always* non-empty, which
    # is exactly how gate #860b's null->3600 pinning happened).
    assert "dataset.initialValue" in body
    assignment_index = body.index("body.timeout_seconds = t;")
    comparison_index = body.index("timeoutRaw !== (")
    assert comparison_index != -1
    assert comparison_index < assignment_index, (
        "body.timeout_seconds must be assigned only inside the "
        "unchanged-value comparison, not unconditionally on a non-empty field"
    )


# ---------------------------------------------------------------------------
# The dispatcher banner states plainly when nothing is observed, rows stay
# editable regardless, and "Next due" never implies a run will actually
# happen without a dispatcher.
# ---------------------------------------------------------------------------


def _render_dispatcher_banner_body(page: str) -> str:
    return _between(
        page,
        "function renderDispatcherBanner(seen, lastSeenAt) {",
        "\n        }\n",
    )


def test_dispatcher_banner_states_edits_are_saved_but_nothing_runs() -> None:
    body = _render_dispatcher_banner_body(_page())
    rendered = _between(body, "el.innerHTML = `", "`;")
    assert "not observed" in rendered.lower()
    assert "saved" in rendered.lower()
    assert "nothing runs" in rendered.lower() or "nothing here runs" in rendered.lower()
    assert "installed" in rendered.lower()


def _job_flag_badges_body(page: str) -> str:
    return _between(
        page,
        "function jobFlagBadges(job, dispatcherSeen) {",
        "\n        function lastRunText(lastRun) {",
    )


def test_host_only_row_gets_a_not_running_badge_when_dispatcher_unobserved() -> None:
    body = _job_flag_badges_body(_page())
    assert "if (job.host_only)" in body
    assert "if (!dispatcherSeen)" in body
    assert "not running" in body


def test_schedule_row_stays_editable_regardless_of_dispatcher_state() -> None:
    body = _render_schedule_row_body(_page())
    # disabledAttr must depend only on stored/degraded state, never on
    # whether a dispatcher has been observed -- a row stays editable even
    # when nothing has run it recently.
    disabled_line = next(
        line for line in body.splitlines() if "const disabledAttr" in line
    )
    assert "dispatcherSeen" not in disabled_line
    assert "job.stored" in disabled_line
    assert "storeDegraded" in disabled_line


def test_next_due_cell_warns_when_dispatcher_is_not_observed() -> None:
    body = _render_schedule_row_body(_page())
    assert "notRunningNote" in body
    assert "dispatcherSeen" in body
    # The caveat text itself must not claim the opposite: that a run is
    # guaranteed when no dispatcher has been observed.
    note_assignment = _between(
        body, "const notRunningNote = dispatcherSeen", "\n            const nextDueCell"
    )
    assert (
        "may not run" in note_assignment.lower()
        or "not observed" in note_assignment.lower()
    )


# ---------------------------------------------------------------------------
# No internal store/dispatcher implementation name survives in rendered
# text or a tooltip on the Schedules tab.
# ---------------------------------------------------------------------------


def test_no_internal_implementation_names_render_on_schedules_tab() -> None:
    page = _page()
    view = _between(
        page, '<div id="view-schedules"', '\n        <div id="view-tagrules"'
    )
    for forbidden in ("JOB_CATALOG", "due-jobs", "schedule.json"):
        assert forbidden not in view, (
            f"{forbidden!r} leaked into rendered Schedules markup"
        )
    body = _render_schedule_row_body(page)
    for forbidden in ("JOB_CATALOG", "due-jobs", "schedule.json"):
        assert forbidden not in body, f"{forbidden!r} leaked into renderScheduleRow"


# ---------------------------------------------------------------------------
# Toggling `enabled` asks first, inline, and never PATCHes until a
# separate Confirm click.
# ---------------------------------------------------------------------------


def _toggle_job_enabled_body(page: str) -> str:
    return _between(
        page,
        "function toggleJobEnabled(name) {",
        "\n        async function confirmToggleJobEnabled(name) {",
    )


def test_toggle_job_enabled_asks_before_patching() -> None:
    body = _toggle_job_enabled_body(_page())
    # The handler that flips the checkbox must never itself call patchJSON
    # -- that only happens from confirmToggleJobEnabled, after an explicit
    # Confirm click.
    assert "patchJSON" not in body
    assert 'data-action="confirmToggleJobEnabled"' in body
    assert 'data-action="cancelToggleJobEnabled"' in body


def test_confirm_toggle_job_enabled_is_the_only_place_that_patches_it() -> None:
    page = _page()
    confirm_body = _between(
        page,
        "async function confirmToggleJobEnabled(name) {",
        "\n        function cancelToggleJobEnabled(name) {",
    )
    assert "patchJSON(" in confirm_body
    assert "{ enabled: checked }" in confirm_body


def test_toggle_consequence_names_the_loops_a_job_drives() -> None:
    page = _page()
    body = _between(
        page,
        "function toggleEnabledConsequence(name, enabling) {",
        "\n        function toggleJobEnabled(name) {",
    )
    assert "scheduleJobsCache" in body
    assert "job.loops" in body


# ---------------------------------------------------------------------------
# The Loops <-> Schedules join is visible from both tabs.
# ---------------------------------------------------------------------------


def test_schedule_row_names_the_loops_it_drives_and_links_to_loops_tab() -> None:
    body = _render_schedule_row_body(_page())
    assert "job.loops" in body
    assert "switchView('loops')" in body


def test_loop_row_names_its_job_and_links_to_schedules_tab() -> None:
    page = _page()
    body = _between(
        page,
        "function renderLoopRow(loop) {",
        "\n        async function loadReportsTab() {",
    )
    assert "loop.job_name" in body
    assert "switchView('schedules')" in body


# ---------------------------------------------------------------------------
# "Use catalog default" explicitly clears a stored timeout override by
# PATCHing an explicit null, never by leaving the field blank (which
# confirmEditJob treats as a no-op, by design -- see above).
# ---------------------------------------------------------------------------


def test_use_catalog_default_button_exists_in_the_edit_form() -> None:
    body = _show_edit_job_form_body(_page())
    assert 'data-action="resetJobTimeoutToDefault"' in body


def test_reset_job_timeout_to_default_patches_an_explicit_null() -> None:
    page = _page()
    body = _between(
        page,
        "async function resetJobTimeoutToDefault(name) {",
        "\n        function confirmRunJob(name) {",
    )
    assert "{ timeout_seconds: null }" in body
    assert "patchJSON(" in body


# ---------------------------------------------------------------------------
# Every nav data-view entry has a matching view-<name> container, and
# every view-<name> container has a matching nav entry -- the JS `views`
# array is derived from the nav DOM, so this is the only place that can
# actually drift.
# ---------------------------------------------------------------------------


def test_nav_data_views_and_view_containers_match_bidirectionally() -> None:
    page = _page()
    nav = _between(page, '<nav class="sidebar-nav">', "\n        </nav>")
    nav_views = set(re.findall(r'data-view="([a-z0-9_-]+)"', nav))
    assert nav_views, "expected at least one nav data-view entry"
    # Pass 6 converted each container's inline style="display:none" (always
    # overridden by switchView()'s explicit block/none on every call,
    # dashboard's initial render included) to the shared ".hide" class.
    container_views = set(
        re.findall(r'<div id="view-([a-z0-9_-]+)" class="hide">', page)
    )
    assert container_views, "expected at least one view-<name> container"
    missing_containers = nav_views - container_views
    missing_nav_entries = container_views - nav_views
    assert not missing_containers, (
        f"nav links with no view container: {missing_containers}"
    )
    assert not missing_nav_entries, (
        f"view containers with no nav link: {missing_nav_entries}"
    )


# ---------------------------------------------------------------------------
# patchJSON formats a 422 as "field: message", never pydantic's raw-value
# echo (`input`).
# ---------------------------------------------------------------------------


def _format_patch_validation_detail_body(page: str) -> str:
    return _between(
        page,
        "function formatPatchValidationDetail(errors) {",
        "\n        async function patchJSON(path, body) {",
    )


def _patch_json_body(page: str) -> str:
    return _between(
        page,
        "async function patchJSON(path, body) {",
        "\n        async function deleteJSON(path) {",
    )


def test_format_patch_validation_detail_never_echoes_raw_input() -> None:
    body = _format_patch_validation_detail_body(_page())
    assert "e.loc" in body
    assert "e.msg" in body
    # `input` is pydantic's echo of the raw value sent -- it can carry a
    # secret or just-typed credential back to the operator verbatim, so
    # this formatter must never read it.
    assert "e.input" not in body
    assert ".input" not in body


def test_patch_json_routes_a_422_array_through_the_formatter() -> None:
    body = _patch_json_body(_page())
    assert "Array.isArray(d)" in body
    assert "formatPatchValidationDetail(d)" in body
    # The Array.isArray branch must come before the generic JSON.stringify
    # fallback, or a 422 array matches that branch first and renders as
    # raw JSON (which is itself fine data-wise, but defeats the "field:
    # message" formatting this exists to add).
    array_branch = body.index("Array.isArray(d)")
    generic_branch = body.index("detail = JSON.stringify(d);")
    assert array_branch < generic_branch
