"""A score/ratio/effect-size value must never render as raw NaN/Infinity/null.

``.toFixed()`` does not throw on a non-finite JS number -- it silently
stringifies it, so a bare ``x.toFixed(n)`` call site can leak the literal
text "Infinity" or "NaN" into the page. This is not a hypothetical: a tuner
gate has shipped ``effect_size: Infinity`` off an empty baseline (a
divide-by-zero in a ratio with no prior data). Every ``.toFixed()`` call
site in the admin UI now routes through one of two shared helpers,
``fmtNum``/``fmtPct``, which check ``Number.isFinite()`` first and only ever
return a normal bounded numeral (with ``toLocaleString`` thousands
separators) or one of two named states: "not measured" (null/undefined) or
"unbounded (no baseline)" (NaN and +-Infinity alike -- both mean the
comparison had no baseline to measure against, so neither renders as a bare
dash or a raw non-finite token).

The Reports tab's ``computed_at`` stamp used to render the raw ISO string
with no timezone label; it now goes through the existing ``fmtTime()``
helper like every other absolute timestamp in the page.
"""

from __future__ import annotations

import re
from pathlib import Path

import trellis_api
from tests.ast_rules import assert_hand_read_floor

INDEX_HTML = Path(trellis_api.__file__).parent / "static" / "index.html"


def _page() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


def _function_body(page: str, name: str) -> str:
    found = re.search(
        r"function " + re.escape(name) + r"\([^)]*\) \{(.*?)\n        \}",
        page,
        re.DOTALL,
    )
    assert found is not None, f"function {name}() not found"
    return found.group(1)


_RENDER_CARD_CALL_RE = re.compile(r"(?<!function )renderCard\(")


def _render_card_call_arg_counts(page: str) -> list[int]:
    """Argument count of every ``renderCard(...)`` call site.

    A hand-rolled bracket-depth walk rather than a single regex: the
    calls' arguments include nested ``(...)``/``[...]`` expressions (for
    example ``(data.item_scores || []).length``), and a comma inside one
    of those must not be counted as a fourth argument.
    """
    counts = []
    for match in _RENDER_CARD_CALL_RE.finditer(page):
        i = match.end()
        depth = 1
        commas_at_top = 0
        while depth > 0:
            ch = page[i]
            if ch in "([":
                depth += 1
            elif ch in ")]":
                depth -= 1
            elif ch == "," and depth == 1:
                commas_at_top += 1
            i += 1
        counts.append(commas_at_top + 1)
    return counts


class TestSharedFormattersExist:
    def test_fmt_num_defined(self) -> None:
        assert "function fmtNum(value, digits = 2) {" in _page()

    def test_fmt_pct_defined(self) -> None:
        assert "function fmtPct(value, digits = 1) {" in _page()

    def test_fmt_num_guards_both_non_finite_states(self) -> None:
        body = _function_body(_page(), "fmtNum")
        assert "value == null" in body
        assert "Number.isFinite(n)" in body
        assert "not measured" in body
        assert "no baseline" in body

    def test_fmt_pct_guards_both_non_finite_states(self) -> None:
        body = _function_body(_page(), "fmtPct")
        assert "value == null" in body
        assert "Number.isFinite(n)" in body
        assert "not measured" in body
        assert "no baseline" in body


class TestNoRawToFixedLeft:
    """Regression guard: every former ``.toFixed()`` call site must now
    route through fmtNum/fmtPct, which format their bounded branch with
    ``toLocaleString`` (so displayed numbers also get thousands
    separators) rather than ``toFixed``. ``.toFixed(`` must not appear
    anywhere in the file, inside the helpers or out."""

    def test_only_the_helpers_themselves_call_tofixed(self) -> None:
        page = _page()
        fmt_num_body = _function_body(page, "fmtNum")
        fmt_pct_body = _function_body(page, "fmtPct")
        assert fmt_num_body.count(".toFixed(") == 0
        assert fmt_pct_body.count(".toFixed(") == 0
        code_lines = [
            line for line in page.splitlines() if not line.strip().startswith("//")
        ]
        outside_helpers = (
            "\n".join(code_lines).replace(fmt_num_body, "").replace(fmt_pct_body, "")
        )
        assert outside_helpers.count(".toFixed(") == 0


class TestKnownTroubleSitesUseTheHelper:
    """The effect_size sites are the documented real-world Infinity source;
    confirm each one now goes through fmtNum rather than a bare toFixed."""

    def test_precedent_card_effect_size_uses_fmt_num(self) -> None:
        assert "effect_size: ${fmtNum(p.effect_size, 4)}" in _page()

    def test_prediction_effect_size_uses_fmt_num(self) -> None:
        assert "fmtNum(pv.effect_size, 4)" in _page()

    def test_advisory_effect_size_uses_fmt_num(self) -> None:
        assert "${fmtNum(ev.effect_size, 2)}" in _page()

    def test_advisory_confidence_uses_fmt_num(self) -> None:
        # The Ledger components pass right-aligns the numeric cell with
        # class="num"; the fmtNum() call itself is unchanged.
        assert '<td class="num">${fmtNum(a.confidence, 2)}</td>' in _page()

    def test_advisory_evidence_confidence_uses_fmt_num(self) -> None:
        assert "const evidenceConf = fmtNum(ev.evidence_confidence, 2);" in _page()

    def test_effectiveness_success_rate_uses_fmt_pct(self) -> None:
        page = _page()
        assert "fmtPct(data.success_rate, 1)" in page
        assert "fmtPct(item.success_rate, 1)" in page

    def test_effectiveness_item_bar_width_is_clamped_separately_from_label(
        self,
    ) -> None:
        """The bar's CSS width must stay a bounded number even when the
        label falls back to a named non-finite state -- otherwise an
        unclamped NaN/Infinity would land directly in a style attribute."""
        page = _page()
        assert "Number.isFinite(item.success_rate)" in page
        assert "Math.max(0, Math.min(100, item.success_rate * 100)) : 0" in page


class TestReportsComputedAtUsesFmtTime:
    def test_computed_at_goes_through_fmt_time(self) -> None:
        page = _page()
        assert "fmtTime(data.computed_at, { empty: 'unknown' })" in page
        assert "computed_at: ${escHtml(data.computed_at || 'unknown')}" not in page


class TestRenderCardRawArgument:
    """``renderCard(title, value, sub, raw)``'s fourth parameter drives the
    ``.value.na``/``.value.inf`` modifiers (null/non-finite), but a
    parameter nothing calls with is dead on arrival. Exactly one of the
    nine call sites has a display string that can actually be null or
    non-finite -- the rest pass a plain count, never routed through
    fmtNum/fmtPct, so adding raw to them would just be a no-op argument.
    """

    def test_render_card_still_declares_the_raw_parameter(self) -> None:
        assert "function renderCard(title, value, sub, raw) {" in _page()

    def test_exactly_one_caller_passes_a_fourth_argument(self) -> None:
        counts = _render_card_call_arg_counts(_page())
        assert len(counts) == 9, (
            f"expected 9 total renderCard(...) call sites, found "
            f"{len(counts)} -- the floor below assumes this population"
        )
        four_arg_calls = [c for c in counts if c == 4]
        assert_hand_read_floor(
            len(four_arg_calls),
            1,
            subject="renderCard(...) call(s) passing a fourth (raw) argument",
            hint="hand count: 1.",
            sole_site_reason=(
                "the other eight renderCard() callers (Total Packs, With "
                "Feedback, Items Scored, Traces, Documents, Graph Nodes, "
                "Graph Edges, Events) all pass a plain integer count that "
                "never goes through fmtNum/fmtPct and is never null, so "
                "only the effectiveness dashboard's Success Rate card has "
                "a raw value worth passing through."
            ),
        )

    def test_success_rate_card_raw_mirrors_its_display_branch(self) -> None:
        page = _page()
        assert (
            "const successRateRaw = data.total_feedback === 0"
            " ? null : data.success_rate;" in page
        )
        assert (
            "renderCard('Success Rate', successRateText, successRateSub,"
            " successRateRaw)" in page
        )

    def test_arg_counter_catches_a_deliberately_defective_subject(self) -> None:
        three_args = "renderCard('Total Packs', data.total_packs, 'context packs')"
        four_args = "renderCard('Success Rate', text, sub, raw)"
        nested_parens_three_args = (
            "renderCard('Items Scored', (data.item_scores || []).length, 'x')"
        )
        assert _render_card_call_arg_counts(three_args) == [3]
        assert _render_card_call_arg_counts(four_args) == [4]
        assert _render_card_call_arg_counts(nested_parens_three_args) == [3]
