# Trellis with Neo4j AuraDB (managed cloud)

The blessed cloud path for the Knowledge Plane: Neo4j AuraDB Free for
evaluation, AuraDB Pro for production. Pair with managed Postgres for
the Operational Plane to get a fully-managed Trellis deployment with
no self-hosted infrastructure. For the local equivalent see
[`neo4j-local.md`](./neo4j-local.md).

## Why this path

* AuraDB handles backups, version upgrades, vector index management,
  and TLS termination — none of that is on your operations team.
* Free tier is enough for evaluation and small POCs (50K nodes,
  175K relationships, 200K vectors as of 2026).
* Same Bolt protocol as self-hosted Neo4j, so application code is
  identical to local dev.

## Step 1 — Provision an AuraDB instance

1. Sign up at [console.neo4j.io](https://console.neo4j.io) (Google /
   GitHub OAuth works).
2. Click **New Instance** → **AuraDB Free** (or **Pro** for
   production).
3. Pick a region close to your application servers. Region matters —
   pick once, painful to migrate later.
4. **Save the password the console shows.** It's displayed exactly
   once at instance creation; if you lose it, the only path is
   resetting it via the console.
5. Wait ~30 seconds for the instance to provision.

## Step 2 — Capture the credentials

The console shows three values you need:

| What | Looks like | Notes |
|---|---|---|
| **Connection URI** | `neo4j+s://abcd1234.databases.neo4j.io` | Always `neo4j+s://` (TLS) for Aura |
| **Username** | `neo4j` for Pro, the **instance ID** for Free | See gotcha below |
| **Password** | `XYZ...` (alphanumeric) | Shown once at creation |

**AuraDB Free gotcha — instance ID conflation:**

| Field | Value on AuraDB Free | Value on AuraDB Pro / self-hosted |
|---|---|---|
| **User** | The instance ID (e.g. `abcd1234`) | `neo4j` |
| **Database** | The instance ID (e.g. `abcd1234`) | `neo4j` |
| **Host in URI** | `<id>.databases.neo4j.io` | Yours |

This caught us during the live-test campaign. AuraDB Free hosts the
user database under the instance ID, not under the canonical name
`"neo4j"` — pass it explicitly when constructing the store. The
test fixtures honor `TRELLIS_TEST_NEO4J_DATABASE`; the production
config field is `database`.

## Step 3 — Configure Trellis

### Option A — env vars

```bash
export TRELLIS_KNOWLEDGE_GRAPH_BACKEND=neo4j
export TRELLIS_KNOWLEDGE_VECTOR_BACKEND=neo4j
export TRELLIS_NEO4J_URI=neo4j+s://abcd1234.databases.neo4j.io
export TRELLIS_NEO4J_USER=abcd1234           # AuraDB Free: == instance ID
export TRELLIS_NEO4J_PASSWORD=<from console>
export TRELLIS_NEO4J_DATABASE=abcd1234       # AuraDB Free: == instance ID
```

For AuraDB Pro / self-hosted, `TRELLIS_NEO4J_USER` and
`TRELLIS_NEO4J_DATABASE` are both `neo4j`.

### Option B — `~/.trellis/config.yaml`

See the "cloud-default" block in
[`recommended-config.yaml`](./recommended-config.yaml).

## Step 4 — Combined with Postgres operational plane

For the full managed stack, pair AuraDB Knowledge with managed
Postgres for the Operational Plane (traces / event log / parameters):

```yaml
knowledge:
  graph:
    backend: neo4j
    uri: neo4j+s://abcd1234.databases.neo4j.io
    user: abcd1234
    password: ${TRELLIS_NEO4J_PASSWORD}
    database: abcd1234
  vector:
    backend: neo4j
    uri: neo4j+s://abcd1234.databases.neo4j.io
    user: abcd1234
    password: ${TRELLIS_NEO4J_PASSWORD}
    database: abcd1234
operational:
  trace:
    backend: postgres
    dsn: ${TRELLIS_OPERATIONAL_PG_DSN}
  event_log:
    backend: postgres
    dsn: ${TRELLIS_OPERATIONAL_PG_DSN}
```

[Neon](https://neon.tech) (free tier with pgvector preinstalled) is
the cheapest managed Postgres option for evaluation; AWS RDS or
Google Cloud SQL are the natural production choices.

## Step 5 — Smoke test

```bash
# Load .env containing the TRELLIS_* vars
set -a && source .env && set +a

# Verify connectivity at startup, not first request
export TRELLIS_VALIDATE_CONNECTIVITY=1

trellis admin init
trellis demo load
trellis admin graph-health
```

`graph-health` should report counts > 0 for nodes / edges. The
connectivity-validate flag turns "AuraDB unreachable" from an opaque
first-request Bolt error into a clean startup failure aggregated with
any other config errors via `RegistryValidationError`.

## Driver tuning

The defaults in
[`DriverConfig`](../../src/trellis/stores/neo4j/base.py) are
production-safe but conservative — 30s connect timeout, 100-connection
pool, keep-alive on. For AuraDB specifically:

* **Free tier**: defaults are fine.
* **Pro tier with high concurrency**: bump `max_connection_pool_size`
  to match expected concurrent agents (default 100 covers most;
  larger if you have many parallel workers).
* **Cross-region setup**: leave `connection_timeout` at 30s, but
  consider tightening `max_transaction_retry_time` if a transient
  failure should escalate faster than 30s.

Override via the `driver_config` block under each Neo4j store entry:

```yaml
knowledge:
  graph:
    backend: neo4j
    uri: neo4j+s://abcd1234.databases.neo4j.io
    user: abcd1234
    password: ${TRELLIS_NEO4J_PASSWORD}
    database: abcd1234
    driver_config:
      max_connection_pool_size: 200
      max_transaction_retry_time: 10.0
```

The graph + vector pair against the same `(uri, user)` shares one
driver — first store's config wins.

## Upgrading from Free to Pro

1. In the AuraDB console, **Create new instance** → Pro tier in the
   same region.
2. Use AuraDB's built-in **Database export/import** to move the data,
   OR use Trellis's `trellis admin migrate-graph --from <free-uri>
   --to <pro-uri>` (Phase 2.3 of the hardening plan).
3. Update env vars / config. **Pro changes the user from instance ID
   back to `neo4j`** — adjust accordingly.
4. Retire the Free instance once Pro is verified.

## Multi-writer caveat

AuraDB supports the partial-uniqueness `NODE KEY` constraint that
Community Edition lacks. If your deployment runs concurrent Trellis
writers (multiple API replicas, parallel ingest workers), opt into
the strict-uniqueness constraint manually via the AuraDB console:

```cypher
CREATE CONSTRAINT node_id_current_unique IF NOT EXISTS
FOR (n:Node) REQUIRE n.node_id IS NODE KEY
```

This guarantees the SCD-2 invariant ("at most one current version
per `node_id`") holds even under concurrent close-then-insert
transactions. Without the constraint, single-writer deployments are
safe; concurrent writers can race on Community-style schemas.

A future Trellis release will add an opt-in
`enable_strict_uniqueness=True` constructor flag that adds this
automatically when the backend supports it. Tracked in the hardening
plan as Phase 1.5 Option B; deferred until a real concurrent-writer
deployment asks for it.

## Vector index cohabitation — single index per `(:Node, embedding)`

Neo4j only allows **one vector index per `(label, property)` pair**. On a
single AuraDB instance, that means the unit-test suite, the eval
scenarios, and a production-shaped load can't all create their own
named vector indexes against `(:Node).embedding` — the second `CREATE
VECTOR INDEX <other-name> IF NOT EXISTS` is silently a no-op (the slot
is taken), and the subsequent ONLINE-wait will time out at 30s waiting
for an index that will never appear.

How Trellis handles this:

* **Production deployments** use the default index name
  `trellis_node_embeddings` and the dimension that matches the embedder
  (e.g. 1536 for OpenAI ada-002). Set this via the registry config and
  leave it alone.
* **Unit tests** ([`tests/unit/stores/test_neo4j_vector.py`](../../tests/unit/stores/test_neo4j_vector.py))
  share a persistent index named `trellis_test_node_embeddings` at
  `dim=3` so the suite doesn't pay the AuraDB async-provisioning cost
  on every run.
* **Eval scenarios** ([`eval/scenarios/`](../../eval/)) and the
  [`scripts/load_eval_dataset_to_aura.py`](../../scripts/load_eval_dataset_to_aura.py)
  loader use the production default name (`trellis_node_embeddings`)
  but at the eval-default dim (16). The loader **drops the test index
  first** so the eval slot is free; running the loader therefore
  invalidates the unit-test fixture until a unit test recreates it.

Practical implication: if you run the eval loader against the same
AuraDB instance the unit tests use, the next unit-test run will
recreate `trellis_test_node_embeddings` from scratch — the fixture's
`CREATE VECTOR INDEX IF NOT EXISTS` succeeds because the slot is
empty, but the first vector test pays AuraDB's async-provisioning
wait (~30s on Free).

To avoid this churn on a shared instance, give eval and unit tests
**separate AuraDB instances** (a second AuraDB Free instance is free)
or run eval against a Docker self-hosted Neo4j and reserve AuraDB for
unit tests + production.

The same constraint applies to the column on the
[`pgvector` backend](../../src/trellis/stores/pgvector/store.py): the
`embedding` column carries a fixed dimension, and `CREATE TABLE IF NOT
EXISTS` is a no-op against an existing table. The store now fail-fast
detects a dimension mismatch at construction time and raises with an
actionable message; same operational rule applies — keep eval and
unit-test corpora at the same dim, or use separate databases.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `AuthError` | Password / user mismatch | AuraDB Free user is the instance ID, not `neo4j` |
| `Database '<x>' does not exist` | Wrong `database` value | AuraDB Free database name == instance ID; Pro uses `neo4j` |
| `ServiceUnavailable` after instance pause | AuraDB Free auto-pauses idle instances | Resume from console; first request after resume can take ~30s |
| `no such vector schema index` on first query | Async index provisioning race | Phase 1.4's `wait_for_vector_index_online` handles this — make sure you're on the latest release |
| `VectorIndexNotOnlineError` after CREATE that should have run | Second CREATE silently no-op'd against existing index on `(:Node).embedding` | Drop the existing index first, then create the one you want — or use the existing name |
| Slow first query of the day | Cold cache after pause | Expected on Free; one-time per session |
| pgvector `expected N dimensions, not M` on upsert | Existing `vectors.embedding` column dim ≠ store's `dimensions=` kwarg | The store now fails fast at construction with this hint; pass the matching dim or DROP TABLE vectors |

## Next steps

* For local dev on the same backend shape:
  [`neo4j-local.md`](./neo4j-local.md)
* For the recommended-config side-by-side (local / cloud /
  Postgres-only): [`recommended-config.yaml`](./recommended-config.yaml)
* For the design rationale: [`../design/plan-neo4j-hardening.md`](../design/plan-neo4j-hardening.md)
