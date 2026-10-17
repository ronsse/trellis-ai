"""The env-pinned row's clear action is gated on a real stored value.

``test_settings_tab_ui.py`` already pins most of this cell's text, but its
checks are substring-based and the condition text
``s.stored_value !== null && s.stored_value !== undefined`` also appears,
verbatim, one line above in the same function body (``currentValue``'s own
null-check). A mutant that collapses ``hasStoredValue`` to a bare ``true``
(or ``false``) survives every one of those checks, because the substring
they look for is still present elsewhere in the same function -- it is just
no longer the thing deciding ``actHtml``.

This file pins the ``hasStoredValue`` declaration as one exact line, and
the ternary that reads it, so a collapsed condition has nowhere left to
hide.
"""

from __future__ import annotations

import re
from pathlib import Path

import trellis_api

INDEX_HTML = Path(trellis_api.__file__).parent / "static" / "index.html"

_HAS_STORED_VALUE_DECL = (
    "const hasStoredValue = s.stored_value !== null && s.stored_value !== undefined;"
)


def _page() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


def _between(page: str, start: str, end: str) -> str:
    found = re.search(re.escape(start) + r"(.*?)" + re.escape(end), page, re.DOTALL)
    assert found is not None, f"could not find region between {start!r} and {end!r}"
    return found.group(1)


def _render_setting_row_body(page: str) -> str:
    return _between(
        page,
        "function renderSettingRow(s, groups, degraded) {",
        "function coerceSettingValue(",
    )


def test_has_stored_value_is_declared_from_the_row_itself_not_a_literal() -> None:
    """Exact-line match: a mutant collapsing this to ``true``/``false``
    removes this precise line, where a substring check on the shared
    condition text alone would not notice (it also appears one line up,
    in ``currentValue``'s own null-check)."""
    body = _render_setting_row_body(_page())
    assert _HAS_STORED_VALUE_DECL in body


def test_the_clear_action_ternary_reads_has_stored_value_both_ways() -> None:
    """Both the "has a stored value" and "nothing to clear" branches must
    be reached through the ``hasStoredValue`` ternary specifically -- not
    just present somewhere in the function."""
    body = _render_setting_row_body(_page())
    ternary = _between(
        body,
        "actHtml = hasStoredValue",
        "            } else {",
    )
    assert (
        'data-action="beginResetSetting" data-id="${escHtml(s.name)}">'
        "Clear stored value</button>" in ternary
    )
    assert '<button type="button" class="btn btn-sm" disabled>Edit</button>' in ternary


def test_env_pinned_row_without_a_stored_value_cannot_reach_the_clear_branch() -> None:
    """A row with no stored value must resolve ``hasStoredValue`` to a
    falsy real check, not a literal that always lands on the clear
    branch regardless of ``s.stored_value``."""
    body = _render_setting_row_body(_page())
    decl_index = body.index(_HAS_STORED_VALUE_DECL)
    ternary_index = body.index("actHtml = hasStoredValue")
    # The declaration must precede, and feed, the ternary that branches on it.
    assert decl_index < ternary_index


def test_each_pattern_catches_a_deliberately_defective_subject() -> None:
    """Proves the exact-line check actually fails on the mutation this
    file exists to catch -- collapsing hasStoredValue to a literal --
    even though the substring ``s.stored_value !== null && s.stored_value
    !== undefined`` is still present (from the unrelated currentValue
    line directly above it)."""
    defective = (
        "            const currentValue = (s.stored_value !== null && "
        "s.stored_value !== undefined) ? s.stored_value : s.default;\n"
        "            let ctlHtml;\n"
        "            let actHtml;\n"
        "            if (isPinned) {\n"
        "                const hasStoredValue = true;\n"
        "                actHtml = hasStoredValue\n"
        "                    ? `<button>Clear stored value</button>`\n"
        "                    : '<button disabled>Edit</button>';\n"
        "            } else {\n"
    )
    # The substring the OLD test relied on is still here...
    assert "s.stored_value !== null && s.stored_value !== undefined" in defective
    # ...but this file's exact-line check correctly rejects it.
    assert _HAS_STORED_VALUE_DECL not in defective
