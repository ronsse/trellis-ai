# Trellis operator runbook

The cross-cutting reference for running Trellis in production: every
env var the API and CLI honour, what the probes return and how to
read them, how to scrape `/metrics`, how to size the Postgres pool,
and how to schedule the three closed feedback loops.

Pairs with the infrastructure-specific guides in this folder
([aws-ecs.md](aws-ecs.md), [local-compose.md](local-compose.md),
[neo4j-local.md](neo4j-local.md),
[neo4j-self-hosted-cloud.md](neo4j-self-hosted-cloud.md),
[neo4j-auradb.md](neo4j-auradb.md)) — those describe *where* Trellis
runs; this one describes *how* to operate it once it's running.

## Moving an existing SQLite deployment

Changing a store's `backend` in `config.yaml` points Trellis at the new
store; it does not copy what the old one held. The one migration tool is
`trellis admin migrate-graph`, and it copies **current graph rows** only:
closed SCD-2 versions are not copied (the run counts them), timestamps are
not preserved (the destination sets them when it writes each row), and an
edge or alias attached to an id with no current node is not copied (the run
counts those edges; aliases have no count). A re-run adds missing rows and
does not update or remove existing ones, and `--dry-run` still creates the
destination database. Documents, traces, the event log, vectors, blobs and
feedback are not moved by it and have no migration tool, so data already in
them stays in the SQLite files or the local blob directory. Details, the
JSON shape and the exit codes:
[`trellis admin migrate-graph`](../agent-guide/operations.md#trellis-admin-migrate-graph).

## Environment variables

`config.yaml` selects each store's backend (see
[recommended-config.yaml](recommended-config.yaml)); no variable
below selects one.

The two `*_PG_DSN` variables supply a DSN; they do not choose a backend. A store
reads one only when its `backend` is `postgres` or `pgvector` and it sets no `dsn`
of its own. If a variable is set and no store of that plane uses either backend,
the registry logs a `registry_plane_dsn_unused` warning (plane, variable and file,
never the value). `config.yaml` itself is closed: an unknown top-level key, a store
name the plane does not have, a store under the wrong plane, or the old flat
`stores:` block raises `ConfigError` (CLI exit 5, API startup refused) rather than
falling back to SQLite.

The minimum set to bring up the API against Postgres + Neo4j:

| Variable                       | Required? | Purpose                                                                  |
|--------------------------------|-----------|--------------------------------------------------------------------------|
| `TRELLIS_AUTH_MODE`            | **Yes** in prod | Set `required`. `off` and `optional` serve a caller with no credential every scope. See [Authentication](#authentication). |
| `TRELLIS_KNOWLEDGE_PG_DSN`     | Yes       | DSN for the knowledge plane (graph / vector / document).                 |
| `TRELLIS_OPERATIONAL_PG_DSN`   | Yes       | DSN for the operational plane (trace / event_log).                       |
| `TRELLIS_API_HOST`             | No        | Bind address. **Defaults to `127.0.0.1`** — set `0.0.0.0` for containers, which the API then refuses to start on unless `TRELLIS_AUTH_MODE=required`. `trellis serve`, `trellis admin serve` and `trellis-api` all read it; the first two take `--host`, which beats it. |
| `TRELLIS_API_PORT`             | No        | Default `8420`. Read by the same three entry points; `--port` beats it on the first two. |
| `TRELLIS_API_KEY`              | No        | Legacy shared secret: a token equal to it gets every scope. Prefer scoped keys. |
| `TRELLIS_CONFIG_DIR`           | No        | Path to `config.yaml`. Defaults to `~/.trellis`.                         |

Backend-specific:

| Variable                       | When                                    |
|--------------------------------|-----------------------------------------|
| `TRELLIS_S3_BUCKET`            | Blob backend = S3.                       |
| `TRELLIS_NEO4J_URI`            | Graph backend = Neo4j.                   |
| `TRELLIS_NEO4J_USER`           | Graph backend = Neo4j.                   |
| `TRELLIS_NEO4J_PASSWORD`       | Graph backend = Neo4j.                   |
| `TRELLIS_NEO4J_DATABASE`       | Database name. Self-hosted defaults to `neo4j`. AuraDB-specific: set to the instance ID. |
| `OPENAI_API_KEY`               | Running EnrichmentService or LLM-tier extractors. |
| `ANTHROPIC_API_KEY`            | Same — alternative provider.             |

Observability (no-op unless set, even with the `[observability]` extra
installed):

| Variable                              | Purpose                                          |
|---------------------------------------|--------------------------------------------------|
| `OTEL_EXPORTER_OTLP_ENDPOINT`         | OTLP collector URL. Without it OTel is no-op.    |
| `OTEL_SERVICE_NAME`                   | Service tag in spans. Default service name applies otherwise. |
| `TRELLIS_DISABLE_OBSERVABILITY=1`     | Hard-disable both OTel + Prometheus even when the extras are installed. |

Pool sizing (covered in detail below):

| Variable                       | Default | Purpose                                |
|--------------------------------|---------|----------------------------------------|
| `TRELLIS_PG_POOL_MIN_SIZE`     | 2       | Minimum live connections per Postgres store. |
| `TRELLIS_PG_POOL_MAX_SIZE`     | 20      | Maximum live connections per Postgres store. |

## Authentication

`TRELLIS_AUTH_MODE` sets the posture for every `/api/v1` route:

| Mode | A request with no credential | A credential that is present |
|------|------------------------------|------------------------------|
| `required` | 401 | checked, then limited to the key's scopes |
| `optional` | served with every scope | invalid is 401, valid is limited to its scopes |
| `off` | served with every scope | ignored, served with every scope |

Unset, the mode is inferred: `required` when `TRELLIS_API_KEY` is set,
otherwise `off`. An unrecognised value stops startup rather than picking a
posture. The API refuses to bind a non-loopback address (`TRELLIS_API_HOST=0.0.0.0`,
a container's default `--host`) unless the mode is `required`; the refusal is a
`ConfigError` (exit 5) that names its two ways out, and
`TRELLIS_API_ALLOW_INSECURE_BIND=1` is the second, for a listener that really is
on a trusted interface. The full matrix and the router-to-scope map are in
[operations.md](../agent-guide/operations.md#api-authentication).

From a fresh install to an enforced API and a working key:

```bash
trellis admin init

# One key per caller. The token prints once; only its SHA-256 is stored.
# List every scope the caller needs: only `admin` implies the others.
trellis admin api-keys create --name agent --scopes read,ingest,mutate --format json
trellis admin api-keys create --name operator --scopes admin --format json

TRELLIS_AUTH_MODE=required trellis admin serve

# In another shell, with a token from above in $TRELLIS_ADMIN_KEY:
curl -H "X-API-Key: $TRELLIS_ADMIN_KEY" http://127.0.0.1:8420/api/v1/advisories
curl -H "Authorization: Bearer $TRELLIS_ADMIN_KEY" http://127.0.0.1:8420/api/v1/advisories
```

`X-API-Key` wins when both headers are sent. A wrong or missing credential is
an undifferentiated 401; a valid key without the scope the route needs is a 403.
Keys live in the operational store, so `api-keys` and the API must read the same
`config.yaml`: the same `TRELLIS_CONFIG_DIR`, and on Postgres the same
`TRELLIS_OPERATIONAL_PG_DSN`. List keys with `trellis admin api-keys list` and
revoke one with `trellis admin api-keys revoke <key_id>`; a revoked key is
refused by the running API on its next request, with no restart.

The loops below call `/api/v1/advisories/generate` and
`/api/v1/effectiveness/apply-noise-tags`, which need an `admin` key, as does
the `/api/v1/advisories` check under [Validating a deployment](#validating-a-deployment).

Two things to avoid:

- **Do not put a scoped token in `TRELLIS_API_KEY`** where the API process can
  read it. That variable is the legacy shared secret, and the API checks it
  first: a token equal to it is granted every scope, so a `read` key exported
  there acts as `admin`. The examples here call the client-side variable
  `TRELLIS_ADMIN_KEY`.
- **Do not rely on the probes for access control.** `/healthz`, `/readyz` and
  `/api/version` answer without a credential so orchestrator probes work;
  in `required` mode `/readyz` shows its per-backend breakdown only to a valid
  credential unless `TRELLIS_OPS_DETAIL=public`. `/metrics` is open to a scraper by default, and
  `TRELLIS_METRICS_PUBLIC=false` makes it require a credential. `TRELLIS_UI_ENABLED=false`
  stops the API mounting `/ui`. See
  [Production exposure](../agent-guide/operations.md#production-exposure).

## Probes

Two endpoints, both **outside** `/api/v1` and **unauthenticated**:

### `/healthz` — liveness

Returns 200 whenever the process is up. Never touches stores. Wire to
the orchestrator's liveness check (k8s `livenessProbe`, ECS health check).

```json
{ "status": "ok" }
```

### `/readyz` — readiness

Returns 200 only when the StoreRegistry is initialized **and** every
cloud backend probes cleanly. Wire to the orchestrator's readiness
check so traffic doesn't get routed to a pod whose Postgres pool
hasn't connected yet.

The `backends` block below is shown to a caller holding a valid credential, and
to everyone when auth is `off` or `TRELLIS_OPS_DETAIL=public`; an anonymous
caller on a `required` deployment gets only the `status` line, with the same
status code. An orchestrator probe needs no credential either way.

Probes four backends — each runs the cheapest possible round-trip
(`count()` for stores, `count_nodes()` for graph) — and reports
per-backend status + latency. A single failure flips the whole probe
to 503 but the body stays informative:

```json
{
  "status": "ready",
  "backends": {
    "event_log":      { "status": "ok", "latency_ms": 4.21 },
    "graph_store":    { "status": "ok", "latency_ms": 12.30 },
    "vector_store":   { "status": "ok", "latency_ms": 3.05 },
    "document_store": { "status": "ok", "latency_ms": 2.81 }
  }
}
```

503 with one backend down:

```json
{
  "status": "degraded",
  "backends": {
    "event_log":      { "status": "ok", "latency_ms": 4.21 },
    "graph_store":    { "status": "degraded", "latency_ms": 30021.4,
                        "error": "ServiceUnavailable: connection timed out" },
    "vector_store":   { "status": "ok", "latency_ms": 3.05 },
    "document_store": { "status": "ok", "latency_ms": 2.81 }
  }
}
```

503 during startup, before the lifespan finishes constructing the registry:

```json
{ "status": "initializing" }
```

**Operator reading guide.** Latency rising on the same backend across
successive probes is a leading indicator — pool exhaustion typically
shows as `event_log` and `document_store` latency creeping up while
`graph_store` (different connection) stays flat.

## Metrics

`GET /metrics` is wired by the `[observability]` optional extra and
exposes Prometheus-format scrape data. Install with:

```bash
pip install 'trellis-ai[observability]'
```

Out of the box:

* per-route HTTP latency histograms + counters
  (`prometheus-fastapi-instrumentator`)
* OpenTelemetry FastAPI auto-instrumentation (per-request spans)
* psycopg auto-instrumentation (per-statement spans + connection metrics)

The endpoint is unauthenticated by design — Prometheus servers and
k8s ServiceMonitors need it open.

### Prometheus scrape config

```yaml
scrape_configs:
  - job_name: trellis-api
    metrics_path: /metrics
    scrape_interval: 15s
    static_configs:
      - targets: ['trellis-api.internal:8420']
```

### Kubernetes ServiceMonitor

```yaml
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: trellis-api
spec:
  selector:
    matchLabels: { app: trellis-api }
  endpoints:
    - port: http
      path: /metrics
      interval: 15s
```

### Disabling observability

Set `TRELLIS_DISABLE_OBSERVABILITY=1` to skip wiring even when the
extras are installed (useful when one container in a fleet exports to
a different collector). Without `OTEL_EXPORTER_OTLP_ENDPOINT` set the
OTel pipeline is already no-op, so the disable flag only matters when
you've wired an exporter you want to bypass for this deploy.

## Pool sizing

Every Postgres-backed store (`PostgresTraceStore`,
`PostgresDocumentStore`, `PostgresGraphStore`, `PostgresEventLog`,
`PgVectorStore`) owns its own `psycopg_pool.ConnectionPool`. Pools are
**per-store, per-process** — five stores × N processes = the
connection budget you need to provision against on the Postgres side.

Defaults are sized for one uvicorn worker against a small managed
Postgres (a few GB RAM, ~100 max connections at the DB):

```
TRELLIS_PG_POOL_MIN_SIZE=2     # idle connections per store
TRELLIS_PG_POOL_MAX_SIZE=20    # ceiling per store
```

| Workload                                  | Suggested config                                              |
|-------------------------------------------|---------------------------------------------------------------|
| Single uvicorn worker, dev / staging      | Defaults (2 / 20).                                            |
| Multi-worker prod (4 workers × 5 stores)  | `MIN_SIZE=2`, `MAX_SIZE=10` → max 200 connections at the DB.   |
| Heavy concurrent ingestion                | Raise `MAX_SIZE` after watching `pg_stat_activity` saturate.  |
| Tiny RDS / Neon free tier                 | `MIN_SIZE=1`, `MAX_SIZE=5`. Five stores × 5 = 25 connections. |

Garbage values (`MIN_SIZE > MAX_SIZE`, non-numeric, zero) silently
fall back to the defaults rather than producing a malformed pool.
Each pool logs `pg_pool_opened` with its actual `min_size` /
`max_size` at startup so the live config is verifiable in logs.

## Closed feedback loops

Trellis runs three cooperating loops on top of the EventLog. All
three are idempotent and safe to schedule on independent cadences.
Reconciliation is a separate **recovery operation**, not a steady-state
loop — see [Recovery operations](#recovery-operations) below.

> **One command for all three.** `trellis worker curate` runs the
> advisory, noise-demote, and learning-candidate stages in a single
> governed cycle (and `--reconcile-first` folds in the reconciliation
> step below). For the worker-command operating model — each process,
> its autonomy tier, and the human-in-the-loop steps — read
> [`../getting-started/running-trellis.md`](../getting-started/running-trellis.md).
> For scheduler recipes and a recommended-cadence table, see
> [`scheduled-curation.md`](scheduled-curation.md). The per-loop
> commands below remain valid for operators who want to schedule the
> stages independently.

| Loop                | Surface                                              | Suggested cadence       |
|---------------------|------------------------------------------------------|-------------------------|
| Advisory generation | `trellis analyze generate-advisories` / `POST /api/v1/advisories/generate` | Hourly                  |
| Noise demote        | `POST /api/v1/effectiveness/apply-noise-tags`  | Daily                   |
| Learning promote    | `trellis analyze learning-candidates` + `trellis curate promote-learning` | Weekly (human-gated)   |

### 1. Advisory generation

Reads the last *N* days of `PACK_ASSEMBLED` + `FEEDBACK_RECORDED`
events, fits patterns with a minimum sample size and effect size,
and stores the survivors in `advisories.json`. Cheap to over-run.

```bash
trellis analyze generate-advisories \
  --days 30 --min-sample 5 --min-effect 0.15 --format json
```

QUERY advisories are keyed on words from the pack intent. Function words
(a stop-list in `advisory_generator.py`) are always withheld. A second rule,
off by default, withholds any word in more than `--max-keyword-df` of the
analysed packs (try `0.25`; it is skipped on a window too small for it to admit
any word). It ships off because at 0.25 it also retires domain nouns that still
have a 75% comparison arm, and nobody has counted on production data how many
rows that is: with it off, the report previews the 0.25 figure
(`query_keyword_df_preview`: `keywords_would_withhold` and `rows_would_retire`;
`worker curate` carries it as `advisory_query_df_preview`), so read it from one
nightly before enabling the rule. Active QUERY rows the generator would no
longer write are **suppressed**, never deleted, with an `ADVISORY_SUPPRESSED`
event naming the rule; the row records which rule (and, for frequency, which
ceiling) in its metadata. A frequency-retired row comes back only when the word
is emitted again, because it must clear `min_sample_size`, `min_effect_size` and
the comparison arm; otherwise restore it by hand with
`trellis curate advisory restore <advisory_id>`, or every row the rule retired
with `--rule document_frequency --all` (preview with `--dry-run`; each restore
emits an `ADVISORY_RESTORED` event, and the command exits `5` without writing
while the advisory file is degraded or was rewritten by another process). While
the rule is enforced it also needs a night
that enforces the ceiling, and a ceiling higher than the one that retired it or
a share fallen to 80% of the ceiling, so a share hovering at the bar, or a
window too short to enforce it, does not flip the row nightly. With the rule
off, the word being emitted again is enough. A stop-list retirement comes back
once the word leaves the list. The restore command also restores a stop-list row
(`--rule stop_list --all`, and it clears the retirement), but the next run
retires it again while the word is still excluded. The JSON report counts each
rule under `query_keywords_withheld` and `query_rows_retired`, and says which
state the frequency rule is in under `query_keyword_df_rule` (`off`, `enforced`
or `stood_down`).

REST equivalent:

```bash
curl -X POST -H "X-API-Key: $TRELLIS_ADMIN_KEY" \
  "http://localhost:8420/api/v1/advisories/generate?days=30"
```

A run that generated and wrote nothing answers **409**, never 200, and
names which refusal in a `code`. Two of the three sit at the top level
of the body beside `status` — `degraded_store` (the file could not be
read; go and look at it) and `stores_dir_unconfigured`. The third,
`stale_store_write` (another writer got there first; retry), sits under
`detail`, the shape that arm has answered since #438 and which #484
deliberately left alone — so read the code as `.code // .detail.code`,
not as `.code`.

Only the *first and third* were ever unsafe to script on `response.ok`:
the stale arm has answered 409 since #438, while those two returned 200
with `"status": "degraded"` / `"status": "error"` in the body until #484.

### 2. Noise demote

Tags low-value items with `signal_quality="noise"`, which causes
`PackBuilder` to exclude them from future packs by default. Run after
enough feedback has accumulated that effectiveness analysis can rank items.

```bash
curl -X POST -H "X-API-Key: $TRELLIS_ADMIN_KEY" \
  "http://localhost:8420/api/v1/effectiveness/apply-noise-tags?days=30&min_appearances=2"
```

There is no CLI wrapper for this endpoint as of PR-8 — REST is the
operator surface.

### 3. Learning promote

Two-step, human-gated. The analyzer surfaces candidates; the operator
edits the decisions file and submits approved promotions through the
governed mutation pipeline.

```bash
# 1. Discover candidates: writes intent_learning_candidates.json and
#    promotion_decisions.template.json to $TRELLIS_LEARNING_ARTIFACTS_DIR,
#    else <data_dir>/learning (the directory the API's Review queue reads);
#    the JSON output names both paths
trellis analyze learning-candidates --format json

# 2. Operator copies the template to decisions.json, sets `approved: true`
#    on rows to keep

# 3. Submit
trellis curate promote-learning \
  --candidates <data_dir>/learning/intent_learning_candidates.json \
  --decisions decisions.json --format json
```

`--dry-run` prints the planned mutations without executing them.

The command exits `0` when every approved row landed, `2` when any was refused
and none failed, and `5` when a write did not land, in either format; the JSON
`status` reads `ok`, `refused` or `failed` to match. See
[`operations.md`](../agent-guide/operations.md#trellis-curate-promote-learning).

## Upgrading

This is for the default SQLite stores. `scripts/upgrade_check.sh` upgrades a store
from the previous release to the build under test, then reads it back with the
previous release, and `.github/workflows/upgrade-check.yml` runs it on release
tags and nightly. Postgres, pgvector, Neo4j, ArcadeDB and S3 are **not** covered
by it. There is no migration command. The registry does keep a schema
fingerprint: `stores/_trellis_meta.json` records `<store>/<backend>/v<N>` per
store, and when the API server starts it refuses a store whose recorded value
differs from the build's (`SchemaFingerprintMismatch`;
`TRELLIS_SKIP_FINGERPRINT_CHECK=1` bypasses it). Every store class has been at
`v1` from v0.7.0 to now, so that check has never refused an upgrade, and the
script does not test a release that bumps it. Apart from that fingerprint a build
does not check that it can read a store, so the steps below are the compatibility
signal.

1. **Back up.** `trellis admin backup --output DIR` (see
   [`operations.md`](../agent-guide/operations.md#trellis-admin-backup); do not
   `cp` the `.db` files). Keep `config.yaml` beside it. The check does not test a
   restore.
2. **Install.** Stop `trellis admin serve` and any worker, then
   `pip install --upgrade trellis-ai` (or pull the new image). The new build
   opens the existing stores in place.
3. **Run.** Start the server and workers as before.
4. **Check.** `trellis admin health --format json` exits 0 with the store files
   present, and `trellis admin stats --format json` reports at least the counts
   you saw before. A pack serves different items under a different build by
   design, so compare counts, not served items.
5. **Roll back.** Reinstall the previous version against the same stores. What
   the check proves: after the new build had written to a store, the previous
   release read every row back, the new build's included (documents, traces,
   entities, edges, pack and feedback events, and `admin health`). It does not
   prove the previous release can *write* to an upgraded store, a rollback past
   the immediately preceding release, or a store a worker has touched since the
   upgrade. If a read fails, restore the backup from step 1.

To run the check from a git checkout with its tags (`git fetch --tags`):
`scripts/upgrade_check.sh` (an optional wheel or source tree as its argument,
`UPGRADE_FROM=<tag>` to pick the release to start from). It covers one document,
one trace, two entities and an edge, one pack and its feedback. Per-item pack
feedback (the MCP `record_feedback` tool and `POST /packs/{id}/feedback`) is not
in it.

## Recovery operations

### Feedback reconciliation

Feedback recorded through the MCP `record_feedback` tool or
`POST /api/v1/packs/{pack_id}/feedback` is written to two paths: a
`pack_feedback.jsonl` file on disk (file-based capture) and the
`FEEDBACK_RECORDED` event in the EventLog (governed mutation pipeline). On a
healthy system both writes succeed and stay in lockstep. If the EventLog write
fails (transient backend outage, process crash between writes), the JSONL has
the row but the EventLog does not — analytics that read from the EventLog miss
it. Feedback recorded with `trellis curate feedback` or `POST /api/v1/feedback`
goes straight to the EventLog and writes no JSONL row, so reconciliation has
nothing to say about it.

`trellis admin reconcile-feedback` closes that gap. It is **not** a
scheduled loop — running it against a healthy system scans the JSONL
and emits zero events because every entry is `already_present`. Run it
**when monitoring shows drift**, not on a cron.

Signals that you need to run reconciliation:

* Persistent gap between the JSONL row count and the
  `FEEDBACK_RECORDED` event count for the same time window
* Logs showing repeated EventLog write failures during a recent outage
* Imported a JSONL feedback log from another instance and want to
  promote those rows into the governed pipeline

```bash
# Count what would be emitted, then do it. Safe to repeat: rows already in the
# EventLog are left alone.
trellis admin reconcile-feedback --dry-run --format json
trellis admin reconcile-feedback --format json
```

It reads `pack_feedback.jsonl` from the feedback directory under the configured
stores directory, or from `--log-dir`. The report counts `scanned`,
`already_present`, `emitted` and `failed`. A directory with no
`pack_feedback.jsonl` is never reported as a clean pass: the report says
`no_log` and names the file looked for, which exits `0` for the default
directory (a fresh install has no log) and `2` for a `--log-dir` you named. A log
that exists but cannot be read exits `5`.

If you find yourself running this often, the right fix is to harden
the underlying EventLog sink (retry policy, failover, monitoring) —
not to wrap reconciliation in a recurring schedule.

## Scheduling the loops

### Cron (per-host or container sidecar)

```cron
# /etc/cron.d/trellis-loops
# Advisory generation, hourly at :05
5 *  * * * trellis  /usr/local/bin/trellis analyze generate-advisories --format json >> /var/log/trellis/advisory.log 2>&1

# Noise demote, daily at 03:15
15 3 * * * trellis  curl -fsS -X POST -H "X-API-Key: $TRELLIS_ADMIN_KEY" \
  "http://localhost:8420/api/v1/effectiveness/apply-noise-tags?days=30&min_appearances=2" \
  >> /var/log/trellis/noise.log 2>&1
```

### Kubernetes CronJob

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: trellis-advisory-generate
spec:
  schedule: "5 * * * *"
  jobTemplate:
    spec:
      template:
        spec:
          restartPolicy: OnFailure
          containers:
            - name: trellis
              image: ghcr.io/your-org/trellis-ai:latest
              command:
                - trellis
                - analyze
                - generate-advisories
                - --format
                - json
              envFrom:
                - secretRef: { name: trellis-secrets }
---
apiVersion: batch/v1
kind: CronJob
metadata:
  name: trellis-noise-demote
spec:
  schedule: "15 3 * * *"
  jobTemplate:
    spec:
      template:
        spec:
          restartPolicy: OnFailure
          containers:
            - name: curl
              image: curlimages/curl:8.10.1
              command: ["sh", "-c"]
              args:
                - >-
                  curl -fsS -X POST
                  -H "X-API-Key: $TRELLIS_ADMIN_KEY"
                  "http://trellis-api:8420/api/v1/effectiveness/apply-noise-tags?days=30&min_appearances=2"
              env:
                - name: TRELLIS_ADMIN_KEY
                  valueFrom:
                    secretKeyRef: { name: trellis-secrets, key: admin_key }
```

The learning-promote loop is intentionally not on this schedule — it
needs a human in the loop to review candidates before promotion.

## Validating a deployment

For the local docker-compose stack specifically, the dependency-free
[`deploy/smoke.sh`](../../deploy/smoke.sh) probe runs the full
endpoint + Postgres round-trip checklist — see
[local-compose.md](local-compose.md#smoke-test--verification-checklist).
For any deployment (cloud included), the single-command path:

```bash
trellis admin smoke-test --url http://localhost:8420 \
  --api-key "$TRELLIS_ADMIN_KEY" --format json
```

`smoke-test` exits 0 only when every check passes; non-zero on any
failure. Suitable for a k8s init-container hook or a deploy-pipeline
gate. The `--url` flag falls back to `TRELLIS_API_HOST` and
`TRELLIS_API_PORT`, and `--api-key` to `TRELLIS_API_KEY`. Pass a scoped
key with `--api-key`, as above, rather than through `TRELLIS_API_KEY`, which
is the legacy shared-secret variable (see [Authentication](#authentication)).
The key needs the `admin` scope, since the auth check calls an admin route.

The five checks it runs, equivalent to running these manually:

```bash
# 1. Liveness — should always return 200
curl -fsS http://localhost:8420/healthz

# 2. Readiness — 200 only when every cloud backend round-trips.
#    smoke-test sends the resolved key here too, so the per-backend
#    breakdown shows on an auth-required deployment; without a key
#    (or with one readyz itself rejects) the breakdown is withheld and
#    smoke-test notes it rather than reporting a bare pass/fail.
curl -fsS -H "X-API-Key: $TRELLIS_ADMIN_KEY" http://localhost:8420/readyz | jq

# 3. Auth — should 401 without the key, 200 with it
curl -fsS -o /dev/null -w "%{http_code}\n" \
  http://localhost:8420/api/v1/advisories
curl -fsS -o /dev/null -w "%{http_code}\n" \
  -H "X-API-Key: $TRELLIS_ADMIN_KEY" \
  http://localhost:8420/api/v1/advisories

# 4. Metrics — should return Prometheus-format text (404 means the
#    [observability] extra isn't installed; smoke-test reports this
#    as INFO rather than FAIL). smoke-test sends the key here too, so
#    a gated /metrics (TRELLIS_METRICS_PUBLIC=false) doesn't 401.
curl -fsS -H "X-API-Key: $TRELLIS_ADMIN_KEY" http://localhost:8420/metrics | head
```

If `/readyz` returns `degraded` with `event_log` or `document_store`
showing high latency, check `pg_pool_opened` log lines from startup
to confirm the pool sizes match what you set, then check
`pg_stat_activity` on the Postgres side for connection saturation.
