"""Guards for the operator-console polish pass: topbar, tables,
empty/loading states, timestamps and the one shared pager.

Static assertions on ``index.html``'s own markup/script -- no browser, no
JS engine (there is none on this host). Each guard below pins the shape
a later edit could silently break, not merely that the right words
appear somewhere in the file:

1. Every ``<table`` sits inside a ``.table-wrap`` ancestor, so the page
   body never scrolls sideways (brief row 2).
2. Every view-local pager was replaced by the one shared ``.pager``
   component, with the graph panel as the sole, documented exception
   (out of scope this pass -- brief "Out of scope").
3. Every empty-state *site* renders through ``emptyStateHtml()`` rather
   than hand-rolled ``class="empty-state"`` markup (brief row 3).
4. The spinner and the shimmer bars both carry a CSS ``animation:`` and
   both sit under the universal ``prefers-reduced-motion`` freeze
   (brief row 4).
5. Exactly two ``new Date(iso)`` parses of a server-supplied timestamp
   exist (``fmtTime``, ``timeAgo``); every other renderer -- including
   the live "as-of" clock -- delegates rather than re-parsing (brief
   row 5).

Each test below is proven against a deliberately defective string
inline (the same method ``test_ui_buttons.py``'s
``test_each_pattern_catches_a_deliberately_defective_subject`` uses):
the regex/assertion must reject a hand-written broken snippet and
accept the fixed one. The same shapes were additionally proven against
the real file during development -- each guard's defect was introduced
into a working copy of ``index.html``, confirmed to turn the new test
red, then restored byte-identical from a pristine copy kept outside the
tree (never ``git checkout``/``stash``/``reset``, which the brief
forbids and which, for a worktree, write to shared state anyway) and
confirmed green again; that session is not re-run on every CI pass, but
the inline proofs below are.
"""

from __future__ import annotations

import re
from pathlib import Path

import trellis_api
from tests.ast_rules import assert_hand_read_floor

INDEX_HTML = Path(trellis_api.__file__).parent / "static" / "index.html"


def _page() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


_JS_LINE_COMMENT_RE = re.compile(r"^\s*//")


def _strip_js_line_comments(text: str) -> str:
    """Blank out ``//``-style JS comment lines.

    This file's own formatter-family comment names ``new Date(iso)``
    literally (explaining why there are only two), which would otherwise
    inflate the count the regex below pins. Same helper/rationale as
    ``test_ui_buttons.py``'s.
    """
    return "\n".join(
        "" if _JS_LINE_COMMENT_RE.match(line) else line for line in text.splitlines()
    )


def _function_body(page: str, name: str) -> str:
    escaped_name = re.escape(name)
    pattern = (
        rf"(?:async )?function {escaped_name}\([^)]*\)[^{{]*\{{\n(.*?)\n        \}}\n"
    )
    found = re.search(pattern, page, re.DOTALL)
    assert found is not None, f"{name}() not found"
    return found.group(1)


# ---------------------------------------------------------------------------
# 1. Every <table> sits inside .table-wrap.
# ---------------------------------------------------------------------------

_TABLE_TAG_RE = re.compile(r"<table\b")
_TABLE_WRAP_RE = re.compile(
    r'<div class="[^"]*\btable-wrap\b[^"]*"[^>]*>\s*<table\b',
    re.DOTALL,
)


def test_every_table_sits_inside_table_wrap() -> None:
    page = _page()
    n_tables = len(_TABLE_TAG_RE.findall(page))
    n_wrapped = len(_TABLE_WRAP_RE.findall(page))
    assert n_wrapped == n_tables, (
        f"{n_tables - n_wrapped} <table> site(s) lack an immediate "
        ".table-wrap ancestor div -- the page body can scroll sideways "
        "at phone width for that table"
    )
    assert_hand_read_floor(
        n_tables,
        13,
        subject="<table> site(s)",
        hint="hand count: 9 static status-section renderer tables "
        "(Traces/Memories/Events/Packs/Advisories/Policies/Schedules/"
        "tag-rule candidates/tag-rules-effective) plus 4 JS-built tables "
        "(effectiveness item scores, pack-detail injected items, "
        "rejected items, budget trace).",
    )


def test_table_wrap_pattern_catches_a_deliberately_defective_subject() -> None:
    defective = '<div class="status-section" style="padding:0">\n<table><thead>'
    assert _TABLE_WRAP_RE.search(defective) is None
    assert _TABLE_TAG_RE.search(defective) is not None

    fixed = '<div class="status-section table-wrap" style="padding:0">\n<table><thead>'
    assert _TABLE_WRAP_RE.search(fixed) is not None


# ---------------------------------------------------------------------------
# 2. No view-local pager remains -- one shared .pager component.
# ---------------------------------------------------------------------------

_PAGER_VIEW_PREFIXES = ("trace", "mem", "event", "pack")
# The graph panel (search-results pagination) is explicitly out of scope
# this pass (brief "Out of scope: The graph panel.") and keeps its own
# Prev/Next, offset-based renderer (renderPagination()) -- this is the
# one documented exception to "every view uses the shared component".
_GRAPH_PANEL_PAGER_ID = "srp-pagination"


def test_every_non_graph_view_pager_shares_the_pager_component() -> None:
    # Pass 6 converted each of these divs' inline style="display:none"
    # (always overridden by an explicit pag.style.display set on every
    # real render -- see renderCursorPagerFooter/renderPagination) to the
    # shared ".hide" utility class, so "pager" plus an optional trailing
    # "hide" is still exactly the shared component, not a view-local one.
    page = _page()
    for prefix in _PAGER_VIEW_PREFIXES:
        div_match = re.search(
            rf'<div class="pager(?: hide)?" id="{prefix}-pagination"[^>]*>(.*?)</div>',
            page,
            re.DOTALL,
        )
        assert div_match is not None, (
            f'{prefix}-pagination is not a <div class="pager" ...> -- a '
            "view-local pager may have crept back in"
        )
        body = div_match.group(1)
        assert re.search(rf'<span class="range num" id="{prefix}-page-info">', body), (
            f"{prefix}-pagination has no range-text span"
        )
        assert re.search(rf'<select id="{prefix}-limit"', body), (
            f"{prefix}-pagination has no rows-per-page select"
        )
        buttons = re.findall(
            r'<button[^>]*class="btn btn-sm"[^>]*>(.*?)</button>', body, re.DOTALL
        )
        assert len(buttons) == 2, (
            f"{prefix}-pagination does not have exactly 2 .btn.btn-sm "
            f"buttons (found {len(buttons)})"
        )
        assert any("Newer" in b for b in buttons), (
            f"{prefix}-pagination's first button is not labelled Newer"
        )
        assert any("Older" in b for b in buttons), (
            f"{prefix}-pagination's second button is not labelled Older"
        )
        # disabled is set via the attribute (brief row 6), not a CSS class
        # or inline style that only looks disabled.
        assert f"document.getElementById('{prefix}" in page
        assert ".disabled =" in page


def test_only_the_graph_panel_pager_is_exempt_from_the_shared_pattern() -> None:
    page = _page()
    all_ids = set(re.findall(r'<div class="pager(?: hide)?" id="([\w-]+)"', page))
    expected = {f"{p}-pagination" for p in _PAGER_VIEW_PREFIXES} | {
        _GRAPH_PANEL_PAGER_ID
    }
    assert all_ids == expected, (
        f"unexpected .pager id set {sorted(all_ids)} -- either a new "
        "view-local pager was added outside the shared component, or "
        "the graph-panel exception above is stale"
    )
    assert_hand_read_floor(
        len(all_ids),
        5,
        subject=".pager id site(s)",
        hint="hand count: 4 shared-component views (trace/mem/event/pack) "
        "plus the graph panel's own out-of-scope srp-pagination.",
    )


def test_pager_pattern_catches_a_deliberately_defective_subject() -> None:
    defective = """
            <div class="view-pagination" id="trace-pagination" style="display:none">
                <span class="range num" id="trace-page-info"></span>
                <button type="button" class="btn-flat" id="trace-newer">Prev</button>
            </div>
    """
    assert (
        re.search(
            r'<div class="pager(?: hide)?" id="trace-pagination"[^>]*>(.*?)</div>',
            defective,
            re.DOTALL,
        )
        is None
    )

    # The real shape after pass 6: the footer starts hidden via the
    # shared ".hide" class rather than an inline style.
    fixed = """
            <div class="pager hide" id="trace-pagination">
                <span class="range num" id="t-info"></span>
                <label class="hint">Rows <select id="t-limit"></select></label>
                <button class="btn btn-sm" id="t-new">&laquo; Newer</button>
                <button class="btn btn-sm" id="t-old">Older &raquo;</button>
            </div>
    """
    m = re.search(
        r'<div class="pager(?: hide)?" id="trace-pagination"[^>]*>(.*?)</div>',
        fixed,
        re.DOTALL,
    )
    assert m is not None
    buttons = re.findall(
        r'<button[^>]*class="btn btn-sm"[^>]*>(.*?)</button>', m.group(1)
    )
    assert len(buttons) == 2
    assert any("Newer" in b for b in buttons)
    assert any("Older" in b for b in buttons)


# ---------------------------------------------------------------------------
# 3. Every empty-state site renders through emptyStateHtml().
# ---------------------------------------------------------------------------

_EMPTY_STATE_LITERAL_RE = re.compile(r"""['"]empty-state['"]""")


def test_empty_state_literal_appears_only_in_the_component_definition() -> None:
    page = _page()
    hits = _EMPTY_STATE_LITERAL_RE.findall(page)
    # The only legitimate literal mention of the class name left in the
    # file is emptyStateHtml()'s own return statement -- every markup
    # SITE that renders an empty state calls the function instead of
    # hand-rolling the div. A floor of 1 needs a written reason (see
    # assert_hand_read_floor): this really is a single site, by design --
    # one shared component, one place that spells its own class name.
    assert len(hits) == 1, (
        f"found {len(hits)} literal 'empty-state' class reference(s), "
        "expected exactly 1 (emptyStateHtml()'s own definition) -- a new "
        "site may be building empty-state markup by hand instead of "
        "calling emptyStateHtml()"
    )
    assert_hand_read_floor(
        len(hits),
        1,
        subject="literal 'empty-state' class string(s)",
        sole_site_reason=(
            "emptyStateHtml() is the single shared component (brief row "
            "3); every call site builds its markup through the function "
            "rather than writing the class name itself, so exactly one "
            "literal mention -- the function's own return statement -- "
            "is correct, not a scan that stopped matching."
        ),
    )
    assert page.count("function emptyStateHtml(") == 1


def test_empty_state_call_sites_exist_above_a_hand_read_floor() -> None:
    page = _page()
    n = len(re.findall(r"emptyStateHtml\(", page))
    # 1 definition + call sites: 21 direct markup sites plus the Review
    # tab's reviewEmpty()/reviewError() helpers, which delegate
    # internally and cover 9 original call sites between them.
    assert_hand_read_floor(
        n,
        24,
        subject="emptyStateHtml( occurrence(s) (definition + call sites)",
        hint="hand count: 1 definition + 21 direct call sites + 2 "
        "Review-tab helpers (reviewEmpty/reviewError) that delegate "
        "internally.",
    )


def test_empty_state_pattern_catches_a_deliberately_defective_subject() -> None:
    defective = (
        'tbody.innerHTML = `<tr><td colspan="5">'
        '<div class="empty-state">No rows</div></td></tr>`;'
    )
    assert len(_EMPTY_STATE_LITERAL_RE.findall(defective)) == 1
    assert "emptyStateHtml(" not in defective

    fixed = (
        'tbody.innerHTML = `<tr><td colspan="5">'
        "${emptyStateHtml({ title: 'No rows', body: 'None yet.' })}</td></tr>`;"
    )
    assert len(_EMPTY_STATE_LITERAL_RE.findall(fixed)) == 0
    assert "emptyStateHtml(" in fixed


# ---------------------------------------------------------------------------
# 4. Spinner + shimmer both animate, and both freeze under
#    prefers-reduced-motion.
# ---------------------------------------------------------------------------


def test_spinner_and_shimmer_both_animate_and_both_freeze_under_reduced_motion() -> (
    None
):
    page = _page()
    assert re.search(r"\.spinner\s*\{[^}]*\banimation:\s*spin\b", page, re.DOTALL), (
        "`.spinner` no longer carries a CSS `animation:` -- nothing for "
        "prefers-reduced-motion to freeze"
    )
    assert re.search(
        r"\.shimmer-bar\s*\{[^}]*\banimation:\s*shimmer\b", page, re.DOTALL
    ), (
        "`.shimmer-bar` no longer carries a CSS `animation:` -- nothing "
        "for prefers-reduced-motion to freeze"
    )
    reduced_motion = re.search(
        r"@media \(prefers-reduced-motion: reduce\)\s*\{\s*"
        r"\*,\s*\*::before,\s*\*::after\s*\{([^}]*)\}",
        page,
    )
    assert reduced_motion is not None, (
        "no universal `*, *::before, *::after` block under "
        "`@media (prefers-reduced-motion: reduce)` -- a rule scoped to "
        "specific selectors would need `.spinner`/`.shimmer-bar` named "
        "explicitly, and neither is"
    )
    body = reduced_motion.group(1)
    assert "animation-duration" in body
    assert "animation-iteration-count" in body


def test_reduced_motion_pattern_catches_a_deliberately_defective_subject() -> None:
    defective = """
        @media (prefers-reduced-motion: reduce) {
            .modal { transition-duration: 0.01ms !important; }
        }
    """
    assert (
        re.search(
            r"@media \(prefers-reduced-motion: reduce\)\s*\{\s*"
            r"\*,\s*\*::before,\s*\*::after\s*\{([^}]*)\}",
            defective,
        )
        is None
    )

    fixed = """
        @media (prefers-reduced-motion: reduce) {
            *, *::before, *::after {
                animation-duration: 0.01ms !important;
                animation-iteration-count: 1 !important;
            }
        }
    """
    m = re.search(
        r"@media \(prefers-reduced-motion: reduce\)\s*\{\s*"
        r"\*,\s*\*::before,\s*\*::after\s*\{([^}]*)\}",
        fixed,
    )
    assert m is not None
    assert "animation-duration" in m.group(1)


# ---------------------------------------------------------------------------
# 5. One timestamp formatter family.
# ---------------------------------------------------------------------------


def test_exactly_two_new_date_iso_parses_exist() -> None:
    page = _page()
    # fmtTime(iso) and timeAgo(iso) are the only two places a
    # server-supplied ISO timestamp is parsed. fmtTimeCell() delegates to
    # timeAgo() rather than parsing a second time. fmtAsOf()/
    # markDataFetched() -- the topbar's live "as-of" clock -- take an
    # already-constructed `new Date()` (wall-clock "now", not a data
    # timestamp read from the server), so they are deliberately outside
    # this count: `new Date(iso)` with the literal `iso` argument name
    # is what's being pinned, not every `new Date(...)` call in the file.
    hits = re.findall(r"new Date\(iso\)", _strip_js_line_comments(page))
    assert len(hits) == 2, (
        f"expected exactly 2 `new Date(iso)` parses (fmtTime, timeAgo), "
        f"found {len(hits)} -- a new ad-hoc timestamp parse may have "
        "been added instead of reusing the shared formatters"
    )
    assert "function fmtTime(iso" in page
    assert "function timeAgo(iso" in page

    fmt_time_cell_body = _function_body(page, "fmtTimeCell")
    assert "new Date(" not in fmt_time_cell_body, (
        "fmtTimeCell() parses a Date itself instead of delegating to "
        "timeAgo()/fmtTime()"
    )
    assert "timeAgo(" in fmt_time_cell_body or "fmtTime(" in fmt_time_cell_body


def test_no_date_object_chains_straight_into_locale_string() -> None:
    # The "one formatter, not ad-hoc toLocaleString calls" -- every
    # `.toLocaleString(` call in the file operates on a Number (row 2's
    # thousands-separator requirement), never directly on a `new Date(...)`
    # result.
    page = _page()
    assert re.search(r"new Date\([^)]*\)\s*\.toLocaleString", page) is None


def test_timestamp_formatter_pattern_catches_a_deliberately_defective_subject() -> None:
    defective = "const shown = new Date(t.created_at).toLocaleString();"
    assert re.search(r"new Date\([^)]*\)\s*\.toLocaleString", defective) is not None

    fixed = "const shown = fmtTime(t.created_at, { empty: '-' });"
    assert re.search(r"new Date\([^)]*\)\s*\.toLocaleString", fixed) is None


def test_card_renderer_wires_na_and_inf_from_the_raw_value() -> None:
    # Ledger pass 6 item 5: ".card .value.na"/".value.inf" CSS exists, and
    # renderCard() already adds those classes from a caller-supplied `raw`
    # argument in the same branch that picks the display string -- the
    # "no renderer wires this" premise does not hold for this file. This
    # guard pins that wiring so a future refactor of renderCard() can't
    # quietly drop it.
    page = _page()
    assert ".card .value.na" in page
    assert ".card .value.inf" in page

    body = _function_body(page, "renderCard")
    assert "raw" in body, "renderCard() dropped its raw parameter"
    assert "' na'" in body, "renderCard() no longer adds the na modifier class"
    assert "' inf'" in body, "renderCard() no longer adds the inf modifier class"
    # fmtNum/fmtPct themselves must stay pure formatters -- the na/inf
    # decision belongs to the renderer, not the string formatter (brief:
    # "keeping fmtNum/fmtPct pure"). Neither function appends or even
    # mentions the CSS modifier classes.
    for fn in ("fmtNum", "fmtPct"):
        fn_body = _function_body(page, fn)
        assert "' na'" not in fn_body, (
            f"{fn}() appends a na class -- that decision belongs to the renderer"
        )
        assert "' inf'" not in fn_body, (
            f"{fn}() appends an inf class -- that decision belongs to the renderer"
        )

    # At least one real call site exercises the wiring with a 4th `raw`
    # argument -- defined-but-unused would pass the checks above too.
    assert re.search(r"renderCard\([^)]*,\s*\w+Raw\)", page), (
        "no renderCard(...) call site passes a raw value -- the na/inf "
        "wiring is defined but never exercised"
    )


def test_na_inf_wiring_pattern_catches_a_deliberately_defective_subject() -> None:
    wired = (
        "function renderCard(title, value, sub, raw) {\n"
        "  let valueClass = 'value';\n"
        "  if (raw !== undefined) {\n"
        "    if (raw == null) valueClass += ' na';\n"
        "    else if (!Number.isFinite(Number(raw))) valueClass += ' inf';\n"
        "  }\n"
        "}"
    )
    assert "' na'" in wired
    assert "' inf'" in wired

    defective = (
        "function renderCard(title, value, sub) {\n  let valueClass = 'value';\n}"
    )
    assert "raw" not in defective
    assert "' na'" not in defective
    assert "' inf'" not in defective


def test_every_column_header_th_has_scope() -> None:
    # Ledger pass 6 item 6: every <th> in a column-header row gets
    # scope="col" (this file hand-checked to have no row-header <th>
    # sites -- every table's first body cell is a <td>, not a <th>).
    page = _page()
    th_tags = re.findall(r"<th(?:\s[^<>]*)?>", page)
    assert_hand_read_floor(
        len(th_tags),
        70,
        subject="<th> site(s)",
        hint="hand count for this pass: 83 (74 source-line occurrences, "
        "several lines carrying more than one <th> in a JS template "
        "string table header row).",
    )
    missing = [tag for tag in th_tags if "scope=" not in tag]
    assert missing == [], (
        f"{len(missing)} <th> site(s) have no scope attribute: {missing[:5]}"
    )


def test_th_scope_pattern_catches_a_deliberately_defective_subject() -> None:
    defective = '<thead><tr><th>Intent</th><th class="num">Count</th></tr></thead>'
    th_tags = re.findall(r"<th(?:\s[^<>]*)?>", defective)
    assert len(th_tags) == 2
    assert all("scope=" not in tag for tag in th_tags)

    fixed = (
        '<thead><tr><th scope="col">Intent</th>'
        '<th scope="col" class="num">Count</th></tr></thead>'
    )
    th_tags_fixed = re.findall(r"<th(?:\s[^<>]*)?>", fixed)
    assert len(th_tags_fixed) == 2
    assert all("scope=" in tag for tag in th_tags_fixed)

    # <thead> itself must never be mistaken for a <th> site.
    assert re.findall(r"<th(?:\s[^<>]*)?>", "<thead>") == []


# ---------------------------------------------------------------------------
# Elements that start hidden carry the ".hide" class, so an inline
# ``style.display`` read no longer reports their state.
# ---------------------------------------------------------------------------

_INLINE_DISPLAY_NONE_READ_RE = re.compile(
    r"""\.style\.display\s*[!=]==?\s*['"]none['"]"""
)


def test_no_script_reads_hidden_state_from_the_inline_style() -> None:
    """A ``.hide`` element has an empty inline display until a script sets
    one, so ``el.style.display === 'none'`` is false for it while it is
    hidden. A toggle written that way needs two clicks to open. Read
    ``getComputedStyle(el).display`` instead."""
    page = _page()
    assert_hand_read_floor(
        len(re.findall(r'class="[^"]*\bhide\b', page)),
        25,
        subject='class="... hide" site(s)',
        hint="hand count for this pass: 31 elements start hidden via .hide.",
    )
    reads = _INLINE_DISPLAY_NONE_READ_RE.findall(page)
    assert reads == [], f"inline display read(s) of hidden state: {reads}"


def test_inline_display_read_guard_catches_a_defective_subject() -> None:
    defective = "if (banner.style.display === 'none') showKeyBanner('');"
    assert _INLINE_DISPLAY_NONE_READ_RE.search(defective) is not None
    assert _INLINE_DISPLAY_NONE_READ_RE.search('el.style.display !== "none"')
    fixed = "if (getComputedStyle(banner).display === 'none') showKeyBanner('');"
    assert _INLINE_DISPLAY_NONE_READ_RE.search(fixed) is None
