"""The canonical retrieve -> act -> record loop.

This is the pattern Trellis exists to enable: every non-trivial task starts
by pulling relevant prior art into context, and ends by recording what
happened so future tasks can do the same.

You can drop the `do_the_work` function with whatever your agent actually
does (LLM call, tool sequence, etc.) — the retrieve/record bookends stay
the same.

Run:
    python examples/retrieve_before_task.py
"""

from __future__ import annotations

from typing import Any

from trellis_sdk import TrellisClient
from trellis_sdk.skills import get_context_for_task


def do_the_work(intent: str, context_md: str) -> dict[str, Any]:
    """Stand-in for whatever your agent actually does.

    Replace this with your LLM call, tool loop, LangGraph invocation, etc.
    The `context_md` argument is the markdown blob you'd inject into the
    system prompt or first user turn.
    """
    print("--- Injected Context ---")
    print(context_md[:400] + ("..." if len(context_md) > 400 else ""))
    print("--- End Context ---\n")
    return {
        "status": "success",
        "summary": "Pretended to do the work successfully.",
        "steps": [
            {"step_type": "tool_call", "name": "demo_step", "result": {"ok": True}}
        ],
    }


def run(intent: str, domain: str | None = None) -> str:
    client = TrellisClient()

    # 1. RETRIEVE — pull a token-budgeted markdown summary of relevant context.
    context_md = get_context_for_task(
        client, intent, domain=domain, max_tokens=1500
    )

    # 2. ACT — run your actual workload with that context in scope.
    outcome = do_the_work(intent, context_md)

    # 3. RECORD — write a trace so this work is searchable next time.
    trace_id = client.ingest_trace(
        {
            "source": "examples.retrieve_before_task",
            "intent": intent,
            "steps": outcome["steps"],
            "outcome": {
                "status": outcome["status"],
                "summary": outcome["summary"],
            },
            "context": {"domain": domain or "general"},
        }
    )
    print(f"Recorded trace: {trace_id}")

    client.close()
    return trace_id


if __name__ == "__main__":
    run(
        intent="Add structured logging to the orders service",
        domain="backend",
    )
