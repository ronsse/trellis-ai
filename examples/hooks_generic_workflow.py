"""Generic pre/post workflow hooks around a fake agent task.

Shows the full Trellis workflow-integration loop with plain Python — no
workflow engine, no LLM — so the pre/post wiring is the only thing on
screen:

    inject context  ->  run the task  ->  record a trace  ->  record feedback

The three hooks (:class:`ContextInjector`, :class:`TraceRecorder`,
:class:`ResultFeedback`) all take a live :class:`TrellisClient` and degrade
gracefully: if the server is down, the "agent task" still runs — you just
get empty context and ``None``/``ok=False`` sentinels instead of crashes.

Run it (two terminals, zero env vars):

    # Terminal 1 — start the API server (defaults to 127.0.0.1:8420)
    trellis admin init
    trellis admin serve

    # Terminal 2 — run this example
    python examples/hooks_generic_workflow.py

Nothing to configure: the script points at the default
``http://127.0.0.1:8420`` and seeds the one entity it links results to.
"""

from __future__ import annotations

import time
from typing import Any

from trellis_sdk import (
    ContextInjector,
    ResultFeedback,
    TraceRecorder,
    TrellisClient,
)

API_URL = "http://127.0.0.1:8420"
WORKFLOW_ID = "demo-workflow-001"
DOMAIN = "backend"


def fake_agent_task(intent: str, context_brief: str) -> dict[str, Any]:
    """Stand-in for a real agent step.

    A real implementation would call an LLM / tool here, using
    ``context_brief`` as injected prior art. We just simulate a short unit
    of work and return a structured result the post-hooks can record.
    """
    print(f"  [task] running: {intent}")
    print(f"  [task] context brief was {len(context_brief)} chars")
    time.sleep(0.1)  # pretend to do work
    return {
        "status": "success",
        "summary": "Added a token-bucket rate limiter to the orders API.",
        "artifact": "rate_limit = 100 requests / minute / api_key",
    }


def main() -> None:
    client = TrellisClient(base_url=API_URL)

    # Seed the entity our result will be linked to. In a real pipeline this
    # already exists in the graph; we create it so the demo is self-contained.
    target_entity_id = client.create_entity(
        "orders-api", entity_type="service", properties={"name": "orders-api"}
    )
    print(f"target entity: {target_entity_id}\n")

    injector = ContextInjector(client, default_max_tokens=2000)
    recorder = TraceRecorder(
        client, workflow_id=WORKFLOW_ID, agent_id="demo-agent", domain=DOMAIN
    )
    feedback = ResultFeedback(client)

    intent = "add rate limiting to the orders API"

    # -- PRE: inject context (empty string if the graph has nothing yet) --
    print("1. injecting context...")
    context_brief = injector.for_intent(intent, domain=DOMAIN)

    # -- ACT: run the task --
    print("2. running the agent task...")
    started = time.monotonic()
    result = fake_agent_task(intent, context_brief)
    duration_ms = int((time.monotonic() - started) * 1000)

    succeeded = result["status"] == "success"

    # -- POST: record the trace (success AND failure are worth recording) --
    print("3. recording the trace...")
    trace_id = recorder.record(
        step_name="add_rate_limiting",
        status="success" if succeeded else "failure",
        duration_ms=duration_ms,
        entity_ids=[target_entity_id],
        summary=result["summary"],
        error=None if succeeded else result.get("summary"),
    )
    print(f"   trace_id: {trace_id}")

    # -- POST: record evidence + grade the supporting pack --
    print("4. recording result feedback...")
    if succeeded:
        hook_result = feedback.record_success(
            target_entity_id=target_entity_id,
            result_name="orders-api rate limit config",
            summary=result["summary"],
            full_content=result["artifact"],
        )
    else:
        hook_result = feedback.record_failure(
            target_entity_id=target_entity_id,
            error_summary=result["summary"],
            trace_id=trace_id,
        )
    print(f"   feedback ok={hook_result.ok} ids={hook_result.ids}")

    client.close()
    print("\ndone — check `trellis trace list` and the graph for the new nodes.")


if __name__ == "__main__":
    main()
