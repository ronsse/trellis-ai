"""The Metrics tab stops promising a tooltip feature it already ships, and
starts saying when a chart's history is incomplete.

Two defects, pinned one test group each:

1. ``advisory_fitness``'s sub-text read "suppressed count in tooltip", as if
   the tooltip's ``n`` were a planned addition. It already is the point's
   ``sample_count`` (see ``renderTrendChart``'s ``title.textContent``), and
   for ``advisory_fitness`` that figure literally *is* the bucket's
   suppressed-event count (``_compute_advisory_fitness``'s docstring). The
   sub-text now states this as a present fact instead of promising it.
2. ``compute_timeseries`` has computed a ``ScanCoverage`` (whether the
   underlying EventLog read hit its cap) since #374, but the route built
   ``MetricsTimeseriesResponse`` without ever reading ``result.scan`` — a
   truncated window went flat at its recent end with no signal anywhere
   that it was incomplete. The DTO now carries ``scan_truncated`` /
   ``scanned_events``, and every trend/strip chart renders a warning when
   truncated.
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


def _trend_metrics_block(page: str) -> str:
    return _between(page, "const TREND_METRICS = [", "];\n")


def test_advisory_fitness_subtext_no_longer_promises_a_future_tooltip_field() -> None:
    block = _trend_metrics_block(_page())
    assert "suppressed count in tooltip" not in block
    # It should instead say the tooltip already carries this figure.
    advisory_entry = _between(block, "{ metric: 'advisory_fitness'", "pct: false },")
    assert "tooltip" in advisory_entry
    assert re.search(r"already (shows|carries|is)", advisory_entry), (
        f"sub-text does not state the tooltip fact as present, not future:\n"
        f"{advisory_entry}"
    )


def test_metrics_timeseries_response_has_scan_truncation_fields() -> None:
    # The DTO is the authoritative shape, not the UI reading it -- pin the
    # field names so a route regression (dropping result.scan again) is
    # caught at the schema layer, not just in index.html.
    from trellis_wire import dtos

    fields = dtos.MetricsTimeseriesResponse.model_fields
    assert "scan_truncated" in fields
    assert "scanned_events" in fields
    assert fields["scan_truncated"].default is False
    assert fields["scanned_events"].default is None


def test_render_trend_chart_warns_on_a_truncated_scan() -> None:
    page = _page()
    body = _between(
        page,
        "function renderTrendChart(meta, result) {",
        "\n        function renderEventsStrip(result) {",
    )
    assert "appendScanWarning(card, result.data)" in body


def test_render_events_strip_warns_on_a_truncated_scan() -> None:
    page = _page()
    body = _between(
        page,
        "function renderEventsStrip(result) {",
        "\n        // ====",
    )
    assert "appendScanWarning(card, result.data)" in body


def test_scan_warning_helper_reads_the_new_dto_fields_and_is_a_noop_otherwise() -> None:
    page = _page()
    body = _between(page, "function appendScanWarning(card, data) {", "\n        }\n")
    assert "data.scan_truncated" in body
    assert "data.scanned_events" in body
