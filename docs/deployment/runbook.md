# Trellis operator runbook

The cross-cutting reference for running Trellis in production: every
env var the API and CLI honour, what the probes return and how to
read them, how to scrape `/metrics`, how to size the Postgres pool,
and how to schedule the three closed feedback loops.

Pairs with the infrastructure-specific guides in this folder
([aws-ecs.md](aws-ecs.md), [local-compose.md](local-compose.md),
[neo4j-local.md](neo4j-local.md), [neo4j-auradb.md](neo4j-auradb.md))
— those describe *where* Trellis runs; this one describes *how* to
operate it once it's running.

## Environment variables

The minimum set to bring up the API against Postgres + Neo4j:

| Variable                       | Required? | Purpose                                                                  |
|--------------------------------|-----------|--------------------------------------------------------------------------|
| `TRELLIS_API_KEY`              | **Yes** in prod | Shared secret for `X-API-Key` auth on `/api/v1`. Unset = open API.   |
| `TRELLIS_KNOWLEDGE_PG_DSN`     | Yes       | DSN for the knowledge plane (graph / vector / document).                 |
| `TRELLIS_OPERATIONAL_PG_DSN`   | Yes       | DSN for the operational plane (trace / event_log).                       |
| `TRELLIS_API_HOST`             | No        | Bind address. **Defaults to `127.0.0.1`** — set `0.0.0.0` for containers. |
| `TRELLIS_API_PORT`             | No        | Default `8420`.                                                          |
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

> **Auth note.** `TRELLIS_API_KEY` is opt-in by code default — if the env
> var is unset, every `/api/v1` route accepts unauthenticated traffic and
> startup logs `api_key_unset`. Always set it for production. The
> `/healthz`, `/readyz`, and `/metrics` endpoints are deliberately
> unauthenticated so orchestrator probes work without holding the secret.

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

REST equivalent:

```bash
curl -X POST -H "X-API-Key: $TRELLIS_API_KEY" \
  "$TRELLIS_API_HOST:$TRELLIS_API_PORT/api/v1/advisories/generate?days=30"
```

### 2. Noise demote

Tags low-value items with `signal_quality="noise"`, which causes
`PackBuilder` to exclude them from future packs by default. Run after
enough feedback has accumulated that effectiveness analysis can rank items.

```bash
curl -X POST -H "X-API-Key: $TRELLIS_API_KEY" \
  "$TRELLIS_API_HOST:$TRELLIS_API_PORT/api/v1/effectiveness/apply-noise-tags?days=30&min_appearances=2"
```

There is no CLI wrapper for this endpoint as of PR-8 — REST is the
operator surface.

### 3. Learning promote

Two-step, human-gated. The analyzer surfaces candidates; the operator
edits the decisions file and submits approved promotions through the
governed mutation pipeline.

```bash
# 1. Discover candidates
trellis analyze learning-candidates --format json > candidates.json

# 2. Operator edits decisions.json, sets `approved: true` on rows to keep

# 3. Submit
trellis curate promote-learning \
  --candidates candidates.json --decisions decisions.json --format json
```

`--dry-run` prints the planned mutations without executing them.

## Recovery operations

### Feedback reconciliation

Trellis writes feedback to two paths: a `pack_feedback.jsonl` file on
disk (file-based capture) and the `FEEDBACK_RECORDED` event in the
EventLog (governed mutation pipeline). On a healthy system both writes
succeed and stay in lockstep. If the EventLog write fails (transient
backend outage, process crash between writes), the JSONL has the row
but the EventLog does not — analytics that read from the EventLog miss
it.

`reconcile_feedback_log_to_event_log` closes that gap. It is **not** a
scheduled loop — running it against a healthy system scans the JSONL
and emits zero events because every entry is `already_present`. Run it
**when monitoring shows drift**, not on a cron.

Signals that you need to run reconciliation:

* Persistent gap between the JSONL row count and the
  `FEEDBACK_RECORDED` event count for the same time window
* Logs showing repeated EventLog write failures during a recent outage
* Imported a JSONL feedback log from another instance and want to
  promote those rows into the governed pipeline

Invocation is currently programmatic — no CLI / REST wrapper:

```python
from pathlib import Path
from trellis.feedback.recording import reconcile_feedback_log_to_event_log
from trellis.stores.registry import StoreRegistry

registry = StoreRegistry.from_config_dir()
result = reconcile_feedback_log_to_event_log(
    log_dir=Path("/var/lib/trellis/feedback"),
    event_log=registry.operational.event_log,
)
print(result)  # ReconcileResult(scanned=N, already_present=N, emitted=N, failed=N)
```

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
15 3 * * * trellis  curl -fsS -X POST -H "X-API-Key: $TRELLIS_API_KEY" \
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
                  -H "X-API-Key: $TRELLIS_API_KEY"
                  "http://trellis-api:8420/api/v1/effectiveness/apply-noise-tags?days=30&min_appearances=2"
              env:
                - name: TRELLIS_API_KEY
                  valueFrom:
                    secretKeyRef: { name: trellis-secrets, key: api_key }
```

The learning-promote loop is intentionally not on this schedule — it
needs a human in the loop to review candidates before promotion.

## Validating a deployment

After the API comes up but before sending real traffic:

```bash
# 1. Liveness — should always return 200
curl -fsS http://localhost:8420/healthz

# 2. Readiness — 200 only when every cloud backend round-trips
curl -fsS http://localhost:8420/readyz | jq

# 3. Auth — should 401 without the key, 200 with it
curl -fsS -o /dev/null -w "%{http_code}\n" \
  http://localhost:8420/api/v1/advisories
curl -fsS -o /dev/null -w "%{http_code}\n" \
  -H "X-API-Key: $TRELLIS_API_KEY" \
  http://localhost:8420/api/v1/advisories

# 4. Metrics — should return Prometheus-format text
curl -fsS http://localhost:8420/metrics | head
```

If `/readyz` returns `degraded` with `event_log` or `document_store`
showing high latency, check `pg_pool_opened` log lines from startup
to confirm the pool sizes match what you set, then check
`pg_stat_activity` on the Postgres side for connection saturation.
