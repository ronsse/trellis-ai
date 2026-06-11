# ADR: REST API and credential security model

**Status:** Proposed
**Date:** 2026-06-03
**Deciders:** Trellis core
**Resolves:** [#189](https://github.com/) (fail-closed bind), [#190](https://github.com/) (static UI auth/disable), [#191](https://github.com/) (scoped credentials), [#192](https://github.com/) (readiness/metrics exposure), [#193](https://github.com/) (ArcadeDB credential split), [#194](https://github.com/) (DataClassification enforcement), [#206](https://github.com/) (error-payload sanitization), [#207](https://github.com/) (SQL statement-type allowlist)
**Related:**
- [`./adr-mcp-contract.md`](./adr-mcp-contract.md) — MCP is a separate, narrower contract; this ADR governs the wide REST surface only.
- [`./adr-planes-and-substrates.md`](./adr-planes-and-substrates.md) — Knowledge vs Operational plane split and the per-plane DSN env vars this ADR builds credential separation on.
- [`./adr-enterprise-ontology-capability-framing.md`](./adr-enterprise-ontology-capability-framing.md) — #217 umbrella; the data-classification governance story (Layer D) is a capability that framing depends on.
- [`./adr-terminology.md`](./adr-terminology.md) — canonical meanings of `DataClassification` (access policy), `ContentTags` (retrieval), `Lifecycle`, Substrate vs Backend, Knowledge vs Operational Plane.

---

## 1. Context

Trellis exposes a wide programmatic surface through `trellis_api` (the REST
API, mounted at `/api/v1/*`; see [`adr-mcp-contract.md`](./adr-mcp-contract.md)
for the surface inventory). Today that surface ships with a deliberately
minimal security posture, captured in the docstring of
[`src/trellis_api/auth.py`](../../src/trellis_api/auth.py): a single shared
secret (`X-API-Key` matched against `TRELLIS_API_KEY`) that is a **no-op when
the env var is unset**. This was the cheapest auth that closed the
"unauthenticated REST on a corporate network" gap for a single operator, and
it keeps loopback dev frictionless.

Eight open issues identify where that posture is now too thin for any
deployment beyond a developer's loopback:

| # | Gap (verified in code) |
|---|---|
| 189 | [`app.py`](../../src/trellis_api/app.py) reads `TRELLIS_API_HOST` (default `127.0.0.1`). A container that sets `TRELLIS_API_HOST=0.0.0.0` with `TRELLIS_API_KEY` unset binds the **unauthenticated** API to a routable interface. [`warn_if_unauthenticated()`](../../src/trellis_api/auth.py) only *logs* — it does not refuse. |
| 190 | The static UI is mounted unauthenticated at `/ui` and fetches `/api/v1` routes that *are* gated. Turning on `TRELLIS_API_KEY` breaks the UI's `fetch` calls (no header), which pressures operators to leave auth off. |
| 191 | `require_api_key` grants **all-or-nothing** access. A read-only caller and an admin caller present the same secret and receive the same capability across every `/api/v1` router. |
| 192 | [`/readyz`](../../src/trellis_api/routes/health.py) returns a per-backend breakdown including `str(exc)` from probe failures; [`/metrics`](../../src/trellis_api/observability.py) is mounted **unauthenticated** by design ("orchestrator scrape jobs need it open"). Both can leak internal topology to untrusted networks. |
| 193 | [`registry.py`](../../src/trellis/stores/registry.py) resolves the ArcadeDB store from `TRELLIS_ARCADEDB_USER`/`_PASSWORD`, defaulting `user` to `"root"`, and calls `ensure_database` (DDL) on first driver build. Long-running runtime paths therefore hold **admin** credentials that are only needed for init/migration. |
| 194 | [`DataClassification`](../../src/trellis/schemas/classification.py) exists but, per its own docstring, "no classifier populates it and no policy gate enforces it yet." The [`DefaultPolicyGate`](../../src/trellis/mutate/policy_gate.py) matches on scope/operation, never on `sensitivity`. PackBuilder applies no classification filter. |
| 206 | The structured-error path is partly in place — [`middleware.py`](../../src/trellis_api/middleware.py) already strips internals from the 500 envelope — but route-level JSON error payloads for commands that touch external metadata systems still risk echoing raw exception text carrying SQL, emails, or token-shaped values. |
| 207 | A SQL statement-type classifier (on an unmerged consumer-integration branch) transformed an unrecognized first token straight into a `statement_type` value with no allowlist, so an email-like or display-name-like token could leak into curator-safe artifacts. |

These are not eight unrelated bugs. They are five **layers** of one security
model that was never drawn as a whole: where the API may listen, who may call
which routes, which credentials the runtime holds, what classified data may
cross the retrieval/mutation boundary, and what leaves the process in error
and artifact payloads.

### Constraint: loopback dev must stay frictionless

The blessed local story (`trellis admin init`, SQLite substrates, no env
vars) must keep working with **zero security configuration**. The default
bind is already loopback ([`app.py`](../../src/trellis_api/app.py):
`DEFAULT_HOST = "127.0.0.1"`). Everything in this ADR is designed so that a
loopback-only deployment sees no new friction; the fail-closed behavior
triggers only when a deployment makes a posture-changing choice (binds
off-loopback, enables a leaky endpoint, etc.).

---

## 2. Threat model (brief)

In scope:

- **Untrusted network reach.** The API binds a routable interface (the #189
  Docker `0.0.0.0` case) on a corporate or cloud network where unauthenticated
  callers can reach it.
- **Over-privileged caller.** A credential issued for read or ingest is reused
  (or stolen) to perform admin/mutate operations (#191).
- **Over-privileged runtime process.** A compromised application process holds
  ArcadeDB `root` and can drop/alter schema, not just read/write rows (#193).
- **Classified-data exfiltration via retrieval.** A low-trust caller assembles
  a pack (or reads the graph) that surfaces `confidential`/`restricted`
  content it should not see (#194).
- **Information leak via side channels.** Internal topology, SQL shapes,
  emails, or secrets leak through `/readyz`, `/metrics`, error payloads, or
  curator-safe artifacts (#192, #206, #207).

Out of scope (explicit non-goals, §6): per-tenant identity, OAuth/OIDC/JWT,
mTLS, encryption-at-rest, and rate limiting. This ADR hardens the *existing*
shared-secret model into a scoped, fail-closed one; richer identity is a
follow-up that can layer on the scope abstraction defined here.

---

## 3. Decision — five layers

The model is five layers, applied outside-in. Each layer states its **default
posture** (what an unconfigured loopback dev install sees) and its
**fail-closed trigger** (the condition under which startup refuses or a
request is denied).

### Layer A — Network / bind posture (#189)

**Decision.** Startup computes an *effective auth-required* boolean and
refuses to boot when the API would bind a **non-loopback** interface **without
authentication configured**.

- Loopback bind (`127.0.0.1`, `::1`, `localhost`) → no auth required; dev
  stays frictionless. This is the current default and does not change.
- Non-loopback bind (`0.0.0.0`, a pod IP, any routable address) **with**
  `TRELLIS_API_KEY` set → boots normally.
- Non-loopback bind **without** `TRELLIS_API_KEY` → **refuse to start** with a
  message naming the two escape routes: set `TRELLIS_API_KEY`, or set the
  explicit dev override.
- Explicit override `TRELLIS_API_ALLOW_INSECURE_BIND=1` (clearly named, opt-in,
  documented as "local/test only") permits unauthenticated non-loopback bind
  for the scenarios the loopback default can't serve (e.g. a CI container that
  must be reached from the test runner). The override is loud: it logs a
  `WARNING` on every boot.

The bind check belongs at process startup, alongside the existing
`StoreRegistry.validate()` call in the FastAPI `lifespan`
([`app.py`](../../src/trellis_api/app.py)) and the `trellis serve` CLI
entrypoint, so the refusal happens before uvicorn accepts its first request —
mirroring the eager-validation discipline already used for store
misconfiguration.

`warn_if_unauthenticated()` is **upgraded from warn to fail** for the
non-loopback case; it stays a warning for the loopback case (where running
without a key is legitimate).

### Layer B — Authn / authz scopes (#191, #190)

**Decision.** Replace the single all-or-nothing secret with **scoped
credentials** over four ordered scopes, enforced via FastAPI `Depends()`.

**Scope set** (least → most privileged; each implies the ones below it):

| Scope | Grants | Example routers |
|---|---|---|
| `read` | Retrieval, search, pack assembly, graph/entity reads | `retrieve`, `observations` (GET) |
| `ingest` | `read` + append-only writes through the governed pipeline | `ingest`, `extract` |
| `mutate` | `ingest` + entity/edge mutation, batch commands, curation | `mutations`, `curate` |
| `admin` | `mutate` + policy CRUD, stats, effectiveness, detailed readiness | `admin`, `policies`, detailed `/readyz` (Layer E) |

The scope ladder is **inclusive** (an `admin` credential can read), matching
the route grouping already present in
[`app.py`](../../src/trellis_api/app.py) where each router is included with a
single `dependencies=[Depends(require_api_key)]`. That single dependency
becomes a scope-parameterized factory:

```python
# conceptual — not yet implemented
auth_read   = Depends(require_scope("read"))
auth_ingest = Depends(require_scope("ingest"))
auth_mutate = Depends(require_scope("mutate"))
auth_admin  = Depends(require_scope("admin"))

app.include_router(retrieve.router,  prefix="/api/v1", dependencies=[auth_read])
app.include_router(ingest.router,    prefix="/api/v1", dependencies=[auth_ingest])
app.include_router(mutations.router, prefix="/api/v1", dependencies=[auth_mutate])
app.include_router(admin.router,     prefix="/api/v1", dependencies=[auth_admin])
```

`require_scope(scope)` resolves the presented `X-API-Key` to its granted
scopes, then admits the request iff the granted set covers the required scope.
Resolution is config-driven: a `keys:` map (key value or hash → granted
scope) loaded at startup. **Backward-compat:** a bare `TRELLIS_API_KEY` with
no scope map is treated as a single credential granted **all four scopes** —
identical to today's behavior — so existing single-key deployments are
unaffected.

**Static UI (#190).** The UI mount stays at `/ui`, but the all-or-nothing
breakage is resolved two ways, both opt-in for the operator:

1. **Authenticated UI flow.** The UI reads a session-scoped API key
   (operator-provisioned, `read`-scope by default) and sends it as `X-API-Key`
   on every `fetch` to `/api/v1`. The UI never embeds a privileged key.
2. **Disable in production.** `TRELLIS_API_DISABLE_UI=1` skips the
   `StaticFiles` mount in [`app.py`](../../src/trellis_api/app.py) entirely, so
   hardened deployments expose no UI surface at all.

Default posture: UI mounted (loopback dev). Production guidance: either wire
the authenticated flow or disable the mount.

### Layer C — Credential separation (#193)

**Decision.** Split ArcadeDB credentials into an **admin/migration** identity
and a **runtime** identity, resolved from distinct env vars, used by distinct
code paths.

| Purpose | Env vars | Used by | Privilege |
|---|---|---|---|
| Init / migration | `TRELLIS_ARCADEDB_ADMIN_USER`, `TRELLIS_ARCADEDB_ADMIN_PASSWORD` | `ensure_database` / DDL path in [`registry.py`](../../src/trellis/stores/registry.py) and the migration CLI | Database create + schema DDL (`root`-class) |
| Runtime | `TRELLIS_ARCADEDB_USER`, `TRELLIS_ARCADEDB_PASSWORD` | Steady-state graph/vector read+write through the resolved store | Row-level CRUD on the `trellis` database only |

Today both flow through the same `TRELLIS_ARCADEDB_USER`/`_PASSWORD`
resolution (defaulting `user` to `"root"`) and the same first-use
`ensure_database` call. The decision:

- The DDL/`ensure_database` step runs only when admin credentials are present
  **and** an explicit init/migration phase is requested (a `trellis admin
  migrate` step or `ensure_database_exists=True` during provisioning) — not on
  every cold start of a long-running runtime process.
- Runtime resolution prefers the runtime env vars and **must not** silently
  fall back to `root`. Absent runtime credentials is a config error, not a
  default to admin.
- The same split generalizes to other admin-capable substrates (Postgres
  roles behind `TRELLIS_KNOWLEDGE_PG_DSN` / `TRELLIS_OPERATIONAL_PG_DSN` from
  [`adr-planes-and-substrates.md`](./adr-planes-and-substrates.md)): documentation
  describes provisioning a least-privilege runtime role separate from the
  migration role. Deployment order: **provision (admin) → migrate (admin) →
  run (runtime)**.

**Backward-compat:** if the admin vars are unset, the runtime vars are used
for both phases (today's behavior) with a deprecation log line, so existing
single-credential ArcadeDB deployments keep working for one release.

### Layer D — Data-classification enforcement (#194)

**Decision.** Make [`DataClassification`](../../src/trellis/schemas/classification.py)
load-bearing on **both** the mutation and retrieval paths, **failing closed on
unclassified content** once enforcement is enabled.

- **Caller scope.** Each request carries an effective *clearance* derived from
  its credential's scope (Layer B) — minimally a `max_sensitivity` ceiling
  (`public` < `internal` < `confidential` < `restricted`, the existing
  `Sensitivity` literal order). How clearance maps to credentials is config;
  the enforcement points are fixed.
- **Mutation path.** A classification gate is added to the
  [`MutationExecutor`](../../src/trellis/mutate/) policy-check stage, alongside
  the existing [`DefaultPolicyGate`](../../src/trellis/mutate/policy_gate.py).
  It denies a write that sets or changes `DataClassification` above the
  caller's clearance, and (when enforcement is on) denies a write that targets
  classified content the caller may not modify. This reuses the gate
  Protocol — it is an additional gate, not a rewrite of `check()`.
- **Retrieval path.** [`PackBuilder`](../../src/trellis/retrieve/pack_builder.py)
  and the graph/entity read routes ([`retrieve.py`](../../src/trellis_api/routes/retrieve.py))
  filter out items whose `sensitivity` exceeds the caller's clearance, *before*
  similarity scoring and budget enforcement — the same pre-filter stage that
  already drops `signal_quality="noise"` items.
- **Fail closed on unclassified.** When classification enforcement is enabled,
  an item carrying **no** `DataClassification` is treated as its configured
  default ceiling (recommended: `internal`), not as `public`. Unclassified
  content is therefore withheld from sub-`internal` callers rather than leaking
  by omission. The `DataClassification` default of `sensitivity="internal"`
  already encodes this conservative default at the schema level.

Enforcement is **opt-in per deployment** (`TRELLIS_ENFORCE_CLASSIFICATION=1`)
for one release so existing data (which carries no populated classification)
is not retroactively hidden; the *fail-closed-on-unclassified* rule applies
only once enforcement is turned on. This is the only layer whose default
posture is "off," because turning it on against unclassified historical data
would otherwise hide everything.

### Layer E — Output sanitization (#192, #206, #207)

**Decision.** Nothing internal leaves the process in error payloads, probe
output, or curator-safe artifacts unless an `admin`-scoped (or internal-only)
caller asked for it.

- **Readiness/metrics exposure (#192).**
  - `/healthz` stays a minimal, unauthenticated liveness probe (it never
    touches stores — unchanged).
  - `/readyz` keeps a minimal unauthenticated form (`{"status": "ready"|"degraded"}`)
    by default; the **per-backend breakdown with `str(exc)`** currently in
    [`health.py`](../../src/trellis_api/routes/health.py) becomes gated behind
    `admin` scope (or `TRELLIS_API_EXPOSE_READYZ_DETAIL=1` for internal-only
    networks). Untrusted callers learn "degraded," not which backend or why.
  - `/metrics` (the Prometheus mount in
    [`observability.py`](../../src/trellis_api/observability.py)) becomes
    gateable: `TRELLIS_API_METRICS_REQUIRE_AUTH=1` puts it behind `admin`
    scope; the default-open behavior is preserved only when explicitly chosen,
    so the "orchestrator scrape jobs need it open" path is a deliberate opt-in,
    not a silent default on a public bind.
- **Error-payload sanitization (#206).** The middleware-level 500 envelope in
  [`middleware.py`](../../src/trellis_api/middleware.py) already strips
  internals (it returns only `code`/`message`/`request_id`). This layer
  extends the *same discipline* to route- and CLI-level JSON error payloads: a
  shared JSON-safe error helper keeps `status`, command/config context, and
  `error_type`, and replaces raw external exception text when it matches a
  leak shape (SQL fragments, emails, token/secret-shaped strings, raw external
  field names). Full detail goes to the structured log stream under the
  request ID, never to the client. The MCP markdown surface
  ([`adr-mcp-contract.md`](./adr-mcp-contract.md)) is out of scope here — it
  returns markdown, not JSON error envelopes.
- **SQL statement-type allowlist (#207).** Any shared Trellis SQL-classification
  path that emits a `statement_type` into a review artifact or graph-safe JSON
  output **allowlists known SQL verbs** and falls back to `UNKNOWN` for an
  unrecognized first token, so email-like / display-name-like tokens can never
  become a leaked transformed fragment.

---

## 4. Phased rollout

Each phase is independently shippable and leaves the tree working. **Default
posture stays frictionless for loopback dev throughout; fail-closed triggers
only on the posture-changing choices noted.**

| Phase | Layer | What lands | Posture |
|---|---|---|---|
| 1 | A | Bind check at startup + `TRELLIS_API_ALLOW_INSECURE_BIND` override | **Fail-closed** for non-loopback-without-auth. Loopback unaffected. |
| 1 | E (errors) | Shared JSON-safe error helper across routes + CLI (#206); SQL statement-type allowlist (#207) | Fail-closed; strictly removes leakage, no behavior change for clean paths. |
| 2 | B | `require_scope()` factory + `keys:` scope map; bare `TRELLIS_API_KEY` ⇒ all-scopes (compat) | Opt-in scopes; single-key deployments unchanged. |
| 2 | B (UI) | `TRELLIS_API_DISABLE_UI` + authenticated UI fetch flow (#190) | Opt-in; UI stays mounted by default. |
| 3 | E (probes) | `/readyz` detail + `/metrics` gated behind `admin`/internal-only env flags (#192) | Default minimal; detail is opt-in exposure. |
| 3 | C | ArcadeDB admin/runtime credential split + deployment-order docs (#193) | Opt-in vars; single-credential compat for one release. |
| 4 | D | Classification gate in `MutationExecutor` + PackBuilder/retrieval filter; `TRELLIS_ENFORCE_CLASSIFICATION` (#194) | **Opt-in** enforcement; fail-closed-on-unclassified once enabled. |
| 5 | — | Deprecation cleanup: remove single-credential ArcadeDB fallback; make scope map the documented default | Breaking for deployments that ignored the one-release window. |

**Fail-closed vs opt-in summary.** Layer A (bind) and Layer E-errors are
fail-closed from Phase 1 because they have **no legitimate loopback-dev
cost**. Layers B, C, E-probes, and D are **opt-in** because each has existing
deployments (single key, single ArcadeDB credential, open scrape jobs,
unclassified data) that a hard cutover would break; each carries a
one-release compat window and an explicit enabling switch.

---

## 5. Acceptance Criteria

Mapped to the resolving issues. ("Refuses" = process exits non-zero before
serving; "denies" = HTTP 401/403 with a sanitized body.)

- **#189** — Booting with a non-loopback `TRELLIS_API_HOST` and no
  `TRELLIS_API_KEY` **refuses** with a message naming both remedies.
  `TRELLIS_API_ALLOW_INSECURE_BIND=1` permits it with a per-boot warning.
  Loopback bind without a key still boots.
- **#190** — With auth enabled, the UI either authenticates its `/api/v1`
  calls (configured key in `X-API-Key`) or is absent when
  `TRELLIS_API_DISABLE_UI=1`. Docs describe the production-safe path.
- **#191** — Operators can issue `read` / `ingest` / `mutate` / `admin`
  credentials; each `/api/v1` router enforces its required scope via
  `Depends(require_scope(...))`; a `read` key is **denied** on a `mutate`
  route. A lone `TRELLIS_API_KEY` still grants full access (compat).
- **#192** — Minimal `/healthz` and `/readyz` stay unauthenticated; the
  per-backend `/readyz` breakdown and `/metrics` require `admin` scope (or an
  internal-only env flag) and are not exposed by default on a non-loopback
  bind.
- **#193** — Runtime graph/vector operations succeed with
  `TRELLIS_ARCADEDB_USER`/`_PASSWORD` that lack DDL rights; the
  `ensure_database`/DDL path runs only under the admin vars during an explicit
  init/migration phase; runtime resolution does not silently fall back to
  `root`. Docs give the provision → migrate → run order.
- **#194** — With enforcement on: a write classified above the caller's
  clearance is **denied** in the `MutationExecutor` gate; a pack/graph read
  omits items above the caller's clearance; an **unclassified** item is
  treated as the default ceiling (`internal`) and withheld from lower-clearance
  callers.
- **#206** — A JSON error payload from a metadata command that raises with raw
  SQL / an email / a token-shaped value carries `status`, context, and
  `error_type` but **not** the raw text; full detail is in the structured log
  under the request ID. A regression test simulates such an exception.
- **#207** — A SQL-classification path given an email-like first token emits
  `UNKNOWN`, never a transformed fragment, in any review/graph-safe artifact.

---

## 6. Non-goals

- **Per-tenant identity, OAuth/OIDC, JWT, mTLS.** This ADR hardens the
  shared-secret model into a scoped, fail-closed one. Richer identity layers
  onto the scope abstraction in a follow-up; the `auth.py` docstring already
  flags JWT/OAuth as a later PR.
- **Encryption at rest / field-level encryption.** Classification gates
  *access*; it does not encrypt stored values.
- **Rate limiting / quota / DoS protection.** Orthogonal; belongs in the
  ingress/proxy tier.
- **MCP surface auth.** MCP is a separate, in-process contract
  ([`adr-mcp-contract.md`](./adr-mcp-contract.md)) consumed over stdio by agent
  hosts, not over the network; its auth model is out of scope here.
- **Populating `DataClassification` automatically.** This ADR enforces
  classification where present and fails closed where absent; *which*
  classifier or extractor populates it is a separate decision (the schema
  docstring notes no classifier populates it today).
- **Removing the loopback no-auth default.** Frictionless loopback dev is a
  first-class project value and is preserved; the fail-closed behavior is
  scoped to the network-exposed and explicitly-enabled cases only.
