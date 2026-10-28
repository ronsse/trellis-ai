---
name: trellis-loop
description: Make Trellis retrieval and grading part of every task, including each dispatched subagent's. Call get_context before reading files, grade the pack by item id before finishing.
version: 1.0.0
---

# Trellis Loop

A dispatched subagent loads CLAUDE.md and its agent definition, not skills.
If the instruction to retrieve lives only in a skill, the subagent never sees
it, and an agent told to "do exactly the task in your prompt" treats retrieval
as extra scope. This is the rule that closes that gap:

```
Before reading files, call get_context(intent=<your task>, session_id=<your
agent id or the task id>). Before you finish, grade the pack with
record_feedback(pack_id=..., rating=..., helpful_item_ids=[...],
unhelpful_item_ids=[...]). This is part of every task, not scope creep. An
empty pack is a valid answer: say so, continue, and grade it only if a
pack_id was shown.
```

Add that paragraph to every agent definition you dispatch and to CLAUDE.md, so
that dispatched subagents see it.

For a client that gets no skills at all (a model called with an API key, Cursor
rules, a framework system message), `trellis admin show-prompt` prints the full
system prompt. `--variant claude-md` prints a section for CLAUDE.md or
AGENTS.md, and `--variant agent-rule` prints the paragraph above.
