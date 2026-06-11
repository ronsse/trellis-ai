# Setup Decisions — the human choices Trellis can't make for you

> **Who this is for:** anyone standing up Trellis beyond a local single-user
> sandbox. `trellis admin init` gives you a working SQLite substrate with **zero
> required decisions**. Everything in this doc is about the choices that a person
> has to make deliberately — and that are easy to miss because the default
> install never prompts for them.

The point of this page is so that **nothing on this list gets skipped by
accident.** Each item says *when* it matters, *where* the decision is recorded,
and links the issue / ADR that owns the detail.

> **Will Trellis enforce these, or just document them?** Both, by tier. The
> enforcement model — fail-closed startup invariants, a `trellis doctor`
> pre-deploy lint that turns this checklist into one executable command, and
> runtime policy gates — is [`adr-setup-enforcement-model.md`](../design/adr-setup-enforcement-model.md).
> Until those land, the statuses below tell you what's enforced today.

## How to read the status column

Trellis ships the open-string core today; several governance features below are
**designed but not yet built** (their ADRs are `Status: Proposed`). We mark each
decision so you know whether you are configuring something enforced today or
adopting a convention that tooling will formalize later:

| Status | Meaning |
|---|---|
| **Now** | Available and enforced in the current release — make the decision at setup. |
| **Convention** | You decide and record it now (as metadata / a profile / a doc); enforcement tooling is planned. Skipping it means drift, not an error. |
| **Planned** | The mechanism is an accepted/proposed ADR, not yet implemented. Decide your intent now; wire it when it lands. |

---

## Tiers — find your row, then read only what applies

| Deployment | What you're doing | Sections you must not skip |
|---|---|---|
| **Local sandbox** | One user, SQLite, your laptop | None. `trellis admin init` is the whole setup. (Optionally §1 LLM.) |
| **Team / shared substrate** | Multiple agents hit one Trellis over the REST API | §1, **§2 (Security)** |
| **Data-platform / enterprise graph** | Ingesting real datasets, lineage, query history, BI metadata into a governed graph | §1, §2, **§3 (Domains & ontology)** |
| **Production** | Any of the above, exposed beyond loopback, with real data | **All of §2**, plus §3 if enterprise |

---

## §1 — LLM enrichment (optional, all tiers)

| Decision | Status | Where | Reference |
|---|---|---|---|
| Use an LLM for memory extraction / enrichment? Which provider + model? | **Now** | Uncomment the `llm:` block in `~/.config/trellis/config.yaml` (written by `admin init`); set `TRELLIS_ENABLE_MEMORY_EXTRACTION=1` | [playbooks.md → "Configuring LLM extraction"](../agent-guide/playbooks.md) |

Deterministic classification works with no LLM. The LLM path is an opt-in
addition, never a silent substitution.

---

## §2 — Security & credentials (team / production)

**Read this before you bind the REST API to anything other than `127.0.0.1`.**
The full model is [`adr-rest-api-security-model.md`](../design/adr-rest-api-security-model.md).

| # | Decision | Status | Where / how | Issue |
|---|---|---|---|---|
| 2.1 | **Network bind posture.** A non-loopback bind without an API key should refuse to start (fail closed). | **Planned** | Set `TRELLIS_API_KEY` before `admin serve --host 0.0.0.0`; loopback dev needs nothing | [#189](https://github.com/ronsse/trellis-ai/issues/189) |
| 2.2 | **Scoped credentials.** Which callers get `read` / `ingest` / `mutate` / `admin`? | **Planned** | Per-scope API keys (inclusive scopes: read ⊂ ingest ⊂ mutate ⊂ admin) | [#191](https://github.com/ronsse/trellis-ai/issues/191) |
| 2.3 | **Static UI.** Authenticate it, or disable it in production. | **Planned** | UI sends `X-API-Key`, or set the disable flag | [#190](https://github.com/ronsse/trellis-ai/issues/190) |
| 2.4 | **Readiness / metrics exposure.** Detailed health + `/metrics` should not leak topology by default. | **Planned** | Gate detail behind admin scope / internal-only flag | [#192](https://github.com/ronsse/trellis-ai/issues/192) |
| 2.5 | **ArcadeDB credential split.** Separate admin/migration creds from runtime creds (least privilege). | **Planned** | `TRELLIS_ARCADEDB_ADMIN_*` (DDL/init) vs runtime `TRELLIS_ARCADEDB_*` | [#193](https://github.com/ronsse/trellis-ai/issues/193) |
| 2.6 | **Provide the actual secrets.** ArcadeDB password, AWS creds for S3, etc. Don't commit them. | **Now** | Env vars / `.env` (referencing a secret manager); never literals in `config.yaml` | [#208](https://github.com/ronsse/trellis-ai/issues/208) |
| 2.7 | **Data-classification enforcement.** Enforce `DataClassification` on retrieval + mutation; treat unclassified as `internal` (fail closed). | **Planned** | One-release opt-in env flag, then default | [#194](https://github.com/ronsse/trellis-ai/issues/194) |

Backend selection (Postgres / pgvector / S3 / ArcadeDB / Neo4j) is a
configuration choice, not a security one — see
[getting-started/README.md → "Local vs Remote"](README.md) and
[deployment/](../deployment/). **Self-hosting ArcadeDB?** It only exposes Bolt
(7687) if you enable the Bolt plugin — see
[`adr-arcadedb-blessed-substrate.md`](../design/adr-arcadedb-blessed-substrate.md)
("Self-hosting requirement").

---

## §3 — Domains & ontology (data-platform / enterprise)

This is the section most often missed, because the open-string core happily
accepts anything you throw at it — which means **modeling drift is silent.** If
you are building a real knowledge graph from datasets, lineage, query history, or
BI metadata, make these decisions *before* the first bulk ingest. The framing
doc is [`adr-enterprise-ontology-capability-framing.md`](../design/adr-enterprise-ontology-capability-framing.md).

| # | Decision | Status | Where / how | Issue / ADR |
|---|---|---|---|---|
| 3.1 | **Define your domains.** What are the named domains (e.g. `product-analytics`, `customer-360`)? | **Convention** | `context.domain` on traces/items; domain list in an ontology profile | [#217](https://github.com/ronsse/trellis-ai/issues/217) |
| 3.2 | **Assign domain ownership.** Which domain owns each dataset? Generic tables (e.g. `landing.raw_events.events`) need an explicit owner or they fall outside every curator scope. | **Convention** | Decide per dataset; record in the profile / enrichment config before curation | [#205](https://github.com/ronsse/trellis-ai/issues/205), [#204](https://github.com/ronsse/trellis-ai/issues/204), [#200](https://github.com/ronsse/trellis-ai/issues/200) |
| 3.3 | **Adopt an ontology profile (optional).** Declare allowed entity types, edge kinds, source authority, node-role defaults, required/forbidden properties, and projections for this deployment. | **Planned** | A `*.profile.yaml`, validated by a CLI linter (no runtime change) | [#219](https://github.com/ronsse/trellis-ai/issues/219) |
| 3.4 | **Set query-history promotion gates.** What stays behavioral evidence vs becomes a curated pattern vs an accepted fact? Who reviews promotions? | **Convention** | Promotion ladder + review gate; pipeline vs analyst usage kept separate | [#218](https://github.com/ronsse/trellis-ai/issues/218), [#200](https://github.com/ronsse/trellis-ai/issues/200), [#213](https://github.com/ronsse/trellis-ai/issues/213), [#216](https://github.com/ronsse/trellis-ai/issues/216) |
| 3.5 | **Column / leaf modeling stance.** Default columns to `Dataset` properties; declare the explicit exceptions where column *nodes* are justified. | **Convention** | Follow [modeling-guide.md → "Column and leaf metadata policy"](../agent-guide/modeling-guide.md); profile can set `Column.node_role_default=structural` | [#221](https://github.com/ronsse/trellis-ai/issues/221) |
| 3.6 | **Enterprise graph (EG) interop (if applicable).** How do accepted enterprise facts flow in, and which Trellis candidates flow out after review? | **Planned** | Bridge mapping + fact states as profile metadata | [#220](https://github.com/ronsse/trellis-ai/issues/220) |

### Why 3.2 keeps coming up

The recurring "decision needed" in the issue tracker (#205, #204) is a dataset —
typically a generic one like `raw_events` — that no configured domain
claims. Trellis won't guess: a curator scoped to enrichment domains finds zero
candidates for an unowned table, while a scope-only scout finds it. **Decide
ownership explicitly** (add it to a domain, create a new domain, or deliberately
leave it out of the first curator scope) and record the decision; do not run LLM
curation or graph writes against an unowned dataset until you have.

---

## A minimal recorded-decisions file

Until the ontology-profile linter (#219) lands, the lightest way to keep §3 from
drifting is a short committed doc in your deployment repo — e.g.
`trellis-decisions.md` — capturing: your domain list, the owner of each dataset
(especially the ambiguous ones), your promotion-review policy, and your
column-node exceptions. When profiles ship, that doc becomes the profile.

## See also

- [`adr-enterprise-ontology-capability-framing.md`](../design/adr-enterprise-ontology-capability-framing.md) — the umbrella: where each kind of fact belongs
- [`adr-ontology-profiles.md`](../design/adr-ontology-profiles.md) — the optional governance overlay (#219)
- [`adr-query-history-promotion.md`](../design/adr-query-history-promotion.md) — behavioral evidence → accepted fact (#218)
- [`adr-rest-api-security-model.md`](../design/adr-rest-api-security-model.md) — the layered security model (§2)
- [modeling-guide.md](../agent-guide/modeling-guide.md) · [source-modeling-cookbook.md](../agent-guide/source-modeling-cookbook.md) — node/property/document decisions
