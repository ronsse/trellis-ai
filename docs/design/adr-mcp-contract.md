# ADR: MCP as a Separate, Narrower Contract

**Status:** Accepted
**Date:** 2026-04-18
**Context:** Client Boundary & Extension Contracts — Phase 1, Step 6

## Context

Trellis exposes its capabilities through three surfaces:

1. **REST API** (`trellis_api`, mounted at `/api/v1/*`) — the wide,
   programmatic surface.  30 routes across admin, ingest, retrieve,
   curate, mutations, policies, extract.  Consumed by the Python
   SDK, CI scripts, custom integrations.
2. **MCP server** (`trellis.mcp`) — the narrow, agent-shaped
   surface.  11 tools consumed by LLM agents via the Model Context
   Protocol.  Returns markdown strings optimized for token budgets.
3. **Python SDK** (`trellis_sdk`) — an HTTP-only client for the
   REST API.  Zero dependency on core.

Steps 1–5 of Phase 1 dealt with REST + SDK.  Step 6 establishes
that MCP is a **separate contract** with its own versioning and
evolution cadence.

## Decision

### MCP and REST version independently

Add `mcp_tools_version: int` to the `GET /api/version` response.
Bump it when the MCP tool surface changes in a breaking way:

- A tool is removed or renamed.
- A tool's arguments or return shape change in a non-additive way.

**Not** when:

- The REST API major moves.
- A new tool is added (additive).
- A new optional argument is added (additive).
- The server's internal implementation changes.

Rationale: MCP consumers (LLM agents via Claude Desktop, Cursor,
custom Agent SDKs) have a fundamentally different release cadence
than programmatic REST consumers.  A REST API v2 migration should
not invalidate deployed MCP clients; a breaking MCP change should
not force a REST API major bump.

### MCP stays in-process

Unlike the SDK, the MCP server is **not** structurally isolated
from core.  It imports from `trellis.stores.registry`,
`trellis.retrieve.pack_builder`, `trellis.mutate.executor`, etc.
This is intentional:

- MCP runs alongside the Trellis runtime (via `trellis-mcp` entry
  point or embedded in a host application).  It's not a remote
  client.
- MCP tools need token-budgeted markdown rendering, direct store
  access for dedup, and in-process context assembly.  Funneling
  those through HTTP would add 10+ms per tool call for no benefit.
- The SDK's structural isolation (`trellis_sdk/test_isolation.py`)
  exists so client packages can depend on `trellis_sdk` alone.
  MCP has no such consumer — it's the server, not a client.

**Therefore: the wire-DTO-only rule from Steps 2/3 does NOT apply
to MCP.**  MCP tools return markdown strings (not Pydantic
models), so there's no wire contract to defend.  The "narrow
contract" is the tool *signatures*, not the internal types.

### Capabilities matrix

Each capability has a canonical surface.  Use that surface unless
there's a specific reason not to.

| Capability | REST | MCP | SDK |
|---|---|---|---|
| Ingest a trace | `POST /api/v1/traces` | `save_experience` | `client.ingest_trace` |
| Ingest evidence | `POST /api/v1/evidence` | `save_knowledge` | `client.ingest_evidence` |
| Store a memory / note | — | `save_memory` | — |
| Full-text search | `GET /api/v1/search` | `search` | `client.search` |
| Assemble context pack | `POST /api/v1/packs` | `get_context` | `client.assemble_pack` |
| Sectioned context pack | `POST /api/v1/packs/sectioned` | `get_sectioned_context` | `client.assemble_sectioned_pack` |
| Objective-level context (markdown) | — | `get_objective_context` | `client.get_objective_context` |
| Task-level context (markdown) | — | `get_task_context` | `client.get_task_context` |
| Get an entity | `GET /api/v1/entities/{id}` | — | `client.get_entity` |
| Create an entity | `POST /api/v1/entities` | — | `client.create_entity` |
| Get the graph around an entity | `GET /api/v1/graph/search` | `get_graph` | — |
| Get lessons / advisories | `GET /api/v1/advisories` | `get_lessons` | — |
| Record feedback | `POST /api/v1/feedback` | `record_feedback` | — |
| Submit extraction drafts | `POST /api/v1/extract/drafts` | — | `client.submit_drafts` |
| Batch mutations | `POST /api/v1/commands/batch` | — | — |
| Bulk ingest (entities+edges+aliases) | `POST /api/v1/ingest/bulk` | — | — |
| Policy CRUD | `POST/GET/DELETE /api/v1/policies` | — | — |
| Admin stats | `GET /api/v1/stats` | — | — |
| Admin effectiveness | `GET /api/v1/effectiveness` | — | — |

**MCP-only:** `save_memory`, `get_objective_context`,
`get_task_context`, `get_lessons`, `get_graph` (all return
markdown optimized for LLM token budgets).

**REST-only:** batch mutations, bulk ingest, policy CRUD, admin
endpoints, extraction draft submission, entity CRUD.

**Both:** trace ingest, evidence ingest, search, pack assembly,
feedback.

### Minor differences between surfaces

MCP tools always return markdown.  REST endpoints return JSON with
structured payloads.  When both surfaces expose the same capability,
they're semantically equivalent at the data level but differ at the
presentation level — that's the whole point of having two surfaces.

If a capability moves from REST-only to also-MCP (or vice versa),
that's additive and doesn't require a version bump on either side.

## Consequences

### Positive

- **Independent evolution.** MCP changes don't move the REST API
  major; REST API changes don't invalidate MCP clients.
- **Clear canonical surface per capability.** The matrix above is
  the source of truth.  Code reviews can reference it.
- **Operators can audit both versions.** `GET /api/version`
  returns both `api_version` and `mcp_tools_version`; `trellis
  admin version` prints them side-by-side.

### Negative

- **Two contracts to maintain.**  Adding a capability to both
  surfaces is two implementations.  Not ideal, but they serve
  different consumers and different presentation needs.
- **The matrix has to stay current.**  Drift between this ADR and
  reality is possible.  Mitigation: the matrix is short enough that
  a contributor adding a tool should update it as a matter of
  habit, and reviewers should check.

### Neutral

- MCP's in-process architecture is unchanged.  No wire-DTO purge,
  no import refactor.  The "narrow" in "narrow contract" refers to
  *tool signatures*, not to whether those tools use internal types.

## Alternatives considered

### A. Mass import purge of MCP server → wire DTOs only

Rejected.  MCP runs in-process, imports from core deliberately for
performance (direct store access, no HTTP round-trip), and returns
markdown (not DTOs).  Forcing wire-DTO-only imports would add an
unnecessary translation layer with no contract benefit.

### B. Share the same version number between REST and MCP

Rejected.  Surfaces have different consumers, different
presentation, different evolution cadence.  Coupling them would
mean every REST API major forces MCP clients to upgrade too, which
is exactly the fragility we're trying to avoid.

### C. MCP-over-HTTP (remove in-process coupling)

Deferred.  If a deployment needs MCP tools served from a separate
process, wrapping them in an HTTP layer is a valid path — but the
default stays in-process because most agent hosts (Claude Desktop,
Cursor) prefer stdio MCP servers and wouldn't benefit from the
extra hop.

## Versioning rules (reference)

| Change | REST minor | REST major | MCP tools | Wire schema |
|---|---|---|---|---|
| New REST route (backwards compat) | ✓ | — | — | — |
| New optional field on DTO | ✓ | — | — | ✓ |
| Remove/rename REST route | — | ✓ | — | — |
| Change DTO shape (breaking) | — | ✓ | — | ✓ |
| New MCP tool | — | — | — (additive) | — |
| New optional arg on MCP tool | — | — | — (additive) | — |
| Rename/remove MCP tool | — | — | ✓ | — |
| Change MCP tool return shape (breaking) | — | — | ✓ | — |

## References

- [src/trellis/api_version.py](../../src/trellis/api_version.py) —
  source of truth for all four version numbers.
- [src/trellis_api/routes/version.py](../../src/trellis_api/routes/version.py) —
  handshake endpoint.
- [src/trellis/mcp/server.py](../../src/trellis/mcp/server.py) —
  MCP tool definitions (11 tools as of `MCP_TOOLS_VERSION=1`).
- [docs/design/adr-plugin-contract.md](adr-plugin-contract.md) —
  runtime extension contract (Step 5).
- [TODO.md — Client Boundary Phase 1, Step 6](../../TODO.md).
