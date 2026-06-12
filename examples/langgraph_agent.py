"""LangGraph ReAct agent with Trellis tools + workflow hooks.

STATUS: PREVIEW — examples are in flux while parallel work lands. Expect
breaking changes before the next minor release.

The Trellis tools give the agent ways to use shared institutional memory
*from inside the loop*: get_context (before acting), search (targeted
lookup), save_trace, save_knowledge, recent_activity. This script also
demonstrates the deterministic **workflow hooks** that wrap the agent run
from the outside — the pattern a workflow engine uses to inject context
before a step and record a trace + feedback after it, without relying on
the model to remember to call a tool:

    ContextInjector  ->  agent.invoke(...)  ->  TraceRecorder + ResultFeedback

The hooks degrade gracefully: if the Trellis server is down, the agent
still runs — you just get empty context and ``None``/``ok=False``
sentinels instead of an exception.

Prerequisites:
    pip install "trellis-ai" langgraph langchain-openai
    export OPENAI_API_KEY=...
    trellis admin init
    trellis admin serve          # the SDK is HTTP-only; a server must be up
    trellis demo load            # optional, for richer retrieval

Run from the repo root:
    python examples/langgraph_agent.py

The LangGraph tool wrapper used here (``create_trellis_tools``) is a reference
template at ``examples/integrations/langgraph/tools.py`` — copy it into
your own project rather than importing from this repo. We add it to
``sys.path`` below so this script is runnable from a fresh clone.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from trellis_sdk import (
    ContextInjector,
    ResultFeedback,
    TraceRecorder,
    TrellisClient,
)

# Make the reference template importable for this demo. In your own project
# you'd just `cp examples/integrations/langgraph/tools.py <your-pkg>/`.
_TEMPLATE_DIR = Path(__file__).parent / "integrations" / "langgraph"
sys.path.insert(0, str(_TEMPLATE_DIR))

from langchain_openai import ChatOpenAI  # noqa: E402
from langgraph.prebuilt import create_react_agent  # noqa: E402

from tools import create_trellis_tools  # type: ignore[import-not-found]  # noqa: E402


# The SDK is HTTP-only — point both the in-loop tools and the outer hooks
# at the same running server.
API_URL = "http://127.0.0.1:8420"
WORKFLOW_ID = "langgraph-demo-001"
DOMAIN = "backend"


SYSTEM_PROMPT = """You are an engineering agent with access to a shared
experience graph (Trellis) for institutional memory.

Before starting non-trivial work:
- Call trellis_get_context with your task intent to find prior art.

After completing meaningful work:
- Call trellis_save_trace with a JSON trace of what you did.

When you discover important services, systems, or concepts:
- Call trellis_save_knowledge to add them to the knowledge graph."""


def main() -> None:
    # One HTTP client for the outer hooks; the tools template builds its own
    # against the same base_url.
    client = TrellisClient(base_url=API_URL)
    injector = ContextInjector(client)
    recorder = TraceRecorder(
        client, workflow_id=WORKFLOW_ID, agent_id="langgraph-agent", domain=DOMAIN
    )
    feedback = ResultFeedback(client)

    tools = create_trellis_tools(base_url=API_URL)
    model = ChatOpenAI(model="gpt-4o-mini", temperature=0)
    agent = create_react_agent(model, tools, prompt=SYSTEM_PROMPT)

    intent = (
        "Add rate limiting to the orders API. Check what we already know "
        "about rate limiting in this codebase, then summarize the "
        "recommended approach."
    )

    # -- PRE-STEP HOOK: deterministic context injection (was hand-wired) --
    context_brief = injector.for_intent(intent, domain=DOMAIN)
    user_content = intent
    if context_brief:
        user_content = f"{intent}\n\n## Prior art from the experience graph\n{context_brief}"

    started = time.monotonic()
    response = agent.invoke(
        {"messages": [{"role": "user", "content": user_content}]}
    )
    duration_ms = int((time.monotonic() - started) * 1000)

    final = response["messages"][-1].content
    print("=== Agent Response ===")
    print(final)

    # -- POST-STEP HOOKS: record the run as a trace + feedback (was hand-wired) --
    trace_id = recorder.record(
        step_name="add_rate_limiting",
        status="success",
        duration_ms=duration_ms,
        summary=str(final)[:500],
    )
    feedback.record_success(
        target_entity_id=WORKFLOW_ID,
        result_name="rate-limiting-summary",
        summary=str(final)[:500],
    )
    print(f"\n[hooks] recorded trace {trace_id}")

    client.close()


if __name__ == "__main__":
    main()
