# Trellis local stack — docker compose

Offline rehearsal of the AWS ECS + RDS deployment. Runs the same API
container against Postgres + pgvector in a sibling container, so if the
stack boots green and smoke tests pass here, the cloud deployment is
mostly infrastructure provisioning — the code paths are identical.

> Last smoke-tested 2026-06-12 against Docker 29.x / Compose v2 — the
> outputs pasted below are real captures from that run.

## Compose services

| Service    | Image                       | Role                                                                 |
|------------|-----------------------------|---------------------------------------------------------------------|
| `postgres` | `pgvector/pgvector:pg16`    | Postgres 16 with the `pgvector` extension. First boot runs [`deploy/init-db.sql`](../../deploy/init-db.sql) to create `trellis_knowledge` + `trellis_operational` and enable `vector`. |
| `api`      | built from [`Dockerfile`](../../Dockerfile) | The Trellis REST API + static UI. `ENTRYPOINT` is `trellis serve --host 0.0.0.0 --port 8420`. Waits on the `postgres` healthcheck before starting. |

## What's the same as AWS

- `api` container built from the same [Dockerfile](../../Dockerfile)
- Postgres 16 with the `pgvector` extension
- Two databases: `trellis_knowledge`, `trellis_operational`
- `TRELLIS_KNOWLEDGE_PG_DSN` / `TRELLIS_OPERATIONAL_PG_DSN` env vars
- `/etc/trellis/config.yaml` mounted into the container
- `TRELLIS_LOG_FORMAT=json` structured logs
- `/healthz` + `/readyz` probes wired to compose health checks

## What's different from AWS

| Compose                              | AWS                                |
|--------------------------------------|------------------------------------|
| Local volume mount for blobs         | S3 bucket + VPC gateway endpoint   |
| Plain text passwords in env          | AWS Secrets Manager → task env     |
| Single postgres container            | RDS Multi-AZ                       |
| Host port 8420                       | ALB or direct task ingress         |

## Environment variables that matter

The compose file sets these on the `api` service:

| Var                          | Compose value                                              | What it does |
|------------------------------|-----------------------------------------------------------|--------------|
| `TRELLIS_CONFIG_DIR`         | `/etc/trellis`                                            | Where the registry reads `config.yaml` (mounted read-only from [`deploy/config.compose.yaml`](../../deploy/config.compose.yaml)). |
| `TRELLIS_DATA_DIR`           | `/var/lib/trellis`                                       | Stores dir for the local blob backend (the `trellis-data` named volume). |
| `TRELLIS_KNOWLEDGE_PG_DSN`   | `postgresql://trellis:trellis@postgres:5432/trellis_knowledge` | Graph / vector / document DSN. |
| `TRELLIS_OPERATIONAL_PG_DSN` | `postgresql://trellis:trellis@postgres:5432/trellis_operational` | Trace / event-log DSN. |
| `TRELLIS_LOG_FORMAT`         | `json`                                                   | Structured logs (one JSON object per line). |

Not set in the compose file, so they take their defaults — note these
when you adapt the file:

- **`TRELLIS_UI_ENABLED`** — unset → `true`. The static UI mounts at
  `/ui` and `/` redirects to it. Set `false` to skip the mount (`/`
  then redirects to `/api/version`). Any other value crashes startup.
- **`TRELLIS_AUTH_MODE`** — unset → `off`. **The compose stack runs
  unauthenticated** — every `/api/v1` request is accepted with full
  scopes (the startup log emits an `api_auth_permissive` warning to
  flag this). That is fine for a loopback smoke test; before exposing
  the API beyond loopback set `TRELLIS_AUTH_MODE=required` and mint
  keys with `trellis admin api-keys create`. Because auth is off,
  there is no separate authenticated-admin step in the checklist
  below — the admin routes answer without a credential here.
- **`TRELLIS_OPS_DETAIL`** — unset → backend latencies are included in
  the `/readyz` body (see the readiness output below). Set `false` to
  collapse it to a bare `{"status":"ready"}`.

The backend shape is **Postgres + pgvector for everything except
blobs** (local volume) — see [`deploy/config.compose.yaml`](../../deploy/config.compose.yaml).
This is the same backend wiring ECS+RDS uses; only the blob store and
secret delivery differ (see the table above).

## Prerequisites

- Docker Desktop or equivalent with `docker compose` v2+
- Ports 8420 (API) and 5432 (Postgres) free on the host. If either is
  taken, remap the host side in `docker-compose.yml` (e.g. `"55432:5432"`)
  or run under a custom project name with an override.
- ~500 MB for the image + volumes

## Bring it up

From the repo root:

```bash
docker compose up --build         # foreground, or:
docker compose up -d --build      # detached
```

First boot: Postgres initializes the two databases via
[deploy/init-db.sql](../../deploy/init-db.sql), then the API container
waits for the postgres healthcheck before starting. Schema tables
autocreate inside the API on first store access.

## Smoke test — verification checklist

The repo ships [`deploy/smoke.sh`](../../deploy/smoke.sh), a
dependency-free bash probe that runs the whole checklist below and exits
non-zero on any failure:

```bash
docker compose up -d --build
./deploy/smoke.sh
```

Real output from the 2026-06-12 run (10/10 pass):

```text
=== Endpoint probes against http://localhost:8420 ===
  [PASS] GET /healthz                   http://localhost:8420/healthz -> 200
  [PASS] GET /healthz body              body contains "status":"ok"
  [PASS] GET /readyz                    http://localhost:8420/readyz -> 200
  [PASS] GET /readyz body               body contains "status":"ready"
  [PASS] GET /api/version               http://localhost:8420/api/version -> 200
  [PASS] GET /api/version body          body contains "api_version":
  [PASS] GET /ui/                       http://localhost:8420/ui/ -> 200
  [PASS] GET /ui/ body                  body contains <title>Trellis</title>

=== Backend round-trip via /api/v1/traces ===
  POST /api/v1/traces -> {"status":"ok","trace_id":"01KTYG45VVMRCND10MZT2XV00W","evidence_id":null}
  [PASS] trace ingested                 trace_id=01KTYG45VVMRCND10MZT2XV00W
  [PASS] trace round-trips              GET returns the same trace_id

=== Summary ===
  passed: 10
  failed: 0
```

### The same checks, by hand

```bash
# Liveness — 200 as soon as the process is up
curl -fsS http://localhost:8420/healthz
# {"status":"ok"}

# Readiness — 200 once the Postgres round-trip succeeds. With
# TRELLIS_OPS_DETAIL unset, per-backend latencies are included:
curl -fsS http://localhost:8420/readyz
# {"status":"ready","backends":{"event_log":{"status":"ok","latency_ms":1.36},
#  "graph_store":{"status":"ok","latency_ms":1.07},
#  "vector_store":{"status":"ok","latency_ms":1.04},
#  "document_store":{"status":"ok","latency_ms":0.9}}}

# Version handshake
curl -fsS http://localhost:8420/api/version
# {"api_major":1,"api_minor":0,"api_version":"1.0","wire_schema":"0.1.0",
#  "sdk_min":"0.1.0","package_version":"0.0.0-dev","mcp_tools_version":1}

# UI in a browser
open http://localhost:8420/ui/     # serves <title>Trellis</title> HTML
```

### Prove the ingest → store → retrieve loop through Postgres

> **`trellis demo load` is local-only — do not use it for this.** The
> demo loader and the `trellis retrieve` CLI write to and read from
> *local* stores via the host's own `StoreRegistry`; neither honours a
> `TRELLIS_API_URL` and there is no remote-target flag. Pointing them
> at the container does nothing — they hit local SQLite. To exercise
> the *containerized* Postgres path, drive the REST API directly:

```bash
# 1. POST a trace through the governed mutation pipeline
curl -fsS -X POST http://localhost:8420/api/v1/traces \
  -H 'Content-Type: application/json' \
  -d '{
        "source": "agent",
        "intent": "smoke-test ingest via Postgres+pgvector",
        "context": {"agent_id": "smoke-test", "domain": "smoke"},
        "steps": [{"step_type": "tool_call", "name": "noop", "args": {}, "result": {}}],
        "outcome": {"status": "success", "summary": "ok"}
      }'
# {"status":"ok","trace_id":"01KTYG41811V8ECEQ3K50HD3BB","evidence_id":null}

# 2. Read it back — proves it persisted in trellis_operational
curl -fsS http://localhost:8420/api/v1/traces/01KTYG41811V8ECEQ3K50HD3BB
# {"status":"ok","trace":{...,"trace_id":"01KTYG41811V8ECEQ3K50HD3BB",...}}

# 3. Assemble a pack — exercises the keyword + graph retrieval strategies
curl -fsS -X POST http://localhost:8420/api/v1/packs \
  -H 'Content-Type: application/json' \
  -d '{"intent":"smoke-test ingest","max_items":5,"max_tokens":2000}'
# {"status":"ok","pack_id":"...","count":0,"items":[],"advisories":[],
#  "retrieval_report":{...,"strategies_used":["keyword","graph"],...}}
```

A `200` with a `pack_id` and a populated `retrieval_report` means the
mutation pipeline, the Postgres graph + document stores, and pgvector
all answered — that is the cloud-readiness contract. (`count:0` here is
expected: a single bare trace surfaces no retrievable evidence for that
intent. Ingest a richer corpus to see non-empty packs.)

## Inspecting logs

```bash
docker compose logs -f api      # one JSON object per line
docker compose logs -f postgres
```

Every line in the api stream is a structlog JSON record — same shape
that will land in CloudWatch under the AWS setup.

## Tear down

```bash
docker compose down       # stop containers, keep data
docker compose down -v    # stop + drop volumes (fresh next boot)
```

## Gotchas

- **Windows line endings in the init script**: if `deploy/init-db.sql`
  gets checked out with CRLF, the Postgres entrypoint can choke. The
  repo's `.gitattributes` should keep it LF — if you see `psql` errors
  about `\r`, run `dos2unix deploy/init-db.sql` and re-bring-up.
- **Port conflicts**: if `5432` is already bound on the host
  (local Postgres install), change the compose mapping to
  `"55432:5432"` and update any local client.
- **Config changes need a restart**: `config.compose.yaml` is read
  on registry init, not per-request. `docker compose restart api`
  after editing.
- **`trellis demo load` / `trellis retrieve` are local-only**: they
  drive the host's own `StoreRegistry`, not a remote API, and there is
  no `TRELLIS_API_URL` target. To exercise the *containerized*
  Postgres path, POST to the REST API (see the loop above) — running
  the CLI against the container silently hits a local SQLite store.
- **Auth is off in this stack**: `TRELLIS_AUTH_MODE` is unset, so the
  startup log prints an `api_auth_permissive` warning and every
  `/api/v1` route answers without a key. Set `TRELLIS_AUTH_MODE=required`
  before binding anything but loopback.

## What this does NOT validate

Things that only appear in the AWS setup:

- Secrets Manager → ECS task env injection
- RDS-specific behavior (parameter groups, SSL, IAM auth)
- S3 blob store + VPC endpoint routing
- ALB health checks, CloudWatch log delivery
- IAM task role permissions

Those are all infrastructure questions that the [AWS ECS
runbook](aws-ecs.md) addresses. The point of this compose stack is to
prove the **code and config are correct** before you spend time
provisioning cloud resources.
