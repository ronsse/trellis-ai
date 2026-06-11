# ADR: Setup-decision enforcement model

**Status:** Proposed
**Date:** 2026-06-04
**Deciders:** Trellis core
**Related:**
- [`../getting-started/setup-decisions.md`](../getting-started/setup-decisions.md) — the human-decision checklist this ADR makes enforceable
- [`./adr-rest-api-security-model.md`](./adr-rest-api-security-model.md) — the security layer whose invariants this ADR fails closed (#189, #191–#194)
- [`./adr-ontology-profiles.md`](./adr-ontology-profiles.md) — the profile linter that is this model's ontology arm (#219)
- [`./adr-column-leaf-modeling-guardrails.md`](./adr-column-leaf-modeling-guardrails.md) — the structural-node smell `doctor` reports (#221)
- [`./adr-query-history-promotion.md`](./adr-query-history-promotion.md) — promotion gates `doctor` checks for (#218)
- [`./adr-enterprise-ontology-capability-framing.md`](./adr-enterprise-ontology-capability-framing.md) — umbrella framing
- [`../../src/trellis/stores/registry.py`](../../src/trellis/stores/registry.py) — `StoreRegistry.validate()` fail-fast hook (extended here)
- [`../../src/trellis/mutate/policy_gate.py`](../../src/trellis/mutate/policy_gate.py) — `DefaultPolicyGate` + `Enforcement` levels reused here
- [`../../src/trellis_api/auth.py`](../../src/trellis_api/auth.py) — `warn_if_unauthenticated()` upgraded to fail closed

---

## 1. Context

[`setup-decisions.md`](../getting-started/setup-decisions.md) enumerates the
choices a team / data-platform / production deployment must make — domains and
ontology, domain ownership, API security — that the default `trellis admin init`
never prompts for. Today they are **documented but unenforced**: an operator who
doesn't read the checklist can expose an unauthenticated API on a corporate
network, or bulk-ingest an ungoverned graph, with no signal that a decision was
skipped.

The question this ADR answers: *which of those decisions can Trellis enforce, by
what mechanism, and how strict should the default be?*

### What already exists

Trellis is not starting from zero. Three enforcement primitives are already in
the codebase and this ADR routes setup decisions through them rather than
inventing a fourth:

| Primitive | Location | Today |
|---|---|---|
| Fail-fast config validation | `StoreRegistry.validate()`, called in the API lifespan (`src/trellis_api/app.py`) | Aggregates store-construction errors and crashes startup before serving |
| Enforcement spectrum | `Enforcement` enum (`enforce` / `warn` / `audit_only`) consumed by `DefaultPolicyGate` (`src/trellis/mutate/policy_gate.py`) | Per-mutation deny/warn/audit in `MutationExecutor` |
| Auth posture warning | `warn_if_unauthenticated()` (`src/trellis_api/auth.py`) | **Logs a warning** when `TRELLIS_API_KEY` is unset — does not block |

The `Enforcement` enum is the conceptual anchor: the project *already* models a
soft→hard spectrum for writes. This ADR applies the same spectrum to setup
posture.

## 2. Decision

### 2.1 The governing principle

> **Enforce the invariant, lint the convention, document the judgment.**

Trellis can detect the *absence* of a decision; it cannot validate a decision's
*correctness*. It can refuse to curate an unowned dataset — it cannot know
whether the owner you chose is right. Enforcement therefore means: make a missing
or unsafe decision **detectable**, and **block** on it only where the cost of
missing it is high and the check is unambiguous. Everything else is surfaced, not
forced.

### 2.2 Three mechanisms

Every setup decision maps to exactly one mechanism by its nature:

| Tier | Mechanism | When it runs | Reuses |
|---|---|---|---|
| **T1 — Fail-closed invariant** | Raise at startup; refuse to serve | API/worker boot | `StoreRegistry.validate()`, lifespan |
| **T2 — Pre-deploy / CI lint** | `trellis doctor` → severities + exit code | On demand, in CI, pre-deploy | new command + profile linter (#219) |
| **T3 — Runtime policy gate** | Per-write deny/warn/audit | Every mutation | `DefaultPolicyGate` + `Enforcement` |

T1 is reserved for unambiguous safety invariants. T2 is the workhorse — it turns
the whole checklist into one executable command. T3 governs ongoing writes after
setup.

### 2.3 Default posture: **fail closed**

New enforcement defaults to the **strict** end:

- **T1 invariants raise by default.** A non-loopback bind without
  `TRELLIS_API_KEY`, or a configured backend missing its required secret, refuses
  to start.
- **T2 `fail` severities exit non-zero by default.** `doctor` is a real gate, not
  advisory output.
- **T3 gates ship at `ENFORCE`** for the classification and profile policies once
  their backing ADRs land (#194, #219).

Strict-by-default is the safe choice for a system that holds governed knowledge
and can be exposed on a network. But it has an **upgrade cost**: an existing
deployment that today serves an unauthenticated API on `0.0.0.0` will fail to
start after upgrading. We accept that cost and mitigate it with:

1. **Loud, named escape hatches.** Every T1 invariant has an explicit
   single-purpose override env var (e.g. `TRELLIS_API_ALLOW_INSECURE_BIND=1`).
   The override is logged at `WARN` on every boot so it can't hide in a
   deployment forever.
2. **An actionable error.** The startup failure names the exact env var to set
   (the key) *and* the override to bypass — never a bare stack trace.
3. **A release note.** The first release carrying T1 invariants calls out the
   breaking posture change and the override in its upgrade notes.

Loopback-only development is unaffected: `127.0.0.1` binds and SQLite-only
deployments trip no T1 invariant, so `admin init` → `admin serve` stays
zero-friction.

### 2.4 `trellis doctor` — the executable checklist

A new CLI command audits a deployment against `setup-decisions.md` and exits
non-zero when any check fails.

```
trellis doctor [--strict] [--scope read|ingest|curate|serve] [--format text|json]
```

- **Severities:** `fail` (blocks — exit non-zero), `warn` (surfaced; promoted to
  `fail` under `--strict`), `info` (FYI).
- **Exit codes:** reuse the CLI exit-code convention
  ([`adr-cli-exit-codes.md`](./adr-cli-exit-codes.md)) — `0` clean, a dedicated
  non-zero `blocked` code when any `fail` is present.
- **JSON mode:** machine output per the project's `--format json` rule, one
  object per check (`id`, `severity`, `passed`, `detail`, `remediation`).
- **Composable:** the T1 startup path calls the *same* check functions, so a
  startup invariant and its `doctor` check never drift.

#### Check catalog (initial)

| ID | Checks | Source | Default severity | Also T1? | Decision |
|---|---|---|---|---|---|
| `SEC-BIND` | Non-loopback bind with no `TRELLIS_API_KEY` | host config + env | **fail** | **yes** | #189 |
| `SEC-SECRETS` | A configured backend's required secret env is unset | registry config | **fail** | **yes** | #208 |
| `SEC-CREDSPLIT` | ArcadeDB runtime creds equal admin/migration creds | env | warn | no | #193 |
| `SEC-CLASS` | Non-loopback serve with classification enforcement off | env + config | warn | no | #194 |
| `SEC-EXPOSURE` | `/metrics` or detailed readiness reachable unauthenticated on non-loopback | route config | warn | no | #192 |
| `ONT-OWNERSHIP` | A dataset in curator scope has no owning domain | graph + scope config | **fail** (at curate) | no | #205, #200 |
| `ONT-PROFILE` | Enterprise mode with no ontology profile; or graph violates the active profile | profile + graph | warn / **fail** on violation | no | #219 |
| `ONT-COLUMNS` | Structural-node ratio over threshold; column nodes with only `contains` edges | graph stats | warn | no | #221 |
| `ONT-PROMOTION` | Pipeline-only query history being promoted as analyst usage | curation config | warn | no | #218, #216 |

The catalog is open — checks register through a small `Check` protocol so plugin
packages (e.g. an enterprise graph (EG) integration) can add deployment-specific checks, mirroring
the open-string / plugin posture of the rest of Trellis.

### 2.5 Mapping every setup decision to a mechanism

| Setup decision (`setup-decisions.md`) | Tier | Enforced how |
|---|---|---|
| §2.1 Bind posture (#189) | T1 + T2 `SEC-BIND` | Refuse to serve; `doctor` fails |
| §2.6 Secrets present (#208) | T1 + T2 `SEC-SECRETS` | Extend `StoreRegistry.validate()` to assert required secrets |
| §2.2 Scoped credentials (#191) | T2 (config audit) | `doctor` warns if a single all-scope key serves a multi-role deployment |
| §2.5 Credential split (#193) | T2 `SEC-CREDSPLIT` | warn |
| §2.7 Classification (#194) | T2 `SEC-CLASS` + T3 gate | warn at setup; `ENFORCE` policy gate at runtime |
| §2.4 Exposure (#192) | T2 `SEC-EXPOSURE` | warn |
| §3.2 Domain ownership (#205) | T2 `ONT-OWNERSHIP` | **fail before any curator write** to an unowned dataset |
| §3.3 Ontology profile (#219) | T2 `ONT-PROFILE` + T3 gate | linter; optional write-time gate |
| §3.4 Promotion gates (#218) | T2 `ONT-PROMOTION` + T3 gate | warn; review-gated promotion |
| §3.5 Column stance (#221) | T2 `ONT-COLUMNS` | warn on smell |
| §3.1 Define domains (#217) | *documented judgment* | Not enforceable; `ONT-OWNERSHIP` flags the symptom |
| §3.6 EG interop (#220) | *documented judgment* | Not enforceable |

## 3. What this ADR does *not* do

- **Does not validate decision quality.** No check asserts the *right* domain
  owner, the *right* promotion gate, or the *right* ontology — only that a
  required decision is present and that unsafe postures are blocked.
- **Does not add a new enforcement framework.** It routes decisions through
  `StoreRegistry.validate()`, `Enforcement`/`DefaultPolicyGate`, and one new
  `doctor` command.
- **Does not break loopback/local dev.** No T1 invariant trips for a
  `127.0.0.1` + SQLite deployment.
- **Does not make profiles or classification mandatory.** Those remain opt-in
  per their own ADRs; `doctor` only warns when they're absent in a posture where
  their absence is risky.
- **Does not replace deployment-platform controls.** Network policy, secret
  managers, and RBAC at the orchestrator remain the operator's responsibility;
  `doctor` complements them.

## 4. Implementation phases

1. **Phase 1 — fail-closed bind + secrets (T1).** Upgrade
   `warn_if_unauthenticated()` to a bind-aware invariant; extend
   `StoreRegistry.validate()` to assert required backend secrets. Ships with the
   `TRELLIS_API_ALLOW_INSECURE_BIND` override and a release note. *(Resolves the
   enforcement half of #189; partial #208.)*
2. **Phase 2 — `trellis doctor` (T2) with the security checks.** The command,
   the `Check` protocol, exit codes, JSON output, and `SEC-*` checks calling the
   same functions as Phase 1.
3. **Phase 3 — ontology checks (T2).** `ONT-OWNERSHIP` as a hard pre-curation
   gate; `ONT-COLUMNS` / `ONT-PROMOTION` smells; `ONT-PROFILE` once #219 lands.
4. **Phase 4 — runtime gates (T3).** Classification (#194) and profile (#219)
   policy gates at `ENFORCE`, after their ADRs are accepted.

## 5. Acceptance criteria

- A deployment binding non-loopback without `TRELLIS_API_KEY` refuses to start by
  default, with an error naming both the key env var and the override, and boots
  (with a logged warning) when the override is set.
- `StoreRegistry.validate()` fails closed when a configured backend's required
  secret is absent.
- `trellis doctor` exists, audits the catalog, exits non-zero on any `fail`,
  supports `--strict` (warn→fail) and `--format json`, and its checks share code
  with the T1 startup invariants.
- A curator write targeting a dataset with no owning domain is blocked with a
  review-first error, not a partial write.
- Each check's output includes a remediation pointer back to the relevant
  `setup-decisions.md` section.
- Loopback + SQLite quickstart trips no invariant and requires no override.
- The breaking posture change is documented in the upgrade notes for the release
  that introduces Phase 1.
