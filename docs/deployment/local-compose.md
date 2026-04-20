# Trellis local stack — docker compose

Offline rehearsal of the AWS ECS + RDS deployment. Runs the same API
container against Postgres + pgvector in a sibling container, so if the
stack boots green and smoke tests pass here, the cloud deployment is
mostly infrastructure provisioning — the code paths are identical.

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

## Prerequisites

- Docker Desktop or equivalent with `docker compose` v2+
- Ports 8420 (API) and 5432 (Postgres) free on the host
- ~500 MB for the image + volumes

## Bring it up

From the repo root:

```bash
docker compose up --build
```

First boot: Postgres initializes the two databases via
[deploy/init-db.sql](../../deploy/init-db.sql), then the API container
waits for the postgres healthcheck before starting. Schema tables
autocreate inside the API on first store access.

## Smoke test

In a second terminal:

```bash
# Liveness — 200 as soon as the process is up
curl -fsS http://localhost:8420/healthz
# {"status":"ok"}

# Readiness — 200 once Postgres round-trip succeeds
curl -fsS http://localhost:8420/readyz
# {"status":"ready"}

# Version handshake
curl -fsS http://localhost:8420/api/version | python -m json.tool

# UI in a browser
open http://localhost:8420/ui/     # or just visit the URL
```

Prove the full ingest → retrieve loop works through Postgres (not just
the default SQLite):

```bash
# From the host, pointed at the containerized API
pip install -e ".[dev]"
TRELLIS_API_URL=http://localhost:8420 trellis demo load
TRELLIS_API_URL=http://localhost:8420 \
  trellis retrieve pack --intent "explore the demo corpus"
```

The demo loader seeds ~66 items. If the pack returns content, the
full mutation pipeline + Postgres graph + pgvector semantic search
all work — which is the whole cloud-readiness contract.

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
