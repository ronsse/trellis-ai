---
name: link-evidence
description: Store a durable fact (API contract, config value, gotcha) or a whole reference document in Trellis, anchored to its knowledge-graph entity so later work retrieves it. Not for opinions.
version: 1.3.0
---

# Link Evidence

retrieve-before-task pulls knowledge; record-after-task captures process.
This skill captures **discrete facts** — the kind you'd otherwise jot in a
comment and lose.

## When to invoke

Mid-task, when you encounter a fact that:

- Is non-obvious from reading the code (rate limits, retry budgets, contracts).
- Will be needed again by you or another agent.
- Belongs to a specific service, system, or concept (so it can anchor to a node).

Examples:

- "Stripe's idempotency layer drops requests during the 02:00-03:00 UTC window."
- "The `orders-api` service is owned by TEAM-ORD; PRs need their +1."
- "PostgreSQL queries above 5s get killed by `statement_timeout`."

## How to invoke

One call stores the fact and anchors it:

```
save_knowledge(
  name="<the fact, as a short title>",
  content="<one or two sentences stating the fact, and where you learned it>",
  relates_to="<id or name of the entity the fact is about>",
  properties={"domain": "<area>"}
)
```

- `content` is stored as an evidence document, which is what gets embedded and
  retrieved; the new graph node carries a pointer to it. The reply names both:
  `Entity created: <id> (...)` and `Evidence document: <doc_id>`.
- `relates_to` takes a node id or a name. **Read the reply to see whether the
  link was made**: `Edge created: ...` means the fact is anchored;
  `Warning: ... — edge not created` means it is floating. A name that matches
  no node, or more than one, links to nothing; the reply lists the candidates
  when there are several, so pass one id.
- A `domain` in `properties` also links the fact to that domain's node when
  one exists. When none does, the reply says so (`Note: domain '...' has no
  domain node — not linked`) and the fact is otherwise unaffected.

### When the entity does not exist yet

Create it first, once, then store the fact with `relates_to` set to the id it
returns:

```
save_knowledge(name="<e.g. 'orders-api'>", entity_type="<service|system|concept|...>")
# Entity created: <id> (service: orders-api)
```

`save_knowledge` **always creates a new node**. It does not update an existing
entity of the same name, so calling it again "to be safe" leaves duplicate
nodes with the facts split between them. Use an id you already have from a
pack, or check with `search(query="orders-api")`, before creating.

### When the fact is already stored

If you saved the fact earlier with `save_memory` (it replies
`Memory saved: <doc_id>`), point at that document instead of repeating the
prose:

```
save_knowledge(name="<short title>", evidence_ref="<doc_id>",
               relates_to="<entity id or name>")
```

Do **not** pass a document id as `relates_to`. Edge targets must be graph
nodes and a document is not one: the entity is created, no edge is, and the
only sign is a `target entity not found` warning in the reply.

### When the thing to keep is a whole document

A report, a paper or a long analysis is a source, not a fact. Store it once as
a **reference document**, with a summary you write yourself:

```
save_memory(
  content="<the full text>",
  summary="<what it concludes, in a few short paragraphs>",
  uri="<link to the original>",
  metadata={"title": "<title>", "domain": "<area>"}
)
# Memory saved: <doc_id>
```

- Only the `summary` is embedded and shown in a pack, with the link under it.
  The full text is held and comes back from
  `get_items(item_ids=["<doc_id>"])`; pass a larger `max_tokens` for a long
  body, or the reply says `over token budget`.
- The summary is what a later search matches by meaning, so state the
  conclusions, not the table of contents. One over 8,000 characters is
  refused.
- No field records who wrote what. When you know which model or person wrote
  the document, and which wrote the summary, say so in `metadata`.
- Saving the same text again with a new summary is a duplicate: the stored
  row is kept and the new summary is dropped.

Then anchor it, so work on that subject retrieves it:

```
save_knowledge(name="<title>", evidence_ref="<doc_id>",
               relates_to="<entity id or name>")
```

Do not paste a long document into `save_knowledge(content=...)` or a
`save_memory` without a summary: only its first 8,000 characters are embedded.

## Picking entity_type

Open strings; consistency beats precision. Conventions: `service` (deployable
units) · `system` (Stripe, PostgreSQL) · `concept` (idempotency, rate-limiting)
· `team` · `runbook`. A fact stored without an `entity_type` is a `concept`.

## Example

While debugging an orders-api failure, with the `orders-api` service already
in the graph:

```
save_knowledge(
  name="orders-api read replica kills queries over 5s",
  content="orders-api uses a 5s statement_timeout on its read replica; long analytical queries must go to the primary. Found in the 2026-04 incident.",
  relates_to="orders-api"
)
# Entity created: 01JRK5P2XA... (concept: orders-api read replica kills queries over 5s)
# Evidence document: 01JRK5N7QF...
# Resolved relates_to 'orders-api' -> 01JRK4Z9TM... (service, via name-alias)
# Edge created: 01JRK5Q8WD... --[entity_related_to]--> 01JRK4Z9TM...
```

The next `get_context(intent="query the orders DB", ...)` can now surface it.

## Failure modes to avoid

- **Don't store soft opinions** — "the orders code is messy" isn't durable.
- **Don't store state that changes weekly** — that's trace material.
- **Don't skip the anchor, and don't assume it took** — a floating fact with
  no graph link is much harder to retrieve than one attached to an entity,
  and a failed link is reported only as a warning line in the reply.
