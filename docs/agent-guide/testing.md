# Testing — marker convention

Trellis uses **opt-in pytest markers** to keep the default test run fast and
hermetic. Heavy tests (live backends, slow subprocess boots, optional extras)
are tagged with markers that the default `pytest` invocation deselects, and
each marker has a `--include-<name>` CLI flag (and a `TRELLIS_TEST_<NAME>=1`
environment variable) that re-enables it.

The wiring lives in three places:

- `pyproject.toml` registers the markers and sets the default-exclude
  expression in `addopts`.
- `tests/conftest.py` registers the `--include-<name>` CLI flags and
  rewrites the active mark expression at `pytest_configure` time so the
  opted-in markers stop being filtered out.
- Individual test modules apply the markers via `pytestmark = [...]` at
  module level or `@pytest.mark.<name>` per test.

## Markers at a glance

| Marker     | When to add it                                                                 | CLI flag              | Env var                  |
|------------|--------------------------------------------------------------------------------|-----------------------|--------------------------|
| `live`     | Test requires a real backend (uvicorn, real Postgres, real Neo4j, real S3).    | `--include-live`      | `TRELLIS_TEST_LIVE=1`    |
| `slow`     | Test typically takes longer than ~5s (subprocess boots, multi-round loops).    | `--include-slow`      | `TRELLIS_TEST_SLOW=1`    |
| `neo`      | Test requires Neo4j. `neo4j` is a registered synonym (already used by tests).  | `--include-neo`       | `TRELLIS_TEST_NEO=1`     |
| `postgres` | Test requires Postgres (`TRELLIS_TEST_PG_DSN`).                                | `--include-postgres`  | `TRELLIS_TEST_POSTGRES=1`|
| `pgvector` | Test requires Postgres with the `pgvector` extension.                          | `--include-pgvector`  | `TRELLIS_TEST_PGVECTOR=1`|
| `arcadedb` | Test requires a running ArcadeDB instance (set TRELLIS_TEST_ARCADEDB_URI).     | `--include-arcadedb`  | `TRELLIS_TEST_ARCADEDB=1`|

`neo` and `neo4j` are both registered marker names and `--include-neo` (or
`TRELLIS_TEST_NEO=1`) gates both. New tests should prefer `neo`; existing
tests using `pytest.mark.neo4j` keep working unchanged.

## When to add which marker

Add `@pytest.mark.live` whenever the test:

- spawns a real `uvicorn` (or anything that opens a TCP socket on a real
  HTTP server), **or**
- connects to a real Postgres / Neo4j / S3 endpoint, **or**
- depends on cloud credentials (AuraDB, Neon, AWS) being set in the
  environment.

Add `@pytest.mark.slow` whenever the test:

- spawns a subprocess that has Python interpreter cold-start cost
  (e.g. the `trellis` or `trellis-mcp` console scripts), **or**
- does a `time.sleep(>1)` in steady state, **or**
- runs more than a few seconds in CI under typical conditions.

Add the backend-specific marker (`neo`, `postgres`, `pgvector`,
`arcadedb`) whenever the test imports from / talks to that backend,
even if it also carries `live`. The backend markers let CI matrices
target a single backend without picking up unrelated live tests.

## ArcadeDB locally for tests

Stand up an ArcadeDB container with the Neo4j-Bolt plugin enabled so
the `neo4j` Python driver can connect:

```bash
docker run -d --name trellis-arcadedb \
  -p 2480:2480 -p 7687:7687 \
  -e JAVA_OPTS='-Darcadedb.server.rootPassword=playwithdata \
                -Darcadedb.server.plugins=Bolt:com.arcadedb.bolt.BoltProtocolPlugin' \
  arcadedata/arcadedb:latest

export TRELLIS_TEST_ARCADEDB_URI=bolt://localhost:7687
export TRELLIS_TEST_ARCADEDB_USER=root
export TRELLIS_TEST_ARCADEDB_PASSWORD=playwithdata
export TRELLIS_TEST_ARCADEDB_HTTP_URL=http://localhost:2480
export TRELLIS_TEST_ARCADEDB_DATABASE=trellis_test

pytest tests/unit/stores/contracts/test_arcadedb_graph_contract.py \
       tests/unit/stores/test_arcadedb_vector.py --include-arcadedb -v
```

The graph contract suite (`test_arcadedb_graph_contract.py`) reuses the
same `GraphStoreContractTests` that the Neo4j backend runs — 76 tests,
all green against ArcadeDB. The vector tests
(`test_arcadedb_vector.py`) cover the shape-#2 paired-graph workflow:
nodes created via Cypher, embeddings attached via SQL.

A test can carry multiple markers — the loop suite under
`tests/integration/loops/` carries `live`, `slow`, `neo4j`, and `postgres`
because it spawns uvicorn against Neon + AuraDB and runs multi-round
end-to-end scenarios.

## Running tests

```bash
# Default — fast, hermetic. No live backends. No slow subprocess boots.
pytest

# Include live tests (still respects -m if you also pass it).
pytest --include-live

# Include just the Neo4j live tests.
TRELLIS_TEST_NEO4J_URI=neo4j+s://... TRELLIS_TEST_NEO4J_PASSWORD=... \
    pytest --include-neo

# Include Postgres live tests via env var (no CLI flag).
TRELLIS_TEST_POSTGRES=1 TRELLIS_TEST_PG_DSN=postgresql://... pytest

# Include slow CLI subprocess smoke tests.
pytest --include-slow tests/integration/cli/test_subprocess_smoke.py

# Run the full live cloud-shape suite (loops, API, SDK).
TRELLIS_TEST_NEO4J_URI=... TRELLIS_TEST_NEO4J_PASSWORD=... \
TRELLIS_TEST_PG_DSN=... \
    pytest --include-live --include-slow --include-neo --include-postgres

# Discover all live tests without running them (useful for CI sharding).
pytest -m live --collect-only
```

## How the gating works

`tests/conftest.py:pytest_configure` reads the `--include-<name>` flags
and the matching env vars, then strips matching `not <marker>` segments
from the active `-m` expression before pytest's collection filter runs.
This means:

- `pytest --include-live` re-enables tests carrying `live` while keeping
  the other exclusions intact (so `live + slow` tests still get filtered
  unless you also pass `--include-slow`).
- `pytest -m live` (without `--include-live`) **also** works — passing
  `-m` on the command line replaces the filter wholesale, so the default
  exclusions no longer apply to the explicitly-requested marker.
