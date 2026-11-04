"""The FastMCP instance and its middleware chain.

Middleware is registered here, in the order it runs.
"""

from __future__ import annotations

from fastmcp import FastMCP

from trellis.mcp.access_log import AccessLogMiddleware
from trellis.mcp.rejections import (
    _ExplainArgumentRejections,
    _SanitizeUncaughtToolErrors,
)
from trellis.mcp.wiring import _get_registry

# The ``instructions`` string is returned in the MCP ``initialize`` response and
# is the only guidance that travels with the connector itself — every client
# (Claude Code, claude.ai, Cowork, any other host) receives it without local
# setup. Host-side skills and hooks cannot be assumed: they exist on one machine
# and do not follow the server. So the behavioural contract lives here.
#
# It earns its tokens by carrying the three things measurement showed were
# actually going wrong on a live deployment (2026-08-15, skynet corpus), not a
# description of what the product is:
#   * retrieval was the starved half of the loop — 6 of 88 sessions ever called
#     a retrieval tool, against 1143 governed writes and 15 assembled packs;
#   * feedback arrived without item attribution, which cannot join to the pack
#     in ``trellis.learning.pack_observations`` and so drove nothing;
#   * ``domain=`` was widely believed unsafe after a since-fixed hard-exclusion
#     defect, so callers avoided a working scope.
# Keep it short — it is injected into every session on every client.
mcp = FastMCP(
    "trellis",
    instructions=(
        "Trellis — persistent memory for AI agents. Retrieval responses are concise "
        "markdown sized for LLM context windows.\n"
        "\n"
        "Use it as a loop, not a lookup table:\n"
        "\n"
        "1. BEFORE non-trivial work, call `get_context(intent=...)`. Prior "
        "traces and precedents are cheap; re-deriving them is not. Pass "
        "`session_id` to dedup across calls in one conversation. `domain=` is "
        "safe to pass — it scopes with default-pass semantics and never "
        "hard-excludes memories that carry no domain. The pack header carries "
        "its `pack_id`, and each item's id is in backticks. An empty pack is a "
        "real answer: it means greenfield, so say so; a `**Withheld:**` line, "
        "when present, counts what matched but was not served.\n"
        "\n"
        "2. AFTER meaningful work — a fix, a discovery, an instructive failure "
        "— call `save_experience`. Each step's `result` is an object, never a "
        "bare string. Put failures in the step's `error` field; a workaround "
        "the next agent would otherwise rediscover is the most reusable thing "
        "you can leave behind.\n"
        "\n"
        "3. GRADE what you were served: `record_feedback(pack_id=..., "
        "rating=0.0-1.0)` naming `helpful_item_ids` and `unhelpful_item_ids` "
        "(and `followed_advisory_ids` for advisories you acted on). Feedback "
        "without item ids cannot join to the pack and is invisible to the "
        "learning loop, so a reflexive success=true teaches nothing. An honest "
        "low `rating` is worth more than a polite high one.\n"
        "\n"
        "The common failure is retrieval that never happens, not retrieval "
        "that returns nothing."
    ),
)


mcp.add_middleware(_SanitizeUncaughtToolErrors())


mcp.add_middleware(_ExplainArgumentRejections(mcp))
# The lambda defers the lookup to call time, so a test that replaces the
# registry on ``wiring`` is seen by the access log as well.
mcp.add_middleware(AccessLogMiddleware(lambda: _get_registry()))  # noqa: PLW0108
