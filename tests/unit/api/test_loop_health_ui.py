"""The Loops tab renders per-loop health truthfully (e167).

Static assertions on ``index.html``'s own markup/script, mirroring the
pattern in ``test_ui_review_truth.py``: no browser, no JS engine — pin the
exact code shape so a mutant that moves a gate or drops a branch is caught,
not just "the right words appear somewhere in the file."

Three claims under test:

1. A loop with no event yet (``last_run_at`` is ``null``) must render
   "Not measured" and a distinct "never run" badge — never folded into the
   same path as a real, zero-valued run (``renderLoopRow``'s ``if
   (!loop.last_run_at)`` branch).
2. A real run's timestamp is labelled UTC (``fmtUtc``) with a relative age
   (``timeAgo``), and staleness (no run in over ``LOOP_STALE_HOURS``) is
   computed from that same timestamp, not from ``last_status`` — a loop can
   report ``last_status: "ok"`` from its last run while that run itself is
   long stale.
3. A fetch failure on ``GET /loops`` renders a visible error in
   ``#loops-rows``, not a silent empty state.
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


def _render_loop_row_body(page: str) -> str:
    return _between(
        page,
        "function renderLoopRow(loop) {",
        "\n        //  SIDEBAR RAIL",
    )


# ---------------------------------------------------------------------------
# 1. Never-run vs a real zero-valued run.
# ---------------------------------------------------------------------------


def test_null_last_run_at_renders_not_measured_not_zero() -> None:
    body = _render_loop_row_body(_page())
    branch = _between(body, "if (!loop.last_run_at) {", "} else {")
    assert "Not measured" in branch
    assert "never run" in branch
    # The zero-counters case must not share this branch.
    assert "counters" not in branch


def test_never_run_branch_is_the_falsy_last_run_at_check() -> None:
    # Pin the guard itself, not just that the strings exist somewhere: a
    # mutant that inverts or drops this condition would otherwise still
    # pass the string-presence checks above.
    body = _render_loop_row_body(_page())
    assert re.search(r"if\s*\(!loop\.last_run_at\)\s*\{", body)


# ---------------------------------------------------------------------------
# 2. UTC label + age, and staleness keyed on the timestamp, not last_status.
# ---------------------------------------------------------------------------


def test_real_run_timestamp_uses_fmt_utc_and_time_ago() -> None:
    body = _render_loop_row_body(_page())
    else_branch = _between(body, "} else {", "\n            }\n")
    assert "fmtUtc(loop.last_run_at)" in else_branch
    assert "timeAgo(loop.last_run_at)" in else_branch


def test_staleness_is_computed_from_last_run_at_age_not_status() -> None:
    page = _page()
    body = _render_loop_row_body(page)
    else_branch = _between(body, "} else {", "\n            }\n")
    # The stale computation reads the timestamp's own age...
    assert re.search(
        r"ageMs\s*=\s*Date\.now\(\)\s*-\s*new Date\(loop\.last_run_at\)\.getTime\(\)",
        else_branch,
    )
    assert re.search(
        r"stale\s*=\s*ageMs\s*>\s*LOOP_STALE_HOURS\s*\*\s*3600\s*\*\s*1000", else_branch
    )
    # ...and the "stale" badge append is gated on that boolean, not on
    # loop.last_status.
    assert re.search(r"if\s*\(stale\)\s*\{", else_branch)
    stale_badge_block = _between(else_branch, "if (stale) {", "</span>`;")
    assert "last_status" not in stale_badge_block
    assert "stale" in stale_badge_block


def test_stale_threshold_is_36_hours() -> None:
    page = _page()
    # The constant is defined once and reused by the row renderer above —
    # pinning the literal here catches a drift between the two.
    assert re.search(r"const LOOP_STALE_HOURS\s*=\s*36\s*;", page)


# ---------------------------------------------------------------------------
# 3. Fetch failure is a visible error, not a silent empty state.
# ---------------------------------------------------------------------------


def test_fetch_failure_renders_visible_error_in_loops_rows() -> None:
    page = _page()
    body = _between(
        page,
        "async function loadLoops() {",
        "\n        function renderLoopRow(loop) {",
    )
    catch_block = _between(body, "} catch (err) {", "\n            }\n        }")
    assert "loops-rows" not in catch_block  # container ref already captured above
    assert "container.innerHTML" in catch_block
    assert "err.message" in catch_block
    assert "color:var(--error)" in catch_block


def test_loops_view_is_wired_into_nav_and_switch_view() -> None:
    page = _page()
    assert 'data-view="loops" data-label="Loops"' in page
    assert 'id="view-loops"' in page
    assert re.search(r"views\s*=\s*\[[^\]]*'loops'[^\]]*\]", page)
    assert re.search(r"if\s*\(name === 'loops'\)\s*loadLoops\(\);", page)
