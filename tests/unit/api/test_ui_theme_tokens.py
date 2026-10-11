"""The light theme is real: every colour it changes resolves from a hex
token defined on bare ``:root`` too, and every pairing a reader actually
looks at -- body text on the page background, and each badge's text on its
own tint -- clears WCAG's 4.5:1 contrast floor in both palettes.

Computed from the token hex values with the standard sRGB-luminance
formula, never eyeballed.
"""

from __future__ import annotations

import re
from pathlib import Path

import trellis_api

INDEX_HTML = Path(trellis_api.__file__).parent / "static" / "index.html"

_TOKEN_RE = re.compile(r"--([\w-]+)\s*:\s*([^;]+);")
_HEX_RE = re.compile(r"^#([0-9a-fA-F]{6})$")


def _block(text: str, selector_re: str) -> dict[str, str]:
    match = re.search(selector_re + r"\s*\{(.*?)\}", text, re.DOTALL)
    assert match is not None, f"selector not found: {selector_re}"
    return {name: value.strip() for name, value in _TOKEN_RE.findall(match.group(1))}


def _page() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


def _root_tokens(text: str) -> dict[str, str]:
    # The bare ":root {" block -- not ":root:not(...)" or ":root[...]",
    # both of which also start with ":root" but never have "root" directly
    # followed by "{", so the stricter pattern (brace appended in _block)
    # can only land on this one.
    return _block(text, r":root")


def _media_light_tokens(text: str) -> dict[str, str]:
    return _block(text, r':root:not\(\[data-theme="dark"\]\)')


def _attr_light_tokens(text: str) -> dict[str, str]:
    return _block(text, r':root\[data-theme="light"\]')


def _linearize(channel: int) -> float:
    c = channel / 255.0
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def _luminance(hex_value: str) -> float:
    match = _HEX_RE.match(hex_value.strip())
    assert match is not None, f"not a hex colour: {hex_value!r}"
    rgb = match.group(1)
    r, g, b = (int(rgb[i : i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _linearize(r) + 0.7152 * _linearize(g) + 0.0722 * _linearize(b)


def contrast_ratio(fg_hex: str, bg_hex: str) -> float:
    l1, l2 = _luminance(fg_hex), _luminance(bg_hex)
    lighter, darker = max(l1, l2), min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05)


MIN_CONTRAST = 4.5

# (foreground token, background token) pairs that a reader actually looks
# at: body copy on the page/card background, and each badge's text on its
# own tint. Shared semantic text colours used ad hoc elsewhere (e.g. an
# inline var(--error) in a template string) are out of scope -- tightening
# those would mean re-shading tokens with dozens of other consumers.
TEXT_PAIRS = [
    ("text", "bg"),
    ("text", "surface"),
    ("text-dim", "bg"),
    ("text-dim", "surface"),
]
BADGE_PAIRS = [
    ("success", "success-tint-bg"),
    ("warn", "warn-tint-bg"),
    ("error", "danger-tint-bg"),
    ("info-tint-fg", "info-tint-bg"),
    ("accent-tint-fg", "accent-tint-bg"),
]


def _assert_palette_passes(tokens: dict[str, str], label: str) -> None:
    for fg, bg in TEXT_PAIRS + BADGE_PAIRS:
        ratio = contrast_ratio(tokens[fg], tokens[bg])
        assert ratio >= MIN_CONTRAST, (
            f"{label}: {fg} on {bg} is {ratio:.2f}:1, below {MIN_CONTRAST}:1 "
            f"({tokens[fg]} on {tokens[bg]})"
        )


def test_dark_palette_clears_contrast_floor() -> None:
    _assert_palette_passes(_root_tokens(_page()), "dark (:root)")


def test_light_palette_via_media_query_clears_contrast_floor() -> None:
    _assert_palette_passes(_media_light_tokens(_page()), "light (prefers-color-scheme)")


def test_light_palette_via_manual_toggle_clears_contrast_floor() -> None:
    _assert_palette_passes(_attr_light_tokens(_page()), "light (data-theme toggle)")


def test_no_colour_is_defined_only_inside_a_theme_block() -> None:
    text = _page()
    base_keys = set(_root_tokens(text))
    media_keys = set(_media_light_tokens(text))
    attr_keys = set(_attr_light_tokens(text))

    missing_from_media = media_keys - base_keys
    missing_from_attr = attr_keys - base_keys
    assert not missing_from_media, (
        f"tokens overridden under prefers-color-scheme but never defined on "
        f"bare :root: {sorted(missing_from_media)}"
    )
    assert not missing_from_attr, (
        f"tokens overridden under [data-theme=light] but never defined on "
        f"bare :root: {sorted(missing_from_attr)}"
    )


def test_the_two_light_blocks_override_the_same_tokens() -> None:
    # The OS-preference block and the manual-toggle block are meant to be
    # the same palette reached two ways; a token added to one and not the
    # other is a drift bug, not a deliberate difference.
    text = _page()
    assert set(_media_light_tokens(text)) == set(_attr_light_tokens(text))
