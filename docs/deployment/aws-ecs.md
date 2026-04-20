# Trellis on AWS ECS + RDS — POC deployment

First-client POC runbook for standing Trellis up on AWS. Target: a bastion-hosted
REST API + UI in a VPN'd VPC, talking to RDS PostgreSQL (pgvector) and S3.

Assumptions: network-layer auth (VPN + security groups) is sufficient; no app-layer
auth, no rate limiting, no horizontal scaling. Single Fargate task is enough for POC
scale.

## Topology

```
VPN ──► Bastion (private subnet, SG-restricted)
         │
         └── ECS Fargate task: trellis-api
              • image from ECR: <account>.dkr.ecr.<region>.amazonaws.com/trellis-ai:<tag>
              • port 8420 (REST + UI)
              • env vars from Secrets Manager + Parameter Store
              │
              ├──► RDS PostgreSQL 15+ (private subnet)
              │     • pgvector extension enabled
              │     • Two databases: trellis_knowledge, trellis_operational
              │
              └──► S3 bucket (blob store via VPC gateway endpoint)
```

## Components to provision

| AWS service           | Purpose                                                    |
|-----------------------|------------------------------------------------------------|
| VPC + subnets         | Private subnets for RDS + ECS; bastion in its own subnet.  |
| RDS for PostgreSQL    | Version 15.3+ so `CREATE EXTENSION vector;` works natively.|
| S3 bucket             | Blob store. Enable versioning + encryption at rest.        |
| VPC gateway endpoint  | S3 endpoint so blob traffic stays on the AWS backbone.     |
| ECR repository        | Host the `trellis-ai` container image.                     |
| Secrets Manager       | Postgres DSNs, OpenAI key (when added).                    |
| IAM task role         | S3 read/write on the blob bucket; Secrets Manager read.    |
| IAM execution role    | ECR pull, CloudWatch Logs write.                           |
| CloudWatch log group  | `/ecs/trellis-api`.                                        |
| Security groups       | Task → RDS (5432), Task → S3 via endpoint, Bastion → Task. |

## Step 1 — Build and push the image

From the repo root:

```bash
docker build -t trellis-ai:$(git rev-parse --short HEAD) .
aws ecr get-login-password --region $AWS_REGION \
  | docker login --username AWS --password-stdin $AWS_ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com
docker tag trellis-ai:$SHA $AWS_ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com/trellis-ai:$SHA
docker push $AWS_ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com/trellis-ai:$SHA
```

## Step 2 — Provision RDS

Create a PostgreSQL 15.3+ instance. Once reachable, on each database:

```sql
CREATE DATABASE trellis_knowledge;
CREATE DATABASE trellis_operational;

\c trellis_knowledge
CREATE EXTENSION IF NOT EXISTS vector;
```

Tables auto-create on first API boot — no migration step needed for the POC.

## Step 3 — Provision S3 + VPC endpoint

```bash
aws s3api create-bucket --bucket my-trellis-blobs --region $AWS_REGION
aws s3api put-bucket-versioning --bucket my-trellis-blobs \
  --versioning-configuration Status=Enabled
aws s3api put-bucket-encryption --bucket my-trellis-blobs \
  --server-side-encryption-configuration '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
```

Create an S3 Gateway VPC Endpoint attached to the route table serving the private
subnets so task → S3 traffic doesn't traverse the public internet.

## Step 4 — Store secrets

Put both DSNs in Secrets Manager as a single JSON secret, e.g. `trellis/api`:

```json
{
  "TRELLIS_KNOWLEDGE_PG_DSN":   "postgresql://trellis:<pw>@<rds-endpoint>:5432/trellis_knowledge",
  "TRELLIS_OPERATIONAL_PG_DSN": "postgresql://trellis:<pw>@<rds-endpoint>:5432/trellis_operational"
}
```

When you add LLM enrichment later, append:

```json
  "OPENAI_API_KEY": "sk-..."
```

## Step 5 — Mount the config file

Bake `docs/deployment/config.yaml.aws.example` into the image, or mount it from an
EFS volume / drop it in the image at `/etc/trellis/config.yaml`. The simplest POC
path is to copy the example into `deploy/config.yaml`, build a thin
`Dockerfile.config` on top of the base image that `COPY`s it to
`/etc/trellis/config.yaml`, and push that tagged image.

The task definition sets `TRELLIS_CONFIG_DIR=/etc/trellis` (Dockerfile default).

## Step 6 — ECS task definition (sketch)

```json
{
  "family": "trellis-api",
  "requiresCompatibilities": ["FARGATE"],
  "cpu": "1024",
  "memory": "2048",
  "networkMode": "awsvpc",
  "executionRoleArn": "arn:aws:iam::<account>:role/trellis-ecs-execution",
  "taskRoleArn":      "arn:aws:iam::<account>:role/trellis-ecs-task",
  "containerDefinitions": [{
    "name": "trellis-api",
    "image": "<account>.dkr.ecr.<region>.amazonaws.com/trellis-ai:<tag>",
    "portMappings": [{ "containerPort": 8420, "protocol": "tcp" }],
    "environment": [
      { "name": "TRELLIS_CONFIG_DIR", "value": "/etc/trellis" },
      { "name": "TRELLIS_LOG_FORMAT", "value": "json" },
      { "name": "TRELLIS_S3_BUCKET",  "value": "my-trellis-blobs" },
      { "name": "AWS_REGION",         "value": "us-east-1" }
    ],
    "secrets": [
      { "name": "TRELLIS_KNOWLEDGE_PG_DSN",
        "valueFrom": "arn:aws:secretsmanager:<region>:<account>:secret:trellis/api:TRELLIS_KNOWLEDGE_PG_DSN::" },
      { "name": "TRELLIS_OPERATIONAL_PG_DSN",
        "valueFrom": "arn:aws:secretsmanager:<region>:<account>:secret:trellis/api:TRELLIS_OPERATIONAL_PG_DSN::" }
    ],
    "healthCheck": {
      "command": ["CMD-SHELL",
        "python -c \"import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8420/healthz',timeout=2).status==200 else 1)\""],
      "interval": 30, "timeout": 5, "retries": 3, "startPeriod": 20
    },
    "logConfiguration": {
      "logDriver": "awslogs",
      "options": {
        "awslogs-group": "/ecs/trellis-api",
        "awslogs-region": "<region>",
        "awslogs-stream-prefix": "trellis"
      }
    }
  }]
}
```

If you front the task with an ALB, the target group should probe `/readyz` on port
8420 (returns 503 during store init, 200 once the event log is reachable). If the
bastion talks to the task directly, `/healthz` is enough.

## Step 7 — Bastion access + MCP note

Agents running Claude Code / Cursor on dev laptops reach the REST API through the
bastion. Configure the Trellis SDK with `base_url=https://<bastion-dns>:<port>`.

**MCP stays local.** `trellis-mcp` is stdio-only and runs as a subprocess on each
agent's host, talking to the same remote REST API via the SDK under the hood. You
do not need to deploy MCP to ECS.

## Step 8 — First-boot verification

Once the task is `RUNNING`:

```bash
# From the bastion:
curl -fsS http://<task-ip>:8420/healthz        # → {"status":"ok"}
curl -fsS http://<task-ip>:8420/readyz         # → {"status":"ready"}
curl -fsS http://<task-ip>:8420/api/version    # → version handshake JSON

# Seed the demo corpus (optional — proves the full pipeline end-to-end):
TRELLIS_API_URL=http://<task-ip>:8420 trellis demo load
```

UI is reachable at `http://<task-ip>:8420/ui/` (tunnel through the bastion in your
browser).

## Backups

- **Postgres:** RDS automated snapshots (daily, 7-day retention is fine for POC).
  Before schema-breaking upgrades, run `pg_dump` manually.
- **Blobs:** S3 bucket versioning is on; no separate backup job needed for POC.
- **Restore order:** Postgres first (tables + event log), then verify blob refs.
  The event log is the source of truth; if a blob is missing, the event still
  records what should have been there.

## What's out of scope for this runbook

- App-layer authentication (VPN-only assumption)
- Horizontal scaling / multiple Fargate tasks
- Prometheus metrics / custom CloudWatch metrics
- Postgres migration framework (tables autocreate; safe for greenfield)
- Multi-tenancy

Each of those has a home in the next phase — see [TODO.md](../../TODO.md)
"cloud deployment" section.
