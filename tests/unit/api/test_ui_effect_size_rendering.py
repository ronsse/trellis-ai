"""A non-finite ``effect_size`` must never render as the raw word
"Infinity"/"NaN".

Tier-1 auto-promotion gated against an empty baseline reports
``effect_size: Infinity`` (division by zero against a baseline of zero is
the literal signal "there is nothing to compare against" — see
``docs/design/...`` on the first-proposal-passes-any-baseline-gate defect),
and JSON carries that as the bare token ``Infinity``, not a string.
``Number(Infinity).toFixed(n)`` happily renders the word "Infinity" with no
unit, reading as a rendering bug rather than as what it means. All three
dashboard sites that render an ``effect_size`` now route through a shared
``fmtEffectSize()`` helper: ``null`` -> ``'—'``, non-finite (``Infinity``,
``-Infinity``, ``NaN``) -> ``'∞ (no baseline)'``, otherwise a fixed-point
string.
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


def _fmt_effect_size_body(page: str) -> str:
    return _between(
        page,
        "function fmtEffectSize(value, digits) {",
        "\n        }\n",
    )


def test_fmt_effect_size_returns_em_dash_for_null() -> None:
    body = _fmt_effect_size_body(_page())
    assert "if (value == null) return '—';" in body


def test_fmt_effect_size_labels_non_finite_as_no_baseline_not_raw_infinity() -> None:
    body = _fmt_effect_size_body(_page())
    assert "!Number.isFinite(n)" in body
    assert "'∞ (no baseline)'" in body
    # The raw numeric value must never be interpolated into this branch —
    # that is exactly how "Infinity" leaked into the page before.
    assert "${value}" not in body
    assert "${n}" not in body


def test_fmt_effect_size_finite_path_still_rounds() -> None:
    body = _fmt_effect_size_body(_page())
    assert "n.toFixed(digits)" in body


def test_tuner_proposal_card_uses_fmt_effect_size() -> None:
    page = _page()
    body = _between(
        page,
        "async function loadReviewProposals() {",
        "async function confirmPromoteProposal(id) {",
    )
    assert "effect_size: ${escHtml(fmtEffectSize(p.effect_size, 4))}" in body
    # The old inline pattern, which rendered a raw "Infinity", must be gone.
    assert "Number(p.effect_size).toFixed(4)" not in body


def test_promote_preview_uses_fmt_effect_size() -> None:
    page = _page()
    body = _between(
        page,
        "async function confirmPromoteProposal(id) {",
        "async function promoteProposal(id) {",
    )
    assert "fmtEffectSize(pv.effect_size, 4)" in body
    assert "Number(pv.effect_size).toFixed(4)" not in body


def test_advisory_row_uses_fmt_effect_size() -> None:
    page = _page()
    body = _between(
        page,
        "function renderAdvisoryRow(a) {",
        "function showGenerateAdvisoriesForm() {",
    )
    assert "fmtEffectSize(ev.effect_size, 2)" in body
    assert "ev.effect_size.toFixed(2)" not in body
