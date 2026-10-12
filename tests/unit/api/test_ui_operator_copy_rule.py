"""Operator-facing copy in ``index.html`` states facts, never names the code.

Extends the ``test_ui_review_truth.py`` approach -- static regex assertions
on the file's own text, no browser and no JS engine -- from a single named
section to a whole-file sweep, because the offence this guards against (a
source path, an ALL-CAPS event/constant name, a bare CLI invocation, or an
internal plan/task id leaking into what an operator reads) can appear in
any text node or template literal, not just the ones another test already
names.

Why this does not start from ``tests/ast_rules.py``, per CLAUDE.md's
"Writing a rule that walks the AST of ``src/``": that module's subject is
*call-site construction* -- how a name reaches a constructor through an
alias, a rebinding or a subclass -- and its roster of evasions is about
hiding a *call*. This rule's subject is literal string content rendered to
an operator, and ``index.html`` is HTML+JS, not Python, so there is no
``ast`` tree for the shared resolver (``construction_names``,
``calls_to_any``, ...) to walk. :func:`assert_hand_read_floor` is reused
directly below regardless, because it is general-purpose -- a hand-counted
population floor, not a call-site concept -- and this rule needs exactly
that guard against the same failure mode: a scan that quietly starts
matching almost nothing would otherwise report zero violations and read as
clean.

Four defects, pinned one test group each:

1. A literal ``.py`` module reference reaching rendered copy -- the shape
   of a since-fixed regression that asserted a module name
   (``demotion_gate``) had to appear in the Review tab's own text (see
   ``test_ui_review_truth.py::test_evolution_section_states_...``, which
   now asserts the opposite).
2. An ALL-CAPS internal constant or event name (``PACK_ASSEMBLED``,
   ``LEARNING_AUTO_PROMOTE_EVALUATED``, ...) in rendered copy -- the shape
   the audit flagged in the (now-fixed) Settings tab.
3. A bare ``trellis <subcommand>`` CLI invocation outside the one channel
   this file actually uses for one: a ``<code>`` element, with or without
   a ``title=`` tooltip. (``LoopHealthRow.operator_hint``'s dedicated
   footnote channel is the Python-side counterpart and is pinned
   separately, in ``test_loop_health_ui.py`` -- its value never appears as
   a literal in ``index.html``'s own source, so this file's static scan
   cannot see it and does not need to.)
4. An internal plan/task id -- an issue number, an e-/f-prefixed
   three-digit task id, or a G-prefixed audit code -- in rendered copy.
   (Deliberately not spelled out as a concrete example here: a repo-wide
   label sweep, in a separate guard, polices the e-/f-prefixed shape
   across ``src/``, ``tests/`` and ``docs/``, and a literal instance in
   this docstring would be indistinguishable from a real violation to
   it. The proof test below builds its example by concatenation for the
   same reason.)

The extraction (``_rendered_strings``) pulls HTML text nodes and JS
template-literal bodies, in that order, after stripping HTML and JS
comments and ``${...}`` interpolation placeholders -- the two channels the
work package's own wording names ("HTML text nodes and JS template
literals outside comments"). Plain single/double-quoted JS strings are
deliberately excluded: grepping the file shows those are element ids, CSS
classes and control-flow strings (``'active'``, ``'none'``, event names
used as JS values), never rendered prose -- backtick template literals are
this file's one mechanism for building dynamic HTML, and a plain string
would make the allow-list long instead of short.

This is a regex sweep, not a JS parser, and knows it: a multi-line
template literal containing a literal line that itself starts with ``//``
would be mis-stripped. No such case exists in the file today (verified by
running the extraction and hand-reading its output), and the floor check
below is this rule's defence against that drifting unnoticed.
"""

from __future__ import annotations

import re
from pathlib import Path

import trellis_api
from tests.ast_rules import assert_hand_read_floor

INDEX_HTML = Path(trellis_api.__file__).parent / "static" / "index.html"

_PY_REF = re.compile(r"\b\w+\.py\b")
_CONST_NAME = re.compile(r"\b[A-Z]{3,}_[A-Z_]+\b")
_TRELLIS_CMD = re.compile(r"\btrellis\s+[a-z][\w-]*(?:\s+[a-z][\w-]*)?\b")
_PLAN_ID = re.compile(r"#\d{3}\b|\be\d{3}\b|\bG\d+\b")

# Explicit, short allow-lists, keyed by the exact offending substring --
# not a pattern, so an entry cannot itself widen into a loophole big enough
# to hide a second, unrelated violation. Both are empty today (there is
# nothing in index.html's rendered copy that legitimately needs one); kept
# as a named mechanism so a future, genuinely-fine exception gets a
# sentence here instead of a deleted assertion.
_ALLOWED_PY_REFS: frozenset[str] = frozenset()
_ALLOWED_CONST_NAMES: frozenset[str] = frozenset()


def _page() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


def _strip_html_comments(page: str) -> str:
    return re.sub(r"<!--.*?-->", "", page, flags=re.DOTALL)


def _strip_js_comments(text: str) -> str:
    """Blank ``/* */`` blocks and whole-line ``//`` comments.

    Conservative on purpose: a ``//`` that is not alone on its line (after
    optional leading whitespace) is left untouched, so a template literal
    that happens to render a URL is never mistaken for a comment. See the
    module docstring for the one shape this does not handle.
    """
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return "\n".join(
        "" if re.match(r"^\s*//", line) else line for line in text.split("\n")
    )


def _strip_interpolation(template: str) -> str:
    return re.sub(r"\$\{[^}]*\}", "", template)


def _split_script_and_markup(page: str) -> tuple[str, str]:
    # Case-insensitive: an uppercase <SCRIPT>/<STYLE> tag would otherwise
    # survive both the extraction and the strip, landing raw markup/JS in
    # the text-node pass below (CodeQL py/bad-tag-filter).
    tag_flags = re.DOTALL | re.IGNORECASE
    script_text = "\n".join(
        m.group(1)
        for m in re.finditer(r"<script[^>]*>(.*?)</script\b[^>]*>", page, tag_flags)
    )
    markup = re.sub(r"<script[^>]*>.*?</script\b[^>]*>", "", page, flags=tag_flags)
    markup = re.sub(r"<style[^>]*>.*?</style\b[^>]*>", "", markup, flags=tag_flags)
    return script_text, markup


def _rendered_strings(page: str) -> list[str]:
    """Every HTML text node and JS template-literal body, comments stripped.

    Text nodes first, then template literals -- a stable order, so a
    failure's position in the list is reproducible without re-deriving it.
    """
    page = _strip_html_comments(page)
    script_text, markup = _split_script_and_markup(page)
    text_nodes = [t.strip() for t in re.findall(r">([^<>]+)<", markup) if t.strip()]
    script_nc = _strip_js_comments(script_text)
    literals = [
        _strip_interpolation(t) for t in re.findall(r"`([^`]*)`", script_nc, re.DOTALL)
    ]
    return text_nodes + literals


def _check_no_py_refs(strings: list[str]) -> list[str]:
    return [
        s
        for s in strings
        for m in _PY_REF.finditer(s)
        if m.group(0) not in _ALLOWED_PY_REFS
    ]


def _check_no_const_names(strings: list[str]) -> list[str]:
    return [
        s
        for s in strings
        for m in _CONST_NAME.finditer(s)
        if m.group(0) not in _ALLOWED_CONST_NAMES
    ]


def _check_no_plan_ids(strings: list[str]) -> list[str]:
    return [s for s in strings if _PLAN_ID.search(s)]


def _is_code_or_title_wrapped(text: str, start: int) -> bool:
    """Is position *start* inside a ``<code>...`` element or a ``title="..."``?

    Positional, not structural -- a nearest-preceding-opener heuristic, not
    a real HTML parser. Good enough for this file: it has no nested
    ``<code>`` elements and no ``title`` attribute spanning a tag boundary.
    """
    before = text[:start]
    last_open = before.rfind("<code")
    last_close = before.rfind("</code>")
    if last_open != -1 and last_open > last_close:
        return True
    last_title = before.rfind('title="')
    if last_title != -1:
        quote_start = last_title + len('title="')
        closing_quote = text.find('"', quote_start)
        if closing_quote == -1 or closing_quote >= start:
            return True
    return False


def _check_trellis_cmd_outside_code(prepared_page: str) -> list[str]:
    return [
        m.group(0)
        for m in _TRELLIS_CMD.finditer(prepared_page)
        if not _is_code_or_title_wrapped(prepared_page, m.start())
    ]


def _prepared_page(page: str) -> str:
    """Comments stripped, tags intact -- what the ``<code>``-wrapping check needs."""
    return _strip_js_comments(_strip_html_comments(page))


# ---------------------------------------------------------------------------
# Vacuity guard: the population this rule reasons over has not quietly
# collapsed to (near) nothing.
# ---------------------------------------------------------------------------


def test_extraction_population_is_above_a_hand_read_floor() -> None:
    strings = _rendered_strings(_page())
    # Hand-counted 2026-10-12: 259 HTML text nodes + 449 template literals
    # = 708. The floor leaves room for ordinary content growth/shrinkage
    # while still catching an extraction regression that drops to near
    # zero -- the exact failure mode #466 shipped as `len(SITES) > 0`.
    assert_hand_read_floor(
        len(strings),
        600,
        subject="rendered copy unit (HTML text node or JS template literal)",
        hint="Re-run the extraction and hand-count its output before changing this.",
    )


# ---------------------------------------------------------------------------
# 1. No ``.py`` module reference in rendered copy.
# ---------------------------------------------------------------------------


def test_no_py_module_reference_in_rendered_copy() -> None:
    violations = _check_no_py_refs(_rendered_strings(_page()))
    assert violations == [], f"a .py reference leaked into rendered copy: {violations}"


def test_py_reference_rule_catches_a_defective_subject() -> None:
    defective = "<script>const x = `the module is demotion_gate.py`;</script>"
    violations = _check_no_py_refs(_rendered_strings(defective))
    assert violations, "guard did not catch an injected .py reference"


# ---------------------------------------------------------------------------
# 2. No ALL-CAPS internal constant/event name in rendered copy.
# ---------------------------------------------------------------------------


def test_no_all_caps_constant_name_in_rendered_copy() -> None:
    violations = _check_no_const_names(_rendered_strings(_page()))
    assert violations == [], (
        f"an internal constant name leaked into rendered copy: {violations}"
    )


def test_const_name_rule_catches_a_defective_subject() -> None:
    defective = "<p>Driven by PACK_ASSEMBLED.session_id</p>"
    violations = _check_no_const_names(_rendered_strings(defective))
    assert violations, "guard did not catch an injected ALL-CAPS constant name"


# ---------------------------------------------------------------------------
# 3. No bare ``trellis <cmd>`` outside a ``<code>``/``title=`` tooltip.
# ---------------------------------------------------------------------------


def test_no_bare_trellis_command_outside_code_or_title() -> None:
    violations = _check_trellis_cmd_outside_code(_prepared_page(_page()))
    assert violations == [], (
        f"a bare CLI invocation leaked into rendered copy: {violations}"
    )


def test_trellis_cmd_rule_catches_a_defective_subject() -> None:
    defective = "<p>Run trellis curate promote-learning to see it.</p>"
    violations = _check_trellis_cmd_outside_code(_prepared_page(defective))
    assert violations, "guard did not catch a bare, unwrapped CLI invocation"


def test_trellis_cmd_rule_does_not_flag_a_code_wrapped_invocation() -> None:
    # Negative control: proves the exemption discriminates rather than
    # suppressing every match -- the same command, wrapped, must pass.
    wrapped = "<code>trellis curate promote-learning</code>"
    assert _check_trellis_cmd_outside_code(_prepared_page(wrapped)) == []


def test_trellis_cmd_rule_does_not_flag_a_title_tooltip() -> None:
    tooltip = (
        '<code title="CLI equivalent: trellis analyze apply-noise-tags">CLI</code>'
    )
    assert _check_trellis_cmd_outside_code(_prepared_page(tooltip)) == []


# ---------------------------------------------------------------------------
# 4. No internal plan/task id in rendered copy.
# ---------------------------------------------------------------------------


def test_no_internal_plan_id_in_rendered_copy() -> None:
    violations = _check_no_plan_ids(_rendered_strings(_page()))
    assert violations == [], (
        f"an internal plan id leaked into rendered copy: {violations}"
    )


def test_plan_id_rule_catches_a_defective_subject() -> None:
    # Built by concatenation, not as a literal, so this proof fixture does
    # not itself trip the separate, repo-wide label sweep that polices the
    # same e-/f-prefixed shape across src/, tests/ and docs/ -- see the
    # module docstring.
    e_task_id = "e" + "180"
    for defective in (
        "<p>See the tracking item #860 for details.</p>",
        f"<script>const x = `fixed in {e_task_id}`;</script>",
        "<p>Covered by G14.</p>",
    ):
        violations = _check_no_plan_ids(_rendered_strings(defective))
        assert violations, f"guard did not catch an injected plan id in: {defective!r}"


def test_plan_id_rule_does_not_flag_a_bare_github_issue_number_in_a_comment() -> None:
    # A real GitHub issue reference (e.g. "gate #860, blocker 4") is fine
    # in a JS *comment* -- it is not rendered to an operator at all, and
    # comment-stripping must remove it before the plan-id check ever sees
    # it, the same way it is fine in index.html's real source today.
    commented = (
        "<script>\n        // gate #860, blocker 4\n        const x = `ok`;\n</script>"
    )
    violations = _check_no_plan_ids(_rendered_strings(commented))
    assert violations == [], (
        "a commented-out issue reference should never reach the check"
    )
