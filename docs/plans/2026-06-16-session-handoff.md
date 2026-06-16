# Session Handoff — 2026-06-16

**For:** the next agent (fresh or returning) picking up Trellis.
**State in one line:** the agent-integration program (all 13 work packages) is **merged, pushed, and fully green on CI** — including the live-infra suite against a rebuilt AuraDB instance. The next milestone is **Step 3: the quality/impact assessment.**

Read this top-to-bottom first. It is self-contained. Deeper references are linked inline.

---

## 1. Current state

- **Branch:** `main` at `d3e3444`, in sync with `origin/main`. Working tree clean.
- **CI: all six workflows green** on `d3e3444` — Tests (3.11/3.12/3.13), Lint, Type Check, OpenAPI, CodeQL, **Live infrastructure tests** (AuraDB + Neon).
- **Local gate:** `make lint && make typecheck && pytest tests/ -q` → green (~3881 passed). With all extras + live env, the live suites pass too.

### How to re-verify quickly
```bash
make lint && make typecheck && pytest tests/ -q     # default backends (SQLite)
```
The live (Neo4j/Postgres) suites are env-gated and skip cleanly without secrets. To run them locally you need a repo-root `.env` (see §4 — it does NOT currently exist; only `.env.example` does).

---

## 2. What landed (the agent-integration program)

The plan and per-package detail live in [`2026-06-12-agent-integration-handoff.md`](./2026-06-12-agent-integration-handoff.md) — all 13 packages marked ✅. Summary of capabilities now on `main`:

| Area | What an agent/operator can now do |
|---|---|
| **Onboard an MCP agent** | `trellis admin quickstart --with-skills user` — stores + MCP registration + the three behavior skills (installed from the wheel). Front door: [`docs/getting-started/integrate-your-agent.md`](../getting-started/integrate-your-agent.md). |
| **Onboard a non-MCP framework** | `trellis_sdk.hooks` — `ContextInjector` / `TraceRecorder` / `ResultFeedback` (never-raise contract). SDK is **HTTP-only**. |
| **Populate the graph from runs** | `TRELLIS_ENABLE_TRACE_EXTRACTION=1` → agent traces become PROV-aligned subgraphs; `trellis extract traces` backfills the existing corpus. |
| **Run the curation loops** | `trellis worker curate / tune / enrich / mine-precedents` (+ `--interval`). Operating runbook: [`docs/getting-started/running-trellis.md`](../getting-started/running-trellis.md); schedules: [`docs/deployment/scheduled-curation.md`](../deployment/scheduled-curation.md). |
| **Govern autonomy** | Tiered model in [`docs/design/adr-autonomy-ladder.md`](../design/adr-autonomy-ladder.md); tier-1 config-gated auto-promotion with auto-rollback (`learning.auto_promote`, default off). |
| **Human review in a UI** | Review-queue view + admin endpoints (`REVIEW_DECISION_RECORDED` audit). `trellis admin serve` → `/ui`. |
| **See improvement** | Metrics dashboard — `GET /api/v1/metrics/timeseries`, five metrics whose formulas match `eval/scenarios/_convergence_common.py`. |
| **Configure domains** | `classify.domain_keywords` config seeds custom domains (free strings); `trellis analyze domains` reports per-domain usage incl. a `(none)` coverage row. |
| **Deploy via Docker** | `docs/deployment/local-compose.md` — verified working (Postgres+pgvector, all probes, REST round-trip). |

### Reusable helpers created in review — use these, don't re-inline
- `trellis.learning.submit_learning_promotion` — the single governed write path for reviewed learning promotions (CLI + API).
- `trellis.learning.pack_observations.join_pack_feedback` — canonical `PACK_ASSEMBLED ⋈ FEEDBACK_RECORDED` join.
- `trellis.extract.trace_ingest_hook.extract_trace_batch` — shared trace→graph extraction core (live hook + backfill).
- `trellis_cli.output.emit_json` — CLI JSON emission.

---

## 3. The next milestone — Step 3: quality / impact assessment

**Goal:** measure whether Trellis actually makes agents better — the question the whole program was built to answer.

The instruments are in place and **definitionally aligned**:
- `eval/scenarios/` convergence scenarios (`agent_loop_convergence`, `agent_loop_convergence_real_llm`, `program_convergence`, …) compute pack success rate, reference rate, advisory suppression, etc. in `eval/scenarios/_convergence_common.py`.
- The **metrics dashboard** (`GET /api/v1/metrics/timeseries`, served at `/ui` via `trellis admin serve`) reads the *same* formulas — so dashboard numbers and eval numbers are the same numbers by construction.

**Suggested first moves for Step 3:**
1. Read [`docs/agent-guide/pack-quality-evaluation.md`](../agent-guide/pack-quality-evaluation.md) and `eval/scenarios/_example/`.
2. Run a convergence scenario end-to-end and confirm the dashboard renders the same trend it reports.
3. Decide the assessment's claims (does retrieval lift success rate over N rounds? does curation demote noise measurably? does the promote loop add durable value?) and which scenarios + real-LLM runs substantiate each.

No code is blocking Step 3.

---

## 4. Operational facts & gotchas (hard-won — don't rediscover)

### AuraDB live-test instance (rebuilt 2026-06-16)
- Old instance was **deleted** (not paused); recreated as **Instance01**, ID/host/user/db all = `3760dbe7`. Aura project `1d05fcb0-c53d-468d-ac3b-c420ab950786`.
- Connection: URI `neo4j+s://3760dbe7.databases.neo4j.io`, **user `3760dbe7`** (Aura **Free uses the instance-ID as the DB username**, NOT `neo4j` — this was the silent breaker), database `3760dbe7`.
- The four `TRELLIS_TEST_NEO4J_*` GitHub Actions secrets were updated to these values (2026-06-16). The password is in those secrets and in the user's downloaded creds `.txt` — **not stored in the repo or memory.**
- **Aura facts:** API credentials (Client ID + Secret, OAuth at `api.neo4j.io/oauth/token`) are *management* creds and cannot open a Bolt connection. Aura has **no in-place password reset**; the console Query session is a restricted SSO principal that can't `ALTER USER`/`CREATE USER` — a lost DB password means **recreate the instance**. Free tier = 1 instance. The create dialog **defaults to the paid "Professional free-trial" tier** — you must explicitly pick **Free** ($0/hour).

### CI reproduction
- **Reproduce CI-only failures in a container:** `docker run --rm -e CI=true -e GITHUB_ACTIONS=true python:3.11-slim` → `pip install -e ".[dev,vectors]"` → `pytest tests/ -v`. Several failures only manifest with CI's empty `HOME` (no `~/.config/trellis`).
- **click 8.2+:** `CliRunner.result.output` is interleaved stdout+stderr. CLI tests parsing JSON must use `result.stdout`. Error/warning text must go to stderr (`Console(stderr=True)`).
- **Live-infra workflow** needs `TRELLIS_TEST_LIVE` + `TRELLIS_TEST_SLOW` + `TRELLIS_TEST_NEO` + `TRELLIS_TEST_POSTGRES` toggles (the loop suites are marked `live+slow+neo4j+postgres`); it's dispatchable via `gh workflow run live-infra.yml --ref main`.
- **OpenAPI** check: regenerate with `make openapi` and commit `docs/api/v1.yaml` whenever routes change.

### Backend divergence (caused the last bug)
- The **SQLite graph store silently allows dangling edges; the Bolt backends (Neo4j, ArcadeDB) reject them** ("source/target has no current version"). Any code writing an edge must ensure both endpoints are materialized nodes. Fixed in `meta/recorder.py` `produced_finding` (`d3e3444`).

---

## 5. Open follow-ups (none blocking)

| Item | Notes |
|---|---|
| **Rotate the AuraDB DB password** | It passed through a chat transcript during the rebuild. Instance is fresh, so low urgency. After rotating, update the `TRELLIS_TEST_NEO4J_PASSWORD` secret. |
| **Delete unused Aura API credentials** | Client IDs `985676d4` / `d664924e` are not used by anything now — widen credential surface for no benefit. |
| **`consumed_event` latent bug** | Sibling of the fixed `produced_finding` issue: it writes an edge to an `event_id` (an EventLog event, not a graph node). Same dangling-edge shape; **not exercised by current live tests** so it doesn't fail today, but would on Neo4j if hit. Fix with the same materialize-or-skip discipline if/when a path triggers it. |
| **Two inline join copies in `effectiveness.py`** | `analyze_effectiveness` / `analyze_advisory_effectiveness` still inline the pack⋈feedback join that's now canonical as `join_pack_feedback`. Deferred cleanup (touches stable analytics — not worth the re-test churn unless you're already in there). |
| **Recreate local `.env`** | Gone (only `.env.example`). Needed only to run the env-gated live suites locally; CI has the secrets. |
| **AuraDB Free auto-deletes after 30 days idle** | If live-infra goes red with DNS/`gaierror` again, the instance lapsed — recreate per §4 and update the four secrets. |

---

## 6. Memory & references

- Persistent memory: `memory/agent-integration-handoff-2026-06.md` (the full record, including all the above gotchas) and `memory/MEMORY.md` (index).
- Project conventions / hard rules: [`CLAUDE.md`](../../CLAUDE.md).
- Long-running roadmap: [`docs/design/implementation-roadmap.md`](../design/implementation-roadmap.md) (older; this handoff supersedes its "state" for the integration program).
- The integration plan with per-WP detail: [`2026-06-12-agent-integration-handoff.md`](./2026-06-12-agent-integration-handoff.md).
