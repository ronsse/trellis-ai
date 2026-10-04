"""A value the UI page writes into an attribute cannot end the attribute.

escHtml serialised its text through the DOM, which encodes ``&``, ``<`` and
``>`` but leaves quotes. Inside ``title="${escHtml(d.doc_id)}"`` a ``"`` in an
agent-written value ended the attribute, and the rest of the value became
attributes of its own: a document id could add a ``data-action`` to its row's
cell, and a click on the cell dispatched it. escHtml now encodes both quotes,
and every interpolated attribute value goes through it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import trellis_api
from tests.ast_rules import assert_hand_read_floor

INDEX_HTML = Path(trellis_api.__file__).parent / "static" / "index.html"

# An attribute and its value: double-quoted, single-quoted or bare.
_ATTR = re.compile(r"""\s([a-zA-Z][\w:-]*)=("[^"]*"|'[^']*'|[^\s"'>`]+)""")
_INTERPOLATION = re.compile(r"\$\{([^}]*)\}")
# The page computes these values itself (badge classes from a fixed map,
# palette colours, percentages); none is read from the API.
_COMPUTED = {"class", "style"}


def _is_one_eschtml_call(expr: str) -> bool:
    """``escHtml(...)`` and nothing after its closing parenthesis."""
    name = "escHtml"
    if not expr.startswith(name + "("):
        return False
    depth = 0
    for i, char in enumerate(expr[len(name) :], start=len(name)):
        depth += (char == "(") - (char == ")")
        if depth == 0:
            return i == len(expr) - 1
    return False


def _unescaped(page: str) -> tuple[int, list[str]]:
    """Count interpolated attributes and list those not wholly escHtml."""
    checked = 0
    hits = []
    for match in _ATTR.finditer(page):
        name, value = match.groups()
        if "${" not in value or name in _COMPUTED:
            continue
        checked += 1
        exprs = _INTERPOLATION.findall(value)
        escaped = (
            value[0] in "\"'"
            and value.count("${") == len(exprs)
            and all(_is_one_eschtml_call(e.strip()) for e in exprs)
        )
        if not escaped:
            hits.append(match.group(0).strip())
    return checked, hits


def test_eschtml_encodes_quotes_as_well_as_markup() -> None:
    page = INDEX_HTML.read_text(encoding="utf-8")
    body = re.search(r"function escHtml\(s\) \{\n(.*?)\n\s*\}\n", page, re.DOTALL)
    assert body is not None, "function escHtml not found in index.html"
    # The DOM serialiser encodes & < > in text; the replaces add the quotes
    # it leaves, so the result also holds inside a quoted attribute.
    assert "d.textContent = String(s);" in body.group(1)
    returned = re.search(r"return (d\.innerHTML.*);", body.group(1))
    assert returned is not None, "escHtml no longer returns the serialised text"
    expr = returned.group(1)
    replace = r"\.replace\(/(.)/g, '([^']*)'\)"
    assert re.fullmatch(rf"d\.innerHTML(?:{replace})*", expr), expr
    assert dict(re.findall(replace, expr)) == {'"': "&quot;", "'": "&#39;"}


def test_every_interpolated_attribute_value_is_escaped() -> None:
    page = INDEX_HTML.read_text(encoding="utf-8")
    checked, hits = _unescaped(page)
    # Counted by hand: 17 title, 13 data-id, 4 data-candidate, 3 id, 3 value
    # and 1 data-entity-id.
    assert_hand_read_floor(
        checked, 41, subject="interpolated attributes other than class and style"
    )
    listing = "\n".join(hits)
    assert not hits, f"{len(hits)} attribute values skip escHtml:\n{listing}"


@pytest.mark.parametrize(
    "snippet",
    [
        """<td title="${t.intent}">""",
        """<tr data-id="${id}">""",
        """<div id="adr-${escHtml(a)}-${b}">""",
        "<td title=${escHtml(x)}>",
        """<td title="${escHtml(a) + b}">""",
        """<div data-entity-id="${escHtml(id).replace(/"/g, '&quot;')}">""",
    ],
)
def test_the_scan_catches_each_way_of_skipping_it(snippet: str) -> None:
    checked, hits = _unescaped(snippet)
    assert (checked, len(hits)) == (1, 1)


def test_the_scan_passes_escaped_and_computed_values() -> None:
    page = (
        """<td title="${escHtml(t.intent)}">"""
        """<div id="adr-${escHtml(c.candidate_id)}">"""
        """<span class="badge ${cls}" style="width:${pct}%">"""
        """<button onclick="loadTraces()">"""
    )
    assert _unescaped(page) == (2, [])
