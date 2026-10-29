"""The Memories pager stops at the offset ceiling the list route enforces.

A key below ``restricted`` is refused (400) a ``GET /documents`` offset above
``_MAX_FILTERED_OFFSET``, and the response says nothing about the ceiling, so
the page carries its own copy. A copy drifts, so this pins it to the route's
constant and checks the Next button is disabled on it.
"""

from __future__ import annotations

import re
from pathlib import Path

import trellis_api
from trellis_api.routes import explore

INDEX_HTML = Path(trellis_api.__file__).parent / "static" / "index.html"


def _page() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


def test_the_page_copies_the_routes_offset_ceiling() -> None:
    found = re.search(r"const MEM_FILTERED_OFFSET_CEILING = (\d+);", _page())

    assert found is not None, "the pager's ceiling constant is gone"
    assert int(found.group(1)) == explore._MAX_FILTERED_OFFSET


def test_next_is_disabled_past_the_ceiling_on_an_inexact_total() -> None:
    page = _page()
    body = page[page.index("async function loadMemories") :]
    body = body[: body.index("function memPage")]

    assert "data.total_exact === false" in body
    assert "memOffset + limit > MEM_FILTERED_OFFSET_CEILING" in body
    assert re.search(r"mem-next'\)\.disabled = [^;]*\|\| pastCeiling;", body)
