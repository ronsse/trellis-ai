# Surfaces: REST, MCP, SDK — which to use when

Trellis exposes three surfaces.  This page is the short answer to
"where do I call X?".  The full contract rationale is in
[`docs/design/adr-mcp-contract.md`](../design/adr-mcp-contract.md).

## TL;DR

- **Writing an LLM agent** → MCP.  Tools are token-budgeted and
  return markdown optimized for context windows.
- **Writing a script, CI job, or integration** → Python SDK, which
  calls REST.
- **Building a client extractor package** (Unity Catalog, dbt, etc.)
  → Python SDK's `submit_drafts` method.  See
  [Playbook 13](playbooks.md).
- **Running infra automation (operator)** → REST directly or
  `trellis admin` CLI.

## Versioning

`GET /api/version` returns both the REST API version and the MCP
tools version.  They evolve independently:

```bash
$ trellis admin version --format json | jq '{api_version, mcp_tools_version}'
{
  "api_version": "1.0",
  "mcp_tools_version": 1
}
```

- `api_major` bumps on REST breaking changes.  SDK refuses to talk
  to a different major.
- `api_minor` bumps on REST additive changes.
- `mcp_tools_version` bumps on MCP breaking changes.  Decoupled from
  `api_major` so MCP deployments don't move with REST migrations.

## Capability map

| Capability | REST | MCP | SDK |
|---|---|---|---|
| Ingest a trace | `POST /api/v1/traces` | `save_experience` | `client.ingest_trace` |
| Ingest evidence | `POST /api/v1/evidence` | `save_knowledge` | `client.ingest_evidence` |
| Save a memory / note | — | `save_memory` | — |
| Full-text search | `GET /api/v1/search` | `search` | `client.search` |
| Assemble context pack | `POST /api/v1/packs` | `get_context` | `client.assemble_pack` |
| Sectioned context pack | `POST /api/v1/packs/sectioned` | `get_sectioned_context` | `client.assemble_sectioned_pack` |
| Objective context (markdown) | — | `get_objective_context` | `client.get_objective_context` |
| Task context (markdown) | — | `get_task_context` | `client.get_task_context` |
| Get entity | `GET /api/v1/entities/{id}` | — | `client.get_entity` |
| Create entity | `POST /api/v1/entities` | — | `client.create_entity` |
| Graph subgraph | `GET /api/v1/graph/search` | `get_graph` | — |
| Advisories / lessons | `GET /api/v1/advisories` | `get_lessons` | — |
| Record feedback | `POST /api/v1/feedback` | `record_feedback` | — |
| Submit extraction drafts | `POST /api/v1/extract/drafts` | — | `client.submit_drafts` |
| Batch mutations | `POST /api/v1/commands/batch` | — | — |
| Bulk ingest | `POST /api/v1/ingest/bulk` | — | — |
| Policy CRUD | `/api/v1/policies[/...]` | — | — |
| Stats / effectiveness | `/api/v1/stats`, `/api/v1/effectiveness` | — | — |

## Why three surfaces?

Each has a different consumer and a different optimization target.

- **MCP** is optimized for LLM context windows: markdown, token
  budgets, merged session dedup, objective/task separation for
  workflow-level context.  Running in-process gives it direct access
  to stores and pack builders without an HTTP hop.
- **REST** is the full programmatic surface.  30+ routes, JSON in/out,
  suitable for any language.  OpenAPI spec at
  [`docs/api/v1.yaml`](../api/v1.yaml).
- **SDK** is a thin Python wrapper over REST.  Typed exceptions,
  version handshake, bounded-concurrency async, no dependencies on
  Trellis core.  Client packages depend on `trellis_sdk` alone.

If you're building a new capability and wondering where it should
live: if LLMs will call it interactively, MCP.  If humans /
automation will call it deterministically, REST (with an SDK wrapper
if it's high-traffic).  Both surfaces is fine when the capability is
useful to both audiences — trace ingest is a good example.

## See also

- [Playbook 8: Using the Python SDK](playbooks.md#playbook-8-using-the-python-sdk)
- [Playbook 13: Building a client extractor package](playbooks.md#playbook-13-building-a-client-extractor-package)
- [ADR: MCP as a separate contract](../design/adr-mcp-contract.md)
- [ADR: Plugin contract](../design/adr-plugin-contract.md)
