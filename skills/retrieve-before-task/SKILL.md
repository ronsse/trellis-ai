---
name: retrieve-before-task
description: Pull a token-budgeted context pack from Trellis (prior traces, precedents, evidence, knowledge graph) before starting non-trivial work. Use whenever the task is more than a one-line tweak — investigations, refactors, new features, debugging, design decisions.
version: 1.0.0
---

# Retrieve Before Task

Before doing real work, check what's already known. Trellis is a shared experience store — past agents and humans have left traces, precedents, and graph knowledge that you should not re-derive from scratch.

## When to invoke

Trigger this skill at the **start** of any task that is more than trivial. Concrete signals:

- The user describes work that touches more than one file.
- The task involves a service, system, or concept that might already be tracked.
- You're about to debug something — past incidents are gold.
- You're about to make a design decision — past precedents constrain the space.

Do **not** invoke for: greetings, single-character edits, "what does this code do" reads of the current buffer.

## How to invoke

Call the Trellis MCP `get_context` tool. Recommended starting parameters:

```
get_context(
  intent="<one sentence describing what you're about to do>",
  domain="<optional: backend / frontend / data-platform / infra / ...>",
  max_tokens=1500
)
```

The tool returns pre-formatted markdown — drop it into your context window and reason from it.

## What to do with the result

1. **Skim it before generating any plan.** If a precedent already covers the task, use it as the spine of your approach.
2. **Cite specific items by id** when explaining your plan to the user. ("Trace 01JRK… shows the team already added retry-with-backoff to the payments client; I'll mirror that.")
3. **Adjust your approach if you see warnings.** Advisories in the pack flag patterns that have failed before.
4. **If the pack is empty, say so explicitly.** That's a signal to the user that this is genuinely greenfield work.

## Example

User: "Add rate limiting to the orders API."

```
get_context(intent="add rate limiting to the orders API", domain="backend")
```

Then read the returned markdown, summarize the relevant prior art in 2-3 sentences, and propose a plan grounded in it. After the work is done, follow up with the **record-after-task** skill.

## Failure modes to avoid

- **Don't skip retrieval to "save time".** A 1500-token retrieval saves you from re-deriving work that already exists, and from repeating mistakes the team has already made.
- **Don't paraphrase the result back to the user verbatim.** Use it as input to your own plan.
- **Don't stop at one query.** If the first `get_context` call doesn't surface what you need, follow up with `search` or `get_lessons` for targeted lookups.
