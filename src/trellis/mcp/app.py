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
from trellis_wire.loop_text import mcp_instructions

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
#
# The text lives in ``trellis_wire.loop_text``, beside every other statement of
# the loop, so the surfaces are edited in one file.

#: Tag on a tool whose reply can carry a ``**pack_id:**`` line, so a client can
#: grade what the call served. The packaged Claude Code hooks
#: (``trellis_cli.hooks.PACK_TOOLS``) keep a literal copy of these names, to
#: start without importing this server; ``tests/unit/mcp/test_hook_text_claims.py``
#: compares the copy to the tools carrying this tag, so a tagged tool the hooks
#: do not know fails it. Tag every tool whose reply can print a pack id.
PACK_TOOL_TAG = "returns-pack"

mcp = FastMCP(
    "trellis",
    instructions=mcp_instructions(),
)


mcp.add_middleware(_SanitizeUncaughtToolErrors())


mcp.add_middleware(_ExplainArgumentRejections(mcp))
# The lambda defers the lookup to call time, so a test that replaces the
# registry on ``wiring`` is seen by the access log as well.
mcp.add_middleware(AccessLogMiddleware(lambda: _get_registry()))  # noqa: PLW0108
