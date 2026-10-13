"""The Memories tab pages a search the same way it pages the unfiltered
list.

``loadMemories()`` used to special-case ``q``: the pagination bar was
hidden outright whenever a search was active, because ``GET /documents``
ignored ``offset``/``limit`` past the first page for a query. The route
now pages a search's matches the same way it pages the unfiltered list
(see ``src/trellis_api/routes/explore.py::list_documents``), so the UI no
longer needs the special case -- one pagination idiom covers both, and a
query matching at least the server's scan bound renders a "N+" total
(``total_exact: false``) rather than a silently wrong exact count.
"""

from __future__ import annotations

import re
from pathlib import Path

import trellis_api

INDEX_HTML = Path(trellis_api.__file__).parent / "static" / "index.html"


def _page() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


def _load_memories_body() -> str:
    page = _page()
    match = re.search(
        r"async function loadMemories\(\) \{(.*?)"
        r"\n        \}\n\n        function memPage",
        page,
        re.DOTALL,
    )
    assert match is not None, "loadMemories() not found"
    return match.group(1)


def test_memories_pagination_container_exists_in_markup() -> None:
    # The Ledger components pass replaced every view-local pager (this one
    # included) with the one shared `.pager` component.
    page = _page()
    assert '<div class="pager" id="mem-pagination" style="display:none">' in page


def test_load_memories_no_longer_special_cases_search_pagination() -> None:
    """Regression guard for the fixed defect: no branch keyed on ``q``
    hides the pagination bar any more."""
    body = _load_memories_body()
    assert "search results are unpaged" not in body
    assert not re.search(r"if\s*\(q\)\s*\{\s*pag\.style\.display", body)


def test_load_memories_pagination_runs_unconditionally_on_total() -> None:
    body = _load_memories_body()
    assert "const total = data.total || 0;" in body
    assert "pag.style.display = total > limit ? 'flex' : 'none';" in body


def test_load_memories_renders_an_inexact_total_with_a_plus_suffix() -> None:
    """A query matching the server's scan bound comes back with
    ``total_exact: false``; the UI must say "N+", not claim an exact N."""
    body = _load_memories_body()
    assert "data.total_exact === false" in body
    assert "+`" in body


def test_load_memories_page_info_uses_fmt_num_not_raw_tolocalestring() -> None:
    body = _load_memories_body()
    assert "fmtNum(memOffset + 1, 0)" in body
    assert "fmtNum(memOffset + docs.length, 0)" in body
    assert "fmtNum(total, 0)" in body
