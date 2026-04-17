---
name: link-evidence
description: When you discover a durable fact (an API contract, a config value, a service ownership, a non-obvious gotcha), store it in Trellis as a memory and connect it to the relevant entity in the knowledge graph. This makes the fact retrievable from any future task that touches the same area.
version: 1.0.0
---

# Link Evidence

retrieve-before-task pulls knowledge; record-after-task captures process. This skill captures **discrete facts** — the kind of thing you'd otherwise jot in a comment, then lose track of.

## When to invoke

Mid-task, when you encounter a fact that:

- Is non-obvious from reading the code (rate limits, retry budgets, partner contracts).
- Will be needed again later by you or another agent.
- Belongs to a specific service, system, or concept (so it can attach to a graph node).

Examples:

- "Stripe's idempotency layer drops requests during the 02:00-03:00 UTC window."
- "The `orders-api` service is owned by TEAM-ORD; PRs need their +1."
- "PostgreSQL queries above 5s get killed by `statement_timeout`."

## How to invoke

Two tool calls. First store the fact as a memory:

```
save_memory(
  content="<one or two sentences stating the fact>",
  metadata={"source": "<where you learned it>", "domain": "<area>"}
)
```

Then create or update an entity for the thing the fact is about, and link the memory to it:

```
save_knowledge(
  name="<entity name, e.g. 'orders-api'>",
  entity_type="<service|system|concept|pattern|...>",
  relates_to="<id from save_memory if linking>",
  edge_kind="entity_documented_by"
)
```

If the entity already exists, `save_knowledge` updates it rather than duplicating.

## Picking entity_type

Use whatever string fits — entity types are open at the storage layer. Common conventions:

| `entity_type` | Use for |
|---|---|
| `service` | Deployable units (orders-api, payments-worker). |
| `system` | External or infrastructural systems (Stripe, PostgreSQL, S3). |
| `concept` | Patterns or domain ideas (rate-limiting, idempotency, eventual-consistency). |
| `team` | Owning teams. |
| `runbook` | Operational procedures. |

Don't agonize over the choice — consistency within your project matters more than picking the "right" word.

## Example

While debugging an orders-api failure:

```
save_memory(
  content="orders-api uses a 5s statement_timeout on its read replica. Long analytical queries must go to the primary or hit the warehouse.",
  metadata={"source": "investigated 2026-04 incident", "domain": "backend"}
)
# returns: doc_id = 01JRK5N7QF...

save_knowledge(
  name="orders-api",
  entity_type="service",
  relates_to="01JRK5N7QF...",
  edge_kind="entity_documented_by"
)
```

Now, the next time anyone calls `get_context(intent="query orders DB", ...)`, this fact surfaces.

## Failure modes to avoid

- **Don't store soft opinions.** "I think the orders code is messy" is not durable knowledge. "orders-api enforces a hard 5s statement_timeout" is.
- **Don't store project-current state that changes weekly.** Use traces for "what happened in this sprint"; use memory + entities for facts that hold over months.
- **Don't skip the entity link.** A floating memory with no graph connection is much harder to retrieve than one anchored to a service or concept.
