"""No inline event handler on the UI page interpolates a value.

Ids reached the page's handlers through inline JavaScript, as in
``onclick="loadTraceDetail('${escHtml(t.trace_id)}')"``. escHtml encodes for
HTML, and the browser decodes that before it runs the handler, so a quote in
an agent-written id ended the JS string and the rest of the id ran as script.
A template now names the function in ``data-action`` and carries the id in
``data-id``, and one listener in the page makes the call.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import trellis_api
from tests.ast_rules import assert_hand_read_floor

INDEX_HTML = Path(trellis_api.__file__).parent / "static" / "index.html"

# An on* attribute and its value: double-quoted, single-quoted or bare. A
# quoted value may span lines.
_HANDLER = re.compile(
    r"""\son[a-z]+\s*=\s*("[^"]*"|'[^']*'|[^\s"'>]+)""", re.IGNORECASE
)


def _interpolating(page: str) -> list[str]:
    return [m.group(0).strip() for m in _HANDLER.finditer(page) if "${" in m.group(1)]


def test_no_inline_handler_interpolates_a_value() -> None:
    page = INDEX_HTML.read_text(encoding="utf-8")
    # The page's static handlers (Refresh, Save, Prev/Next, ...) are inline
    # too. A scan that stopped seeing them would pass whatever the page held.
    assert_hand_read_floor(
        len(_HANDLER.findall(page)), 38, subject="inline on* handlers in index.html"
    )
    hits = _interpolating(page)
    listing = "\n".join(hits)
    assert not hits, f"{len(hits)} inline handlers interpolate a value:\n{listing}"


@pytest.mark.parametrize(
    "snippet",
    [
        """<tr onclick="loadTraceDetail('${escHtml(t.trace_id)}')">""",
        """<button onClick='draftAdr("${id}")'>""",
        "<span onmouseover=show(${i})>",
        """<span class="entity-link" onclick="navigateToEntity(\n'${eid}')">""",
    ],
)
def test_the_scan_catches_each_way_of_writing_one(snippet: str) -> None:
    assert len(_interpolating(snippet)) == 1


def test_the_scan_passes_static_handlers_and_data_attributes() -> None:
    page = (
        """<button onclick="loadTraces()">Refresh</button>"""
        """<tr data-action="loadTraceDetail" data-id="${escHtml(t.trace_id)}">"""
    )
    assert len(_HANDLER.findall(page)) == 1
    assert _interpolating(page) == []


def test_every_data_action_reaches_a_function_with_its_id() -> None:
    page = INDEX_HTML.read_text(encoding="utf-8")
    block = re.search(r"const ID_ACTIONS = \{(.*?)\};", page, re.DOTALL)
    assert block is not None, "ID_ACTIONS not found in index.html"
    known = {name.strip() for name in block.group(1).split(",") if name.strip()}
    used = re.findall(r'data-action="([^"]*)"', page)

    assert_hand_read_floor(len(used), 14, subject="data-action attributes")
    # A name the listener lacks is a dead button; a key no template uses is
    # a stale entry.
    assert set(used) == known
    for name in known:
        assert re.search(rf"\bfunction {name}\(", page), name
    # The id rides next to its action, through escHtml: a quote left raw in
    # the id would end data-id.
    with_id = re.findall(r'data-action="[^"]*" data-id="\$\{escHtml\(', page)
    assert len(with_id) == len(used)


def test_the_listener_reads_the_attributes_the_templates_write() -> None:
    page = INDEX_HTML.read_text(encoding="utf-8")
    listener = re.search(
        r"const ID_ACTIONS = \{.*?\};\s*"
        r"document\.addEventListener\('click', e => \{(.*?)\n\s*\}\);",
        page,
        re.DOTALL,
    )
    assert listener is not None, "no click listener follows ID_ACTIONS in index.html"
    body = listener.group(1)
    # A key renamed on one side only leaves every button dead, and no test in
    # CI clicks one.
    assert re.findall(r"closest\('\[([\w-]+)\]'\)", body) == ["data-action"]
    assert set(re.findall(r"\.dataset\.(\w+)", body)) == {"action", "id"}
