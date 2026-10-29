"""The README's MCP tool count is the number of tools the server registers.

The count sits in the architecture diagram and again in the MCP paragraph,
and a tool added or retired changes neither on its own. Deprecated tools
stay registered (and callable), so the README counts them and so does this
test: the registrations are the source, not a second hand-kept number.
"""

from __future__ import annotations

import re
from pathlib import Path

from trellis.mcp.auth import set_auth_enforced
from trellis.mcp.server import mcp

README = Path(__file__).resolve().parents[3] / "README.md"

_WORDS = {
    10: "ten",
    11: "eleven",
    12: "twelve",
    13: "thirteen",
    14: "fourteen",
    15: "fifteen",
    16: "sixteen",
    17: "seventeen",
    18: "eighteen",
    19: "nineteen",
    20: "twenty",
}


async def _registered_tool_count() -> int:
    # ``list_tools`` is scope-filtered, so an anonymous listing is empty;
    # drop enforcement to enumerate. The suite's autouse fixture restores it.
    set_auth_enforced(enforced=False)
    try:
        return len(await mcp.list_tools())
    finally:
        set_auth_enforced(enforced=True)


async def test_the_architecture_diagram_names_the_registered_count() -> None:
    claimed = re.findall(r"MCP Server<br/>(\d+) tools", README.read_text())
    assert [int(n) for n in claimed] == [await _registered_tool_count()]


async def test_the_mcp_paragraph_names_the_registered_count() -> None:
    count = await _registered_tool_count()
    assert count in _WORDS, f"extend _WORDS to spell {count}"
    claimed = re.findall(r"\b(\w+) macro tools\.", README.read_text())
    assert [w.lower() for w in claimed] == [_WORDS[count]]
