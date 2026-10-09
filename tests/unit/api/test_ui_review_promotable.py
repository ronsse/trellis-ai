"""The Review tab's learning-candidate cards gate the Approve control.

``GET /learning/candidates`` computes a server-side ``promotable`` flag from
the same ``PROMOTE_RECOMMENDATIONS`` set ``prepare_learning_promotions``
checks (see ``tests/unit/api/test_review_routes.py::
TestLearningCandidates::test_promotable_flag_matches_prepare_learning_promotions``).
These tests pin that the page actually uses that flag: a candidate the
promote route would skip as ``skipped_non_promotable`` must never render the
``learn-approve`` checkbox that feeds ``submitLearningPromotions()``, and
promotable rows must sort ahead of report-only ones so they are not buried
among the majority non-promotable rows a nightly run produces.
"""

from __future__ import annotations

import re
from pathlib import Path

import trellis_api

INDEX_HTML = Path(trellis_api.__file__).parent / "static" / "index.html"


def _load_review_learning_body(page: str) -> str:
    found = re.search(
        r"async function loadReviewLearning\(\) \{(.*?)"
        r"async function submitLearningPromotions\(\) \{",
        page,
        re.DOTALL,
    )
    assert found is not None, "loadReviewLearning() not found"
    return found.group(1)


def test_sort_places_promotable_candidates_first() -> None:
    body = _load_review_learning_body(INDEX_HTML.read_text(encoding="utf-8"))
    sort = re.search(r"\.sort\(\(a, b\) => \{(.*?)\}\);", body, re.DOTALL)
    assert sort is not None, "candidates are rendered in server order, unsorted"
    clause = sort.group(1)
    # Both sides key off the same server-computed field the route adds.
    assert "a.c.promotable" in clause
    assert "b.c.promotable" in clause
    # promotable (truthy) sorts to 0, non-promotable to 1, so ascending order
    # of that key puts promotable rows first; ties keep original order.
    assert re.search(r"a\.c\.promotable \? 0 : 1", clause)
    assert re.search(r"b\.c\.promotable \? 0 : 1", clause)
    # ...and the comparator is ascending on that key: `pb - pa` would bury
    # the promotable rows under the report-only ones instead.
    assert re.search(r"return pa - pb \|\| a\.i - b\.i;", clause)


def test_approve_checkbox_is_rendered_only_for_promotable_rows() -> None:
    body = _load_review_learning_body(INDEX_HTML.read_text(encoding="utf-8"))
    stmt = re.search(
        r"const approveControl = promotable(.*?)\n\s*return `",
        body,
        re.DOTALL,
    )
    assert stmt is not None, "approveControl assignment not found"
    assert re.match(r"\s*\?", stmt.group(1)), (
        "approveControl does not open with a `?` — not a ternary on `promotable`"
    )
    # The two arms are template literals; the only `` `:` `` sequence in the
    # statement is the ternary's own separator (there is no literal colon
    # immediately between backticks inside either arm's markup).
    parts = re.split(r"`\s*:\s*`", stmt.group(1))
    assert len(parts) == 2, (
        "approveControl is not a simple two-armed `promotable ? ... : ...` "
        f"ternary: {parts!r}"
    )
    promotable_branch, non_promotable_branch = parts

    # The checkbox that submitLearningPromotions() later collects via
    # `.learn-approve` exists only on the promotable side.
    assert 'class="learn-approve"' in promotable_branch
    assert 'class="learn-approve"' not in non_promotable_branch
    # ...and that arm holds the card's only one, so no markup outside the
    # ternary can add an ungated checkbox back.
    assert body.count('class="learn-approve"') == 1

    # The non-promotable side is report-only: a badge, no checkbox, no
    # rationale textarea (nothing a submit could pick up).
    assert "report-only" in non_promotable_branch
    assert "badge" in non_promotable_branch
    assert "review-rationale" not in non_promotable_branch

    # `promotable` itself is strictly derived from the server's field, not
    # presence/truthiness of some other shape.
    assert "const promotable = c.promotable === true;" in body

    # Exactly one approveControl is computed per card and it is the only
    # place the card template interpolates it.
    assert body.count("const approveControl") == 1
    assert body.count("${approveControl}") == 1


def test_non_promotable_tooltip_escapes_recommendation_type() -> None:
    body = _load_review_learning_body(INDEX_HTML.read_text(encoding="utf-8"))
    # The one new interpolated attribute this change adds (the tooltip's
    # `title`) goes through escHtml like every other interpolated attribute
    # value (tests/unit/api/test_ui_attribute_escaping.py's floor already
    # covers the existing sites; this pins the new one specifically).
    tooltip = re.search(
        r'title="[^"]*\$\{escHtml\(c\.recommendation_type[^}]*\)\}', body
    )
    assert tooltip is not None, (
        "the report-only tooltip's title attribute does not escape "
        "c.recommendation_type"
    )
