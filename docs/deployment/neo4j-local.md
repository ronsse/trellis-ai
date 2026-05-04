# Trellis with local Neo4j (Docker)

The blessed local-development path for the Knowledge Plane: a single
Neo4j Community container alongside whatever Trellis process you're
running (CLI, API server, MCP). Operational stores stay on SQLite for
zero-cloud-dep development. For the cloud equivalent see
[`neo4j-auradb.md`](./neo4j-auradb.md).

## Why this path

* Neo4j is the [blessed graph backend](../design/plan-neo4j-hardening.md)
  for both local and cloud, so local dev mirrors production.
* Single container, no compose file, no orchestration cost.
* SQLite for traces / events / parameters keeps zero-cloud-dep dev
  feasible; switch to Postgres when you're ready to mirror cloud
  more closely (see [`neo4j-auradb.md`](./neo4j-auradb.md) §"Combined
  with Postgres operational plane").

## Prerequisites

* Docker installed and running.
* Trellis installed with the Neo4j extra:

  ```bash
  pip install -e ".[neo4j]"
  ```

## Step 1 — Start the Neo4j container

```bash
docker run --rm -d \
  --name trellis-neo4j \
  -p 7474:7474 -p 7687:7687 \
  -e NEO4J_AUTH=neo4j/test1234 \
  -e NEO4J_PLUGINS='["apoc"]' \
  neo4j:5
```

* `7474` is the browser UI (`http://localhost:7474`); `7687` is the
  Bolt protocol Trellis uses.
* `NEO4J_AUTH=neo4j/test1234` sets the initial admin password to
  `test1234`. Pick anything; mirror it in the env vars below.
* The APOC plugin is optional for Trellis itself but useful for ad-hoc
  debugging in the browser.
* Add `-v $(pwd)/neo4j-data:/data` if you want the graph to survive
  container restarts. Without that, every `docker run` starts fresh.

Confirm it's up:

```bash
curl -s http://localhost:7474 | head -2
# expected: <!DOCTYPE html>...
```

## Step 2 — Configure Trellis

Either of these two paths works. Pick one.

### Option A — env vars (simplest)

```bash
export TRELLIS_KNOWLEDGE_GRAPH_BACKEND=neo4j
export TRELLIS_KNOWLEDGE_VECTOR_BACKEND=neo4j
export TRELLIS_NEO4J_URI=bolt://localhost:7687
export TRELLIS_NEO4J_USER=neo4j
export TRELLIS_NEO4J_PASSWORD=test1234
```

Trace, document, event-log, and blob defaults stay on SQLite.

### Option B — `~/.trellis/config.yaml`

See [`recommended-config.yaml`](./recommended-config.yaml) for the
"local default" block — it's the same config, named, with comments.

## Step 3 — Smoke test

```bash
trellis admin init        # one-time: creates SQLite store dirs
trellis demo load         # loads 50 sample traces + entities + edges
trellis admin graph-health  # should report counts > 0 for nodes / edges
```

The graph-health output should show entity types, role distribution,
and edge counts populated against your local Neo4j. If it reports
zero counts, the writes went to SQLite — check that
`TRELLIS_KNOWLEDGE_GRAPH_BACKEND=neo4j` is exported in the same
shell.

## Step 4 — Optional: enable startup connectivity check

For the API server (`trellis serve`), set:

```bash
export TRELLIS_VALIDATE_CONNECTIVITY=1
```

Then on startup the registry pings Neo4j before uvicorn accepts its
first request, so a stopped container surfaces as a clear startup
error instead of an opaque first-request failure. Off by default to
keep dev restarts fast.

## Sharing one driver

`Neo4jGraphStore` and `Neo4jVectorStore` against the same instance
share a single connection pool — the `StoreRegistry` instantiates one
driver per `(uri, user)` and injects it into both stores. You don't
need to do anything to opt in; this is automatic.

## Tuning the driver

Defaults are set in
[`DriverConfig`](../../src/trellis/stores/neo4j/base.py): 30s connect
timeout, 100-connection pool, 30s transaction-retry budget,
keep-alive on. Override per-deployment via the `driver_config` block
under each Neo4j store entry in `config.yaml`:

```yaml
knowledge:
  graph:
    backend: neo4j
    uri: bolt://localhost:7687
    user: neo4j
    password: test1234
    driver_config:
      connection_timeout: 5.0
      max_connection_pool_size: 50
```

The first store wins for a given `(uri, user)` — both graph and
vector share the first store's pool config.

## Multi-writer caveat (Community Edition)

Neo4j Community Edition does not support partial uniqueness
constraints (`UNIQUE ... WHERE valid_to IS NULL`). The "at most one
current SCD-2 version per `node_id`" invariant is enforced by
Trellis's close-then-insert transaction, not by the database. Under
**concurrent writers on Community**, a second writer can observe a
stale "no current" state and create a duplicate current row.

Trellis treats this as a deployment-matrix concern rather than an
active mitigation:

| Edition | Single writer | Multiple concurrent writers |
|---|---|---|
| **Community** (this doc's `docker run neo4j:5`) | Safe | Race possible — see below |
| **Enterprise** (self-hosted) | Safe | Add a node-key constraint |
| **AuraDB** (managed, [`neo4j-auradb.md`](./neo4j-auradb.md)) | Safe | Add a node-key constraint |

For local dev with a single API process or CLI session, this is
never an issue. If you ever hit a "duplicate current version"
warning in `trellis admin graph-health` against Community, the fix
is one of:

1. Use a single writer (most setups).
2. Move to AuraDB / Enterprise so a `NODE KEY` constraint becomes
   available.
3. Issue an open ticket — we'll add an opt-in constraint helper if a
   real Community user reports a duplicate.

## Tearing down

```bash
docker stop trellis-neo4j
# add `-v $(pwd)/neo4j-data:/data` above to persist between runs;
# without that, the next `docker run` starts fresh
```

To wipe Trellis's SQLite stores too:

```bash
rm -rf ~/.trellis/data/stores
trellis admin init
```

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `ServiceUnavailable: Couldn't connect to localhost:7687` | Container not running | `docker ps` → start with the command in Step 1 |
| `AuthError` | Password mismatch between container and env var | Re-run `docker run` with the same `NEO4J_AUTH` value as `TRELLIS_NEO4J_PASSWORD` |
| `no such vector schema index` on first query after init | Race against AuraDB-style async index provisioning (rare on local Docker) | Phase 1.4's `wait_for_vector_index_online` should prevent this; if you hit it, file an issue with the timeline |
| `trellis demo load` writes are slow | First-time Neo4j page-cache warm-up | One-time cost; subsequent runs are fast |

## Next steps

* For managed cloud deployment: [`neo4j-auradb.md`](./neo4j-auradb.md)
* For self-hosted cloud deployment (Helm / k8s / VM): [`neo4j-self-hosted-cloud.md`](./neo4j-self-hosted-cloud.md)
* For the full recommended config (Postgres operational plane optional): [`recommended-config.yaml`](./recommended-config.yaml)
* For the design rationale (why Neo4j is the blessed graph backend): [`../design/plan-neo4j-hardening.md`](../design/plan-neo4j-hardening.md)
