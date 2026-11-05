"""Collect every agent-visible rendering of the retrieve / record / grade loop.

The collector is the shared half of ``test_loop_text_surfaces.py``. It calls
only APIs that exist on ``main``: the golden next to the test was captured
from an unmodified tree (8eae6c98) before the loop text had a single source,
so a change to any surface, by one byte, fails the test.

Ids that a run mints (ULIDs) are normalised to first-appearance placeholders
so the comparison is about the text, not about which ids were drawn.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

import trellis.mcp.server as server_mod
from tests.unit.mcp.conftest import unwrap_tool
from trellis.mcp.auth import set_auth_enforced
from trellis.retrieve.formatters import (
    format_fetched_items_as_markdown,
    format_pack_as_index_markdown,
    format_pack_as_markdown,
    format_sectioned_pack_as_markdown,
)
from trellis.stores.registry import StoreRegistry
from trellis_cli.prompts import PROMPT_VARIANTS, load_prompt
from trellis_cli.skills import SKILL_NAMES
from trellis_sdk._format import format_pack_as_markdown as sdk_format_pack

REPO_ROOT = Path(__file__).resolve().parents[3]
SKILLS_DIR = REPO_ROOT / "src" / "trellis_cli" / "skills"

_ULID = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")
_PACK_ID = re.compile(r"\*\*pack_id:\*\* `([^`]+)`")

#: The tools whose descriptions an agent reads as the loop's instructions.
DESCRIBED_TOOLS = (
    "get_context",
    "search",
    "get_items",
    "get_lessons",
    "record_feedback",
)

DOC_A = "doc-loop-alpha"
DOC_B = "doc-loop-beta"
QUERY = "zephyr lantern ledger"


def _normalise(text: str, seen: dict[str, str]) -> str:
    def swap(match: re.Match[str]) -> str:
        return seen.setdefault(match.group(0), f"<ULID{len(seen) + 1}>")

    return _ULID.sub(swap, text)


def _tool_descriptions() -> dict[str, str]:
    """The description FastMCP advertises for each tool, as a client sees it."""

    async def read() -> dict[str, str]:
        # ``list_tools`` is scope-filtered, so an anonymous listing is empty;
        # drop enforcement to enumerate and put it back after.
        set_auth_enforced(enforced=False)
        try:
            listed = {tool.name: tool for tool in await server_mod.mcp.list_tools()}
        finally:
            set_auth_enforced(enforced=True)
        return {name: listed[name].description or "" for name in DESCRIBED_TOOLS}

    return asyncio.run(read())


def collect_surfaces(registry: StoreRegistry) -> dict[str, Any]:
    """Every rendered surface, keyed by name, as plain strings."""
    surfaces: dict[str, Any] = {
        "mcp_instructions": server_mod.mcp.instructions,
        "tool_descriptions": _tool_descriptions(),
        "prompts": {name: load_prompt(name) for name in PROMPT_VARIANTS},
        "skills": {
            name: (SKILLS_DIR / name / "SKILL.md").read_text(encoding="utf-8")
            for name in SKILL_NAMES
        },
    }

    items = [
        {
            "item_id": "item-one",
            "item_type": "document",
            "excerpt": "First excerpt body.",
            "relevance_score": 0.9,
            "estimated_tokens": 12,
            "metadata": {"title": "One"},
        },
        {
            "item_id": "item-two",
            "item_type": "trace",
            "excerpt": "Second excerpt body.",
            "relevance_score": 0.5,
            "estimated_tokens": 9,
            "metadata": {"title": "Two"},
        },
    ]
    sections = [{"name": "Prior work", "items": items}]
    fetched = [{"item_id": "item-one", "kind": "document", "body": "Whole body."}]
    surfaces["formatters"] = {
        "flat": format_pack_as_markdown(items, "an intent", pack_id="pk-1"),
        "flat_no_pack": format_pack_as_markdown(items, "an intent"),
        "flat_empty": format_pack_as_markdown([], "an intent", pack_id="pk-1"),
        "index": format_pack_as_index_markdown(items, "an intent", pack_id="pk-1"),
        "index_empty": format_pack_as_index_markdown([], "an intent", pack_id="pk-1"),
        "sectioned": format_sectioned_pack_as_markdown(
            sections, "an intent", pack_id="pk-1"
        ),
        "fetched": format_fetched_items_as_markdown(
            fetched, not_found=["gone"], pack_id="01ARZ3NDEKTSV4RRFFQ69G5FAV"
        )[0],
        "sdk_flat": sdk_format_pack(items, "an intent", pack_id="pk-1"),
        "sdk_flat_empty": sdk_format_pack([], "an intent", pack_id="pk-1"),
    }

    store = registry.knowledge.document_store
    store.put(DOC_A, f"{QUERY} first body about the ledger.", {"title": "Alpha"})
    store.put(DOC_B, f"{QUERY} second body about the lantern.", {"title": "Beta"})
    get_context = unwrap_tool(server_mod.get_context)
    search = unwrap_tool(server_mod.search)
    get_items = unwrap_tool(server_mod.get_items)

    flat = get_context(QUERY)
    match = _PACK_ID.search(flat)
    pack_id = match.group(1) if match else ""
    seen: dict[str, str] = {}
    surfaces["replies"] = {
        "get_context": _normalise(flat, seen),
        "get_context_index": _normalise(get_context(QUERY, index=True), seen),
        "get_context_sectioned": _normalise(
            get_context(QUERY, sections=[{"name": "Prior work"}]), seen
        ),
        "get_context_empty": _normalise(get_context("qqq nothing matches xx"), seen),
        "search": _normalise(search(QUERY), seen),
        "get_items_with_pack": _normalise(get_items([DOC_A], pack_id=pack_id), seen),
        "get_items_no_pack": _normalise(get_items([DOC_A]), seen),
    }
    return surfaces
