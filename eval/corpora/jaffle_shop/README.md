# Jaffle Shop corpus fixture

Phase B-1 of [`docs/design/plan-real-corpus-eval.md`](../../../docs/design/plan-real-corpus-eval.md).

## What's in `manifest.json`

A hand-crafted manifest mirroring the structure of
[`dbt-labs/jaffle_shop_classic`](https://github.com/dbt-labs/jaffle_shop_classic)
as of 2026-05-06. Contains:

- **3 sources** (`source.jaffle_shop.raw.{customers,orders,payments}`)
- **3 staging models** (`model.jaffle_shop.stg_{customers,orders,payments}`)
- **2 mart models** (`model.jaffle_shop.{customers,orders}`)
- **13 tests** (uniqueness, not-null, accepted-values, relationships)

Total: **21 entities** with 22 `depends_on` edges. Comparable in scale
to the synthetic baseline corpus (22 entities, 0 edges) but adds the
graph-edge dimension the synthetic corpus lacks.

## Why hand-crafted, not `dbt parse`-generated

Installing `dbt-core` + `dbt-duckdb` would pull ~30 packages into the
trellis venv (including `more-itertools` and `pathspec` downgrades that
risk breaking other tooling). For a fixture this small, the install
cost wasn't justified.

The fields populated mirror what
[`DbtManifestExtractor`](../../../src/trellis_workers/extract/dbt_manifest.py)
actually reads:

| Field | Used by extractor | Populated in fixture |
|---|---|---|
| `unique_id` | yes — used as `entity_id` | ✓ |
| `resource_type` | yes — maps to `entity_type` | ✓ |
| `name` | yes — used as `EntityDraft.name` | ✓ |
| `schema` | yes — properties | ✓ |
| `database` | yes — properties | ✓ |
| `description` | yes — properties | ✓ |
| `tags` | yes — properties | ✓ |
| `config.materialized` (models only) | yes — properties | ✓ |
| `source_name` (sources only) | yes — properties | ✓ |
| `depends_on.nodes` | yes — produces `EdgeDraft` | ✓ |

Fields the extractor does NOT read are omitted from the fixture
(`compiled_sql`, `raw_sql`, `meta`, `columns`, `refs`, `sources`,
manifest envelope keys like `child_map`, `parent_map`, etc.).

## Regenerating from real dbt

If a contributor wants the canonical, dbt-generated artifact:

```bash
# In a separate venv to avoid polluting the trellis project deps:
uv venv .dbt-tmp-venv
.dbt-tmp-venv/Scripts/python -m pip install dbt-core dbt-duckdb

# Clone the source project:
git clone https://github.com/dbt-labs/jaffle_shop_classic /tmp/jaffle
cd /tmp/jaffle

# Set up a minimal duckdb profile:
mkdir -p ~/.dbt
cat > ~/.dbt/profiles.yml <<'EOF'
jaffle_shop:
  target: dev
  outputs:
    dev:
      type: duckdb
      path: /tmp/jaffle.duckdb
EOF

# Generate the manifest:
.dbt-tmp-venv/Scripts/dbt deps
.dbt-tmp-venv/Scripts/dbt parse

# Copy to this directory:
cp target/manifest.json <repo>/eval/corpora/jaffle_shop/manifest.json
```

The dbt-generated manifest is significantly larger (~100-300 KB vs.
this fixture's ~6 KB) because it includes per-column metadata,
compiled SQL, file paths, `parent_map` / `child_map` adjacency lists,
etc. — none of which the current extractor reads. If the extractor
gains support for those fields later, regenerate this fixture from
the real dbt run rather than hand-extending it.

## Why this corpus

Phase B-1's pitch is "convergence on a real-shaped corpus the loop's
authors didn't engineer to make it work." Jaffle Shop is small enough
to reason about (21 entities, 22 edges, fits on a screen) but
structured enough to:

1. Produce non-trivial `dependsOn` edges (the synthetic baseline has
   zero edges — `GraphSearch` is a no-op there).
2. Support naming-overlap distractors organically — `customers`
   (mart) vs `stg_customers` (staging) vs `raw.customers` (source) —
   no hand-written distractor docs needed.
3. Author 10-15 ground-truth queries with verifiable answer sets
   (e.g., "what does `customers` depend on?" → 3 staging models;
   "what tests apply to `orders`?" → 2 model-level tests +
   1 cross-model relationships test).
