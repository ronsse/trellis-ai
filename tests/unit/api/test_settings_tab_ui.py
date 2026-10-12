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
    page = _page()
    header = _between(page, '<table class="settings-table">', "</thead>")
    assert "<th>Read by</th>" in header


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
    assert "notWritable ?" in body
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
