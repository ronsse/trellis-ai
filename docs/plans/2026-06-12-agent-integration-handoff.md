# Agent-Integration Handoff Plan — Opus work packages

**Date:** 2026-06-12
**Authored by:** architecture pass (Fable) over the full integration-readiness analysis
**Executed by:** Opus agents, one work package per session/branch
**Purpose:** close every gap between "the integration surface exists" and "an external agent system can run the full populate → retrieve → curate loop end-to-end," before the Step-3 quality/impact assessment.

Architecture decisions in each package are **already made** — do not re-litigate them. If implementation reveals a conflict with a hard rule in `CLAUDE.md`, stop and surface it rather than working around it.

Read before starting any package: `CLAUDE.md` (hard rules, terminology), `docs/design/implementation-roadmap.md` §5 (hand-off protocol), and the package-specific references listed below.

---

## Execution order & dependency graph

| # | Package | Size | Depends on | Priority |
|---|---------|------|------------|----------|
| WP1 | `trellis_sdk.hooks` module | ~250 LOC + tests | — | ✅ landed on `wp1-sdk-hooks` (`29cb960`, includes WP2) |
| WP2 | SDK `record_feedback` parity | ~60 LOC + tests | — | ✅ landed on `wp2-sdk-record-feedback` (`f114886`) |
| WP3 | `trellis worker` real commands | ~300 LOC + tests | WP9 (worker.py) | ✅ landed on `wp3-worker-commands` (`985ad2f`, includes WP4 + WP9) |
| WP4 | Feedback reconcile CLI command | ~80 LOC + tests | — | ✅ folded into WP3 (`trellis admin reconcile-feedback`) |
| WP5 | Scheduler recipes + curation runbook | docs only | WP3, WP4 | ✅ landed on `wp5-scheduler-recipes` (`28ffe6f`, includes WP3/WP9) |
| WP6 | Trace→graph extraction stage | ~400 LOC + tests | — | ✅ landed on `wp6-trace-extraction` (`4482380`) |
| WP7 | Domain config + observability | ~250 LOC + tests | — | P1 |
| WP8 | Docker compose smoke test (roadmap E.1) | test/runbook | — | P2 (Docker now available on the dev host) |
| WP9 | Autonomy-ladder ADR + tier-1 auto-promotion | ADR + ~200 LOC | — | ✅ landed on `wp9-autonomy-ladder` (`4a0fb65`) |
| WP10 | Review Queue UI (human-decision inbox) | ~600 LOC UI+API | WP9 ADR | ✅ landed on `wp10-review-queue-ui` (`ef51128`) |
| WP11 | Improvement-metrics dashboard | ~400 LOC UI+API | WP10 (same UI file) | ✅ landed on `wp11-metrics-dashboard` (`d555057`, includes WP10) |
| WP12 | Quickstart `--with-skills` + integrate-your-agent front door | ~150 LOC + docs | — | ✅ landed on `wp12-quickstart-onboarding` (`8ee883c`) |

**All packages landed 2026-06-12.** Merge order onto main: `wp1-sdk-hooks` (subsumes WP2) → `wp6-trace-extraction` → `wp3-worker-commands` (subsumes WP9) → `wp5-scheduler-recipes` → `wp10-review-queue-ui` → `wp11-metrics-dashboard` → `wp12-quickstart-onboarding`. Known integration follow-ups for the merge train: (1) WP11's `parameter_promotions` metric was built without WP9 in its tree — extend it to count `PARAMS_AUTO_PROMOTED` / `PARAMS_AUTO_ROLLED_BACK` once both are on main; (2) expected small conflicts in `operations.md`, `main.py`/`admin.py`, `index.html`; (3) add the integrate-your-agent ↔ running-trellis cross-links (WP5/WP12 landed on parallel branches); (4) WP11's endpoint is `/api/v1/metrics/timeseries` (admin router has no `/admin` segment — docs already corrected).

**WP5 scope expansion (2026-06-12):** WP5 now also delivers `docs/getting-started/running-trellis.md` — the operating runbook for everything that runs server-side: `trellis admin serve` (API + UI), every `trellis worker` command from WP3/WP9 (`curate`, `tune`, `enrich`, `mine-precedents`, `--interval` loop mode), what each loop does, recommended cadences, and how the scheduler recipes invoke them. WP5 remains gated on WP3 landing.

WP2 → WP1 is the only hard ordering. Everything else parallelizes across worktrees.

---

## WP1 — Implement `trellis_sdk.hooks` (ContextInjector / TraceRecorder / ResultFeedback)

**Why:** the generic pre/post hook layer for non-MCP agent systems. Designed, never built.

**Spec is already written:** `docs/plans/workflow-integration-hooks.md` defines the three classes and their signatures. Implement it as specified. Decisions locked in:

- New module `src/trellis_sdk/hooks.py`, exported from `trellis_sdk.__init__`.
- **Graceful degradation is the contract:** hook methods never raise into the host workflow. Catch `TrellisError` subclasses, log via `structlog`, return a sentinel (`None` / no-op result). The host agent's task must never fail because Trellis was down.
- **Correction from WP2 (2026-06-12):** the SDK is HTTP-only — the local lazy-import mode was removed in the Step 3 refactor and an isolation test enforces zero `trellis.*` imports in `trellis_sdk`. Hooks accept a `TrellisClient` / `AsyncTrellisClient` (HTTP). In-process testing uses `trellis.testing.in_memory_client` (ASGI), which is also the supported "no server" story. The hooks brief's local-mode language is superseded.
- `ResultFeedback` uses the SDK feedback method from WP2 — do not hand-roll HTTP.
- Type hints on all public APIs; `extra="forbid"` conventions for any new models.

**Deliverables:**
1. `src/trellis_sdk/hooks.py` per the brief.
2. `tests/unit/sdk/test_hooks.py` — mock-client tests covering happy path + every degradation path (server down, validation error, version mismatch).
3. Two runnable examples: `examples/hooks_generic_workflow.py` (plain Python pre/post wrapper) and update `examples/langgraph_agent.py` to use the hooks instead of bespoke wiring.
4. Update `docs/agent-guide/operations.md` (SDK section) + a short hooks section in `docs/agent-guide/playbooks.md`.
5. Mark `docs/plans/workflow-integration-hooks.md` as implemented (status header), don't delete it.

**Done when:** `pytest tests/unit/sdk/ -v` green; both examples run against `trellis admin init` + `trellis demo load` with zero env vars; killing the API server mid-example degrades gracefully instead of crashing.

---

## WP2 — SDK `record_feedback` parity

**Why:** MCP and REST expose feedback recording; the SDK doesn't. Non-MCP agents (the SDK's whole audience) currently hand-construct requests for the most important signal in the system.

**Decisions locked in:**
- Add `record_feedback(...)` (and async twin) to `TrellisClient` / `AsyncTrellisClient`, mirroring the REST `/api/v1/packs/{pack_id}/feedback` and `/api/v1/feedback` payloads: `pack_id`, `success`, `helpful_item_ids`, `unhelpful_item_ids`, `followed_advisory_ids`, optional `target_id`/`rating`/`comment`.
- Local mode routes through `trellis.feedback.recording.record_feedback()` with the registry's event_log so the EventLog-authoritative path holds (see `CLAUDE.md` feedback-path table — the event drives behavior, the JSONL is audit).
- Return a typed result exposing whether the event emission succeeded (surface `FeedbackRecordResult` semantics, don't swallow them).

**Deliverables:** SDK methods + tests in `tests/unit/sdk/`; one paragraph in `docs/agent-guide/operations.md`.

**Done when:** local and remote modes both produce a `FEEDBACK_RECORDED` event verifiable via the EventLog, plus the JSONL row.

---

## WP3 — Put real commands behind `trellis worker`

**Why:** `worker_app` is registered in `src/trellis_cli/main.py:53` with **zero subcommands** — it advertises "Run curation workers" and delivers nothing. The curation library + per-step CLI commands are complete; this package gives them a single operational front door.

**Decisions locked in:**
- `trellis worker curate` — runs one full curation cycle in this order: `run_effectiveness_feedback` (demote/noise-tag) → `AdvisoryGenerator.generate` → `run_advisory_fitness_loop` → `build_learning_observations_from_event_log` + `analyze_learning_observations` + `write_learning_review_artifacts` (promote-half artifacts to `--output-dir`; promotion itself stays human-gated via the existing `trellis curate promote-learning` — do **not** auto-promote). Flags: `--days`, `--output-dir`, `--dry-run`, `--format json`, and per-stage `--skip-*` toggles.
- `trellis worker curate --interval <seconds>` — loop mode for daemon/tmux/container use. Plain `while + sleep` loop with structured logging per cycle; no new scheduler dependency (APScheduler/Celery are explicitly rejected — Trellis stays scheduler-agnostic, the interval flag is a convenience, not an orchestrator).
- `trellis worker enrich` — batch enrichment entry point: select unenriched / low-confidence-tagged items from the DocumentStore, run `EnrichmentService.batch_enrich` (`--concurrency`, `--limit`, `--dry-run`). Requires an LLM extra; exit with a clear error naming the missing config when absent.
- `trellis worker mine-precedents` — wraps `PrecedentMiner.generate_precedent_candidates` (`--domain`, `--min-traces`, `--limit`, `--dry-run`). Candidates surfaced/written, not auto-promoted.
- New CLI module `src/trellis_cli/worker.py`; move the `worker_app` definition there; wire meta-traces via the existing `wrap_cli_meta_analysis` helper (`src/trellis_cli/_meta_wiring.py`) like the analyze commands do.
- All output supports `--format json` (hard rule).

**Deliverables:** `src/trellis_cli/worker.py` + tests (`tests/unit/cli/test_worker.py`, mock-store pattern like the analyze tests); `docs/agent-guide/operations.md` worker section; integration test extending `tests/integration/loops/` proving `worker curate` produces noise tags + advisory updates + review artifacts from seeded events.

**Done when:** `trellis worker curate --dry-run --format json` runs clean on a `trellis demo load` database; integration loop test green; `trellis worker` no longer lists zero commands.

---

## WP4 — `trellis admin reconcile-feedback` CLI command

**Why:** `reconcile_feedback_log_to_event_log()` exists in `src/trellis/feedback/recording.py` but is unreachable without writing Python. If event emission fails soft, operators need the backfill at the command line.

**Decisions locked in:** `trellis admin reconcile-feedback --log-dir <dir> [--dry-run] [--format json]`, reporting `already_present` / `emitted` / `failed` counts from `ReconcileResult`. Also call it (or document calling it) from `trellis worker curate` as an optional `--reconcile-first` flag so a scheduled cycle self-heals divergence.

**Deliverables:** command in `src/trellis_cli/admin.py`, tests, one playbook note in `docs/agent-guide/freshness-and-curation.md`.

---

## WP5 — Scheduler recipes + curation runbook (docs only)

**Why:** "scheduler-agnostic" currently means "scheduler-homework." Make the bring-your-own-cron stance concrete.

**Deliverables:**
1. `docs/deployment/scheduled-curation.md` — what to run, in what order, at what cadence (recommendation: `worker curate` daily, `worker enrich` daily off-peak, `worker mine-precedents` weekly, `analyze schema-evolution` weekly), with copy-paste blocks for: crontab, systemd timer, GitHub Actions workflow, K8s CronJob.
2. A checked-in GitHub Actions example under `examples/integrations/github-actions/curation.yml`.
3. Cross-link from `docs/agent-guide/freshness-and-curation.md` and the getting-started README.

**Constraint:** every command referenced must exist (hence depends on WP3/WP4). Verify each snippet by actually running the underlying command once against a demo database.

---

## WP6 — Trace→graph extraction stage (new capability)

**Why:** agent runs do not populate the graph today — traces land in the TraceStore and stop. This is the single biggest "the graph populates itself from use" gap. The architecture mirrors the existing opt-in `save_memory` extraction stage (`src/trellis/mcp/server.py:161-224` + `src/trellis/extract/save_memory.py`), which is the approved template.

**Decisions locked in:**
- New extractor `TraceExtractor` in `src/trellis/extract/trace.py`. **Pure** (no store writes), conforming to the existing extractor contract, registered with `ExtractionDispatcher` at tier `DETERMINISTIC`.
- Deterministic tier mines **structured trace fields only**: entities from step targets/tool names/artifact references and `context.domain`; edges (`used`, `wasGeneratedBy`-style PROV-aligned kinds from `well_known.py`) from step→artifact relationships and trace→entity references. Canonicalize via `canonicalize_*` like the other extractors (ontology ADR Phase 1 behavior).
- LLM residue pass reuses `LLMExtractor` and stays **opt-in** (`allow_llm_fallback=False` default — hard project stance; never a silent substitution).
- Every draft stamps provenance: `source_trace_id`, `agent_id`, `extractor_tier` in properties (consistent with current property-based provenance; do NOT take on roadmap B.3 column promotion here).
- Wiring: feature-gated `TRELLIS_ENABLE_TRACE_EXTRACTION=1`, applied **post-ingest** in the three trace-ingest paths (CLI `ingest trace`, REST `POST /api/v1/traces`, MCP `save_experience`) exactly the way `save_memory` does it — extraction runs after the trace is durably stored, drafts go through `result_to_batch` → governed `MutationExecutor.execute_batch` (`CONTINUE_ON_ERROR`). A failed extraction must never fail the ingest; log and continue. Traces remain immutable — extraction reads, never mutates the trace.
- Also add `trellis extract traces --since <days> [--domain ...] [--dry-run]` for **backfill** over already-ingested traces (the existing corpus shouldn't need re-ingestion to benefit).

**Deliverables:** extractor + tests (`tests/unit/extract/test_trace.py`, mirroring `test_json_rules.py` structure incl. canonicalization tests); wiring + flag handling in the three ingest paths with tests; backfill CLI command; docs (`docs/agent-guide/trace-format.md` note on what gets extracted, `operations.md` flag reference).

**Done when:** with the flag on, ingesting the three worked-example traces from `trace-format.md` produces inspectable nodes/edges via `trellis retrieve search --format json`, each edge carrying `source_trace_id`; with the flag off, behavior is byte-identical to today; full unit suite stays green.

---

## WP7 — Domain configuration + observability

**Why:** `domain` is the primary retrieval slice, yet the keyword map that assigns it is hardcoded (`src/trellis/classify/classifiers/keyword.py:10-114`) and there is zero visibility into which domains actually exist in a deployment.

**Decisions locked in (and what is deliberately out of scope):**
- **Domains stay free strings.** No enum, no registry, no validation gate — consistent with the type-extensibility stance and the design-partner-gated philosophy in the tag-vocabulary ADR.
- Make the keyword map **config-seeded**: load domain→keywords from `config.yaml` (`classify.domain_keywords:` section) when present, falling back to the current built-in defaults. The existing `extra_domains` constructor param keeps working; config merges over defaults the same way. `trellis admin init` writes a commented-out example section.
- Add `trellis analyze domains` — a read-only usage report joining observed domain values across `TraceContext.domain` (TraceStore), `ContentTags.domain` (DocumentStore metadata), and pack/feedback events: per domain — item count, trace count, packs served, success rate from `FEEDBACK_RECORDED`. Output text + `--format json`. This is the empirical input a human needs to decide domain slices.
- **Out of scope (do not build):** automatic domain discovery/clustering and a domain promotion analyzer. That follows the column-leaf pattern — contract first, implementation gated on production telemetry. If the usage report proves valuable, a future ADR amendment defines the analyzer; note this in the command's docstring.

**Deliverables:** config loading + classifier change + tests; `analyze domains` command + tests (mock-store pattern); doc updates (`docs/agent-guide/tagging-for-retrieval.md`, `operations.md`).

**Done when:** a custom domain defined only in `config.yaml` is assigned by the classifier in ingestion mode; `trellis analyze domains --format json` on a `demo load` database returns a parseable per-domain report.

---

## WP8 — Docker compose smoke test (roadmap item E.1 — now unblocked)

The roadmap gated E.1 on "Docker available on the dev host"; Docker + compose are now available on skynet. Execute E.1 as scoped in `docs/design/implementation-roadmap.md` §3-E.1: `docker compose up --build`, verify `/healthz`, `/readyz`, `/api/version`, `/ui/`, and `trellis demo load` against the containerized API. Deliverables: any fixes surfaced, plus `docs/deployment/local-compose.md` runbook. Update the roadmap when it lands.

---

## WP9 — Autonomy-ladder ADR + tier-1 auto-promotion

**Why:** the self-improvement program (`docs/design/plan-self-improvement-program.md`) deliberately stops at semi-autonomous — every promotion needs a human `--commit`. That's correct for one-way doors but unnecessarily conservative for reversible, monitored changes. The system already owns the safety machinery (`PromotionPolicy` gates, `monitor_post_promotion()` degradation detection, `PostPromotionPolicy.auto_demote` rollback, SCD-versioned `ParameterSet`s) — the missing pieces are the policy knob and the written contract.

**Part 1 — the ADR (`docs/design/adr-autonomy-ladder.md`).** Codify four tiers; the tier assignment rule is **reversibility × blast radius**, not confidence alone:

| Tier | Contract | What lives here |
|---|---|---|
| 0 — fully automatic | Reversible, data-plane only, no approval | Noise tagging (`apply_noise_tags`), advisory confidence decay/suppression — already automatic today; the ADR just names it |
| 1 — automatic with auto-rollback | Reversible via versioned state + post-change monitoring; auto-applies above thresholds, auto-rolls-back on degradation, every action evented for audit | Parameter promotions (tuner proposals) |
| 2 — human-gated, machine-prepared | System does all analysis and drafts the artifact; human clicks approve | Learning promotions to graph, leaf promotions, code-authoring proposals (Item 7) |
| 3 — never automated | One-way commitments | `well_known.py` ontology promotion (per adr-graph-ontology §5.4), merging agent code to main |

The ADR must state the invariants for tier 1: a change may only auto-apply if (a) it is reversible through existing versioned state, (b) post-change monitoring exists and is on, (c) the auto-action emits a dedicated event (`PARAMS_AUTO_PROMOTED` / `PARAMS_AUTO_ROLLED_BACK`), and (d) per-scope opt-in config — global default stays off.

**Part 2 — tier-1 implementation.** Config-driven auto-promotion in the tuner pipeline: `learning.auto_promote` config section (enabled flag + stricter-than-manual thresholds: higher `min_sample_size`, higher `min_effect_size`), consumed by a `trellis worker tune` command (extends WP3's worker group) that runs `RuleTuner` → auto-promotes qualifying proposals → schedules post-promotion monitoring with `auto_demote=True`. Manual `trellis metrics promote` keeps working unchanged. Tests must cover: below-threshold proposals stay pending, degradation triggers rollback + event, disabled config means zero behavior change.

**Done when:** ADR merged; with `auto_promote.enabled=true` a synthetic high-confidence proposal promotes, degrades, and rolls back automatically in an integration test, with all four events visible in the EventLog.

---

## WP10 — Review Queue UI (human-decision inbox)

**Why:** every human-gated decision today is served by CLI + JSON-file editing across four disconnected flows. The UI (`src/trellis_api/static/index.html` — vanilla JS + Cytoscape, views: dashboard/traces/precedents/graph/evolution) already authenticates with an API key and already has an Evolution view; this package adds the missing **decision** surface.

**Decisions locked in:**
- New `Review` view in the existing static UI (same vanilla-JS pattern, no framework adoption). One inbox, four queue sections:
  1. **Tuner proposals** — pending `ParameterProposal`s with effect_size/sample_size/baseline; Approve button calls the promote path (`--commit` equivalent), Reject marks rejected. Needs new REST endpoints `GET /api/v1/admin/proposals`, `POST /api/v1/admin/proposals/{id}/promote|reject` wrapping the existing `trellis_cli/metrics.py` logic (route through the same governance pipeline — no new mutation path).
  2. **Learning promotion candidates** — render `intent_learning_candidates.json` artifacts with approve/reject + rationale field; on submit, build the decisions payload and run the existing `prepare_learning_promotions` → MutationExecutor path server-side. Endpoint: `POST /api/v1/admin/learning/promotions`.
  3. **Schema-evolution candidates** — list `WELL_KNOWN_CANDIDATE` events (already queryable); the only action is **Draft ADR** (calls the `draft-promotion-adr` logic, returns the markdown for download/copy). No approve button — tier 3, the gate stays in git review.
  4. **Code-authoring proposals** — list `PROPOSAL_DRAFTED` events with markdown preview (read-only in this package; approve-to-spawn waits for Item 7 Phase 1).
- All new endpoints live under the admin router, require the admin scope, and respect `TRELLIS_UI_ENABLED` / ops-detail gating (commit 781be8d conventions).
- Every approve/reject emits an audit event with the API-key identity.

**Done when:** an operator can complete the full promote-half loop (run `worker curate` → review candidates in the browser → approve → entities land in the graph) without touching a JSON file; rejected items don't resurface within cooldown.

---

## WP11 — Improvement-metrics dashboard

**Why:** the improvement signal exists (success_rate, reference_rate, advisory fitness, post-promotion baselines, schema-evolution evidence) but is only visible as point-in-time CLI tables. "Is Trellis making agents better?" needs a trend line, and it's also the instrument Step 3 (quality/impact assessment) will read.

**Decisions locked in:**
- Extend the existing Dashboard view (or a sibling `Metrics` view) with time-series charts computed server-side from the EventLog/OutcomeStore — new read-only endpoint `GET /api/v1/metrics/timeseries?metric=...&days=...&group_by=domain|intent_family`, no new storage; aggregate on read, acceptable at POC scale.
- Charts, in priority order: (1) pack success rate over time, per domain; (2) reference rate (items_referenced / items_served) over time — the single best "are packs getting better" proxy; (3) advisory fitness (confidence trajectories, suppression count); (4) noise-tag volume per cycle; (5) post-promotion baseline-vs-current per promoted parameter scope, annotated with promote/rollback events.
- Keep the no-framework constraint; use a small chart lib or inline SVG consistent with the existing UI's zero-build approach.
- Reuse/expose the same aggregations the eval scenarios compute (`eval/scenarios/agent_loop_convergence`) so dashboard numbers and eval numbers are definitionally identical.

**Done when:** after a `demo load` + a few simulated feedback rounds (use the convergence scenario's seeding), the dashboard renders non-empty trend lines for metrics 1–4 with `--format json` parity on the endpoint.

---

## Hand-off protocol per package

1. One package per branch/worktree; branch name `wp<N>-<slug>`.
2. Before code: run `pytest tests/unit/ -q` and record the baseline.
3. Hard rules from `CLAUDE.md` apply everywhere: governed mutations only, traces immutable, `extra="forbid"`, `structlog`, `--format json` support on every new CLI command, type hints on public APIs.
4. `make lint && make typecheck && make test` green before PR.
5. Update `docs/design/implementation-roadmap.md` §1 and this file's table (mark the WP landed) in the same PR.
6. PRs reference this plan: `docs/plans/2026-06-12-agent-integration-handoff.md`.
