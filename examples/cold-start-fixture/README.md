# Cold-Start Fixture

A small, self-contained data-platform graph that exercises the extractor path end-to-end. `trellis demo load` loads this fixture in addition to the legacy hand-crafted demo content, so a fresh install can see both shapes side-by-side.

## What it produces

Running both extractors against this directory produces approximately:

- **8 graph entities** from the dbt manifest: 4 models, 2 sources, 2 tests — all with cross-database routing properties (`source_system="snowflake"`, `database_name`, `schema_name`, `physical_uri`).
- **8 graph entities** from OpenLineage: 6 jobs (Airflow + dbt) and several dataset entities (most overlap with the dbt models via shared identifiers but distinct ID schemes today — see notes below).
- **~15 graph edges**: `depends_on` from the dbt manifest, `reads_from` / `writes_to` from the OpenLineage events.

## Running it manually

```bash
# From the repo root
trellis admin init
trellis extract refresh --type dbt-manifest --path examples/cold-start-fixture/manifest.json
trellis extract refresh --type openlineage --path examples/cold-start-fixture/openlineage-events.jsonl

# Or via sources.yaml (preferred for scripts)
trellis extract refresh --source jaffle-dbt --sources-file examples/cold-start-fixture/sources.yaml
trellis extract refresh --source lineage-events --sources-file examples/cold-start-fixture/sources.yaml
```

## Modifying it to test drift detection

Edit `manifest.json` to:

- Change a `description` field → triggers a `TAGS_REFRESHED` event on the next refresh.
- Add a new model → registers as a new entity.
- Remove a model → the entity becomes "missing in extraction" (the current refresh CLI does not auto-archive removed entities; that's a separate curator action).

`trellis extract refresh --source jaffle-dbt --sources-file examples/cold-start-fixture/sources.yaml --format json` returns the diff payload showing exactly what changed.

## Notes on shape

Today the dbt extractor emits `dbt_model` / `dbt_source` / `dbt_test` entity types, while the OpenLineage extractor emits `dataset` and `job`. The two don't share entity IDs even when they reference the same physical table — there's no auto-merge between extractor namespaces in v1. The `physical_uri` routing property is the only cross-extractor join key (both emit it for the same underlying table). A curator-level reconciliation script that merges duplicates by `physical_uri` is a reasonable v2 follow-up.

See:

- [docs/agent-guide/modeling-guide.md](../../docs/agent-guide/modeling-guide.md) — what the entities mean
- [docs/agent-guide/source-modeling-cookbook.md](../../docs/agent-guide/source-modeling-cookbook.md) — the dbt + OpenLineage recipes this fixture follows
- [docs/agent-guide/freshness-and-curation.md](../../docs/agent-guide/freshness-and-curation.md) — how to keep extractor output fresh
