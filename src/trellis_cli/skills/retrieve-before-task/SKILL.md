---
name: retrieve-before-task
description: Pull prior traces and precedents from Trellis before non-trivial work, again after a pivot or compaction, and in every dispatched subagent. Skip greetings and trivial edits.
version: 1.1.0
status: preview
---

# Retrieve Before Task

> **Status: preview.** Tool signatures are in flux while parallel work lands. Expect revisions before the next minor release.

Trellis holds what earlier agents learned. Ask it before re-deriving anything.

## When

- **Starting non-trivial work**: an investigation, refactor, feature, debugging
  session or design call. Skip greetings and one-line edits.
- **Starting as a subagent.** A dispatched agent about to do non-trivial work
  calls `get_context` with its own intent. The orchestrator's pack was built
  for the orchestrator's intent and is not in your context. This is the case
  most often skipped.
- **At a pivot**, when the work moves to a repo, system or domain the first
  intent did not name: call again with the new intent and the same
  `session_id`.
- **After compaction**: call again with `refresh=True`.

## The call

```
get_context(
  intent="<one sentence: what you are about to do>",
  session_id="<one string, reused for every call this session>",
  domain="<area>",   # optional, see below
)
```

- **`session_id`** turns on dedup: an item served to this session in the last
  hour is left out of later packs, so a pivot returns only what is new. Make it
  unique to this session, e.g. `<repo>-<task>-<date>`. A subagent uses its own,
  never its parent's: the parent's dedup would withhold items you never saw.
- **`refresh=True`** bypasses that dedup for one call. Pass it after
  compaction: the items are gone from your window, but the server still counts
  them as served, so without it the pack comes back thin.
- **`domain=`** is safe to pass when the task sits in one area. Scoping is
  default-pass: an item carrying no domain is never excluded, only an explicit
  mismatch is. Leave it off when the task spans areas.
- **`index=True`** returns one line per item (id, type, title, read cost)
  instead of excerpts, so the same budget surveys many more items. Fetch the
  bodies you pick with `get_items(item_ids=[...], pack_id="<that pack>")`.

## Reading the pack

1. Read it before you plan. A precedent that covers the task is the spine of
   your approach.
2. Keep the `pack_id` and the ids of the items you use (each id is in
   backticks). When the work is done, grade the pack with
   `record_feedback(pack_id=..., helpful_item_ids=[...],
   unhelpful_item_ids=[...])`; see record-after-task. A subagent grades its own
   packs before it hands back, because its parent never saw them.
3. An excerpt ending `[+N chars withheld — get_items fetches the source]` was
   cut. `get_items` with that id returns the whole body.
4. The `**Withheld:**` line counts what matched but was not served, by reason.
   `session_dedup` means this session already got those items (pass
   `refresh=True` if they are no longer in your context). `token_budget` means
   raise `max_tokens` (default 2000) or survey with `index=True`.
5. An empty pack is a real answer: greenfield. Say so, unless a Withheld line
   explains the emptiness.

## Example

User: "Add rate limiting to the orders API."

```
get_context(intent="add rate limiting to the orders API",
            session_id="orders-rate-limit-2026-04-02", domain="backend")
```

Read the pack, name the prior art you are building on by item id, and plan
from it. If the work then turns out to need a change in the auth service, call
again with that intent and the same `session_id`.

## Gotchas

- **An index survey counts as a serve.** With a `session_id`, every id an index
  listed is deduped out of later packs in that session, so a follow-up full
  retrieval that needs those items must pass `refresh=True`.
- **`get_file_context` is not useful yet.** Nothing produces the file-scoped
  memories it looks up, so it answers "No stored context for this path" for
  every file ([#549](https://github.com/ronsse/trellis-ai/issues/549)).
- `get_context` is the one entry point you need. `search`, `get_lessons`,
  `get_graph`, `get_task_context` and `get_sectioned_context` exist but are
  rarely the right first call.

Pair with **record-after-task** when the work completes.
