# ADR: Opt-in MCP-over-HTTP transport with scoped API-key auth

**Status:** Proposed
**Date:** 2026-07-10
**Deciders:** Trellis core
**Amends:**
- [`./adr-mcp-contract.md`](./adr-mcp-contract.md) — resolves Alternative **C, "MCP-over-HTTP" (Deferred)** by accepting it as an *opt-in* transport. The "MCP stays in-process" decision is **upheld**, not reversed.
- [`./adr-rest-api-security-model.md`](./adr-rest-api-security-model.md) — narrows the "MCP surface auth" non-goal, whose premise ("consumed over stdio … not over the network") this ADR invalidates for the http case.

**Related:**
- [`./adr-planes-and-substrates.md`](./adr-planes-and-substrates.md) — the Operational-plane `ApiKeyStore` this transport authenticates against.

---

## 1. Context

The MCP server is a stdio process. An agent host spawns `trellis-mcp` as a
child and speaks JSON-RPC over its stdin/stdout, so the server necessarily
runs on the same machine as the agent. A second machine cannot reach a
Trellis instance at all: there is nothing to point at.

That is a real limitation as soon as more than one workstation shares a
memory. The store already supports it — Postgres, a graph substrate, an
event log, all of it network-reachable — but the *agent-facing* surface
does not.

`adr-mcp-contract.md` anticipated this and deferred rather than rejected it:

> **C. MCP-over-HTTP (remove in-process coupling)** — Deferred. If a
> deployment needs MCP tools served from a separate process, wrapping them
> in an HTTP layer is a valid path — but the default stays in-process
> because most agent hosts (Claude Desktop, Cursor) prefer stdio MCP
> servers and wouldn't benefit from the extra hop.

Two things about that framing are worth being precise on, because they
determine what this ADR may and may not change:

1. The deferral was about *transport*, not about *store coupling*. The
   reason MCP imports `StoreRegistry` directly — "funnelling those through
   HTTP would add 10+ms per tool call for no benefit" — is untouched by
   serving the tools over a socket. The HTTP server still assembles packs
   in-process against a local registry. Only the client↔server hop moves.
2. `adr-rest-api-security-model.md` declares MCP auth a non-goal, and
   states its reason explicitly: MCP "is a separate, in-process contract
   consumed over stdio by agent hosts, not over the network." Adding a
   network transport removes that reason. A remote MCP surface with no
   credential model would be a wide, unauthenticated mutation API — worse
   than the REST gap that ADR was written to close.

So the transport is cheap and the auth model is the actual work.

## 2. Decision

### 2.1 Transport is opt-in; stdio is unchanged

`trellis-mcp` reads `TRELLIS_MCP_TRANSPORT` (`stdio` | `http`, default
`stdio`). With the variable unset the process takes exactly the path it
took before: `configure_stderr_logging()`, signal handlers, `mcp.run()`.

| Env var | Values | Default | Notes |
|---|---|---|---|
| `TRELLIS_MCP_TRANSPORT` | `stdio` \| `http` | `stdio` | Invalid ⇒ `ConfigError` |
| `TRELLIS_MCP_HOST` | host / IP | `127.0.0.1` | Mirrors the REST `DEFAULT_HOST` |
| `TRELLIS_MCP_PORT` | 1..65535 | `8421` | |
| `TRELLIS_MCP_PATH` | path | `/mcp` | FastMCP's own default |
| `TRELLIS_MCP_AUTH_MODE` | `off` \| `required` | `required` | Read only under `http` |
| `TRELLIS_MCP_ALLOW_INSECURE_BIND` | bool | unset | Escape hatch; fails closed on any unrecognised value |

Every resolver raises `ConfigError` on a bad value rather than guessing,
following `resolve_auth_mode()` in `trellis_api/auth.py`.

**Fail-closed bind.** `http` + `auth_mode=off` + a non-loopback host
refuses to boot unless `TRELLIS_MCP_ALLOW_INSECURE_BIND` is set. This is
Layer A of the REST security model applied to the new listener.

### 2.2 A separate auth-mode switch, with no `optional`

MCP gets `TRELLIS_MCP_AUTH_MODE`, not the REST `TRELLIS_AUTH_MODE`. The
REST resolver infers `required` from the presence of the legacy
`TRELLIS_API_KEY` shared secret; sharing the variable would mean that
turning on REST auth silently flips the MCP surface's posture, and vice
versa. The two surfaces share the *scope vocabulary* and the *same*
`registry.operational.api_key_store` — they do not share a switch.

`optional` is not implemented. It exists over REST as a migration mode for
an installed base of un-credentialed callers. MCP-over-HTTP is new and has
no such population, so `optional` would be a permissive branch with no
constituency.

### 2.3 Credentials: reuse `trellis.auth` wholesale

`trellis.auth` is already transport-agnostic — pure functions over an
`ApiKeyStore`, no FastAPI coupling. `TrellisApiKeyVerifier` is a FastMCP
`TokenVerifier` that calls `verify_token(token, store)` on a worker thread
(the store is sync and does database I/O; the event loop is serving other
tool calls) and maps `ApiKeyRecord.scopes` onto `AccessToken.scopes`.

Every failure mode — malformed, unknown, revoked, secret mismatch, store
outage — returns `None`, which FastMCP answers as a bare 401. A caller
cannot probe which key ids exist. `trellis.auth.verify_token` logs the real
category server-side.

**The verifier never logs the credential.** `logger.exception` would render
the frame's locals, which include the presented bearer token, and
`str(exc)` on a psycopg error can carry a DSN with its password. The
outage path logs `error_type` and nothing else. This mirrors the discipline
in `trellis.auth.verify_token`, which logs a failure category and never the
token.

Keys are minted with the existing CLI:

```
trellis admin api-keys create --name omen --scopes read,ingest,mutate
```

### 2.4 stdio needs no bypass, by construction

FastMCP's `_get_auth_context()` returns `skip_auth=True` whenever the
active transport is stdio. Per-tool `auth=` checks are therefore inert
there. Tools are decorated unconditionally at import time; there is no
transport-conditional registration and no bypass branch to keep correct.
`mcp.auth` is attached only in the http branch of `main()`.

### 2.5 Per-tool scopes, declarative, `admin` still implies everything

FastMCP ships `require_scopes`, and it is the wrong tool: it does flat
subset matching, so an `admin`-only key would be **denied** on a `read`
tool. `trellis_scope(required)` is a five-line `AuthCheck` that delegates
to `trellis.auth.scopes_satisfy`, keeping one source of truth for scope
semantics, and raises `ValueError` at import time on an unknown scope.

| Scope | Tools |
|---|---|
| `read` | `get_context`, `search`, `get_graph`, `get_lessons`, `query_observations`, `get_objective_context`, `get_task_context`, `get_sectioned_context` |
| `ingest` | `save_experience`, `save_knowledge`, `save_memory` |
| `mutate` | `record_observation`, `record_feedback`, `execute_mutation` |

Cross-checked against the REST router wiring in `trellis_api/app.py`.
FastMCP filters `tools/list` on the same checks that gate calls, so a
`read`-scoped key does not merely fail to call `execute_mutation` — it
never sees it.

`record_feedback` maps to `mutate` for parity with the `curate` router that
hosts its REST equivalent. A case exists for relaxing it: a read-only
retrieval agent reporting whether a pack helped is the core learning-loop
interaction, and `mutate` is a heavy bar. Deferred until a read-only
consumer actually exists.

### 2.6 Concurrency

Under stdio there is one process per session and nothing is contended.
Under http one process serves many sessions, and FastMCP runs sync tools in
`anyio` worker threads — so thread concurrency is real regardless of
`stateless_http`.

The substrates are already hardened for this; the REST API's thread pool
got there first. Each Postgres store owns a `psycopg_pool.ConnectionPool`
("so concurrent request handlers … don't serialise"); the SQLite base uses
per-thread connections behind a lock-guarded registry.

Two exposures remain, both in code that stdio never stressed:

1. **`MinHashIndex` had no lock.** `stats()` iterates a band dict at the
   Python level while `remove()` deletes from it — `RuntimeError:
   dictionary changed size during iteration`, surfacing as an
   `INTERNAL_ERROR` mid-tool-call. Reproduces on every run. Fixed with an
   internal `RLock`. The check-then-act in `add()` and the set union in
   `query()` are racy by construction but currently serialised by the GIL;
   they are covered by the same lock rather than left to an implementation
   accident that a free-threaded interpreter removes.
2. **Lock-free lazy singletons** — `StoreRegistry._get()`,
   `_get_registry()`, `_get_minhash_index()`, and the `_UNSET` caches for
   `embedding_fn` / `budget_config` are all check-then-act. Two threads
   racing first access each build a store; the loser leaks its pool and
   re-runs `_init_schema`. Mitigated by pre-warming every one of them on
   the main thread in `main()` before uvicorn accepts a request.

`stateless_http=True`: the tools hold no per-session server state
(`session_id` is a dedup key written to the event log, not an in-memory
object), so there is nothing to affinitise. Note this concerns MCP protocol
sessions, **not** threads — it does not mitigate either item above.

### 2.7 Shutdown: each transport installs its own no-op handlers

The obvious move — "uvicorn owns signals under http, so install nothing" —
is wrong, and measurably so. `uvicorn.Server.capture_signals` swaps in its
own SIGINT/SIGTERM handlers for the lifetime of `serve()`, restores the
previous ones, and then calls `signal.raise_signal(...)` once per signal it
caught, so the process exits the way the operator asked. If the restored
handler is Python's default, that second delivery kills the process
*after* uvicorn drains connections but *before* `main()`'s `finally` runs —
so the Postgres pool and Neo4j driver leak on every restart. Observed:
`exit=143`, no `mcp_server_shutting_down`, no `store_closed`.

Both transports therefore install no-op handlers before `mcp.run()`. Under
http they are live only before uvicorn installs its own and after it
restores them, so they never suppress the shutdown itself; they exist to
catch uvicorn's re-raise so `serve()` returns normally into the `finally`.
Verified: SIGTERM and SIGINT both yield `exit=0`, a graceful uvicorn
shutdown, and eight closed stores.

The stdio handler keeps its existing shape (SIGINT re-raises
`KeyboardInterrupt` so `mcp.run()` unwinds; SIGTERM is swallowed because
the parent follows with stdin close). The http handler swallows both,
because uvicorn has already completed the shutdown by the time it fires and
a `KeyboardInterrupt` there would surface as a traceback on a clean stop.

## 3. Consequences

**Positive.** A Trellis instance can back agents on any machine that can
reach it. The credential model is the one the REST surface already uses —
one `trellis admin api-keys` CLI, one store, one set of scopes, revocation
that covers both surfaces. Scope filtering of `tools/list` means a
read-only agent's context window is not spent on tools it cannot call.

**Negative.** MCP is now a network listener, which it was not before. The
default host stays loopback and `auth_mode` defaults to `required`, but the
attack surface exists where it did not. Operators binding off-loopback must
now think about it — which is the point of the fail-closed bind check.

**Neutral.** No `mcp_tools_version` bump: adding a transport is not a
tool-surface change. No dependency changes; FastMCP and uvicorn are already
required.

**Sharp edge.** FastMCP's in-memory transport is neither stdio nor
authenticated, so an in-process host embedding the server (`Client(mcp)`)
gets an empty tool list until it calls `set_auth_enforced(enforced=False)`.
Fail-closed is the correct default for a security control, but an empty
tool list is a silent symptom, so the first anonymous denial logs an
explanation naming the fix.

## 4. Alternatives considered

**A. Proxy MCP through the REST API.** The MCP server would become a
`trellis_sdk` client. Rejected: it breaks the "MCP stays in-process"
decision that `adr-mcp-contract.md` makes on performance grounds, adds an
HTTP round-trip to every store touch inside every tool call, and couples
MCP's evolution to the REST API's — exactly the fragility that ADR exists
to prevent.

**B. Reuse `TRELLIS_AUTH_MODE`.** Rejected: cross-surface coupling, and its
backwards-compat inference from `TRELLIS_API_KEY` would make one surface's
configuration silently change the other's posture.

**C. OAuth / OIDC via `RemoteAuthProvider`.** Rejected for now. Claude Code
sends a static bearer token and, when a static `Authorization` header is
configured, treats a 401 as a failed connection rather than falling back to
OAuth discovery. A static bearer is what the client actually speaks, and
the `ApiKeyStore` already implements issuance and revocation. Nothing here
forecloses adding an OAuth provider later.

**D. Support `off` | `optional` | `required`.** Rejected — see §2.2.

**E. Manual scope checks inside each tool body.** Rejected in favour of the
declarative `auth=` parameter, which FastMCP also uses to filter
`tools/list`. Hand-rolled checks would gate calls but still advertise tools
the caller cannot invoke.

**F. Leave the check-then-act races alone.** Tempting, since only `stats()`
raises under CPython today. Rejected: correctness that depends on the GIL
is not correctness, and the lock is uncontended in practice —
`save_memory` is not a hot path.

**G. Install no signal handlers under http and let uvicorn own them.**
Tried, and it silently skipped registry cleanup — see §2.7. Recorded here
because it is the intuitive design and it is wrong.
