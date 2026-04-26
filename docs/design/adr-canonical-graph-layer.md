# ADR: Canonical Graph Layer — Universal Surface, Per-Backend Translation

**Status:** Phases 0-3 landed (Phase 4 vector DSL deferred — informational only)
**Date:** 2026-04-24 (Phases 0-3 completed 2026-04-25)
**Deciders:** Trellis core
**Related:**
- [`./adr-planes-and-substrates.md`](./adr-planes-and-substrates.md) — defines the Knowledge / Operational plane split that this ADR sits inside
- [`./adr-plugin-contract.md`](./adr-plugin-contract.md) — entry-point-based backend registration; this ADR strengthens the per-backend contract that plugins must satisfy
- [`./adr-graph-ontology.md`](./adr-graph-ontology.md) — names the well-known entity / edge types; this ADR governs how those names are *queried* across backends
- [`../../src/trellis/stores/base/graph.py`](../../src/trellis/stores/base/graph.py) — current `GraphStore` ABC
- [`../../src/trellis/stores/base/vector.py`](../../src/trellis/stores/base/vector.py) — current `VectorStore` ABC
- [`../../src/trellis/stores/sqlite/graph.py`](../../src/trellis/stores/sqlite/graph.py), [`postgres/graph.py`](../../src/trellis/stores/postgres/graph.py), [`neo4j/graph.py`](../../src/trellis/stores/neo4j/graph.py) — three current implementations
- [`../../tests/unit/stores/test_graph_store.py`](../../tests/unit/stores/test_graph_store.py), [`test_temporal_graph.py`](../../tests/unit/stores/test_temporal_graph.py), [`test_neo4j_graph.py`](../../tests/unit/stores/test_neo4j_graph.py) — three parallel test files exercising the same contract

---

## 1. Context

### What we have today

`GraphStore` and `VectorStore` are ABCs with method-level signatures and prose docstrings. Each backend (`sqlite`, `postgres`, `neo4j` for graph; `sqlite`, `pgvector`, `lancedb`, `neo4j` for vector) implements the methods in its native dialect: SQL with JSONB on Postgres, SQL with `json_extract` on SQLite, Cypher on Neo4j, table API on LanceDB. The ABCs constrain *what* each method accepts and returns. They do not constrain *how* it filters, sorts, or fails.

### Where the abstraction is leaking

Adding the Neo4j backend made the leaks impossible to ignore. Concrete ones:

1. **`query()` semantics drift across backends.** The `properties` filter accepts a flat dict. Postgres compiles each key to `properties->>'key' = %s`, SQLite to `json_extract(properties_json, '$.key') = ?`. Both support exact-match equality on scalar values; nested objects fall through to Python-side filtering with no way for the caller to know that happened. Neo4j currently routes everything through Python-side post-fetch filtering with an over-fetch multiplier. **Same call, three different code paths, three different failure modes when the value is non-scalar.**

2. **`get_subgraph()` algorithms diverge.** Postgres uses a recursive CTE with depth tracking; SQLite uses an iterative BFS in Python; Neo4j uses a native variable-length path match. The depth and edge-type filters mean slightly different things in each (CTE depth is exclusive vs Cypher path length is inclusive in some cases — easy to get wrong by one). There is no shared semantic spec that the three implementations are *required* to agree on.

3. **`as_of` temporal reads are re-implemented per backend.** Each backend writes its own `valid_from <= ? AND (valid_to IS NULL OR valid_to > ?)` clause inline in every query. Neo4j writes the equivalent in Cypher. The temporal contract — "what does `as_of` mean exactly?" — is documented in prose only.

4. **Vector store metadata filters are even worse.** pgvector uses JSONB containment (`@>`); LanceDB uses post-fetch Python filters; Neo4j stores metadata as a JSON-encoded string property and post-filters in Python; SQLite uses `json_extract`. **Four backends, four filter semantics.** The ABC just says "filters: `dict[str, Any] | None`" with no specification of operator support.

5. **Tests are parallel, not shared.** [`test_graph_store.py`](../../tests/unit/stores/test_graph_store.py) is the SQLite suite. [`test_temporal_graph.py`](../../tests/unit/stores/test_temporal_graph.py) is shared *style* but only runs against SQLite. The Postgres suite is gated on `TRELLIS_TEST_PG_DSN`; the Neo4j suite on `TRELLIS_TEST_NEO4J_URI`. Each suite was written by hand to exercise the contract — but they exercise *different test cases*. We cannot tell whether a given semantic guarantee holds on all backends because no test runs against all backends simultaneously.

### Why this is the right time to fix it

The cost of fixing this scales with the number of backends. We have 3 graph backends and 4 vector backends today, and the Neo4j work made the cracks visible. Doing this *before* the next backend lands (cloud Neo4j, Memgraph, TigerGraph, ArangoDB, Neptune Analytics — any of which we could plausibly add in the next year) means each new backend is bound by a contract test suite, and the ratio of "shared semantics tested" to "per-backend code" goes up monotonically.

### What the user pointed at

> "I think it's important even if graph store and vector store are required for postgres. It doesn't mean that there isn't a set of translation layers to the canonical store for different data stores. I think, ultimately, a graph shape is the ideal shape."

The canonical surface is graph-shaped. Each backend — *including row-store backends like Postgres* — has a translation layer that compiles canonical queries to its native dialect. The canonical layer is the agent-facing surface; native dialects exist only inside the translation boundary.

This is not the same as "use Neo4j everywhere." It is "use a small, stable graph-shaped query surface that all backends agree on, and let each backend implement it however its substrate makes efficient."

### What is *not* in scope here

- We are **not** designing a new ABC to replace `GraphStore` and `VectorStore`. The current ABCs are fine as the imperative entry-point. The canonical layer sits *inside* the read-path methods, not above them.
- We are **not** building a SPARQL/Gremlin/Cypher-equivalent general-purpose graph query language. The canonical surface is deliberately small — the operations we already do, just specified.
- We are **not** changing storage on disk. This is purely about the read-side contract.

### The decision to make

Do we:
- **(A)** Leave the ABCs as-is; rely on prose docstrings + per-backend test suites to keep semantics aligned
- **(B)** Define a small canonical query DSL (data-class-shaped) plus a parameterized contract test suite that all backends must pass; backends compile the DSL to native dialect
- **(C)** Build a full canonical query language with optimizer, plan cache, and cross-backend join support
- **(D)** Adopt an external standard (GQL, openCypher) as the canonical layer

---

## 2. Decision

**Option B: small canonical DSL + parameterized contract test suite. Phase 0 ships only the contract test suite. The DSL lands in Phase 1 once the contract makes drift visible.**

Three components, shipped in order:

1. **A parameterized `GraphStoreContract` test suite** (Phase 0) — every backend is required to pass identical semantic tests. Drift fails CI, not production.
2. **A small `GraphQuery` DSL** (Phase 1) — typed dataclass-shaped representations of the operations we already do (node-by-id, node-by-type-and-properties, subgraph-from-seeds, edges-of-node, history). Each operation has documented filter operator support (`eq`, `in`, `exists`) and a documented temporal contract.
3. **Per-backend compilers** (Phase 2) — each backend implements `_compile_query(GraphQuery) -> NativeQuery` and routes existing `query()` / `get_subgraph()` calls through the compiler. The public ABC method signatures don't change — callers keep calling `query(node_type=..., properties=...)`.

The reason for this ordering matters: **the contract test suite is the highest-leverage Phase 0 because it exposes drift today, before we know which DSL shape would best capture the divergence.** Building the DSL first risks designing for problems we haven't measured.

### 2.1 Why not adopt openCypher / GQL as the canonical (Option D)

Considered, rejected. openCypher is an excellent language — but adopting it as Trellis's canonical layer means:

- Every backend needs an openCypher parser/compiler (heavyweight; nothing off-the-shelf for SQLite or LanceDB).
- Agents writing extraction code now write Cypher fragments they have to debug.
- We pay the surface area cost for capabilities (general path expressions, MATCH/RETURN composition, function calls) we do not exercise.

The canonical surface should be **smaller** than openCypher, not the same size. Trellis only needs about a dozen distinct read operations across the entire codebase. A DSL sized to those operations compiles trivially to SQL, Cypher, or table-API; openCypher does not.

### 2.2 Why not a full query optimizer (Option C)

We do not have a multi-tenant, multi-backend, query-volume problem. We have a "three backends drift" problem. An optimizer is a 50× larger surface than the problem requires.

### 2.3 Why not "do nothing" (Option A)

Drift is silent. The test suites cover different cases; the prose docstrings describe behaviour in vague terms ("filters by properties" — operator support unspecified). A bug where Postgres exact-matches integer `0` against string `"0"` while Neo4j doesn't (or vice versa) survives forever. We are paying the cost already, just not noticing.

---

## 3. Phase 0 — the contract test suite

### 3.1 Shape

A new file: [`tests/unit/stores/contracts/graph_store_contract.py`](../../tests/unit/stores/contracts/graph_store_contract.py) (does not exist yet). It exposes a `GraphStoreContractTests` class with methods that exercise every documented behaviour:

```python
class GraphStoreContractTests:
    """Run against every GraphStore backend.

    Subclasses set ``store_factory`` to a callable returning a fresh
    GraphStore instance.
    """

    store_factory: Callable[[], GraphStore]

    def test_upsert_node_returns_id(self): ...
    def test_upsert_node_creates_initial_version(self): ...
    def test_upsert_node_closes_old_version_on_update(self): ...
    def test_get_node_returns_none_for_missing(self): ...
    def test_get_node_as_of_returns_correct_version(self): ...
    def test_query_filters_by_node_type(self): ...
    def test_query_filters_by_scalar_property_eq(self): ...
    def test_query_filters_by_in_operator(self): ...
    def test_query_returns_empty_on_unknown_type(self): ...
    def test_subgraph_seeds_at_depth_zero(self): ...
    def test_subgraph_respects_depth_limit(self): ...
    def test_subgraph_filters_by_edge_type(self): ...
    def test_subgraph_temporal(self): ...
    def test_compact_versions_drops_closed_only(self): ...
    # ... ~40-60 tests covering the full ABC surface
```

Each backend has a thin file that subclasses and provides the factory:

```python
# tests/unit/stores/contracts/test_sqlite_contract.py
class TestSQLiteGraphContract(GraphStoreContractTests):
    @pytest.fixture(autouse=True)
    def _store(self, tmp_path):
        self.store_factory = lambda: SQLiteGraphStore(tmp_path / "g.db")
```

```python
# tests/unit/stores/contracts/test_postgres_contract.py
@pytest.mark.skipif(not os.environ.get("TRELLIS_TEST_PG_DSN"), ...)
class TestPostgresGraphContract(GraphStoreContractTests):
    @pytest.fixture(autouse=True)
    def _store(self): ...
```

```python
# tests/unit/stores/contracts/test_neo4j_contract.py
@pytest.mark.skipif(not os.environ.get("TRELLIS_TEST_NEO4J_URI"), ...)
class TestNeo4jGraphContract(GraphStoreContractTests):
    @pytest.fixture(autouse=True)
    def _store(self): ...
```

### 3.2 What this catches

- **Operator drift.** Every filter test specifies the exact operator and the exact result. If Postgres and Neo4j disagree, both run the test, one fails.
- **Semantic drift in `as_of` reads.** Same fixture data, same `as_of` timestamp, same expected result.
- **`get_subgraph` depth semantics.** `depth=2` means the same thing on every backend (we pick a definition and codify it).
- **Empty-result behaviour.** Does `query(node_type="ghost")` return `[]` or raise? Codified.
- **Error paths.** Does `delete_node("missing")` return `False` or raise? Codified.

### 3.3 Existing per-backend tests stay

The current [`test_graph_store.py`](../../tests/unit/stores/test_graph_store.py) (SQLite-flavoured), [`test_neo4j_graph.py`](../../tests/unit/stores/test_neo4j_graph.py), and the implicit Postgres path stay where they are. They test backend-specific concerns (Neo4j `:Node` constraint shape; Postgres index existence; SQLite migration paths) that the contract suite has no business knowing. The contract suite is *additive* — it covers the shared semantics; per-backend suites cover the implementation details.

### 3.4 Phase 0 footprint

| Deliverable | Status | Footprint |
|---|---|---|
| `tests/unit/stores/contracts/graph_store_contract.py` — `GraphStoreContractTests` base (35 tests, ~365 lines) | **Landed (first slice)** | 365 lines |
| `tests/unit/stores/contracts/test_sqlite_graph_contract.py` | **Landed** | 21 lines |
| `tests/unit/stores/contracts/test_postgres_graph_contract.py` (env-gated, `TRELLIS_TEST_PG_DSN`) | **Landed** | 38 lines |
| `tests/unit/stores/contracts/test_neo4j_graph_contract.py` (env-gated, `TRELLIS_TEST_NEO4J_URI`) | **Landed** | 43 lines |
| `tests/unit/stores/contracts/vector_store_contract.py` — `VectorStoreContractTests` base (25 tests, ~250 lines) | **Landed** | 250 lines |
| `tests/unit/stores/contracts/test_sqlite_vector_contract.py` | **Landed** | 21 lines |
| `tests/unit/stores/contracts/test_pgvector_contract.py` (env-gated) | **Landed** | 39 lines |
| `tests/unit/stores/contracts/test_lancedb_vector_contract.py` (skipped if lancedb absent) | **Landed** | 28 lines |
| Neo4j vector contract (shape #2) | **Excluded by design** | covered in `test_neo4j_vector.py` instead — see deviation below |
| Expanded `GraphStoreContractTests` — node-role validation, document_ids, history compaction, alias time-travel, `as_of` for subgraph / query / edges | Pending | ~250 lines (estimate) |
| Update `CLAUDE.md` and the plugin-contract ADR to point new backends at the contract suite | Pending | ~30 lines |

**Graph contract landed 2026-04-25:** ~470 lines of test code, 35 tests × 3 backends. Covers the high-volume drift surfaces: upsert / get / SCD-2 versioning / `as_of` / query / bulk read / edges / subgraph / aliases / deletion / counts. SQLite passes 35/35; Postgres + Neo4j subclasses skip until env vars set. One pre-existing mypy lambda inference issue in `neo4j/graph.py` fixed at the same time — no semantic change.

**Vector contract landed 2026-04-25:** ~340 lines of test code, 25 tests covering 3 backends (SQLite, pgvector env-gated, LanceDB importorskip-gated). SQLite passes 25/25; LanceDB passes 25/25 (lancedb installed locally to exercise it). pgvector skips until env var set. **Coverage validated the highest-divergence surface in the project:** SQLite uses brute-force cosine, pgvector uses HNSW + JSONB containment filters, LanceDB does post-fetch Python filters — and all three agree on every contract test.

### 3.5 Neo4j vector deviation

`Neo4jVectorStore` is **excluded by design** from `VectorStoreContractTests`. Shape #2 (per [`adr-graph-ontology.md`](./adr-graph-ontology.md) — embeddings as optional properties on the graph store's `:Node` rows) makes its `upsert(item_id, vector)` require the underlying node to already exist as a current version. The contract assumes vector backends create storage independently; shape #2 is a deliberate cross-store optimization that violates that assumption.

The shape #2 contract — including the "missing node raises", "delete strips embedding but keeps node", "historical versions excluded by `valid_to IS NULL` filter" tests — lives in [`tests/unit/stores/test_neo4j_vector.py`](../../tests/unit/stores/test_neo4j_vector.py) and runs against a real Neo4j instance via `TRELLIS_TEST_NEO4J_URI`. This is the right home for them: shape #2 *is* the contract for that backend.

The expansion items above land as follow-ups when their gaps are felt (e.g., when the next graph backend lands and forces a contract violation).

### 3.5 What Phase 0 does *not* ship

- No DSL.
- No compiler.
- No new ABC method.
- No per-backend behaviour change. Existing implementations either pass the contract or get fixed; they don't get reorganized.

---

## 4. Phase 1 — the canonical DSL (sketch only, not approved here)

Once Phase 0 has been live for one release and we know which contract tests are most-violated, we design the DSL around those gaps. Provisional shape:

```python
@dataclass(frozen=True)
class FilterClause:
    field: str                                   # "node_type" | "properties.key" | ...
    op: Literal["eq", "in", "exists", "ne"]
    value: Any | tuple[Any, ...]                 # scalar or tuple for "in"

@dataclass(frozen=True)
class NodeQuery:
    filters: tuple[FilterClause, ...] = ()
    limit: int = 50
    as_of: datetime | None = None

@dataclass(frozen=True)
class SubgraphQuery:
    seed_ids: tuple[str, ...]
    depth: int = 2
    edge_type_filter: tuple[str, ...] | None = None
    as_of: datetime | None = None
```

Each backend implements:

```python
class GraphStore(ABC):
    # New, alongside existing methods
    def execute_node_query(self, q: NodeQuery) -> list[dict[str, Any]]: ...
    def execute_subgraph_query(self, q: SubgraphQuery) -> SubgraphResult: ...
```

The existing `query()` / `get_subgraph()` methods become thin shims that build a DSL value and call `execute_*`. Callers don't have to migrate; agents that want richer filtering opt into the DSL.

**Operators supported in Phase 1:** `eq`, `in`, `exists`. Specifically:
- `eq` on scalar values (str / int / float / bool).
- `in` on a tuple of scalars.
- `exists` on a property path (does the JSON path resolve to a non-null value).

**Operators explicitly out of scope for Phase 1:** range (`gt` / `lt`), regex match, full-text search (handled by `DocumentStore`, not `GraphStore`), nested path traversal beyond one level. Each of these can graduate later when a consumer asks.

This subset is small enough that:
- SQLite compiles via `json_extract`.
- Postgres compiles via `properties->>'key' = %s` and `?` operators.
- Neo4j compiles via `WHERE n.properties.key = $value` and `IN [...]`.
- LanceDB and pgvector vector-side compile to their respective filter languages.

### 4.1 Why dataclasses, not strings

Two reasons:
1. **Type-checked.** The DSL is verified at static-analysis time. A string DSL would need its own parser and grammar; a dataclass DSL is just Python.
2. **Round-trippable.** The DSL serializes trivially to JSON for telemetry / pack metadata; a string DSL ties us to its parser even for inspection.

---

## 5. Phase 2 — per-backend compilers (sketch only, not approved here)

Each backend gains a `_compile_*` method that takes a DSL value and returns a native query (SQL string + params, or a Cypher string + params, or a table-API call chain). The compiler is pure and unit-testable — no I/O.

```python
class PostgresGraphStore:
    def _compile_node_query(self, q: NodeQuery) -> tuple[str, list[Any]]:
        sql_parts = ["SELECT * FROM nodes WHERE"]
        params: list[Any] = []
        sql_parts.append(self._temporal_filter_sql(q.as_of))
        params.extend(self._temporal_params(q.as_of))
        for clause in q.filters:
            sql_parts.append("AND")
            sql_parts.append(self._compile_clause(clause, params))
        sql_parts.append(f"LIMIT {q.limit}")
        return " ".join(sql_parts), params

    def execute_node_query(self, q: NodeQuery) -> list[dict[str, Any]]:
        sql, params = self._compile_node_query(q)
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            return [self._node_row_to_dict(r) for r in cur.fetchall()]
```

Each backend gets ~100-200 lines of compiler. Total Phase 2 footprint: ~600 lines of source + ~400 lines of compiler unit tests, across all backends.

The contract tests from Phase 0 keep running against both code paths during the migration — `query()` still routes through the legacy path, `execute_node_query()` routes through the compiler. Once the compiler covers a method, the legacy path becomes a thin shim and the per-backend bespoke code deletes.

---

## 6. Phase 3 — promote new backends to canonical-only (sketch only, not approved here)

Once Phases 0-2 land and the DSL has covered the read surface we exercise, the [plugin contract ADR](./adr-plugin-contract.md) gets updated: new backends added via entry points must implement `execute_*` against the DSL. They are not permitted to reach inside the legacy `query()` shim. Existing built-ins keep both paths during a deprecation window.

This is the long-term win: a third-party Memgraph or ArangoDB backend implements the small DSL and inherits the entire contract test suite for free. The "add a new backend" story collapses from "re-implement 14 methods, write a parallel test file, hope you got the semantics right" to "implement two `execute_*` methods, plug into the contract suite, ship."

---

## 7. Guardrails

The ADR is only safe if Phase 0 is genuinely zero-source-change and the later phases stay opt-in.

### 7.1 Phase 0 is test-only

No code in `src/` changes during Phase 0. If a backend fails a contract test, the fix happens *before* the contract test ships — i.e., we land each contract test alongside the fix it surfaces, not in a giant batch. This way the test suite is always green at HEAD; backends never ship with known violations.

### 7.2 The DSL is additive

`execute_node_query` lives alongside `query`, not as a replacement. Existing callers keep calling `query`. Agents that want operators richer than `eq` opt into the DSL. No deprecation pressure on existing code paths during Phase 1.

### 7.3 The DSL is small

The Phase 1 surface is fixed: `eq` / `in` / `exists`. Adding an operator requires an ADR amendment and a contract-test extension. This is the same gating policy as the [tag vocabulary ADR](./adr-tag-vocabulary-split.md) uses for new reserved namespaces — small core, gated growth.

### 7.4 Backend compilers are pure

Compilers take a DSL value, return a native query + params. No I/O, no logging, no side effects. Means we can unit-test compilers exhaustively without standing up a database.

### 7.5 The contract suite documents semantics

Where the prose docstrings on `GraphStore` are vague, the contract tests are precise. The contract suite *becomes* the spec: if a behaviour isn't in the contract suite, it isn't part of the contract. New per-backend optimizations must not change observable behaviour the suite covers.

---

## 8. Scope — Phases 0-3 landed (revised 2026-04-25)

The original ADR committed only to Phase 0 and listed Phases 1-3 as informational-only. The user explicitly directed (2026-04-25) to land all four phases together as one cohesive change, on the rationale that this is greenfield development and the gating signals (release cadence, drift bugs in the wild) don't apply.

Phase 4 (vector DSL) remains deferred — see §8.5.

### 8.1 What Phases 0-3 ship (landed 2026-04-25)

| Phase | Scope | Status |
|---|---|---|
| **Phase 0** | Parameterized `GraphStoreContractTests` (49 tests) + `VectorStoreContractTests` (25 tests); per-backend subclasses for SQLite (always runs), Postgres (env-gated), Neo4j (env-gated), pgvector (env-gated), LanceDB (importorskip-gated). Plus `CLAUDE.md` and `adr-plugin-contract.md` pointer updates directing new backends at the contract suite. | **Landed** |
| **Phase 1** | `FilterClause` / `NodeQuery` / `SubgraphQuery` / `SubgraphResult` DSL in `src/trellis/stores/base/graph_query.py`. New `execute_node_query` / `execute_subgraph_query` methods on the `GraphStore` ABC with a default implementation that routes through `query()` / `get_subgraph()`. Default routing supports `eq` only (raises `NotImplementedError` on `in` / `exists` to surface the gap loudly). DSL unit tests in `tests/unit/stores/test_graph_query_dsl.py`. | **Landed** |
| **Phase 2** | Per-backend `_compile_*` methods + `execute_node_query` overrides. SQLite uses `json_extract`. Postgres uses JSONB containment (`@>`) for type-aware comparisons + `?` for exists. Neo4j compiles the structural filter to Cypher and applies property predicates client-side after JSON-decoding `properties_json` (matching the legacy `query()` strategy). All three backends pass the full `eq` / `in` / `exists` operator surface against `properties.<key>` and the top-level columns. | **Landed** |
| **Phase 3** | `adr-plugin-contract.md` extended with a "Required: implement the canonical DSL" subsection. Plugin backends must override `execute_node_query` / `execute_subgraph_query` and pass the DSL contract tests; the default ABC routing exists only as a migration aid for the in-tree built-ins, not as a sanctioned plugin path. | **Landed** |

### 8.2 What Phases 0-3 deliberately do *not* ship

- **No deprecation of legacy `query()` / `get_subgraph()`.** Existing callers keep working unchanged. The DSL is additive. Removing the legacy methods would break the SDK, MCP server, retrieval, etc.; that's a separate ADR if it ever happens.
- **No optimizer.** Compilers do straight translation. No plan caching, no rewrite rules. The contract is correctness, not performance.
- **No new operators beyond `eq` / `in` / `exists`.** Range comparisons, regex match, full-text — out of scope. Adding any one is an ADR amendment plus a contract-test extension.
- **No JSON-path traversal beyond one level.** `properties.<key>` is supported; `properties.<key>.<subkey>` is not. Same gating as above.

### 8.3 What live testing this enables

The `Postgres` and `Neo4j` contract subclasses run the full Phase 1-2 surface (`eq` / `in` / `exists` × top-level / properties × all the temporal / role / document_ids / aliases / subgraph tests). When the gating env vars are set the suite runs against real backends, surfacing any compilation / dialect drift immediately. Today only SQLite and LanceDB run live in this repo; Postgres + Neo4j are ready and waiting.

### 8.4 The litmus test (revised)

After Phases 0-3 land:

1. `mypy src/` clean.
2. `pytest tests/unit/stores/contracts/ tests/unit/stores/test_graph_query_dsl.py` reports a sea of green for SQLite, LanceDB; clean skips for Postgres, pgvector, Neo4j (until env vars set).
3. `pytest tests/unit/stores/test_graph_store.py` (the legacy SQLite suite) still passes — the DSL is additive, not a replacement.

### 8.5 Phase 4 — Vector DSL (deferred, informational)

| Phase | Scope | Gating signal |
|---|---|---|
| **Phase 4** | Vector store equivalent — `VectorQuery` DSL with operator-spec'd metadata filters | The vector contract suite (Phase 0 part 2) shows recurring drift, OR a plugin author wants strongly-typed vector filters. Neither has fired yet — the existing pgvector / LanceDB / SQLite vector backends agreed on every contract test in the first run. |

Phase 4 remains genuinely speculative. Surfacing this ADR's commit to "land Phase 4 when drift fires" is enough; we don't pay for it preemptively.

---

## 9. Consequences

### 9.1 What this preserves

- **All existing callers.** No method signature changes, no removed methods, no semantic changes that would surface as test failures in `trellis_cli` / `trellis_api` / `trellis_workers`.
- **Backend implementation freedom.** A backend can still optimize whichever way its substrate makes natural; the contract just pins the *observable* behaviour.
- **The plugin story.** Phase 0 makes the plugin contract more precise (here are the tests you must pass) without changing what plugins do.

### 9.2 What this costs

- **~1100 lines of test code.** Maintenance is real but small — most contract tests are short and stable. Adding a new ABC method means adding a contract test, which is good discipline anyway.
- **Each new contract test may fail on existing backends and require fixes.** This is the *point* — we want drift surfaced — but it does mean Phase 0 lands as a series of small PRs rather than one batch.
- **Backend authors must read the contract suite.** Today, "read the ABC docstrings." After Phase 0, "read the ABC docstrings + run the contract suite." Slightly higher bar; correspondingly higher confidence in correctness.

### 9.3 What this forecloses

- **Adopting an external query language (openCypher, GQL) wholesale.** This decision says "small, Trellis-specific DSL." Reversing it later means undoing Phase 1+, but Phase 0 alone is fully reversible.
- **Backend-specific public APIs that bypass the contract.** A backend can't expose, say, "Neo4j-only Cypher pass-through" as part of its public surface without an ADR amendment. (Internal escape hatches for backend-specific telemetry are fine; the line is: anything callers touch is contract-bound.)

---

## 10. Alternatives considered

### 10.1 Option A — Do nothing

Rejected. Drift is silent; the cost compounds with each new backend. The Neo4j addition this week made cracks visible; the next backend will widen them.

### 10.2 Option C — Full query optimizer

Rejected. The problem is "three backends drift", not "we have query performance". An optimizer is the wrong instrument by an order of magnitude in surface area.

### 10.3 Option D — Adopt openCypher / GQL

Rejected. Excellent languages, far larger than what we exercise. Every backend would need a parser/compiler with no off-the-shelf option for SQLite or LanceDB; agents writing extraction code would be debugging Cypher fragments. The right canonical surface for Trellis is *smaller* than openCypher.

### 10.4 Make `Neo4jGraphStore` the canonical and translate everything else

Rejected for the same reason as adopting openCypher: it imports too much of Neo4j's surface area. The graph *shape* is canonical; the graph *engine* is not.

### 10.5 Build the DSL first, contract suite second

Rejected. Without the contract suite, the DSL is designed against intuition rather than measured drift. Phase 0 is contract-first because the contract is what tells us which DSL operations actually matter.

---

## 11. References

- [`adr-plugin-contract.md`](./adr-plugin-contract.md) — entry-point registration; this ADR will extend the plugin contract in Phase 3
- [`adr-graph-ontology.md`](./adr-graph-ontology.md) — the names this layer queries against
- pytest parameterized fixtures — [https://docs.pytest.org/en/stable/how-to/parametrize.html](https://docs.pytest.org/en/stable/how-to/parametrize.html) — the mechanism that makes the contract suite practical
- Hyrum's Law — the empirical observation that "with a sufficient number of users of an API, all observable behaviours will be depended on by somebody". The contract suite is the explicit, tested subset; everything else is fair game to change.
