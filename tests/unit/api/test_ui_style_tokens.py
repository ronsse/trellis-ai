"""Hand-rewritten radius and shadow declarations stay hand-rewritten.

Every ``border-radius`` and ``box-shadow`` the Ledger pass touched was
converted to a ``--r-*`` / ``--shadow-*`` token read, by hand, line by
line (the full site-by-site classification is in the PR description, not
reproduced here). A literal ``border-radius: Npx`` or a ``box-shadow``
carrying a raw ``rgba(...)`` value, reintroduced anywhere after the three
token-definition blocks, is exactly the regression a reviewer skimming a
diff would miss -- it still *renders*, it just silently drifts from the
token the rest of the file now reads.

Two literal exceptions are real, not escaped by this test:

- ``border-radius: 50%`` makes a circle, which no ``--r-*`` token
  represents (a radius token is a corner-rounding amount, not "fully
  round"). Ten sites use it on purpose -- eight pre-existing dots and
  avatars, the one badge leading dot the tokens pass added, and the
  round knob on the Ledger components pass's ``.toggle::after`` switch
  thumb -- and this test pins that exact count, so an eleventh appearing
  silently (a literal px radius typed as ``50%`` to dodge the ``Npx``
  regex) still has a floor to clear, and a vanished one is worth a look
  too.
- The three token-definition blocks themselves (bare ``:root``, the
  ``prefers-color-scheme`` block, and the ``[data-theme="light"]``
  block) legitimately declare ``--shadow-1`` / ``--shadow-2`` /
  ``--shadow-3`` as ``rgba(...)`` composites -- that *is* the token's
  value, not a bypass of it -- and are excluded from the ``box-shadow``
  sweep by construction: the sweep only reads the text after the last of
  the three blocks closes.

The Fonts pass added the same shape of guard for ``font-size``, scoped
deliberately narrower than the radius/shadow sweeps above: it read only
the ``<style>...</style>`` block (after the token-definition blocks),
not the rest of the file, because the admin script below ``</style>``
still carried about 96 ``style="font-size:...rem"`` template-literal
attributes across roughly a dozen renderers that pass left unconverted.

The buttons/inline-sizes pass closes that gap: every one of those 96
inline literals was hand-converted to its nearest ``--fs-1`` through
``--fs-7`` step (within about 1px, the one ``0.85em`` nested inside an
already-tokenized ``0.8rem`` parent judged against that parent's new
pixel value), and the sweep below now reads the whole page, not just
the ``<style>`` block -- there is still no headless browser on this
host, so which renderer a given inline attribute belongs to, and
whether its layout still reads right, remains unverified by anything
in this file; only that the literal number is gone and the token read
is syntactically present.

Pass 6 closes the gap those two paragraphs leave open: every one of the
95 ``style="font-size:var(--fs-N)"`` attributes the buttons/inline-sizes
pass had converted to a token *read* is now converted again, from an
inline read to a reusable ``.fs-1`` through ``.fs-7`` class (the ``<style>``
block gained exactly those seven one-declaration rules, plus two
``.value.fs-5`` / ``.value.fs-6`` compound rules needed to keep winning
against the higher-specificity ``.card .value`` rule at the four sites
that override a card's default size). Because a class is shared rather
than repeated per site, the whole-page and style-block-only scans now
read the same population -- there is no longer an "inline" font-size
population distinct from the style block's, so the two floor tests below
converge on one number instead of the style-block test's being strictly
smaller.
"""

from __future__ import annotations

import re
from pathlib import Path

import trellis_api
from tests.ast_rules import assert_hand_read_floor

INDEX_HTML = Path(trellis_api.__file__).parent / "static" / "index.html"

_LITERAL_RADIUS_RE = re.compile(r"border-radius:\s*\d+px")
_CIRCLE_RADIUS_RE = re.compile(r"border-radius:\s*50%")
_BOX_SHADOW_RGBA_RE = re.compile(r"box-shadow:[^;]*rgba\(")
_RADIUS_TOKEN_RE = re.compile(r"border-radius:[^;]*var\(--r-")
_LITERAL_FONT_SIZE_RE = re.compile(r"font-size:\s*[0-9]*\.?[0-9]+(?:rem|px)")
_FONT_SIZE_TOKEN_RE = re.compile(r"font-size:\s*var\(--fs-")
_STYLE_CLOSE_RE = re.compile(r"</style>")

# Pass 6: any style="..." attribute (markup or a JS template-string
# attribute built the same way) that still mentions font-size at all --
# literal or var(--fs-N) alike. Item 1's target is zero: every site that
# used to carry one now carries a .fs-N class instead.
_INLINE_STYLE_FONT_SIZE_RE = re.compile(r'style="[^"]*font-size[^"]*"')

# Pass 6: every style="..." attribute left in the page, inline or in a JS
# template string. Hand count right after this pass: 105 -- pinned as a
# ceiling (not a floor) so a future pass can't silently grow the inline
# population back up without a deliberate look; converting one back out
# should lower this number, and a legitimate new one-off inline style
# (below item 2/3's repeat thresholds) should raise it along with this
# constant in the same change.
_STYLE_ATTR_RE = re.compile(r'style="[^"]*"')
_INLINE_STYLE_CEILING = 105

# The bare ``:root`` token block through the end of the
# ``[data-theme="light"]`` block -- the only place a ``box-shadow:
# ...rgba(...)`` declaration is the token's own definition rather than a
# bypass of it. Located by the shared closing brace of the last of the
# three palette blocks, rather than a hardcoded line number, because this
# very pass already shifted every such number once.
_TOKEN_BLOCKS_END_RE = re.compile(
    r':root\[data-theme="light"\]\s*\{.*?\n\s*\}\n', re.DOTALL
)


def _page() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


def _text_after_token_blocks(text: str) -> str:
    match = _TOKEN_BLOCKS_END_RE.search(text)
    assert match is not None, "could not locate the end of the token-definition blocks"
    return text[match.end() :]


def _style_block_after_token_blocks(text: str) -> str:
    """The ``<style>`` element's own text, after the three token blocks.

    Narrower than :func:`_text_after_token_blocks` on purpose -- the
    font-size sweep must not walk into the ``<script>`` below, which still
    carries inline ``style="font-size:...rem"`` template-literal
    attributes this pass does not convert (see the module docstring).
    """
    after_tokens = _text_after_token_blocks(text)
    close = _STYLE_CLOSE_RE.search(after_tokens)
    assert close is not None, "could not locate the closing </style> tag"
    return after_tokens[: close.start()]


def test_no_literal_pixel_radius_remains() -> None:
    hits = _LITERAL_RADIUS_RE.findall(_page())
    assert hits == [], (
        f"literal border-radius: Npx found ({hits}) -- use var(--r-1), "
        f"var(--r-2) or var(--r-pill) instead"
    )


def test_circle_radius_sites_are_pinned_at_a_hand_count() -> None:
    # Hand count: nine from the tokens pass (eight pre-existing circular
    # dots/avatars plus the one badge leading dot it added) plus the
    # components pass's one new round toggle-switch knob.
    hits = _CIRCLE_RADIUS_RE.findall(_page())
    assert len(hits) == 10, (
        f"expected exactly 10 border-radius: 50% sites, found {len(hits)} -- "
        f"a new one needs a deliberate look (is it really a circle, not a "
        f"literal radius dodging the Npx regex?), and a vanished one may "
        f"mean a site was converted to a token instead of staying a circle"
    )


def test_no_box_shadow_rgba_literal_outside_the_token_blocks() -> None:
    hits = _BOX_SHADOW_RGBA_RE.findall(_text_after_token_blocks(_page()))
    assert hits == [], (
        f"box-shadow with a raw rgba(...) literal found outside the "
        f"token-definition blocks ({hits}) -- use var(--shadow-1), "
        f"var(--shadow-2) or var(--shadow-3) instead"
    )


def test_scan_population_is_above_a_hand_read_floor() -> None:
    # The sweeps above are vacuous if they stop matching almost nothing;
    # pin the population the radius sweep actually walks (the box-shadow
    # sweep's floor is a count of zero by design, so it has none).
    n_var_radius = len(_RADIUS_TOKEN_RE.findall(_page()))
    assert_hand_read_floor(
        n_var_radius,
        50,
        subject="border-radius: var(--r-*) site(s)",
        hint="hand count for this pass: 61 (60 simple declarations plus "
        "the one compound two-corner declaration).",
    )


def test_no_literal_font_size_in_style_block_remains() -> None:
    hits = _LITERAL_FONT_SIZE_RE.findall(_style_block_after_token_blocks(_page()))
    assert hits == [], (
        f"literal font-size: <N>rem/px found in the <style> block ({hits}) "
        f"-- use var(--fs-1) through var(--fs-7) instead"
    )


def test_no_literal_font_size_anywhere_in_page_remains() -> None:
    # Whole-page version of the check above -- the buttons/inline-sizes
    # pass converted the ~96 inline style="font-size:...rem" template
    # literals the Fonts pass had deliberately left ungoverned (see the
    # module docstring), so the sweep no longer needs to stop at
    # </style>. Inline SVG markup carries no font-size at all in this
    # file (hand-checked), so there is nothing to carve out for it.
    hits = _LITERAL_FONT_SIZE_RE.findall(_page())
    assert hits == [], (
        f"literal font-size: <N>rem/px/em found in the page ({hits}) -- "
        f"use var(--fs-1) through var(--fs-7) instead"
    )


def test_font_size_token_population_is_above_a_hand_read_floor() -> None:
    # Hand count for this pass: 116 (107 carried over from the prior pass,
    # plus the 7 new .fs-1..fs-7 utility classes and the 2 .value.fs-5 /
    # .value.fs-6 compound overrides pass 6 added).
    n_var_font_size = len(
        _FONT_SIZE_TOKEN_RE.findall(_style_block_after_token_blocks(_page()))
    )
    assert_hand_read_floor(
        n_var_font_size,
        105,
        subject="font-size: var(--fs-*) site(s) in the <style> block",
        hint="hand count for this pass: 116.",
    )


def test_inline_font_size_token_population_is_above_a_hand_read_floor() -> None:
    # Whole-page population. Pass 6 converted every remaining inline
    # style="font-size:var(--fs-N)" attribute to a .fs-N class (see the
    # module docstring), so this scope now reads the same population as
    # the <style>-block-only test above -- the floor sits below the hand
    # count of 116 for the usual reason: a legitimately added site must
    # not turn this suite red.
    n_var_font_size = len(_FONT_SIZE_TOKEN_RE.findall(_page()))
    assert_hand_read_floor(
        n_var_font_size,
        105,
        subject="font-size: var(--fs-*) site(s) in the whole page",
        hint="hand count for this pass: 116 (no inline font-size style "
        "attributes remain after pass 6 -- see "
        "test_no_inline_font_size_style_attribute_remains).",
    )


def test_no_inline_font_size_style_attribute_remains() -> None:
    hits = _INLINE_STYLE_FONT_SIZE_RE.findall(_page())
    assert hits == [], (
        f"a style= attribute still mentions font-size ({hits}) -- use one "
        f"of the .fs-1 through .fs-7 classes instead, merging into any "
        f"existing class= on the same tag"
    )


def test_inline_style_attribute_count_stays_at_or_below_hand_read_ceiling() -> None:
    n_style_attrs = len(_STYLE_ATTR_RE.findall(_page()))
    assert n_style_attrs <= _INLINE_STYLE_CEILING, (
        f"found {n_style_attrs} style= attributes, above the pass-6 "
        f"ceiling of {_INLINE_STYLE_CEILING} -- a new inline style should "
        f"reuse an existing utility class, or the ceiling needs a "
        f"deliberate, hand-counted bump alongside the new class(es) that "
        f"justify it"
    )


_HEX_LITERAL_RE = re.compile(r"#[0-9a-fA-F]{6}\b")


def test_no_hex_literal_remains_outside_the_token_blocks() -> None:
    # Pass 6 moved the graph palette's 26 hex literals (the 10-colour
    # metrics series and the 16-entry well-known entity-type map) into
    # --cat-1..--cat-10 / --entity-<type> tokens inside the three theme
    # blocks, read live via cssVar(). No allowlist is needed: nothing
    # legitimate remains outside the token-definition blocks.
    hits = _HEX_LITERAL_RE.findall(_text_after_token_blocks(_page()))
    assert hits == [], (
        f"hex colour literal(s) found outside the token-definition blocks "
        f"({hits}) -- define a --cat-N or --entity-<type> token (or reuse "
        f"an existing one) instead"
    )


def test_hex_literal_population_inside_token_blocks_above_hand_read_floor() -> None:
    # Sanity floor for _HEX_LITERAL_RE itself: each of the three theme
    # blocks defines its own base palette plus, since pass 6, its own
    # --cat-1..--cat-10 and 16 --entity-* hexes.
    text = _page()
    end = _TOKEN_BLOCKS_END_RE.search(text)
    assert end is not None, "could not locate the end of the token-definition blocks"
    n_hex = len(_HEX_LITERAL_RE.findall(text[: end.end()]))
    assert_hand_read_floor(
        n_hex,
        150,
        subject="hex colour literal(s) inside the three theme token blocks",
        hint="hand count: 189 (base palette hexes across all three blocks, "
        "plus pass 6's 10 --cat-N and 16 --entity-* per block).",
    )


def test_each_pattern_catches_a_deliberately_defective_subject() -> None:
    assert _LITERAL_RADIUS_RE.findall(".card { border-radius: 6px; }") == [
        "border-radius: 6px"
    ]
    toast_rule = ".toast { box-shadow: 0 4px 16px rgba(0,0,0,0.25); }"
    assert _BOX_SHADOW_RGBA_RE.findall(toast_rule) != []
    assert _LITERAL_FONT_SIZE_RE.findall(".card h3 { font-size: 0.75rem; }") == [
        "font-size: 0.75rem"
    ]
    # The inline-attribute case this pass added coverage for -- a
    # template-literal style= string, not a <style> block rule.
    assert _LITERAL_FONT_SIZE_RE.findall('<span style="font-size:0.8rem">x</span>') == [
        "font-size:0.8rem"
    ]
    # Pass 6: a defective subject for each of the two new guards above --
    # a regressed inline var(--fs-N) read, and a style= attribute whose
    # count the ceiling test must actually be capable of noticing.
    defective_markup = '<span style="font-size:var(--fs-3)">x</span>'
    assert _INLINE_STYLE_FONT_SIZE_RE.findall(defective_markup) == [
        'style="font-size:var(--fs-3)"'
    ]
    defective_js_string = 'html += `<b style="font-size:var(--fs-2)">x</b>`;'
    assert _INLINE_STYLE_FONT_SIZE_RE.findall(defective_js_string) == [
        'style="font-size:var(--fs-2)"'
    ]
    assert len(_STYLE_ATTR_RE.findall(defective_markup)) == 1
    # Pass 6: a hex literal reintroduced outside the token blocks (a
    # regressed nodeColor()/metricColor() or a stray style="color:#..."
    # in a renderer) must be caught.
    assert _HEX_LITERAL_RE.findall("const WRONG = ['#6366f1', '#22c55e'];") == [
        "#6366f1",
        "#22c55e",
    ]
    assert _HEX_LITERAL_RE.findall("background: var(--cat-1);") == []
