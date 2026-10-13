"""Static pins for the five shared components the Ledger components pass
introduced: the confirmation dialog, the real ``role="switch"`` toggle, the
``.table-wrap`` wrapper, and the token-only colouring of the cytoscape style
builder and the sidebar wordmark.

Each check below follows the project's existing pattern (see
``test_ui_style_tokens.py``, ``test_ui_inline_handlers.py``): a regex pins
the real-code population above a hand-read floor
(:func:`tests.ast_rules.assert_hand_read_floor`, never a count derived from
the scan itself), and a dedicated test proves that same regex against a
small, deliberately defective inline fixture, not the real page.
"""

from __future__ import annotations

import re
from pathlib import Path

import trellis_api
from tests.ast_rules import assert_hand_read_floor

INDEX_HTML = Path(trellis_api.__file__).parent / "static" / "index.html"

_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)


def _page() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


def _strip_comments(text: str) -> str:
    """Drop ``/* ... */`` blocks and whole lines that open with ``//``.

    Only whole-line ``//`` comments are stripped (never a mid-line
    substring after one), so a quoted ``https://`` URL elsewhere on a code
    line survives untouched -- the page's own comments are written one to
    a line, never trailing same-line as code, which is what makes this
    safe here (see ``test_ui_number_formatting.py``'s identical approach
    for ``.toFixed(``).
    """
    text = _BLOCK_COMMENT_RE.sub("", text)
    lines = [ln for ln in text.splitlines() if not ln.strip().startswith("//")]
    return "\n".join(lines)


def _function_body(page: str, name: str) -> str:
    found = re.search(
        r"function " + re.escape(name) + r"\([^)]*\) \{(.*?)\n        \}",
        page,
        re.DOTALL,
    )
    assert found is not None, f"function {name}() not found"
    return found.group(1)


# ---------------------------------------------------------------------------
# 1. No real confirm( call survives -- only the shared dialog does the job.
# ---------------------------------------------------------------------------

_CONFIRM_CALL_RE = re.compile(r"\bconfirm\(")


class TestNoRealConfirmCallRemains:
    """Broader than
    ``test_generate_advisories_uses_two_step_confirm_not_window_confirm``
    (which only pins the literal substring ``"window.confirm("``): this
    sweeps the whole page, after stripping comments, for *any* call whose
    callee is exactly ``confirm`` -- a bare global ``confirm(...)`` or
    ``window.confirm(...)`` alike -- while leaving alone function names
    that merely contain the word, such as ``confirmDeletePolicy(`` or
    ``openConfirmDialog(`` (neither has ``confirm`` immediately followed
    by ``(``)."""

    def test_raw_page_mentions_confirm_only_in_comments(self) -> None:
        # Three prose mentions explain, in a comment, why the page does not
        # call the browser's native dialog -- pin that floor so a passing
        # stripped-text sweep below is not vacuous (e.g. because the
        # comment-stripping regexes stopped matching anything at all).
        raw_hits = _CONFIRM_CALL_RE.findall(_page())
        assert_hand_read_floor(
            len(raw_hits),
            3,
            subject="confirm( substring in the raw (unstripped) page",
            hint="one CSS block comment on .dialog-scrim, two JS comment "
            "blocks (openConfirmDialog's definition, confirmRejectProposal) "
            "-- all three explain why the page avoids the browser's own "
            "confirm() dialog rather than calling it.",
        )

    def test_no_real_confirm_call_outside_comments(self) -> None:
        code = _strip_comments(_page())
        hits = _CONFIRM_CALL_RE.findall(code)
        assert hits == [], (
            f"found {len(hits)} real confirm(...) call site(s) outside "
            f"comments -- every destructive or state-changing action must "
            f"go through openConfirmDialog(...) instead"
        )

    def test_pattern_catches_a_deliberately_defective_subject(self) -> None:
        defective = "function doIt() {\n    if (confirm('Really?')) run();\n}"
        assert _CONFIRM_CALL_RE.findall(defective) == ["confirm("]
        # A sibling function whose name merely contains the word must not
        # false-positive.
        clean = "function confirmDeletePolicy(id) { openConfirmDialog({}); }"
        assert _CONFIRM_CALL_RE.findall(clean) == []


# ---------------------------------------------------------------------------
# 2. No hardcoded hex outside the token blocks in the graph panel / wordmark.
# ---------------------------------------------------------------------------

_HEX_COLOR_RE = re.compile(r"#[0-9a-fA-F]{3,8}\b")


class TestGraphAndWordmarkStayTokenOnly:
    """``buildCyStyle()`` (the cytoscape style map) and the sidebar wordmark
    SVG must read every colour from a CSS custom property, never a literal
    hex -- that is what lets a theme toggle repaint them. The categorical
    per-entity-type palette (``WELL_KNOWN_TYPE_COLORS`` / ``nodeColor()``)
    is explicitly exempt by the page's own comment: it distinguishes entity
    types from each other, not light mode from dark, so it keeps its own
    hexes on purpose and is scoped out of this sweep deliberately."""

    def test_well_known_type_colors_floor_proves_the_regex_finds_real_hex(
        self,
    ) -> None:
        # Sanity floor: if this ever drops to zero, _HEX_COLOR_RE itself
        # broke, and the "no hex" assertions below would be vacuous rather
        # than meaningful.
        page = _page()
        match = re.search(
            r"const WELL_KNOWN_TYPE_COLORS = new Map\(\[(.*?)\]\);", page, re.DOTALL
        )
        assert match is not None, "WELL_KNOWN_TYPE_COLORS not found"
        assert_hand_read_floor(
            len(_HEX_COLOR_RE.findall(match.group(1))),
            16,
            subject="WELL_KNOWN_TYPE_COLORS categorical hex entries",
            hint="hand count: 16 named entity types, one hex apiece.",
        )

    def test_build_cy_style_reads_every_colour_from_a_token(self) -> None:
        body = _function_body(_page(), "buildCyStyle")
        hits = _HEX_COLOR_RE.findall(body)
        assert hits == [], (
            f"buildCyStyle() has literal hex colour(s) {hits} -- read via "
            f"cssVar(...) instead so a theme toggle repaints it"
        )
        # It must actually read something, not just lack hex incidentally.
        assert body.count("cssVar(") >= 4

    def test_wordmark_svg_reads_colour_from_currentcolor_not_a_literal(self) -> None:
        page = _page()
        match = re.search(
            r'<div class="sidebar-head">\s*(<svg.*?</svg>)', page, re.DOTALL
        )
        assert match is not None, "sidebar wordmark <svg> not found"
        svg = match.group(1)
        hits = _HEX_COLOR_RE.findall(svg)
        assert hits == [], (
            f"wordmark SVG has literal hex colour(s) {hits} -- every fill/"
            f"stroke must be currentColor, with the actual colour set by "
            f"the surrounding CSS (--rail-text-active)"
        )
        assert "currentColor" in svg

    def test_pattern_catches_a_deliberately_defective_subject(self) -> None:
        defective = "<circle cx='1' cy='1' r='1' fill='#4f46e5'/>"
        assert _HEX_COLOR_RE.findall(defective) == ["#4f46e5"]
        clean = "<circle cx='1' cy='1' r='1' fill='currentColor'/>"
        assert _HEX_COLOR_RE.findall(clean) == []


# ---------------------------------------------------------------------------
# 3. Every <table> sits inside a .table-wrap { overflow-x: auto } box.
# ---------------------------------------------------------------------------

_TABLE_RE = re.compile(r"<table\b")
_WRAPPED_TABLE_RE = re.compile(
    r'<div class="[^"]*\btable-wrap\b[^"]*"[^>]*>\s*<table\b'
)


class TestEveryTableSitsInATableWrap:
    def test_every_table_is_immediately_preceded_by_a_table_wrap_div(
        self,
    ) -> None:
        page = _page()
        total = len(_TABLE_RE.findall(page))
        wrapped = len(_WRAPPED_TABLE_RE.findall(page))
        assert_hand_read_floor(
            total,
            13,
            subject="<table> element(s) emitted by the page",
            hint="hand count: 9 static tables (each already wrapped in a "
            "status-section table-wrap container) plus 4 tables built as "
            "template strings in JS (each opens its own inline "
            '<div class="table-wrap">).',
        )
        assert wrapped == total, (
            f"{total - wrapped} <table> site(s) are not immediately "
            "preceded by a div carrying the table-wrap class -- a table "
            "outside .table-wrap (overflow-x: auto) can overflow the page "
            "instead of scrolling in place"
        )

    def test_pattern_catches_a_deliberately_defective_subject(self) -> None:
        defective = (
            '<div class="status-section"><table><tr><td>x</td></tr></table></div>'
        )
        assert len(_TABLE_RE.findall(defective)) == 1
        assert len(_WRAPPED_TABLE_RE.findall(defective)) == 0
        clean = '<div class="status-section table-wrap"><table></table></div>'
        assert len(_WRAPPED_TABLE_RE.findall(clean)) == 1


# ---------------------------------------------------------------------------
# 4. The boolean setting toggle is a real switch: role + aria-checked.
# ---------------------------------------------------------------------------

_ROLE_SWITCH_RE = re.compile(r'role="switch"')
_PAIRED_SWITCH_RE = re.compile(r'<button[^>]*role="switch"[^>]*aria-checked=')

_SOLE_SWITCH_REASON = (
    "There is exactly one switch template in the page: renderValueInput's "
    "bool-type branch, reused for every boolean setting row. A second "
    "hand-built switch elsewhere would be the regression (a toggle built "
    "as a checkbox or a button with no role, bypassing the shared "
    "component), not a second legitimate site to raise the floor for."
)


class TestBooleanToggleIsARealSwitch:
    def test_role_switch_appears_exactly_once_in_real_code(self) -> None:
        code = _strip_comments(_page())
        hits = _ROLE_SWITCH_RE.findall(code)
        assert_hand_read_floor(
            len(hits),
            1,
            subject='role="switch" site(s) outside comments',
            sole_site_reason=_SOLE_SWITCH_REASON,
        )

    def test_the_switch_carries_aria_checked_on_the_same_element(self) -> None:
        code = _strip_comments(_page())
        assert len(_PAIRED_SWITCH_RE.findall(code)) == 1, (
            'the role="switch" button must carry aria-checked on the '
            "same <button>, not a role with no state exposed to a screen "
            "reader"
        )

    def test_toggle_setting_switch_keeps_aria_checked_in_sync_on_flip(
        self,
    ) -> None:
        body = _function_body(_page(), "toggleSettingSwitch")
        assert "setAttribute('aria-checked'" in body

    def test_pattern_catches_a_deliberately_defective_subject(self) -> None:
        defective = '<button type="checkbox" class="toggle"></button>'
        assert _PAIRED_SWITCH_RE.findall(defective) == []
        clean = '<button role="switch" aria-checked="false"></button>'
        assert len(_PAIRED_SWITCH_RE.findall(clean)) == 1


# ---------------------------------------------------------------------------
# 5. The shared dialog's typed-word gate is actually wired at every call.
# ---------------------------------------------------------------------------

_OPEN_DIALOG_RE = re.compile(r"openConfirmDialog\(\{")
_CONFIRM_WORD_KEY_RE = re.compile(r"confirmWord:")

_CONFIRM_WORD_CALL_SITES_HINT = (
    "four call sites pass confirmWord: confirmDeletePolicy, "
    "confirmRevokeTagRule, confirmRejectProposal, and toggleSettingSwitch's "
    "caution-risk branch; the definition itself (openConfirmDialog) is the "
    "fifth openConfirmDialog({ occurrence and supplies no confirmWord of "
    "its own."
)


class TestDialogTypedWordGateIsWired:
    def test_four_call_sites_pass_a_confirm_word(self) -> None:
        page = _page()
        assert_hand_read_floor(
            len(_OPEN_DIALOG_RE.findall(page)),
            5,
            subject="openConfirmDialog({ site(s) (definition plus calls)",
            hint=_CONFIRM_WORD_CALL_SITES_HINT,
        )
        assert_hand_read_floor(
            len(_CONFIRM_WORD_KEY_RE.findall(page)),
            4,
            subject="confirmWord: key(s) passed to openConfirmDialog",
            hint=_CONFIRM_WORD_CALL_SITES_HINT,
        )

    def test_the_definition_disables_confirm_until_the_word_matches(
        self,
    ) -> None:
        body = _function_body(_page(), "openConfirmDialog")
        assert "confirmBtn.disabled = true" in body
        assert "wordInput.value.trim() !== confirmWord" in body
        # Without a confirmWord, the gate must not hold the button closed.
        assert "confirmBtn.disabled = false" in body

    def test_pattern_catches_a_deliberately_defective_subject(self) -> None:
        defective = "openConfirmDialog({ title: 'x', onConfirm: doIt });"
        assert _OPEN_DIALOG_RE.findall(defective) != []
        assert _CONFIRM_WORD_KEY_RE.findall(defective) == []
        clean = "openConfirmDialog({ confirmWord: 'delete', onConfirm: doIt });"
        assert len(_CONFIRM_WORD_KEY_RE.findall(clean)) == 1
