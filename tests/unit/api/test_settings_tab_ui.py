"""The Settings tab stops implying things the registry response doesn't say.

Three defects, pinned one test each:

1. The table never showed which surfaces (MCP, the REST API, the CLI) a
   setting's own real enforcement site reads — an operator had no way to
   tell from the UI whether changing a knob would affect the one surface
   they care about. A "Read by" column now renders ``surfaces`` straight
   from the registry response, with no hardcoded knob list of its own.
2. ``formatSettingBounds`` rendered every ``minimum`` as "min N", which
   reads as "N is accepted". For the one entry whose ``minimum`` is a
   *strict* lower bound (``minimum_exclusive``), N is refused, not
   accepted, so that case now renders "> N" instead.
3. A setting whose shipped default is "unset" (``default: null`` /
   ``effective_value: null``) rendered the literal text "null" — in the
   effective-value cell via ``String(null)``, and in the editable input's
   ``value`` attribute via the same — because ``String()`` turns
   JavaScript's ``null`` into a real four-character string. Both now go
   through a shared "(unset)" / empty-field treatment instead.
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
# 1. "Read by" reflects the real `surfaces` field
# ---------------------------------------------------------------------------


def test_settings_table_has_a_read_by_column() -> None:
    # The Settings view renders as a div-grid of `.setting` rows (the
    # Ledger pass), not a `<table>`; each row's `.meta` strip is what used
    # to be the table's "Read by" column.
    page = _page()
    body = _function_body(
        page,
        "function renderSettingRow(s, groups, degraded) {",
        "function coerceSettingValue(",
    )
    assert "<span>Read by ${formatSurfaces(s.surfaces)}</span>" in body


def test_read_by_cell_renders_the_real_surfaces_field_not_a_fixed_list() -> None:
    page = _page()
    row_body = _function_body(
        page,
        "function renderSettingRow(s, groups, degraded) {",
        "function coerceSettingValue(",
    )
    assert "formatSurfaces(s.surfaces)" in row_body

    format_surfaces_body = _function_body(
        page, "function formatSurfaces(surfaces) {", "function formatSettingValue(v) {"
    )
    # Reads straight from the response field, never a hardcoded roster of
    # setting names -- a new surface still renders (falls back to its own
    # uppercase spelling) rather than disappearing.
    assert ".map(" in format_surfaces_body
    assert "SURFACE_DISPLAY_NAMES[s] || s.toUpperCase()" in format_surfaces_body


# ---------------------------------------------------------------------------
# 2. minimum_exclusive renders "> N", not "min N"
# ---------------------------------------------------------------------------


def test_minimum_exclusive_renders_strict_inequality_not_min() -> None:
    page = _page()
    body = _function_body(
        page, "function formatSettingBounds(s) {", "// Surface codes as the registry"
    )
    assert "s.minimum_exclusive" in body
    assert "`> ${s.minimum}`" in body
    assert "`min ${s.minimum}`" in body  # the inclusive case must still exist


# ---------------------------------------------------------------------------
# 3. An unset (null) default/effective value never renders as "null"
# ---------------------------------------------------------------------------


def test_format_setting_value_treats_null_as_unset_not_the_string_null() -> None:
    page = _page()
    body = _function_body(
        page, "function formatSettingValue(v) {", "function renderSettingsTable() {"
    )
    assert "v === null || v === undefined" in body
    assert "'(unset)'" in body
    # And it must not simply fall through to String(v), which is how the
    # literal "null" got rendered in the first place.
    assert "String(v)" in body  # still used for the non-null branch


def test_effective_value_cell_goes_through_the_null_safe_formatter() -> None:
    page = _page()
    row_body = _function_body(
        page,
        "function renderSettingRow(s, groups, degraded) {",
        "function coerceSettingValue(",
    )
    assert "formatSettingValue(s.effective_value)" in row_body
    # The literal that used to leak "null" into the cell.
    assert "String(s.effective_value)" not in row_body


def test_the_value_input_never_puts_the_literal_null_in_its_value_attribute() -> None:
    page = _page()
    body = _function_body(
        page,
        "function renderValueInput(s, inputId, currentValue, disabled) {",
        "function renderSettingActions(",
    )
    assert "currentValue === null || currentValue === undefined" in body
    assert "? ''" in body
    # Both the number and text branches must read the null-safe variable,
    # not currentValue directly, inside escHtml().
    assert "escHtml(rawValue)" in body
    assert "escHtml(String(currentValue))" not in body


# ---------------------------------------------------------------------------
# 4. settings_writable === false renders the row read-only, never a live
#    Save/Reset form the write would just refuse (B3, re-gate #867)
# ---------------------------------------------------------------------------


def test_a_non_writable_row_never_renders_save_or_reset_actions() -> None:
    """``graph_max_subgraph_depth`` ships ``settings_writable: false`` --
    its row must not render ``renderSettingActions`` (the Save/Reset
    form), which a click would only have sent into a write the handler
    refuses. Degraded-store disabling is a *different* mechanism (grey
    buttons that stay in the DOM); this one removes them outright.
    """
    page = _page()
    body = _function_body(
        page,
        "function renderSettingRow(s, groups, degraded) {",
        "function coerceSettingValue(",
    )
    assert "s.settings_writable === false" in body
    # The Ledger pass wraps this ternary onto its own line; match the
    # operator loosely rather than pinning the exact whitespace.
    assert re.search(r"notWritable\s*\?", body)
    assert "renderSettingActions(s, degraded)" in body  # still the writable branch


def test_a_non_writable_row_names_env_only_instead_of_restart_or_live() -> None:
    """The "Live"/"Restart required" badge is also a claim about what a
    write would do -- a non-writable row must not show either, since a
    write is refused outright, not merely delayed to the next restart.
    """
    page = _page()
    body = _function_body(
        page,
        "function renderSettingRow(s, groups, degraded) {",
        "function coerceSettingValue(",
    )
    live_html_stmt = _between(body, "const liveHtml = ", ";\n")
    assert "notWritable" in live_html_stmt


# ---------------------------------------------------------------------------
# 5. settingEnvPinNote has its OWN env-only branch, separate from
#    renderSettingRow's notWritable badge (B3, re-gate #867 M34)
# ---------------------------------------------------------------------------


def test_env_only_rows_get_a_dedicated_pin_note_not_the_generic_one() -> None:
    """``settingEnvPinNote`` has two independent branches: one for a knob
    that is *always* env-only (``settings_writable === false``, B3 --
    read only once at process start, the settings store never consulted)
    and one for a knob that merely happens to be pinned by the env right
    now (``overridden_by === 'env'``, which a non-writable knob can never
    even reach since it has no settings-store path to pin). A mutant that
    replaces the first branch's own guard with a bare ``false`` collapses
    it into dead code: every env-only row would fall through to the
    second branch (or to the empty-string default), silently losing the
    "read from ``ENV_VAR`` only ... never consulted" wording this knob
    depends on being told apart from an ordinary env override. This is
    a separate guard from ``renderSettingRow``'s ``notWritable`` (pinned
    above), which only gates the Save/Reset form and the Live/Restart
    badge -- not this note's text.
    """
    page = _page()
    body = _function_body(
        page,
        "function settingEnvPinNote(s) {",
        "function settingSourceBadge(",
    )
    assert "s.settings_writable === false" in body
    assert "Read from <code>${escHtml(s.env_var)}</code> only" in body
    assert "settings store is never consulted" in body
    # The second branch must still exist and stay reachable only when the
    # first one's guard does not fire.
    assert "s.overridden_by !== 'env'" in body
    assert "Pinned by <code>${escHtml(s.env_var)}</code>" in body


# ---------------------------------------------------------------------------
# 6. A pinned row with a stored value underneath it gets a "Clear stored
#    value" action, not just a disabled "Edit" button (W1D ui-pass4)
# ---------------------------------------------------------------------------


def test_pinned_row_with_a_stored_value_gets_a_clear_stored_value_action() -> None:
    """Before this pass, an ``isPinned`` row (env-only knob, or a
    settings-store knob currently overridden by env) always rendered a
    disabled "Edit" button and nothing else. A stray stored value
    underneath the env var -- set before the var was exported, or left
    behind after an unset/re-set cycle -- had no way to be cleared, and
    would silently take over the moment the env var went away.

    The fix reuses the existing reset mechanism verbatim: a reset already
    *is* a clear (``PUT .../settings/{name}`` with ``value: null``, the
    same write ``beginResetSetting``/``confirmResetSetting`` send for the
    writable row's "Reset to default" button), so no new API route and no
    new JS action was needed.
    """
    page = _page()
    body = _function_body(
        page,
        "function renderSettingRow(s, groups, degraded) {",
        "function coerceSettingValue(",
    )
    assert "hasStoredValue" in body
    assert "s.stored_value !== null && s.stored_value !== undefined" in body
    assert (
        'data-action="beginResetSetting" data-id="${escHtml(s.name)}">'
        "Clear stored value</button>" in body
    )
    # Explains the env var wins either way, regardless of the clear.
    assert "environment variable wins either way" in body
    # The write's confirm/cancel/error UI (showSettingKeyPrompt,
    # cancelSettingWrite, runSettingWrite) all target this cell by id --
    # without it here, a click would silently do nothing visible.
    assert "setting-error-${escHtml(s.name)}" in body


def test_pinned_row_without_a_stored_value_still_renders_disabled_edit() -> None:
    """Nothing to clear means the old disabled placeholder is still
    correct -- this guards the branch's ``else`` half, not just the
    ``hasStoredValue`` half added above."""
    page = _page()
    body = _function_body(
        page,
        "function renderSettingRow(s, groups, degraded) {",
        "function coerceSettingValue(",
    )
    assert '<button type="button" class="btn btn-sm" disabled>Edit</button>' in body


def test_clear_stored_value_reuses_the_existing_reset_action_not_a_new_one() -> None:
    """No new JS function and no new ID_ACTIONS entry: the pinned row's
    "Clear stored value" button dispatches the identical data-action name
    the writable row's "Reset to default" button already uses -- a reset
    IS a clear (``value: null``), there is no separate verb to add."""
    page = _page()
    assert "beginClearStoredValue" not in page
    assert page.count('data-action="beginResetSetting"') == 2


def test_each_pattern_catches_a_deliberately_defective_subject() -> None:
    """Proves the substring checks above would actually fail on a
    regression, not just pass on anything."""
    defective = (
        "actHtml = hasStoredValue\n"
        '    ? \'<button type="button" class="btn btn-sm" disabled>Edit</button>\'\n'
        '    : \'<button type="button" class="btn btn-sm" disabled>Edit</button>\';'
    )
    assert (
        'data-action="beginResetSetting" data-id="${escHtml(s.name)}">'
        "Clear stored value</button>" not in defective
    )
    assert "environment variable wins either way" not in defective
