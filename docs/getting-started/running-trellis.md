# Running Trellis server-side

> **Who this is for:** anyone who runs Trellis as a *service* — the REST API + UI, plus the unattended curation/learning workers — rather than as a local SDK or one-shot CLI. This is **the** operating runbook for everything that runs server-side.

> **What this covers:** every long-running or scheduled process Trellis ships (`trellis admin serve` and each `trellis worker` command), the autonomy tier each one operates under, exactly which decisions wait for a human, and a minimal single-host setup with a verification checklist.

> **What this is not:** a backend-selection guide (see [`../deployment/recommended-config.yaml`](../deployment/recommended-config.yaml)), a scheduler cookbook (see [`../deployment/scheduled-curation.md`](../deployment/scheduled-curation.md) for crontab / systemd / GHA / K8s recipes), or the full CLI reference ([`../agent-guide/operations.md`](../agent-guide/operations.md)).

---

## The two kinds of process

Trellis server-side splits cleanly in two:

1. **The API + UI** — `trellis admin serve`. One long-lived HTTP process. Agents read/write the substrate over REST; humans browse it via `/ui`.
2. **The workers** — `trellis worker <command>`. Periodic batch jobs that keep the substrate healthy: curate, tune, enrich, mine-precedents. None of them is a daemon by default; you run them on a schedule (or, for `curate`, with a built-in `--interval` loop).

Everything below maps each process to its **autonomy tier** from [`../design/adr-autonomy-ladder.md`](../design/adr-autonomy-ladder.md). The ladder's one-line rule: **tier = f(reversibility, blast radius)**, never confidence. That is what tells you which loops run unattended and which prepare an artifact for a human.

| Tier | What it means here | Server-side example |
|---|---|---|
| **0** Fully automatic | Trivially reversible, one data-plane item, retrieval-shaping only | `worker curate`'s noise-tagging stage |
| **1** Automatic with auto-rollback | Reversible through versioned state; degradation is monitored and unwound | `worker tune` *when* `auto_promote.enabled` |
| **2** Human-gated, machine-prepared | Crosses into the shared graph; the human owns the irreversible step | `worker curate`'s learning-candidate artifacts → `curate promote-learning` |
| **3** Never automated | Irreversible by construction; no machine write path exists | `well_known.py` ontology promotion |

---

## `trellis admin serve` — the API + UI

```bash
trellis admin serve [--host 127.0.0.1] [--port 8420]
```

Starts the Trellis REST API and the static dashboard UI in one uvicorn process. Agents hit `/api/v1/...` (search, packs, ingest, mutations); operators open `/ui` in a browser. The same `StoreRegistry` backs both, so the API and UI always see the same data the CLI does. This process holds no autonomy tier — it serves reads and **governed** writes (every mutation still flows through the `MutationExecutor`); it never curates, tunes, or promotes on its own.

> There are two entry points for the same server: `trellis admin serve` (shown here) and `trellis serve` (a thin container-ENTRYPOINT wrapper that also accepts `--config-dir`). Use whichever fits your deployment; they start the same app.

### Ports and bind address

| | Default | Notes |
|---|---|---|
| Port | `8420` | `--port` or `TRELLIS_API_PORT`. |
| Bind address | `127.0.0.1` (loopback) | Loopback-only on a fresh install so the API isn't exposed before you've configured auth. For containers that must listen on the pod IP, pass `--host 0.0.0.0` or set `TRELLIS_API_HOST=0.0.0.0`. |

### Authentication (env-driven)

The API authenticates with scoped API keys. The full matrix lives in [`../agent-guide/operations.md`](../agent-guide/operations.md#api-authentication); the env vars you set at deploy time:

| Env var | Values | Default | Effect |
|---|---|---|---|
| `TRELLIS_AUTH_MODE` | `off` / `optional` / `required` | inferred | `required` if `TRELLIS_API_KEY` is set, else `off`. `required` rejects missing/invalid credentials with 401; `off`/`optional` log a loud startup warning. An invalid value crashes at startup — no silent fallback. |
| `TRELLIS_API_KEY` | string | unset | Legacy shared secret: a token matching it gets all scopes. Setting it flips the inferred mode to `required`. |

Mint scoped keys with `trellis admin api-keys create --name <n> --scopes read,ingest,mutate,admin`. The token is printed **once** and never stored (only its SHA-256).

### UI and ops-endpoint gating

Three more env toggles control what an unauthenticated caller can reach. All three validate at startup — an unrecognized value crashes `create_app` rather than guessing.

| Env var | Values | Default | Effect |
|---|---|---|---|
| `TRELLIS_UI_ENABLED` | `true` / `false` | `true` | `false`: `/ui` is not mounted; `/` redirects to `/api/version` instead of `/ui/`. Turn the UI off on headless API-only deployments. |
| `TRELLIS_OPS_DETAIL` | `authenticated` / `public` | `authenticated` | Who sees the per-backend `/readyz` breakdown (backend names, latencies, raw errors). The `{"status": ...}` line is always public, so orchestrator probes need zero credentials. |
| `TRELLIS_METRICS_PUBLIC` | `true` / `false` | `true` | `false`: `/metrics` requires a valid credential. `true` keeps it open for credential-less Prometheus scrapes. |

`/healthz`, `/readyz`, `/api/version`, and `/metrics` stay reachable without a credential by default — point your orchestrator's liveness/readiness probes at `/healthz` and `/readyz`.

**What it writes/emits:** nothing on its own. It serves reads and routes agent mutations through the governed pipeline (which emits the normal `MUTATION_EXECUTED` / domain events).

---

## `trellis worker curate` — the curation cycle (Tier 0 + Tier 2)

```bash
trellis worker curate --output-dir DIR [--days 30] [--interval SECONDS] \
  [--dry-run] [--reconcile-first] \
  [--skip-noise-tags] [--skip-advisories] [--skip-learning] \
  [--format text|json]
```

One full curation cycle. It calls the curation library functions directly — no shelling out — in a fixed order: **(1)** effectiveness feedback (demote: noise-tag low-value items), **(2)** advisory generation, **(3)** advisory fitness loop (adjust confidence / suppress weak advisories), **(4)** learning-candidate scoring → review artifacts. This single command spans **two tiers**: stages 1–3 are Tier 0 (fully automatic, reversible re-tagging) and run unattended; stage 4 is **Tier 2** — it only *prepares* promotion artifacts and **never promotes**.

| Flag | Default | Purpose |
|---|---|---|
| `--output-dir` / `-o` | required | Directory the stage-4 review artifacts land in. |
| `--days` | `30` | Days of EventLog history to scan. |
| `--interval` | off | Loop mode: re-run every N seconds until SIGINT/SIGTERM. Plain sleep — **no scheduler dependency**. |
| `--dry-run` | off | Analyze only — no noise tags, no advisory mutations, no artifacts. |
| `--reconcile-first` | off | Backfill `pack_feedback.jsonl` into the EventLog before the cycle (see below). |
| `--skip-noise-tags` / `--skip-advisories` / `--skip-learning` | off | Skip stage 1 / stages 2-3 / stage 4. Run just the demote half with `--skip-advisories --skip-learning`. |
| `--format` | `text` | `text` or `json`. |

**What it writes/emits.** Stage 1 applies `signal_quality="noise"` tags (a governed mutation, reversible by re-tagging) and emits the corresponding events. Stages 2-3 mutate the advisory store. Stage 4 writes two files into `--output-dir` and emits **nothing into the graph**:

- `intent_learning_candidates.json` — the scored candidates.
- `promotion_decisions.template.json` — the human-review template.

Verified shape (`--format json`, demo data, isolated config dir):

```json
{"status": "ok", "noise_tagged": 0, "advisories_generated": 0, "advisories_suppressed": 0,
 "advisories_boosted": 0, "learning_observations": 0, "learning_candidates": 0,
 "candidates_path": "/path/review/intent_learning_candidates.json",
 "decisions_path": "/path/review/promotion_decisions.template.json",
 "skipped_stages": [], "dry_run": false}
```

In `--dry-run`, advisories are skipped wholesale (`"skipped_stages": ["advisories"]`) and both `*_path` fields are `null` — the analysis still runs and reports counts, but nothing is written:

```json
{"status": "ok", "noise_tagged": 0, ..., "candidates_path": null, "decisions_path": null,
 "skipped_stages": ["advisories"], "dry_run": true}
```

In `--interval` mode each cycle logs one structured `worker_curate.cycle` line with the headline counts; SIGINT/SIGTERM drains the current cycle and exits cleanly (no half-written artifact).

> If the cycle warns `learning.parameter_registry.seeded_defaults` on stderr, run `trellis admin init-learning-params` once to seed `learning_params.yaml`. The warning is advisory — the cycle still runs on built-in defaults — and never pollutes the `--format json` stdout payload.

---

## `trellis worker tune` — parameter auto-promotion (Tier 1)

```bash
trellis worker tune [--tuner-name rule_tuner] [--since-days N] [--dry-run] [--format text|json]
```

Runs one `RuleTuner` pass. **Default behaviour is a pure tuner pass** — byte-identical to `trellis metrics tune`: it produces/refreshes proposals and promotes nothing. This is **Tier 1**, and Tier 1 is **opt-in, global default OFF**. Only when you add a `learning.auto_promote` block with `enabled: true` does it auto-promote qualifying proposals through the *same* governance pipeline `trellis metrics promote --commit` uses — and it arms post-promotion monitoring so degradation auto-rolls-back.

| Flag | Default | Purpose |
|---|---|---|
| `--tuner-name` | `rule_tuner` | Logical tuner name (cursor + proposal scope). |
| `--since-days` | cursor | Force a rescan of the last N days, ignoring the tuner cursor. |
| `--dry-run` | off | Report what *would* auto-promote without mutating or emitting. |
| `--format` | `text` | `text` or `json`. |

Verified shape with auto-promote disabled (the default), demo data:

```json
{"enabled": false, "dry_run": true, "proposals_considered": 0, "auto_promoted": 0,
 "rolled_back": 0, "pending_manual": 0, "outcomes": [], "tuner_name": "rule_tuner"}
```

`"enabled": false` is the tell that Tier-1 autonomy is off — the command did a tuner pass and promoted nothing.

### Enabling Tier-1 auto-promotion

Add to `$TRELLIS_CONFIG_DIR/config.yaml` (default `~/.trellis/config.yaml`):

```yaml
learning:
  auto_promote:
    enabled: true              # default false — master switch, global default OFF
    min_sample_size: 30        # stricter than manual promote (5); must be >= 5
    min_effect_size: 0.25      # stricter than manual promote (0.15); must be >= 0.15
    require_baseline: true     # no baseline => nothing to roll back to => left for a human
    post_min_samples: 20       # min post-promotion outcomes before a degradation verdict
    post_regression_threshold: 0.10  # success-rate drop (abs) that triggers auto-rollback
    post_lookback_days: 7      # monitoring window
```

The auto thresholds **must be at least as strict as the manual-promote defaults** — the loader rejects looser values loudly. Monitoring is always armed; you cannot auto-promote without an armed rollback (Tier-1 invariant (b)).

**What it writes/emits.** With auto-promote off: only the tuner's own proposals/events. With it on, each autonomous action emits a **dedicated, self-identifying** event on top of the normal governance event:

| Event | Emitted when |
|---|---|
| `PARAMS_AUTO_PROMOTED` | A qualifying proposal is auto-promoted (alongside `PARAMS_UPDATED`). |
| `PARAMS_AUTO_ROLLED_BACK` | Post-promotion monitoring demotes a degraded snapshot (alongside the rollback's `PARAMS_UPDATED`). |

These distinguish "a human promoted this" from "the system promoted this on its own." Non-qualifying proposals stay `pending` for manual review via `trellis metrics promote` — reported, never rejected.

---

## `trellis worker enrich` — batch LLM tagging (requires an LLM)

```bash
trellis worker enrich [--concurrency 3] [--limit 50] \
  [--confidence-threshold 0.5] [--dry-run] [--format text|json]
```

Batch-enriches under-tagged documents via the LLM `EnrichmentService`, writing the suggested tags / classification / importance back into each document's `metadata.content_tags`. A document is a candidate when its `content_tags` is missing/empty, **or** it has no `tag_confidence` stamp, **or** that stamp is below `--confidence-threshold`.

This is a data-plane tagging pass (re-running it overwrites tags — reversible), not a tiered self-action; it has no autonomy tier of its own. It does, however, **require a configured, buildable LLM client** — and it checks for one **before** the dry-run branch, so even `worker enrich --dry-run` exits non-zero with an actionable message when no `llm:` block + matching `[llm-openai]` / `[llm-anthropic]` extra is present. It never silently no-ops. Run it off-peak (LLM calls cost money and time).

**What it writes/emits:** updated `metadata.content_tags` (with fresh `classified_at` / `tag_confidence` stamps) via `DocumentStore.put`, and enrichment events through the `EnrichmentService`'s event log.

---

## `trellis worker mine-precedents` — precedent candidates (requires an LLM)

```bash
trellis worker mine-precedents [--domain D] [--min-traces 3] \
  [--limit 100] [--dry-run] [--format text|json]
```

Mines precedent candidates from failure/partial traces (wraps `PrecedentMiner.generate_precedent_candidates`). Candidates are **surfaced** (the miner emits `PRECEDENT_PROMOTED` events as it intends) but **not auto-promoted** into the graph — review before acting. `--dry-run` reports how many failure/partial traces are in scope without calling the LLM. Like `enrich`, it requires a configured LLM client and exits loudly when none is present.

**What it writes/emits:** precedent candidates and the miner's `PRECEDENT_PROMOTED` events.

---

## Where the human is in the loop

Three server-side decisions **wait for a human**. The workers prepare the artifact; a person inspects it and runs the commit command. Here is exactly where each lands.

| Decision | Prepared by | Artifact / input | Human runs | Tier |
|---|---|---|---|---|
| **Promote a learning candidate** into a precedent node | `worker curate` stage 4 | `intent_learning_candidates.json` + `promotion_decisions.template.json` in `--output-dir` | Set `approved: true` on rows in the decisions file, then `trellis curate promote-learning --candidates intent_learning_candidates.json --decisions <filled-in>.json` | 2 |
| **Promote a tuner parameter proposal** (when auto-promote is OFF, or for proposals below the auto gate) | `worker tune` / `trellis metrics tune` | a `pending` proposal | `trellis metrics promote <PROPOSAL_ID> --commit` (dry-run by default; `--commit` writes the snapshot) | 1→manual |
| **Promote an open-string type** to the canonical ontology | `trellis analyze schema-evolution` | a `WELL_KNOWN_CANDIDATE` event / `candidate_id` | `trellis admin draft-promotion-adr <candidate_id>` to scaffold an ADR amendment, then a human authors + merges it | 3 |

The pattern is identical each time: **the machine prepares, the human commits.** `worker curate` writes a decisions *template* and stops; `worker tune` (auto-off) leaves proposals `pending`; `analyze schema-evolution` only emits a candidate event. None of them crosses the irreversible line on its own.

> **Tier 3 is enforced by absence, not a flag.** There is no machine write path to `well_known.py` — `draft-promotion-adr` scaffolds a document for a human to author and merge. No worker can `git push origin main`.

---

## Minimal single-host setup

The smallest real deployment is two processes on one host: the API/UI in one, a curation loop in the other. SQLite stores are fine for a single host; switch backends per [`../deployment/recommended-config.yaml`](../deployment/recommended-config.yaml) when you outgrow it.

```bash
# One-time setup
trellis admin init                 # creates config.yaml + SQLite stores
trellis admin init-learning-params # seed learning thresholds (silences the curate warning)
```

**Process 1 — the API + UI** (tmux pane, systemd service, or container):

```bash
TRELLIS_AUTH_MODE=required TRELLIS_API_KEY=$(op read 'op://...') \
  trellis admin serve --host 0.0.0.0 --port 8420
```

**Process 2 — the curation loop** (a second tmux pane or systemd service). Once a day, reconcile the feedback log first, then curate:

```bash
trellis worker curate --output-dir /var/lib/trellis/review \
  --reconcile-first --interval 86400 --format json
```

`--interval 86400` re-runs the cycle every 24h with a plain sleep — no scheduler. `--reconcile-first` backfills any file-only `pack_feedback.jsonl` rows into the EventLog before each cycle so the curate pass never misses feedback. For a real scheduler (cron / systemd timer / GHA / K8s CronJob) instead of `--interval`, use the recipes in [`../deployment/scheduled-curation.md`](../deployment/scheduled-curation.md).

### Verification checklist

After both processes are up, confirm the substrate is live and the workers run clean. Use an isolated config dir while testing so you don't touch production data:

```bash
export TRELLIS_CONFIG_DIR=/tmp/trellis-check/config
export TRELLIS_DATA_DIR=/tmp/trellis-check/data
trellis admin init && trellis demo load          # disposable sandbox

# 1. API is serving and healthy
curl -fsS http://localhost:8420/healthz          # liveness — no credential needed
curl -fsS http://localhost:8420/readyz           # readiness — backend status

# 2. Curate runs clean (dry-run — touches nothing)
trellis worker curate --output-dir /tmp/trellis-check/review --dry-run --format json
#   expect: {"status": "ok", ..., "candidates_path": null, "dry_run": true}

# 3. Tune is a no-op when auto-promote is OFF (the default)
trellis worker tune --dry-run --format json
#   expect: {"enabled": false, ..., "auto_promoted": 0}

# 4. Feedback reconciliation is wired
trellis admin reconcile-feedback --log-dir /tmp/trellis-check/data --dry-run --format json
#   expect: {"status": "ok", "dry_run": true, "scanned": N, "would_emit": M}

# 5. A real curate cycle writes the two human-review artifacts
trellis worker curate --output-dir /tmp/trellis-check/review --format json
ls /tmp/trellis-check/review/
#   expect: intent_learning_candidates.json  promotion_decisions.template.json
```

If step 5's two files exist, the human-in-the-loop handoff is correctly wired: a person reviews `promotion_decisions.template.json`, approves rows, and runs `trellis curate promote-learning`.

---

## Further reading

- [`../deployment/scheduled-curation.md`](../deployment/scheduled-curation.md) — crontab / systemd / GitHub Actions / K8s CronJob recipes and a recommended-cadence table.
- [`../design/adr-autonomy-ladder.md`](../design/adr-autonomy-ladder.md) — the four-tier model these processes operate under.
- [`../agent-guide/operations.md`](../agent-guide/operations.md) — full CLI / REST / MCP reference, including the complete auth matrix.
- [`../agent-guide/freshness-and-curation.md`](../agent-guide/freshness-and-curation.md) — the extraction/refresh half of keeping a deployment current.
- [`../deployment/recommended-config.yaml`](../deployment/recommended-config.yaml) — the four blessed backend shapes.
</content>
</invoke>
