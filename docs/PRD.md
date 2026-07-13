# PRD — trellis-ai

```yaml
status: active
owner: nronsse
last-review: 2026-07-11
```

> Rules: ≤ 2 pages. §3 contains only verifiable facts — every claim cites a path or command.
> Agents load §3–§5 before working here; write for them. Bump `last-review` whenever touched.

## 1. Problem

Agents forget: every session re-derives context that a prior session already earned, and what one agent learns evaporates instead of compounding. The existing outs are bad for a self-hosting operator: SaaS memory (Zep, Mem0, Letta) sends your traces to someone else's cloud, and a bare vector DB gives you similarity but no attribution ("which served item actually helped?"), no governance over agent writes, and no audit trail. The pain is most acute for a solo operator running multiple agents (Claude Code, Hermes, scheduled jobs) against one shared substrate — which is exactly the live deployment today.

## 2. Users & jobs

Agent consumers are first-class users:

- As **Claude Code / Hermes / any MCP client**, I pull a token-budgeted context pack before a task and record a trace + feedback after, so the next run starts smarter (skills: `skills/`, MCP tools: `src/trellis/mcp/server.py`).
- As **the operator (nronsse)**, I ingest my corpus and claude.ai conversations, audit the memory via the Memory Explorer, and meter what the memory costs me (`trellis analyze cost`).

Adopter profiles beyond the author (it's public, on PyPI — who else deploys this?):

1. **MCP-agent power users** — hire Trellis for persistent cross-session memory for Claude Code/Cursor with one `pip install`, SQLite, no cloud. The proven path (skynet dogfood is this profile).
2. **Agent-platform teams (LangGraph, custom Python)** who want *self-hosted* memory instead of Zep/Mem0/Letta SaaS — hire it for Postgres+pgvector on infra they already run, REST/SDK, governed writes + audit log. The wedge vs those systems: feedback attributes to the **specific items served**, not to a session or user, plus SCD-2 time-travel.
3. **Data-platform teams** pointing agents at warehouse/dbt/BI metadata — hire it as a governed usage-evidence graph with an explicit promotion ladder and privacy rules (`docs/design/adr-query-history-promotion.md`). This profile sourced issues #200–#203 via the paused consumer-kg pilot.

Honesty note: verified deployments today = the author's dogfood + that paused pilot. Profiles 2–3 are targets, not evidence.

## 3. Current state (verified)

- **v0.9.0** tagged (`git tag`), published on PyPI as `trellis-ai`; Python ≥3.11, MIT (`pyproject.toml`).
- **Six shipped packages** — `trellis`, `trellis_cli`, `trellis_api`, `trellis_sdk`, `trellis_workers`, `trellis_wire` (`pyproject.toml [tool.hatch.build.targets.wheel]`); entry points `trellis` / `trellis-mcp` / `trellis-api`.
- **Six store ABCs, multi-backend**: graph = SQLite/Postgres/ArcadeDB/Neo4j, vector = SQLite/pgvector/ArcadeDB/Neo4j, blob = local/S3 (`src/trellis/stores/`, table in `CLAUDE.md`). Backends pass shared contract suites (`tests/unit/stores/contracts/` — 49 graph + 25 vector tests per backend).
- **14 MCP tools** (`grep -c '@mcp.tool' src/trellis/mcp/server.py` → 14), markdown output, opt-in HTTP transport with scoped API keys (#252, `docs/design/adr-mcp-http-transport.md`).
- **REST API with scoped auth** (`TRELLIS_AUTH_MODE`, PR #242) + Memory Explorer UI (`src/trellis_api/`); dual-mode Python SDK (`src/trellis_sdk/`).
- **Governed mutation pipeline** — validate → policy → idempotency → execute → emit event (`src/trellis/mutate/executor.py`); traces immutable.
- **Tests/CI**: 4186 unit tests collected by default, 4734 total (`.venv/bin/python -m pytest tests/unit/ -q --co`); all six workflows green on main 2026-07-11 (`gh run list --repo ronsse/trellis-ai --branch main`).
- **Landed 2026-07-11**: `trellis ingest corpus` (bb5a882), `trellis ingest conversations` (0a7e482), `--extract` entity mining (7431488), `trellis analyze cost` (5ea7cd5).
- **Live-deployment truth** (dogfood analysis, `TODO.md` §"Dogfood gap analysis — 2026-07-11"): 36 docs / 6 traces / 44 nodes / 187 events / 5 packs and **0 advisories / 0 lessons** — the learning loop runs but is input-starved; three retrieval defects verified live over MCP (`domain=` hard-exclusion, missing `pack_id` on flat `get_context`, `get_context`/`search` bypassing PackBuilder).

## 4. Invariants / product principles

- **Traces are immutable**; all writes flow through `MutationExecutor` — no direct store writes (`CLAUDE.md` Hard Rules).
- **EventLog is the single authoritative feedback path**; `pack_feedback.jsonl` is audit-only (`docs/design/adr-dual-loop-evolution.md` §8).
- **Deterministic-first, LLM opt-in**: LLM never in the write path; extraction routes `DETERMINISTIC > HYBRID > LLM` with `allow_llm_fallback=False` default; core stays LLM-SDK-free (protocols only, `src/trellis/llm/`). The ladder's destination is `DETERMINISTIC > LOCAL > FRONTIER` (north star, §6): *cost-motivated* judgment-avoidance dissolves as local inference approaches free; *trust-motivated* determinism — governed writes, idempotency, audit — is permanent regardless of where inference runs.
- **Statistical gate before parameter promotion** (defaults: 5 samples, 15% effect) — no evidence, no change.
- **Contract suites are the storage spec**; a backend that doesn't pass them doesn't ship.
- **Open-string type extensibility** — no domain-specific types in core enums.
- **Schemas `extra="forbid"`**; machine surfaces leak-safe (`trellis.core.error_sanitize`); secrets never in the repo (`.env.example` only).

## 5. Component disposition

| Component | Verdict | Rationale |
|---|---|---|
| PackBuilder + strategies + effectiveness (`src/trellis/retrieve/`, ~8.1k LOC) | keep internal | The attribution seam (pack_id → per-item feedback) *is* the product; no OSS retriever (LlamaIndex, Haystack) carries it. Fix direction is more PackBuilder, not less — route the MCP `get_context`/`search` defects through it. |
| Graph/vector backends: SQLite + Postgres/pgvector | keep internal | Local default + the only backends running in production (skynet). Non-negotiable. |
| Graph/vector backends: ArcadeDB + Neo4j (Bolt pair, ~3.3k LOC incl. shared `bolt_opencypher/` base) | keep internal, **feature-frozen** | Zero known external users; carry-cost is capped by the shared base + contract suites + containerized CI, so deleting now buys little — but no new features until an external user exists. If the matrix ever taxes a change (e.g. #194 enforcement), cut to pgvector-only and re-home the pair as a plugin (`docs/design/adr-plugin-contract.md`); extraction cost = a second release train + cross-repo contract-suite wiring. |
| Classification pipeline (`src/trellis/classify/`, ~2k LOC) | keep internal | Deterministic-inline design matches the cost thesis and it's small. But `DataClassification` is dead weight until #194 enforces it — enforce it or stop presenting it as a security feature. |
| Feedback / fitness loops (`feedback` + `learning` + `workers`, ~7.4k LOC) | keep internal | This is the differentiator vs Zep/Mem0 (session/user-level attribution). Live verdict: starved, not broken (0 advisories/0 lessons). Feed it via capture work; re-judge 30 days after session auto-capture lands. |
| MCP server (`src/trellis/mcp/`, ~2.5k LOC) | keep internal | Primary adoption surface; fastmcp does transport. Known defect: hand-rolled retrieval in 2 tools — fix by delegating to PackBuilder. |
| REST API + Memory Explorer (`src/trellis_api/`, ~3.6k LOC) | keep internal | Thin FastAPI over the same registry; SDK-remote and the UI depend on it. There is no replacement that preserves the auth-scope model. |
| LLM provider wrappers (`src/trellis/llm/providers/`) | keep protocol; **cap at 2 wrappers**; new providers via a LiteLLM adapter | `adr-llm-client-abstraction.md` already rejected litellm-in-core; the sanctioned pattern is a ~30-LOC `LiteLLMClient(LLMClient)` consumer-side adapter. Never grow a first-party provider matrix. |
| Tiered extraction (`src/trellis/extract/`, ~4.6k LOC) | keep internal | The LLM-bootstraps/deterministic-inherits ladder is core thesis; extracting it as a standalone lib costs schema+executor decoupling for zero demand. |

## 6. Scope: now / next / not

- **North star (stretch; orders everything else)** — a **small local model performs every
  judged memory operation**: extraction, reconciliation, distillation, curation verdicts.
  Frontier models become optional, never load-bearing. Partially live already (hermes3:8b
  extraction, nomic-embed embeddings — both local). Rules it imposes *today*: every new
  judged stage targets a local model first; no judged stage may be designed to require
  frontier-scale reasoning; every judged op is logged with its downstream outcome as a
  training pair — the memory system generates its own fine-tuning corpus.
- **Now** — Productionization (see `docs/ROADMAP-EDITS-2026-07-11.md`): fix the three live retrieval defects + feedback plumbing (TODO.md dogfood queue), close the security floor (#250 credential hygiene, #194 classification enforcement), land the query-history curation primitives (#200–#203) as fixture-tested code, disposition #208.
- **Next** — Claude Code session auto-capture (the highest-leverage capture gap; new ADR increment); §G.4 corpus handlers strictly by observed dogfood need; Phase F F1 harness when the owner schedules it.
- **Not doing** — the anti-scope list; this project has scope-creep gravity:
  - **No new storage backends.** FalkorDB (SSPL), Kuzu (single-writer), Neptune (cost) already rejected — `adr-arcadedb-blessed-substrate.md`.
  - **No managed/SaaS offering.** Self-hosted is the positioning, not a gap.
  - **No workflow/orchestration engine.** One was built and deleted (`docs/research/workflow-engine-disposition.md`). Do not re-grow it.
  - **No RDF/JSON-LD export** (roadmap B.4) and **no vector DSL** (C.1) until their gating signals fire.
  - **No tag-vocab phases 2/3/5 pre-building** — partner-gated (roadmap §D); only the #194 enforcement slice is pulled forward, and that needs owner sign-off (§8).
  - **No autonomous coding-agent spawn** (Item 7 Cohort 2) without the ADR amendment it's gated on.
  - **No prompt library / Jinja2** — three prompts on `str.format` is below the complexity threshold (`adr-llm-client-abstraction.md` §Phase 4).
  - **No audio transcription in core** — external pre-step (`adr-corpus-ingestion.md`).
  - **No enterprise-graph bridge implementation** (#220 is accepted design only) until the pilot resumes.

## 7. Success criteria

- Quickstart works cold: `pip install trellis-ai && trellis admin init && trellis demo load && trellis retrieve pack --intent "deploy staging for user-api" --format json` exits 0.
- `pytest tests/unit/ -q` green; all six workflows green: `gh run list --repo ronsse/trellis-ai --branch main --limit 6`.
- Loop unstarved: within 30 days of session auto-capture landing, `trellis analyze context-effectiveness --format json` on skynet reports > 0 advisories and the nightly `worker curate` log stops being all-zeros.
- Attribution round-trip from MCP: flat `get_context` output contains `**pack_id:**`, and a subsequent `record_feedback` produces a `FEEDBACK_RECORDED` event joining that pack (`GET /api/v1/events`).
- #194 enforced: a `pytest -k classification` test proves a restricted item is filtered from retrieval and its mutation denied for an unscoped caller.

## 8. Open questions

- **Who is deployer #2?** Every §5 verdict assumes N≈1. Owner: nronsse. Trigger: first external production issue/PR, or 2026-10 quarterly review — whichever first; revisit the Bolt freeze and profiles 2–3 then.
- **#194 pull-forward** contradicts the roadmap's "design partner asks" gate. Owner decision at milestone commit: does MCP-over-HTTP (#252) exposure justify enforcing now?
- **#200–#203 timing**: implement against fixtures now vs honor the anti-pre-build doctrine until the consumer-kg pilot restarts. Owner call.
- **AuraDB reality**: roadmap §1 says the instance is GONE; #250's body says rebuilt 2026-06-16 with a ~2026-07-16 lapse watch. Reconcile before executing #250 (which credentials still exist to rotate?).
- **Is session auto-capture the flagship?** If ambient capture works, the pitch shifts from "call `save_*`" to "memory that fills itself" — affects README positioning and profile 1. Decide after the first 30-day capture run.
