"""LangGraph ReAct agent with Trellis tools.

The Trellis tools give the agent five ways to use shared institutional
memory: get_context (before acting), search (targeted lookup), save_trace
(after success), save_knowledge (when discovering entities), and
recent_activity (to understand what's been happening).

Prerequisites:
    pip install "trellis-ai" langgraph langchain-openai
    export OPENAI_API_KEY=...
    trellis admin init
    trellis demo load   # optional but recommended for richer retrieval

Run from the repo root:
    python examples/langgraph_agent.py

The LangGraph tool wrapper used here (``create_xpg_tools``) is a reference
template at ``examples/integrations/langgraph/tools.py`` — copy it into
your own project rather than importing from this repo. We add it to
``sys.path`` below so this script is runnable from a fresh clone.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make the reference template importable for this demo. In your own project
# you'd just `cp examples/integrations/langgraph/tools.py <your-pkg>/`.
_TEMPLATE_DIR = Path(__file__).parent / "integrations" / "langgraph"
sys.path.insert(0, str(_TEMPLATE_DIR))

from langchain_openai import ChatOpenAI  # noqa: E402
from langgraph.prebuilt import create_react_agent  # noqa: E402

from tools import create_xpg_tools  # type: ignore[import-not-found]  # noqa: E402


SYSTEM_PROMPT = """You are an engineering agent with access to a shared
experience graph (Trellis) for institutional memory.

Before starting non-trivial work:
- Call xpg_get_context with your task intent to find prior art.

After completing meaningful work:
- Call xpg_save_trace with a JSON trace of what you did.

When you discover important services, systems, or concepts:
- Call xpg_save_knowledge to add them to the knowledge graph.

Default to local mode — every tool call is fast and cheap."""


def main() -> None:
    # Local mode — no Trellis API server needed.
    tools = create_xpg_tools()

    model = ChatOpenAI(model="gpt-4o-mini", temperature=0)
    agent = create_react_agent(model, tools, prompt=SYSTEM_PROMPT)

    response = agent.invoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "I'm about to add rate limiting to the orders API. "
                        "Check what we already know about rate limiting in "
                        "this codebase, then summarize the recommended "
                        "approach."
                    ),
                }
            ]
        }
    )

    final = response["messages"][-1].content
    print("=== Agent Response ===")
    print(final)


if __name__ == "__main__":
    main()
