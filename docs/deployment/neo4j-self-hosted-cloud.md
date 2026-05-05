# Trellis with self-hosted Neo4j in the cloud

The blessed production path for the Knowledge Plane when AuraDB isn't
on the table — your own Neo4j running on Kubernetes or a VM in your
cloud account. Same Bolt protocol, same `TRELLIS_NEO4J_*` env vars,
but you own the operational surface (backups, TLS, upgrades, scaling).

For the managed equivalent see [`neo4j-auradb.md`](./neo4j-auradb.md);
for laptop dev see [`neo4j-local.md`](./neo4j-local.md). The Trellis
side of this doc is short — most of it is decisions about *how* you
run Neo4j, with pointers to Neo4j's own docs for the deep dives.

## Why this path

* **Data residency / network policy** — your Neo4j sits in your VPC,
  on subnets you control, with whatever WAF / firewall / private-link
  policy you already enforce.
* **Cost predictability at scale** — AuraDB Pro pricing scales per
  GB; self-hosted is fixed-cost per VM / pod regardless of traffic.
* **Edition flexibility** — pick Community for evaluation, switch to
  Enterprise when you need clustering, online backup, or the
  `NODE KEY` constraint (see [Multi-writer](#multi-writer-and-edition)).

## Decisions before you provision

| Decision | Community | Enterprise |
|---|---|---|
| **License cost** | Free (GPL v3) | Commercial; required for clusters |
| **Multi-writer Trellis safety** | Single-writer only (see below) | Safe with `NODE KEY` constraint |
| **Backups** | Manual `neo4j-admin database dump` (offline) | Online backup, point-in-time recovery |
| **High availability** | Single instance only | Causal Cluster (3+ cores + read replicas) |
| **Vector index** | Same engine | Same engine |

For evaluation / single-writer POCs, **Community is fine**. Move to
Enterprise the moment you need either (a) two API replicas writing
concurrently or (b) zero-downtime backups.

| Hosting target | When it fits |
|---|---|
| **Kubernetes via official Neo4j Helm chart** | Default. Works on EKS / GKE / AKS / DOKS / on-prem k8s. |
| **Docker on a single VM** | Smallest cost; no orchestration. Good for proof-of-value before the k8s investment. |
| **Bare EC2 / VM** | Required if k8s isn't available; Neo4j ships .deb / .rpm / tar archives. |

This doc walks the Helm path because it's the most common and the
cleanest. The application-side Trellis config is identical regardless
of hosting target — the env vars in [Step 3](#step-3--configure-trellis)
work the same way.

## Step 1 — Provision Neo4j on Kubernetes

The `neo4j/helm-charts` repository (
<https://github.com/neo4j/helm-charts>) is the source of truth for
Neo4j's official chart. Two relevant ones:

* **`neo4j/neo4j`** — single instance (standalone). Use this for
  Community Edition or single-writer Enterprise.
* **`neo4j/neo4j-cluster-core`** + **`neo4j/neo4j-cluster-read-replica`**
  — multi-instance Causal Cluster. Enterprise only.

Minimum standalone install (Community):

```bash
helm repo add neo4j https://helm.neo4j.com/neo4j
helm repo update

helm install trellis-neo4j neo4j/neo4j \
  --namespace neo4j --create-namespace \
  --set neo4j.name=trellis-neo4j \
  --set neo4j.password='<choose-a-strong-password>' \
  --set neo4j.acceptLicenseAgreement=yes \
  --set neo4j.edition=community \
  --set volumes.data.mode=defaultStorageClass \
  --set volumes.data.defaultStorageClass.requests.storage=50Gi
```

For Enterprise, swap `neo4j.edition=enterprise` and pass a license
key via `neo4j.licenseAgreementUrl` (commercial — see Neo4j sales).

## Step 2 — Expose Bolt to your application

Neo4j Helm creates two Services per release:

| Service | Port | Use |
|---|---|---|
| `<release>-admin` | 6362, 7474, 7687 | Admin tooling (browser, cypher-shell) |
| `<release>` | 7687 | Bolt traffic from applications |

For Trellis, only `7687` matters. Two patterns:

**In-cluster — Trellis API runs in the same cluster.** Address Neo4j
by Service DNS:

```
TRELLIS_NEO4J_URI=bolt://trellis-neo4j.neo4j.svc.cluster.local:7687
```

No TLS needed if traffic stays inside the cluster's network policy.

**Cross-VPC or external Trellis — TLS required.** Front Bolt with a
TCP / TLS-terminating LB (cloud LB, Istio, NGINX Ingress with the
`tcp-services` ConfigMap). Use the `neo4j+s://` scheme so the driver
verifies the certificate:

```
TRELLIS_NEO4J_URI=neo4j+s://neo4j.your-domain.example:7687
```

Cert provisioning is whatever you already use for k8s services
(cert-manager + Let's Encrypt is the lowest-friction option).

## Step 3 — Configure Trellis

Same env vars as the AuraDB doc, with three differences:

| Variable | Self-hosted value | AuraDB value |
|---|---|---|
| `TRELLIS_NEO4J_URI` | `bolt://...` (in-cluster) or `neo4j+s://...` (TLS) | Always `neo4j+s://` |
| `TRELLIS_NEO4J_USER` | `neo4j` (always, every edition) | `neo4j` (Pro) or instance ID (Free) |
| `TRELLIS_NEO4J_DATABASE` | `neo4j` (always, every edition) | `neo4j` (Pro) or instance ID (Free) |

Self-hosted is consistent: user is `neo4j`, database is `neo4j`. The
AuraDB Free instance-ID conflation is a managed-service quirk, not a
Neo4j thing.

```bash
export TRELLIS_KNOWLEDGE_GRAPH_BACKEND=neo4j
export TRELLIS_KNOWLEDGE_VECTOR_BACKEND=neo4j
export TRELLIS_NEO4J_URI=bolt://trellis-neo4j.neo4j.svc.cluster.local:7687
export TRELLIS_NEO4J_USER=neo4j
export TRELLIS_NEO4J_PASSWORD=<from helm install>
export TRELLIS_NEO4J_DATABASE=neo4j
```

Pair with managed Postgres (RDS / Cloud SQL / Neon) for the
operational plane the same way the AuraDB doc shows in
[`neo4j-auradb.md` §"Combined with Postgres operational plane"](./neo4j-auradb.md#step-4--combined-with-postgres-operational-plane).

## Step 4 — Smoke test

```bash
set -a && source .env && set +a
export TRELLIS_VALIDATE_CONNECTIVITY=1   # fail at startup, not first request

trellis admin smoke-test --url http://your-trellis-api:8420 \
  --api-key "$TRELLIS_API_KEY" --format json
```

`smoke-test` (see [the runbook](./runbook.md)) hits `/readyz` which
in turn round-trips Neo4j; any auth / connectivity / TLS issue
surfaces as `event_log` or `graph_store` `degraded` in the output.

## Multi-writer and edition

Trellis enforces SCD-2 ("at most one current version per `node_id`")
via close-then-insert transactions. Under concurrent writers — multiple
API replicas, parallel ingest workers — the database needs to enforce
the invariant too, otherwise a second writer can race past a first
writer's "close current" before observing it.

**Community Edition does not support partial uniqueness constraints.**
Single-writer deployments are safe; concurrent writers can produce
duplicate current rows.

**Enterprise supports `NODE KEY`.** Add it manually after install:

```cypher
CREATE CONSTRAINT node_id_current_unique IF NOT EXISTS
FOR (n:Node) REQUIRE n.node_id IS NODE KEY
```

This is the same constraint the AuraDB doc describes — Neo4j's
`NODE KEY` is an Enterprise feature regardless of how you deploy it.

| Your situation | Edition |
|---|---|
| Single Trellis API replica, single ingest worker | Community is fine |
| Multiple API replicas behind a load balancer | Enterprise (or one writer + N readers) |
| Need point-in-time backup or HA | Enterprise (Causal Cluster) |
| Future-you might need any of the above | Enterprise from day one — migrating later costs more |

## Persistence and backups

Helm provisions a PVC for `/data` (set `volumes.data.requests.storage`
to whatever you size to). The chart supports Neo4j's full storage
matrix — local, cloud-block, NFS — see Neo4j's
[Helm chart storage docs](https://neo4j.com/docs/operations-manual/current/kubernetes/persistent-volumes/).

| Edition | Backup approach |
|---|---|
| Community | Stop the database, run `neo4j-admin database dump`, snapshot the volume, restart. Schedule via a CronJob. Downtime per backup. |
| Enterprise | `neo4j-admin database backup` while running. Schedule via the chart's `neo4j-admin` Job pattern or a CronJob. |

Whichever you use, **store backups outside the cluster** (S3 / GCS /
the equivalent). A cluster-wide outage shouldn't take your backups
with it.

## Driver tuning

Defaults in [`DriverConfig`](../../src/trellis/stores/neo4j/base.py)
(30s connect timeout, 100-connection pool, keep-alive on) are sized
for AuraDB-Pro-class workloads. For self-hosted:

* **Single API replica, light load**: defaults are fine.
* **Multiple API replicas, high concurrency**: bump
  `max_connection_pool_size` to roughly (replicas × per-replica
  concurrency × 1.5). Each replica owns its own driver, so the value
  is per-replica, not global.
* **Cross-region traffic**: keep `connection_timeout` at 30s, but
  reduce `max_transaction_retry_time` if you'd rather see failures
  fast than wait for the default 30s budget.

Override per-deployment via the `driver_config` block in
`config.yaml`:

```yaml
knowledge:
  graph:
    backend: neo4j
    uri: bolt://trellis-neo4j.neo4j.svc.cluster.local:7687
    user: neo4j
    password: ${TRELLIS_NEO4J_PASSWORD}
    database: neo4j
    driver_config:
      max_connection_pool_size: 200
      max_transaction_retry_time: 10.0
```

The graph + vector pair against the same `(uri, user)` shares one
driver — first store's config wins.

## Vector index

The single-vector-index-per-`(:Node, embedding)` constraint described
in [`neo4j-auradb.md` §"Vector index cohabitation"](./neo4j-auradb.md#vector-index-cohabitation--single-index-per-node-embedding)
applies identically to self-hosted Neo4j. The mitigations are the
same — match the production index name and dimension across all
environments that share an instance, or use separate instances.

## Observability

Neo4j Enterprise exposes Prometheus metrics natively at `:2004/metrics`
when `metrics.prometheus.enabled=true` is set in `neo4j.conf`. The
Helm chart exposes this via `metrics.prometheus.endpoint.enabled=true`.
Pair with the Trellis `/metrics` endpoint (see
[runbook §Metrics](./runbook.md#metrics)) for end-to-end visibility:

* Trellis `/metrics` — request latency, pack assembly time, mutation
  pipeline counters
* Neo4j `:2004/metrics` — query throughput, transaction state,
  cache hit rate, page-cache memory

Community Edition does not export Prometheus metrics; you get JMX
only. For most POCs the Trellis-side `/metrics` is enough.

Logs flow through Neo4j's stdout (Helm) or `/var/log/neo4j/` (raw VM)
— scrape with whatever you already use (Loki, Datadog, CloudWatch).

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `ServiceUnavailable: Couldn't connect to <host>:7687` | Service DNS wrong, NetworkPolicy blocks the pod, LB isn't routing 7687 | Inside the cluster: `kubectl exec -n neo4j -- nc -zv <release> 7687`. Outside: confirm the LB target group has `7687/TCP` |
| `AuthError` after Helm upgrade | Helm regenerated the password (chart version change) | Pull from the `<release>-auth` Secret: `kubectl get secret <release>-auth -n neo4j -o jsonpath='{.data.NEO4J_AUTH}' \| base64 -d` |
| `ssl.SSLCertVerificationError` on `neo4j+s://` | Self-signed or expired cert at the LB | Use `neo4j+ssc://` (skip cert verification — dev only), or fix the cert chain |
| `database 'neo4j' does not exist` after restore | Restore created a different DB name | `SHOW DATABASES;` in cypher-shell, then either rename or set `TRELLIS_NEO4J_DATABASE` to the actual name |
| Read-replica observed as out-of-date | Causal Cluster bookmark not propagated to that replica | Trellis's writes route to the leader by default; reads are fine on replicas with the standard 100ms-ish lag |
| Slow startup after pod restart | Cold page cache | Expected — 30s-2min depending on graph size. Set `TRELLIS_VALIDATE_CONNECTIVITY=1` so this surfaces during startup, not first request |

## Next steps

* For the operator-facing reference (env vars, probes, metrics, pool
  sizing): [`runbook.md`](./runbook.md)
* For laptop dev on the same backend shape:
  [`neo4j-local.md`](./neo4j-local.md)
* For the managed equivalent: [`neo4j-auradb.md`](./neo4j-auradb.md)
* For the design rationale (why Neo4j is the blessed graph backend):
  [`../design/plan-neo4j-hardening.md`](../design/plan-neo4j-hardening.md)
