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
  round"). Nine sites use it on purpose -- eight pre-existing dots and
  avatars, plus the one badge leading dot this pass added -- and this
  test pins that exact count, so a tenth appearing silently (a literal
  px radius typed as ``50%`` to dodge the ``Npx`` regex) still has a
  floor to clear, and a vanished one is worth a look too.
- The three token-definition blocks themselves (bare ``:root``, the
  ``prefers-color-scheme`` block, and the ``[data-theme="light"]``
  block) legitimately declare ``--shadow-1`` / ``--shadow-2`` /
  ``--shadow-3`` as ``rgba(...)`` composites -- that *is* the token's
  value, not a bypass of it -- and are excluded from the ``box-shadow``
  sweep by construction: the sweep only reads the text after the last of
  the three blocks closes.
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


def test_no_literal_pixel_radius_remains() -> None:
    hits = _LITERAL_RADIUS_RE.findall(_page())
    assert hits == [], (
        f"literal border-radius: Npx found ({hits}) -- use var(--r-1), "
        f"var(--r-2) or var(--r-pill) instead"
    )


def test_circle_radius_sites_are_pinned_at_a_hand_count() -> None:
    # Hand count for this pass: eight pre-existing circular dots/avatars
    # plus the one badge leading dot this pass added.
    hits = _CIRCLE_RADIUS_RE.findall(_page())
    assert len(hits) == 9, (
        f"expected exactly 9 border-radius: 50% sites, found {len(hits)} -- "
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


def test_each_pattern_catches_a_deliberately_defective_subject() -> None:
    assert _LITERAL_RADIUS_RE.findall(".card { border-radius: 6px; }") == [
        "border-radius: 6px"
    ]
    toast_rule = ".toast { box-shadow: 0 4px 16px rgba(0,0,0,0.25); }"
    assert _BOX_SHADOW_RGBA_RE.findall(toast_rule) != []
