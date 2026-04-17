# Entity Alias Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan in this session.

**Goal:** Add first-class `entity_alias` support to `trellis-ai` so canonical entities can be reconciled with external system identifiers such as Unity Catalog, dbt, and git.

**Architecture:** Keep the alias model deliberately small. Add a strict `EntityAlias` schema plus three graph-store operations: attach an alias, resolve an alias to a canonical entity, and list aliases for an entity. Implement the same temporal semantics already used for nodes and edges.

**Tech Stack:** Python, Pydantic, SQLite, Postgres, pytest

### Task 1: Add failing schema tests for `EntityAlias`

**Files:**
- Modify: `tests/unit/schemas/test_entity.py`
- Test: `tests/unit/schemas/test_entity.py`

**Step 1: Write the failing test**

Add tests asserting:
- `EntityAlias` validates with `entity_id`, `source_system`, `raw_id`
- `raw_name`, `match_confidence`, and `is_primary` have sane defaults
- extra fields are forbidden

**Step 2: Run test to verify it fails**

Run: `PYENV_VERSION=3.12.7 python -m pytest tests/unit/schemas/test_entity.py -q`
Expected: FAIL because `EntityAlias` is not defined/exported yet.

**Step 3: Write minimal implementation**

Add `EntityAlias` to `src/trellis/schemas/entity.py` and export it in `src/trellis/schemas/__init__.py`.

**Step 4: Run test to verify it passes**

Run: `PYENV_VERSION=3.12.7 python -m pytest tests/unit/schemas/test_entity.py -q`
Expected: PASS

### Task 2: Add failing SQLite graph-store tests for alias operations

**Files:**
- Modify: `tests/unit/stores/test_graph_store.py`
- Modify: `tests/unit/stores/test_temporal_graph.py`
- Test: `tests/unit/stores/test_graph_store.py`
- Test: `tests/unit/stores/test_temporal_graph.py`

**Step 1: Write the failing test**

Add tests asserting:
- `upsert_alias()` attaches an alias to a node
- `resolve_alias()` returns the current alias mapping
- `get_aliases()` lists aliases for a node
- temporal `as_of` lookup returns the alias version valid at that time

**Step 2: Run test to verify it fails**

Run: `PYENV_VERSION=3.12.7 python -m pytest tests/unit/stores/test_graph_store.py tests/unit/stores/test_temporal_graph.py -q`
Expected: FAIL because alias methods and tables do not exist yet.

**Step 3: Write minimal implementation**

Update:
- `src/trellis/stores/base/graph.py`
- `src/trellis/stores/sqlite/graph.py`

Add a temporal `entity_aliases` table and the three store methods.

**Step 4: Run test to verify it passes**

Run: `PYENV_VERSION=3.12.7 python -m pytest tests/unit/stores/test_graph_store.py tests/unit/stores/test_temporal_graph.py -q`
Expected: PASS

### Task 3: Add failing Postgres graph-store tests for alias operations

**Files:**
- Modify: `tests/unit/stores/test_postgres_stores.py`
- Test: `tests/unit/stores/test_postgres_stores.py`

**Step 1: Write the failing test**

Add tests asserting the same alias behavior for `PostgresGraphStore`.

**Step 2: Run test to verify it fails**

Run: `PYENV_VERSION=3.12.7 python -m pytest tests/unit/stores/test_postgres_stores.py -q -m postgres`
Expected: FAIL when `TRELLIS_TEST_PG_DSN` is set because alias schema/methods are not implemented.

**Step 3: Write minimal implementation**

Update `src/trellis/stores/postgres/graph.py` with the alias table and methods.

**Step 4: Run test to verify it passes**

Run: `PYENV_VERSION=3.12.7 python -m pytest tests/unit/stores/test_postgres_stores.py -q -m postgres`
Expected: PASS when Postgres test DSN is available.

### Task 4: Update public schema docs

**Files:**
- Modify: `docs/agent-guide/schemas.md`

**Step 1: Update docs**

Document `EntityAlias` and the required fields.

**Step 2: Run focused verification**

Run: `PYENV_VERSION=3.12.7 python -m pytest tests/unit/schemas/test_entity.py tests/unit/stores/test_graph_store.py tests/unit/stores/test_temporal_graph.py -q`
Expected: PASS

### Task 5: Final verification

**Files:**
- Modify: `src/trellis/schemas/entity.py`
- Modify: `src/trellis/schemas/__init__.py`
- Modify: `src/trellis/stores/base/graph.py`
- Modify: `src/trellis/stores/sqlite/graph.py`
- Modify: `src/trellis/stores/postgres/graph.py`
- Modify: `docs/agent-guide/schemas.md`
- Modify: `tests/unit/schemas/test_entity.py`
- Modify: `tests/unit/stores/test_graph_store.py`
- Modify: `tests/unit/stores/test_temporal_graph.py`
- Modify: `tests/unit/stores/test_postgres_stores.py`

**Step 1: Run SQLite verification**

Run: `PYENV_VERSION=3.12.7 python -m pytest tests/unit/schemas/test_entity.py tests/unit/stores/test_graph_store.py tests/unit/stores/test_temporal_graph.py -q`
Expected: PASS

**Step 2: Run Postgres verification if available**

Run: `PYENV_VERSION=3.12.7 python -m pytest tests/unit/stores/test_postgres_stores.py -q -m postgres`
Expected: PASS when `TRELLIS_TEST_PG_DSN` is configured, otherwise report it as not run.

**Step 3: Commit**

```bash
git add docs/plans/2026-03-16-entity-alias-implementation.md \
  docs/agent-guide/schemas.md \
  src/trellis/schemas/entity.py \
  src/trellis/schemas/__init__.py \
  src/trellis/stores/base/graph.py \
  src/trellis/stores/sqlite/graph.py \
  src/trellis/stores/postgres/graph.py \
  tests/unit/schemas/test_entity.py \
  tests/unit/stores/test_graph_store.py \
  tests/unit/stores/test_temporal_graph.py \
  tests/unit/stores/test_postgres_stores.py
git commit -m "feat: add entity alias support"
```
