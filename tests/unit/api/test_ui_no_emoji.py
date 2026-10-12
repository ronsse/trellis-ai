"""Chrome, nav, and buttons use inline SVG icons with accessible labels, not
emoji -- an emoji glyph renders inconsistently across platforms and carries
no guaranteed accessible name.

The page still uses a handful of plain Unicode symbols (an em dash, an
ellipsis, arrows, a checkmark/cross, a fullscreen glyph) that are not
pictographic emoji; the scanned range is the pictographic blocks only, so
those are left alone.
"""

from __future__ import annotations

import html
from pathlib import Path

import trellis_api

INDEX_HTML = Path(trellis_api.__file__).parent / "static" / "index.html"

# Misc Symbols & Pictographs through Symbols & Pictographs Extended-A --
# the "real emoji" ranges. Deliberately excludes Dingbats, Arrows, Geometric
# Shapes and Misc Symbols (U+2000-U+2BFF), which this page uses as plain,
# non-pictographic UI glyphs: an em dash, a horizontal ellipsis, arrows, a
# checkmark/cross, and a fullscreen square.
EMOJI_START = 0x1F300
EMOJI_END = 0x1FAFF


def _emoji_codepoints(text: str) -> set[str]:
    decoded = html.unescape(text)
    return {ch for ch in decoded if EMOJI_START <= ord(ch) <= EMOJI_END}


def test_index_html_has_no_emoji_codepoints() -> None:
    page = INDEX_HTML.read_text(encoding="utf-8")
    found = _emoji_codepoints(page)
    assert not found, (
        "emoji codepoint(s) found in index.html, decoded from any HTML "
        f"entity form: {[hex(ord(c)) for c in found]!r}. Replace with an "
        "inline SVG icon that carries an accessible label or is "
        "aria-hidden next to visible text."
    )


def test_the_scan_decodes_numeric_character_references() -> None:
    # Prove the decode step actually runs, rather than the hex range simply
    # never matching anything: a raw emoji literal and its numeric-entity
    # form must both be caught.
    assert _emoji_codepoints("&#x1F511;") == {"\U0001f511"}
    assert _emoji_codepoints("\U0001f511") == {"\U0001f511"}


def test_the_scan_leaves_plain_ui_glyphs_alone() -> None:
    # These are real characters this page uses outside the emoji blocks;
    # the test must not treat them as violations.
    assert _emoji_codepoints("— … ↑ → ✓ ✕ ⛶ ◎") == set()
