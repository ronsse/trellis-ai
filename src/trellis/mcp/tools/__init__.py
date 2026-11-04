"""The MCP tools, one module per family.

FastMCP lists tools in registration order, and clients can see it. A tool
registers when its module is imported, so the imports below are the roster
order; they are fenced off from the import sorter on purpose. Importing any
submodule runs this file first, so the order holds whichever module a caller
reaches for.
"""

from __future__ import annotations

# isort: off
from trellis.mcp.tools import context  # get_context
from trellis.mcp.tools import experience_knowledge  # save_experience, save_knowledge
from trellis.mcp.tools import memory  # save_memory
from trellis.mcp.tools import (
    lookup,
)  # get_lessons, get_graph, get_items, get_file_context
from trellis.mcp.tools import feedback  # record_feedback
from trellis.mcp.tools import (
    legacy_context,
)  # search, get_objective_context, get_task_context, get_sectioned_context
from trellis.mcp.tools import observation  # record_observation, query_observations
from trellis.mcp.tools import mutation  # execute_mutation
from trellis.mcp.tools import admin  # list_api_keys, revoke_api_key
# isort: on

__all__ = [
    "admin",
    "context",
    "experience_knowledge",
    "feedback",
    "legacy_context",
    "lookup",
    "memory",
    "mutation",
    "observation",
]
