"""The Graph tab's first-visit sample load surfaces a failure instead of
discarding it.

``autoLoadSample()`` runs once, from ``initGraph()``, to populate the canvas
with a few entities so the tab isn't empty on first visit. Its ``catch``
block used to read ``// Silently ignore -- user can still search
manually`` and do nothing else: a failed ``/graph/search`` call left the
operator looking at an empty canvas with no way to tell "nothing to show
yet" from "the search call failed". The fix renders the error into a
dedicated banner element (``#graph-sample-warning``, present in the markup
from page load, mirroring the Schedules tab's dispatcher-banner container)
instead of swallowing it.
"""

from __future__ import annotations

import re
from pathlib import Path

import trellis_api

INDEX_HTML = Path(trellis_api.__file__).parent / "static" / "index.html"


def _page() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


def _auto_load_sample_body() -> str:
    page = _page()
    match = re.search(
        r"async function autoLoadSample\(\) \{(.*?)\n        \}",
        page,
        re.DOTALL,
    )
    assert match is not None, "autoLoadSample() not found"
    return match.group(1)


def test_graph_tab_has_a_sample_warning_container() -> None:
    """The banner's container is in the Graph tab's markup from page load,
    not conjured by the JS -- the same shape as ``#schedules-dispatcher-banner``,
    which `renderDispatcherBanner` fills the same way."""
    page = _page()
    graph_view = re.search(
        r'<div id="view-graph" style="display:none">(.*?)<div class="search-bar">',
        page,
        re.DOTALL,
    )
    assert graph_view is not None
    assert '<div id="graph-sample-warning"></div>' in graph_view.group(1)


def test_auto_load_sample_no_longer_silently_ignores_the_error() -> None:
    body = _auto_load_sample_body()
    assert "Silently ignore" not in body


def test_auto_load_sample_renders_the_error_into_the_warning_banner() -> None:
    """The catch block writes into ``#graph-sample-warning`` using the
    shared degraded-banner's ``banner-warn`` variant (amber caution, not a
    hard red failure -- a failed convenience load is not a data-integrity
    problem)."""
    body = _auto_load_sample_body()
    catch_block = body.split("catch (e)", 1)[1]
    assert "getElementById('graph-sample-warning')" in catch_block
    assert "degraded-banner banner-warn" in catch_block
    assert "${escHtml(e.message)}" in catch_block


def test_auto_load_sample_failure_still_leaves_manual_search_usable() -> None:
    """A regression here must not re-throw past the catch or otherwise
    block the manual search controls the banner itself points the operator
    at."""
    body = _auto_load_sample_body()
    catch_block = body.split("catch (e)", 1)[1]
    assert "throw" not in catch_block
