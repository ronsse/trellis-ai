---
name: record-after-task
description: After completing meaningful work, write a structured trace to Trellis with steps + outcome, then record success/failure feedback. This is what makes future runs of retrieve-before-task useful — without it, the institutional memory is empty.
version: 1.0.0
---

# Record After Task

The retrieve-before-task skill is only useful if there's something to retrieve. This skill closes the loop: once you finish meaningful work, save a structured trace so future agents (and future-you) inherit the result.

## When to invoke

Trigger at the **end** of any task that:

- Successfully fixed a bug, shipped a feature, or completed a refactor.
- Failed in an instructive way (a wrong path that's worth not repeating).
- Discovered a non-obvious gotcha or pattern worth preserving.

Do **not** invoke for: trivial edits, exploratory reads, conversational turns.

## How to invoke

Call `save_experience` with a JSON trace, then `record_feedback` with the returned id.

```
save_experience(trace_json='{
  "source": "claude-code",
  "intent": "<one sentence describing what you set out to do>",
  "steps": [
    {"step_type": "tool_call", "name": "edit_file",
     "result": {"path": "src/...", "summary": "..."}},
    {"step_type": "tool_call", "name": "run_tests",
     "result": {"passed": 12, "failed": 0}}
  ],
  "outcome": {
    "status": "success",
    "summary": "<2-3 sentences: what changed and why it works>"
  },
  "context": {"domain": "<backend/frontend/data-platform/infra/...>"}
}')

record_feedback(trace_id="<id from save_experience>", success=true, notes="<optional>")
```

## What goes in `steps`

A condensed sequence of meaningful actions, not a verbatim tool log. 3-8 entries is typical. Each step is one action with its result. Skip noise like "read file to understand" — keep the load-bearing actions.

## What goes in `outcome.summary`

The single most important field. Future retrieval will surface this to other agents. Write it for the reader who has *no context* — name the change, say what it accomplishes, mention any constraint that drove the design.

Bad: "Fixed it."
Good: "Added exponential-backoff retry with jitter to PaymentsClient.charge(). Three attempts max, 100-1600ms backoff. Required because Stripe's idempotency layer drops requests during their nightly maintenance window."

## When the task failed

Record it anyway. Use `outcome.status: "failure"` and put the lesson in `outcome.summary`:

> "Tried adding rate limiting via Redis token bucket — abandoned because the orders API runs in a serverless worker pool with no shared cache. Need to revisit once we've decided on a shared state strategy."

Then `record_feedback(success=false)`. This is just as valuable to retrieve-before-task as a success.

## Example end-to-end

After finishing the rate-limiting task from the retrieve-before-task example:

```
save_experience(trace_json='{
  "source": "claude-code",
  "intent": "Add rate limiting to the orders API",
  "steps": [
    {"step_type": "tool_call", "name": "edit_file",
     "result": {"path": "src/orders/middleware.py"}},
    {"step_type": "tool_call", "name": "edit_file",
     "result": {"path": "src/orders/config.py"}},
    {"step_type": "tool_call", "name": "run_tests",
     "result": {"passed": 24, "failed": 0}}
  ],
  "outcome": {
    "status": "success",
    "summary": "Added FastAPI middleware enforcing 100 req/min/user via slowapi. Per-user keying matches the auth-service convention used by the payments service."
  },
  "context": {"domain": "backend"}
}')

record_feedback(trace_id="01JRK...", success=true,
                notes="Pattern lifted from payments-service rate-limiter")
```

## Failure modes to avoid

- **Don't skip recording when you're tired at the end of the task.** That's exactly when the memory is freshest and most valuable.
- **Don't dump raw tool output into `steps`.** Summarize each step to one line.
- **Don't omit the domain.** Untagged traces are nearly invisible to retrieval.
- **Don't conflate success with "no error".** Use `success=false` if the task didn't actually achieve the user's intent.
