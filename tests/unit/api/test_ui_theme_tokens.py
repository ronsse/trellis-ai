"""The light theme is real: every colour it changes resolves from a hex
token defined on bare ``:root`` too, and every pairing a reader actually
looks at -- body text on the page background, and each badge's text on its
own tint -- clears WCAG's 4.5:1 contrast floor in both palettes.

Computed from the token hex values with the standard sRGB-luminance
formula, never eyeballed.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

import pytest

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

# Non-text UI elements (focus rings, borders meant to be seen, the solid
# accent/status colours used as border/outline paint) clear WCAG 1.4.11's
# lower 3:1 floor instead -- they delineate a control, they don't carry a
# reading task.
MIN_CONTRAST_NONTEXT = 3.0

# (foreground token, background token) pairs that a reader actually looks
# at: body copy on the page/card background, and each badge's text on its
# own tint. Shared semantic text colours used ad hoc elsewhere (e.g. an
# inline var(--danger) in a template string) are out of scope -- tightening
# those would mean re-shading tokens with dozens of other consumers.
TEXT_PAIRS = [
    ("text", "bg"),
    ("text", "surface"),
    ("text-dim", "bg"),
    ("text-dim", "surface"),
]
BADGE_PAIRS = [
    ("ok-ink", "ok-tint"),
    ("warn-ink", "warn-tint"),
    ("danger-ink", "danger-tint"),
    ("info-ink", "info-tint"),
    ("accent-ink", "accent-tint"),
    ("muted-ink", "muted-tint"),
]

# Non-text pairs: the focus ring and the "strong" border against both
# surfaces it actually paints on, plus the solid status colours used as a
# border/outline (not as text) against the surface they sit on.
NONTEXT_PAIRS = [
    ("focus-ring", "surface"),
    ("focus-ring", "bg"),
    ("border-strong", "surface"),
    ("border-strong", "bg"),
    ("accent", "surface"),
    ("accent", "bg"),
    ("danger", "surface"),
    ("ok", "surface"),
]

# Graph palette (pass 6): the 10 metric-series slots and the 16 fixed
# entity-type fills, each painted directly on the graph canvas
# (--surface-sunken) as a node/legend dot, never as body text -- the 3:1
# non-text floor applies, same as NONTEXT_PAIRS.
GRAPH_CAT_TOKENS = [f"cat-{i}" for i in range(1, 11)]
GRAPH_ENTITY_TOKENS = [
    "entity-activity",
    "entity-software-application",
    "entity-creative-work",
    "entity-file",
    "entity-agent",
    "entity-concept",
    "entity-person",
    "entity-organization",
    "entity-team",
    "entity-dataset",
    "entity-project",
    "entity-product",
    "entity-event",
    "entity-place",
    "entity-observation",
    "entity-measurement",
]
GRAPH_PAIRS = [
    (tok, "surface-sunken") for tok in GRAPH_CAT_TOKENS + GRAPH_ENTITY_TOKENS
]


def _assert_pairs_clear_floor(
    tokens: dict[str, str], label: str, pairs: list[tuple[str, str]], floor: float
) -> None:
    for fg, bg in pairs:
        ratio = contrast_ratio(tokens[fg], tokens[bg])
        assert ratio >= floor, (
            f"{label}: {fg} on {bg} is {ratio:.2f}:1, below {floor}:1 "
            f"({tokens[fg]} on {tokens[bg]})"
        )


def _assert_palette_passes(tokens: dict[str, str], label: str) -> None:
    _assert_pairs_clear_floor(tokens, label, TEXT_PAIRS + BADGE_PAIRS, MIN_CONTRAST)
    _assert_pairs_clear_floor(tokens, label, NONTEXT_PAIRS, MIN_CONTRAST_NONTEXT)
    _assert_pairs_clear_floor(tokens, label, GRAPH_PAIRS, MIN_CONTRAST_NONTEXT)


def test_dark_palette_clears_contrast_floor() -> None:
    _assert_palette_passes(_root_tokens(_page()), "dark (:root)")


def test_light_palette_via_media_query_clears_contrast_floor() -> None:
    _assert_palette_passes(_media_light_tokens(_page()), "light (prefers-color-scheme)")


def test_light_palette_via_manual_toggle_clears_contrast_floor() -> None:
    _assert_palette_passes(_attr_light_tokens(_page()), "light (data-theme toggle)")


def test_shadow_tokens_are_defined_in_every_theme_block() -> None:
    # Shadows are not hex colours, so they're exempt from the "every colour
    # on bare :root" sweep above, but they still need to exist consistently
    # across all three blocks -- a shadow present only in one palette is a
    # drift bug like any missing colour would be.
    text = _page()
    expected = {"shadow-1", "shadow-2", "shadow-3"}
    for label, tokens in (
        ("dark (:root)", _root_tokens(text)),
        ("light (prefers-color-scheme)", _media_light_tokens(text)),
        ("light (data-theme toggle)", _attr_light_tokens(text)),
    ):
        missing = expected - set(tokens)
        assert not missing, f"{label}: missing shadow tokens {sorted(missing)}"


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


def test_graph_palette_floor_catches_a_deliberately_defective_subject() -> None:
    # The dark-mode yellow (#fbbf24, --entity-organization's original hex)
    # is ~1.45:1 against the light --surface-sunken -- well under the 3:1
    # floor -- proving _assert_pairs_clear_floor() can actually fail, not
    # just pass every time it is called.
    defective_tokens = dict(_media_light_tokens(_page()))
    defective_tokens["cat-1"] = "#fbbf24"
    with pytest.raises(AssertionError):
        _assert_pairs_clear_floor(
            defective_tokens,
            "defective",
            [("cat-1", "surface-sunken")],
            MIN_CONTRAST_NONTEXT,
        )
    # The real light-mode value for the same slot clears it.
    _assert_pairs_clear_floor(
        dict(_media_light_tokens(_page())),
        "light (prefers-color-scheme)",
        [("cat-1", "surface-sunken")],
        MIN_CONTRAST_NONTEXT,
    )


def test_the_two_light_blocks_override_the_same_tokens() -> None:
    # The OS-preference block and the manual-toggle block are meant to be
    # the same palette reached two ways; a token added to one and not the
    # other is a drift bug, not a deliberate difference.
    text = _page()
    assert set(_media_light_tokens(text)) == set(_attr_light_tokens(text))


# nodeColor() gives any entity type outside the fixed list a hashed hue at a
# fixed saturation, with lightness read from --entity-hashed-lightness. The
# hue arithmetic and saturation are read from the page itself, so a change to
# either is swept here rather than drifting from a copy in this file.
_HUE_MOD_RE = re.compile(r"let hue = \(h >>> 0\) % (\d+);")
_HUE_SKIP_RE = re.compile(r"if \(hue >= (\d+)\) hue \+= (\d+);")
_HASHED_HSL_RE = re.compile(
    r"return `hsl\(\$\{hue\}, (\d+)%, \$\{cssVar\('--entity-hashed-lightness'\)"
)


def _reachable_hashed_hues(text: str) -> list[int]:
    mod = _HUE_MOD_RE.search(text)
    skip = _HUE_SKIP_RE.search(text)
    assert mod is not None, "nodeColor() hue modulus not found"
    assert skip is not None, "nodeColor() hue skip band not found"
    start, width = int(skip.group(1)), int(skip.group(2))
    return [h + width if h >= start else h for h in range(int(mod.group(1)))]


def _hsl_hex(hue: int, saturation: float, lightness: float) -> str:
    chroma = (1 - abs(2 * lightness - 1)) * saturation
    x = chroma * (1 - abs((hue / 60) % 2 - 1))
    m = lightness - chroma / 2
    r, g, b = [
        (chroma, x, 0),
        (x, chroma, 0),
        (0, chroma, x),
        (0, x, chroma),
        (x, 0, chroma),
        (chroma, 0, x),
    ][int(hue // 60) % 6]
    return "#" + "".join(f"{round((c + m) * 255):02x}" for c in (r, g, b))


def _assert_hashed_fills_clear_floor(tokens: dict[str, str], label: str) -> None:
    text = _page()
    sat = _HASHED_HSL_RE.search(text)
    assert sat is not None, "nodeColor() does not read --entity-hashed-lightness"
    lightness = float(tokens["entity-hashed-lightness"].rstrip("%")) / 100
    hues = _reachable_hashed_hues(text)
    # Hand-read: 330 hues reachable (0-227 and 258-359).
    assert len(hues) == 330
    worst = min(
        (
            contrast_ratio(
                _hsl_hex(h, int(sat.group(1)) / 100, lightness),
                tokens["surface-sunken"],
            ),
            h,
        )
        for h in hues
    )
    assert worst[0] >= MIN_CONTRAST_NONTEXT, (
        f"{label}: hashed fill at hue {worst[1]} is {worst[0]:.2f}:1 on surface-sunken"
    )


@pytest.mark.parametrize(
    ("tokens_of", "label"),
    [
        (_root_tokens, "dark (:root)"),
        (_media_light_tokens, "light (prefers-color-scheme)"),
        (_attr_light_tokens, "light (data-theme toggle)"),
    ],
)
def test_hashed_entity_fills_clear_contrast_floor(
    tokens_of: Callable[[str], dict[str, str]], label: str
) -> None:
    _assert_hashed_fills_clear_floor(tokens_of(_page()), label)


def test_hashed_fill_floor_catches_a_deliberately_defective_subject() -> None:
    # The dark-mode lightness (62%) in light mode puts hue 60 near 1.4:1.
    defective = dict(_media_light_tokens(_page()))
    defective["entity-hashed-lightness"] = "62%"
    with pytest.raises(AssertionError):
        _assert_hashed_fills_clear_floor(defective, "defective")


def test_hsl_conversion_matches_known_values() -> None:
    assert _hsl_hex(0, 1.0, 0.5) == "#ff0000"
    assert _hsl_hex(120, 1.0, 0.25) == "#008000"
    assert _hsl_hex(240, 1.0, 0.5) == "#0000ff"
