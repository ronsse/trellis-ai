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
2. A real run's timestamp is labelled UTC (``fmtTime``) with a relative age
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
    # fmtUtc/fmtDateUtc were collapsed into fmtTime (one UTC-labelled
    # formatter family, e174 item 3) -- this still pins a UTC-rendering
    # call, not just any timestamp read.
    assert "fmtTime(loop.last_run_at)" in else_branch
    assert "timeAgo(loop.last_run_at)" in else_branch


def test_staleness_badge_is_gated_on_the_servers_stale_field() -> None:
    """Staleness is no longer computed client-side from a fixed hour
    constant — the server computes it per loop from that loop's own
    cadence (``admin.py``'s ``_loop_schedule_status_and_staleness``) and
    hands back a ``stale`` boolean on the row. The badge must read that
    field directly, not re-derive an age from ``last_run_at``.
    """
    page = _page()
    body = _render_loop_row_body(page)
    else_branch = _between(body, "} else {", "\n            }\n")
    assert re.search(r"if\s*\(loop\.stale\)\s*\{", else_branch)
    stale_badge_block = _between(else_branch, "if (loop.stale) {", "</span>`;")
    assert "last_status" not in stale_badge_block
    assert "stale" in stale_badge_block
    # The client must not re-derive the boolean it was just handed.
    assert "ageMs" not in else_branch
    assert "Date.now()" not in else_branch


def test_no_client_side_stale_hours_constant() -> None:
    """``LOOP_STALE_HOURS`` applied one fixed threshold to every loop
    regardless of how often its own job actually runs. Staleness is now
    computed server-side per loop's own cadence, so no such constant —
    under any name — may remain client-side.
    """
    page = _page()
    assert "LOOP_STALE_HOURS" not in page


def test_not_scheduled_badge_reads_the_servers_schedule_status() -> None:
    """A job with no stored, enabled, cadenced schedule row must read
    "not scheduled" rather than ever being evaluated for staleness —
    gated on the server's own ``schedule_status`` field, not re-derived
    from job metadata the client does not have.
    """
    page = _page()
    body = _render_loop_row_body(page)
    assert re.search(
        r"if\s*\(loop\.job_name\s*&&\s*loop\.schedule_status\s*===\s*'not_scheduled'\)\s*\{",
        body,
    )
    not_scheduled_block = _between(
        body,
        "loop.schedule_status === 'not_scheduled') {",
        "\n            }",
    )
    assert "not scheduled" in not_scheduled_block


def test_ran_status_gets_its_own_neutral_badge_not_ok() -> None:
    """An event with no explicit success/outcome field (precedent
    promotion, the tuner, feedback intake) reports ``last_status:
    "ran"`` — a neutral badge distinct from ``ok``, which only an
    event carrying an explicit outcome may ever render.
    """
    page = _page()
    body = _render_loop_row_body(page)
    else_branch = _between(body, "} else {", "\n            }\n")
    status_map_literal = _between(else_branch, "const statusMap = {", "};")
    assert re.search(r"ran:\s*'badge-info'", status_map_literal)


def test_null_counter_renders_an_em_dash_never_the_word_null() -> None:
    """A counter key present in the payload with a ``null`` value (a
    build that doesn't report it yet) must render an em dash with an
    explanatory tooltip — never the literal string ``"null"`` — while a
    real ``0`` must still render as ``0``.
    """
    page = _page()
    body = _render_loop_row_body(page)
    counters_block = _between(
        body, "const countersHtml = loop.counters", "not measured</span>'"
    )
    assert "'null'" not in counters_block
    assert re.search(r"v\s*===\s*null\s*\|\|\s*v\s*===\s*undefined", counters_block)
    assert "not reported by the build that ran" in counters_block


# ---------------------------------------------------------------------------
# 2b. `failed`/`refused` get their own badge classes, not the `skipped`
#     fallback.
# ---------------------------------------------------------------------------


def test_failed_and_refused_statuses_get_distinct_non_default_badges() -> None:
    """A ``LEARNING_AUTO_PROMOTE_EVALUATED`` run's ``outcome`` can be
    ``"refused"`` or ``"failed"`` (``loop_health.py``'s
    ``_build_candidate_auto_promote_row``), not only ``ok`` / ``degraded``
    / ``stale`` / ``skipped``. ``statusMap`` had no entry for either, so
    both fell through to the same ``badge-info`` the never-run and
    skipped states use — reading as informational rather than as a
    problem a human should look at."""
    page = _page()
    body = _render_loop_row_body(page)
    else_branch = _between(body, "} else {", "\n            }\n")
    status_map_literal = _between(else_branch, "const statusMap = {", "};")
    assert re.search(r"refused:\s*'badge-warn'", status_map_literal)
    assert re.search(r"failed:\s*'badge-err'", status_map_literal)


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
    # `views` is derived from the nav's own DOM order (see
    # test_ui_keyboard_shortcuts.py), not a hand-maintained literal, so the
    # nav tag asserted above is what puts "loops" in the derived list --
    # pin the derivation mechanism itself rather than a stale array shape.
    assert "const views = navLinks.map(a => a.dataset.view);" in page
    assert re.search(r"if\s*\(name === 'loops'\)\s*loadLoops\(\);", page)


# ---------------------------------------------------------------------------
# 4. learning_candidate_auto_promote has a real label, not the raw key.
# ---------------------------------------------------------------------------


def test_candidate_auto_promote_loop_has_a_loop_labels_entry() -> None:
    """#861 added an 8th loop-health row,
    ``learning_candidate_auto_promote``. Without a ``LOOP_LABELS`` entry,
    ``renderLoopRow``'s ``LOOP_LABELS[loop.name] || loop.name`` fallback
    would print the raw snake_case key instead of a readable title.
    """
    page = _page()
    loop_labels_body = _between(page, "const LOOP_LABELS = {", "\n        };")
    assert re.search(r"learning_candidate_auto_promote\s*:\s*'[^']+'", loop_labels_body)


def test_every_loop_name_summarize_loop_health_can_emit_has_a_label(
    tmp_path: Path,
) -> None:
    """``LOOP_LABELS[loop.name] || loop.name`` falls back to the raw
    snake_case key for any name missing an entry — this guard reads the
    live set of names straight from ``summarize_loop_health`` (not a
    hand-copied list that could itself drift) so adding a ninth row
    without a label fails here instead of shipping a raw key to a
    screen.
    """
    from trellis.ops.loop_health import summarize_loop_health
    from trellis.stores.sqlite.event_log import SQLiteEventLog
    from trellis.stores.sqlite.tuner_state import SQLiteTunerStateStore

    event_log = SQLiteEventLog(tmp_path / "events.db")
    tuner_state = SQLiteTunerStateStore(tmp_path / "tuner.db")
    report = summarize_loop_health(event_log, tuner_state)

    page = _page()
    loop_labels_body = _between(page, "const LOOP_LABELS = {", "\n        };")
    for row in report.loops:
        assert re.search(rf"{re.escape(row.name)}\s*:\s*'[^']+'", loop_labels_body), (
            f"missing LOOP_LABELS entry for {row.name!r}"
        )


def test_a_loop_name_missing_from_loop_labels_is_caught() -> None:
    """Prove the guard above actually discriminates: a ``LOOP_LABELS``
    object missing one real loop name must fail the same assertion
    style the real test uses, not just happen to pass by coincidence.
    """
    defective = (
        "const LOOP_LABELS = {\n"
        "            noise_demotion: 'Noise demotion',\n"
        "        };"
    )
    loop_labels_body = _between(defective, "const LOOP_LABELS = {", "\n        };")
    assert re.search(r"noise_demotion\s*:\s*'[^']+'", loop_labels_body)
    assert not re.search(r"tuner\s*:\s*'[^']+'", loop_labels_body)
