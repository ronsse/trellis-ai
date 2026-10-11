"""The Schedules tab's UI-level fixes from gate #860 stay fixed.

Three defects, pinned here because none of them is reachable from a
Python-level API test — they are properties of the generated JavaScript
source itself, following the same source-regex convention as
``test_ui_review_truth.py`` and ``test_ui_p2_sweep.py``:

1. Blocker 3: the Edit form read the raw cadence back out of a table
   cell's rendered ``textContent``, which also carries the human cadence
   description and, for a customized job, a "catalog default: ..." line
   — so an unchanged Save on a customized job silently rewrote the
   cadence to that second line's text. It now reads a dedicated
   ``data-cadence`` attribute the cell carries alongside its display
   text, and ``confirmEditJob`` never re-derives cadence from the row at
   all — only from the input the operator sees.
2. Blocker 4 (UI half): due-jobs evaluates every cadence and last-run
   timestamp in UTC. ``describeCron()`` labels every cadence form "UTC"
   rather than leaving the viewer to assume their own browser zone.
3. Blocker 5: the dispatcher banner used to read "nothing will pick it
   up" when no job-run had been recorded recently, even though a healthy
   dispatcher's own ``due-jobs`` polls leave no record at all — so the
   claim was false exactly when nothing was wrong. It now states only
   what the EventLog actually shows, and renders nothing at all once a
   run is seen.
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


def _show_edit_job_form_body(page: str) -> str:
    return _between(
        page,
        "function showEditJobForm(name) {",
        "\n        function cancelEditJobForm() {",
    )


# ---------------------------------------------------------------------------
# Blocker 3: cadence comes from a data attribute, never textContent.
# ---------------------------------------------------------------------------


def test_edit_form_reads_cadence_from_data_attribute_not_textcontent() -> None:
    body = _show_edit_job_form_body(_page())
    assert "cells[2].dataset.cadence" in body
    # The cadence cell's own textContent also carries the human
    # description and an optional "catalog default: ..." line -- reading
    # that back as the cadence is exactly the bug gate #860 found, so its
    # absence here is the regression this test exists to catch.
    assert "cells[2].textContent" not in body


def test_cadence_cell_carries_a_data_cadence_attribute() -> None:
    page = _page()
    assert "data-cadence=\"${escHtml(job.cadence || '')}\"" in page


# ---------------------------------------------------------------------------
# Blocker 4 (UI half): every cadence form is explicitly labelled UTC.
# ---------------------------------------------------------------------------


def test_describe_cron_labels_every_cadence_form_utc() -> None:
    page = _page()
    body = _between(page, "function describeCron(cadence) {", "\n        }\n")
    assert "Daily at" in body
    assert "Weekly on" in body
    assert "Monthly on day" in body
    for line in body.splitlines():
        if "Daily at" in line or "Weekly on" in line or "Monthly on day" in line:
            assert "UTC" in line, f"cadence description not labelled UTC: {line!r}"
    # No wording anywhere in the page claims a cadence or run time is the
    # host's own local clock -- due-jobs evaluates UTC unconditionally
    # (trellis.core.cron), so any such claim would be false.
    assert "local time" not in page.lower()
    assert "local clock" not in page.lower()


# ---------------------------------------------------------------------------
# Blocker 5: the dispatcher banner never claims "nothing will pick it up".
# ---------------------------------------------------------------------------


def test_dispatcher_banner_never_claims_nothing_will_pick_it_up() -> None:
    page = _page()
    body = _between(
        page,
        "function renderDispatcherBanner(seen, lastSeenAt) {",
        "\n        }\n",
    )
    # Only the *rendered* HTML (the el.innerHTML template literal) can
    # ever reach an operator -- a surrounding code comment is allowed to
    # name the retired phrase while explaining why it was removed, so
    # scope the check to the template literal itself, not the whole
    # function body.
    rendered = _between(body, "el.innerHTML = `", "`;")
    assert "nothing will pick" not in rendered.lower()
    # A missing poll record is not evidence of a dead dispatcher -- the
    # banner must say so rather than asserting the dispatcher is gone.
    assert "not proof" in rendered.lower() or "leave no record" in rendered.lower()


def test_dispatcher_banner_renders_nothing_once_a_run_is_seen() -> None:
    body = _between(
        _page(),
        "function renderDispatcherBanner(seen, lastSeenAt) {",
        "\n        }\n",
    )
    assert "if (seen) { el.innerHTML = ''; return; }" in body
