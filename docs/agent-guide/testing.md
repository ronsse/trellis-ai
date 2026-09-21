# Testing

Two topics: the **marker convention** that keeps the default run fast and
hermetic, and **finding fields that are copied everywhere and asserted
nowhere** — the coverage shape that has now shipped three times.

## Marker convention

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

### Markers at a glance

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

### When to add which marker

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

### ArcadeDB locally for tests

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

### Running tests

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

### How the gating works

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

## Fields copied everywhere and asserted nowhere

`scripts/audit_replicated_field_copies.py` ranks `Class.field` pairs by

```
gap = sites − pins
```

where **sites** is the number of places in `src/` that copy a value into that
keyword when constructing the class, and **pins** is the number of test
assertions that read the bare attribute name back. A large positive gap means
a field is written in many places and pinned in few — the shape that lets a
copy be constant-folded, mistyped, or read off the wrong object with the whole
suite green.

Read-only. It never edits a fixture and never proposes one.

```bash
# Rank the current tree; write the ranked table plus per-site detail.
python scripts/audit_replicated_field_copies.py --top 20 --output /tmp/copies.md

# Compare two trees — the only way to know the scan says anything.
python scripts/audit_replicated_field_copies.py \
    --src /path/to/other/src --tests /path/to/other/tests
```

### Why the obvious detector does not work

[#447](https://github.com/ronsse/trellis-ai/issues/447) named two fixture
shapes behind the sectioned path's missing rejection telemetry: **a population
sized 1** (so `len(dropped) == len(deduped)` and no assertion could separate
them) and **a pool too uniform to distinguish the field under test** (every
fixture item `item_type="document"`, so hard-coding `item_type` survived
everything). Both are real. Neither generalises into a scan.

A sweep for fixture uniformity was built and **refused on measurement**. Run
against the tree that still had the defect (`76bf565`) and the tree that fixed
it (`origin/main`), it returns **122 hits on each**, flagging both of #456's
fields on both sides. That is vacuous by #456's own standard — *"a suite green
on both sources proves equivalence only if it can tell them apart"* — applied
to a static scan rather than to a suite.

The reason is structural rather than a tuning problem: **#456 fixed the defect
by changing source and adding tests, and changed no fixture.** Fixture
uniformity is the *enabler*, not the *detector*, so it reads identically on
both sides of the fix by construction. What does move between the two trees is
the arithmetic between source copies and pinning assertions.

### Validation: the scan must discriminate the tree it was built from

| | pre-#456 (`76bf565`) | post-#456 (`1ef5c9c`) |
|---|---|---|
| `RejectedItem.item_type` | 7 sites / 5 pins / **gap +2** — rank 5 | absent |
| `RejectedItem.strategy_source` | 6 sites / 9 pins / gap −3 | absent |
| `RejectedItem.relevance_score` | 7 sites / 22 pins / gap −15 | absent |
| `RejectedItem.item_id` | 7 sites / 35 pins / gap −28 | absent |
| pairs at ≥2 sites | 69 | 63 |

Post-#456 the only `RejectedItem(` in `src/` is its own class definition: the
six hand-written copies became `RejectedItem.from_pack_item`, so the whole
family falls below the ≥2-site threshold and out of the ranking. The scan
separates the two trees. The uniformity sweep does not.

**It caught one of #456's two fields and missed the other, and the miss is the
more useful result.** `relevance_score` scored −15 because `pins` is keyed on
the *bare attribute name*: 22 assertions in the pre-#456 tree read some
`.relevance_score`, and exactly **one** of them has a receiver that could be a
`RejectedItem` (`rejected[0]`) — the rest are served items, pack items and
tuned scores. #456 then proved that same field uncovered at five of its six
sites. So the column over-counted by roughly 21×, in the direction that hides
a defect.

> `gap` is a **lower bound**, and the error runs one way by construction.
> `pins` cannot separate one class's field from another's with the same name,
> so it only ever over-counts. **A high gap is trustworthy. A low or negative
> gap is not evidence of coverage** — it can be off by an order of magnitude
> when a common field name is asserted about a different class. Only a mutant
> settles it.

One deliberate exclusion: `operation=Operation.ENTITY_CREATE` names an **enum
member**, not an instance read. Folding it to a constant *is* the constant, so
there is nothing to detect; only a read off a lower-case binding can silently
diverge. Without that rule `Command.operation` topped the first ranking at
28–31 sites, entirely on enum references.

### The ranking on `1ef5c9c` (63 pairs at ≥2 sites)

| Class.field | sites | same-name | pins | gap |
|---|---:|---:|---:|---:|
| `CommandResult.command_id` | 11 | 11 | 0 | **+11** |
| `CommandResult.operation` | 11 | 11 | 7 | +4 |
| `BulkItemResult.id` | 3 | 0 | 0 | +3 |
| `CommandResponse.command_id` | 3 | 3 | 0 | +3 |
| `ClassificationResult.classifier_name` | 12 | 0 | 10 | +2 |
| `ExtractionDispatcher.event_log` | 3 | 3 | 1 | +2 |
| `PluginEntry.distribution` | 3 | 3 | 1 | +2 |
| `PluginEntry.distribution_version` | 3 | 3 | 1 | +2 |
| `InputDigest.hash` | 2 | 0 | 0 | +2 |
| `InputDigest.length` | 2 | 0 | 0 | +2 |

### Mutants on the top three

Baseline for every run below: the full default selection on `1ef5c9c` —
**7,701 passed / 25 skipped / 366 deselected / 291.78s**.

| Mutant | Change | Selection | Verdict |
|---|---|---|---|
| **M1** | `CommandResult.command_id` → `"mutant-cid"` at all **11** sites | full default | **SURVIVED** (294.94s) |
| **M2** | `CommandResult.operation` → `"entity.create"` at all **11** sites | full default | **KILLED** — `tests/unit/cli/test_curate.py::TestCuratePromote::test_promote_json` |
| **M3** | `CommandResponse.command_id` → `"mutant-cid"` at all **3** REST sites | full default | **SURVIVED** (286.88s) |

M2 is the control the ranking predicted: it is the one of the three carrying
pre-existing pins (7 against 0), and it is the one that dies.

**M1 is a larger instance of #456's shape in a second subsystem.** Eleven
sites against #456's six, and *zero* pre-existing pins against its four.
`command_id` is the correlation key every surface hands back — 17 readers
across MCP (5), the CLI (8), the REST routes (3, which are M3's own
construction sites) and `mutate/evidence.py` (1, a further forward-copy) — and
under batch execution (`SEQUENTIAL` / `CONTINUE_ON_ERROR`) it is the **only**
way to attribute a `CommandResult` to the `Command` that was submitted. Same
"handed to a caller, rendered, never branched on" shape as #456's
`RejectedItem` fields, on a programmatic contract rather than a screen.

The truthiness checks are worth naming, because they are the mechanism rather
than an absence of one. `tests/unit/cli/test_curate.py` asserts
`data["command_id"]` at three places and `tests/unit/mcp/test_execute_mutation.py`
asserts `payload["command_id"]`; every one is satisfied by any non-empty string.
The field is exercised on every one of those paths and pinned on none of them,
which is why `pins` reads 0 while the code is demonstrably reached.

#### M2 per site — the covered fraction

M2 dies as an aggregate, which says nothing about *which* of its 11 sites are
covered. Bisecting each site separately:

| `executor.py` line | pipeline outcome | verdict |
|---|---|---|
| 130 | validate failure (`FAILED`) | **KILLED** |
| 158, 226, 245 | `REJECTED` | SURVIVED |
| 178, 196, 262 | `DUPLICATE` | SURVIVED |
| 208, 286, 311 | `FAILED` | SURVIVED |
| 327 | `SUCCESS` | **KILLED** |

Those nine were bisected against a targeted selection
(`tests/unit/{cli,mutate,api,mcp,extract,workers,schemas}`), which is sound for
a KILL but not for a SURVIVAL. Folding all nine at once (**M2′**) against the **full
default selection** re-runs the survival claim where it has to hold:
**SURVIVED — 7,701 passed / 25 skipped / 366 deselected / 284.67s**, the
baseline's own numbers.

**Nine of eleven sites carry no assertion at all**, and the two that do are
the two an end-to-end CLI test happens to walk. That reproduces #456's exact
proportion — one covered site out of many — in a second subsystem, and it is
the empirical face of the over-count above: seven name-matched "pins" against
two genuinely covered sites.

#### What the whole-file survivals rest on

The aggregate-to-per-site inference is used three times above, so state its
limit. A test that
kills a single-site mutant also kills the whole-file fold — **unless** it
asserts an equality *between* two of the folded sites, which folding both would
restore. No such test exists for these fields: every `command_id` assertion in
`tests/` is either a truthiness check or a comparison against an event payload
or a hand-built fixture, none compares two `CommandResult.command_id` values,
and a test asserting the eleven were *distinct* would have killed M1 outright.
Subject to that, the whole-file survivals of M1, M3 and M2′ establish per-site
survival without 11, 3 and 9 further runs.

### How to use this, and how not to

- **A high gap is a question, not a finding.** Write the mutant. The ranking's
  job is to decide *which* mutant is worth five minutes of suite time.
- **Do not mass-edit fixtures off the ranking.** Uniformity is the enabler and
  is not what the gap measures; a fixture sweep is the thing this method was
  built to replace.
- **Run the scan on a clean tree.** Reading a tree while a mutation runner is
  editing it produced a wrong `CommandResult.operation` row (10 sites, not 11)
  during this work.
- **Restore mutants from a pristine copy held outside the tree**, never
  `git checkout -- <path>` — that restores from the *index* and has silently
  wiped uncommitted work here before.
- **A KILL is sound from any subset of the suite; a SURVIVAL claim needs the
  full default selection.** That asymmetry is what makes a two-tier bisect
  honest as well as cheap: run the targeted selection first, escalate only the
  survivors.
- **Commit a regenerated OpenAPI spec before running mutants.** `make
  openapi-check` is `git diff --exit-code`, so an uncommitted spec
  regeneration makes *every* mutant report as killed.
