# Freshness and Curation

> **Who this is for:** anyone keeping a Trellis deployment in sync with moving production systems. After the initial cold-start ingest succeeds, the question shifts from "what to extract" to "how to keep it fresh without manual intervention."

> **What this covers:** the two refresh modes, the `trellis extract refresh` CLI, schema-drift detection via diff events, curator workflows for keeping domain knowledge usable, and the explicit "Trellis is not a scheduler" boundary.

> **What this is not:** an extractor authoring guide (read [extractor-authoring.md](extractor-authoring.md)) or a modeling decision reference ([modeling-guide.md](modeling-guide.md)).

---

## The framing

Trellis is a substrate for storing and retrieving extracted facts. It does not run cron, watch webhooks, or poll. The "cold start" — getting initial data in — and "ongoing freshness" — keeping it current — are two separate operational concerns; Trellis owns the contract for both but runs the loop for neither.

This guide explains the contracts. The schedulers, runners, and webhook handlers that wire them up live in your existing infrastructure (Airflow, GitHub Actions, Dagster, cron, K8s CronJobs, Atlassian Forge apps, Lambda functions, etc.). Trellis ships hooks that play cleanly with all of them.

---

## Two refresh modes

Every source you ingest from falls into one of two patterns. Pick the right one per source.

### Mode 1: Periodic re-run (pull)

The source has no event stream — re-extracting periodically is the only option. dbt manifests, Unity Catalog metadata, Markdown documentation, S3-bucket file listings.

Pattern:

```
[scheduler]
   └── triggers ──>  `trellis extract refresh --source <name>`
                          └── reads sources.yaml
                          └── runs the matching extractor
                          └── diffs vs prior state
                          └── emits TAGS_REFRESHED events
```

Cadence depends on the source's mutation rate and consumer tolerance:

| Source | Typical cadence | Why |
|---|---|---|
| dbt manifest | On every dbt build (post-build hook) | Manifests change with every model edit. |
| Unity Catalog | Daily | Schemas drift in production; daily catches DDL without burning compute. |
| Confluence | Hourly per active space | Editorial change cadence varies; tune per space. |
| Git repos | On every push (webhook) | Free signal; ingest scoped to the changed files. |
| Markdown docs in a repo | On every push (webhook) | Same. |
| SQL query logs | Hourly poll | Match warehouse query-history view refresh latency. |

If you don't know the right cadence: start daily, watch the `EXTRACTOR_USED` events in the EventLog, and tighten the cadence if `TAGS_REFRESHED` events fire on most runs (indicating you're missing real changes between refreshes).

### Mode 2: Pushed events (push)

The source has an event stream. Don't poll — push at us.

Pattern:

```
[source event stream]
       └── pushes ──>  POST /api/v1/extract/drafts
                              └── extractor runs server-side
                              └── drafts route through MutationExecutor
                              └── per-entity ENTITY_UPDATED events emit naturally
```

Sources that support this shape: OpenLineage events, Jira webhook events (via Atlassian Connect / Forge), Confluence change events, git provider webhooks (GitHub, GitLab, Bitbucket), warehouse query-log streams (Snowflake's notification integration, Databricks' system tables event stream).

The push payload is whatever the extractor expects as `raw_input`. For OpenLineage, the body is the event JSON; for git providers, a translator in your edge service converts the webhook payload to a normalized shape before posting.

The REST endpoint applies the same validators and idempotency machinery as the CLI — there's no second path that bypasses governance.

---

## `trellis extract refresh` — the canonical pull command

The CLI is the periodic-refresh primitive. Two invocation forms:

### Form 1: `--source <name>` (sources.yaml-driven)

```bash
trellis extract refresh --source jaffle-dbt
```

Reads `./sources.yaml` (override with `--sources-file <path>`), looks up the entry named `jaffle-dbt`, dispatches to the extractor whose `supported_sources` includes the entry's `type`, runs against the entry's `path`, and emits the diff.

This is the form to wire into your scheduler. The sources.yaml lives in your config repo; CI / cron / Airflow reads it.

### Form 2: `--type <type> --path <path>` (one-shot)

```bash
trellis extract refresh --type dbt-manifest --path /tmp/manifest.json
```

Direct invocation without a registry entry. Useful for operator commands ("re-extract this one file") or for testing.

### Output

Text mode (default):

```
Refreshed jaffle-dbt
  Extractor: dbt_manifest (deterministic)
  Entities scanned:  47
  New:               2
  Changed:           5
  Unchanged:         40
  Edges emitted:     63

  Per-entity diffs:
    - model.my_project.fct_orders (dbt_model)
      ~ description: 'V1 desc' -> 'V2 desc — drift'
      + new_property_key
    - model.my_project.new_table (dbt_model)
      new entity
```

JSON mode (`--format json`):

```json
{
  "status": "refreshed",
  "source": "jaffle-dbt",
  "extractor_used": "dbt_manifest",
  "tier": "deterministic",
  "entities_scanned": 47,
  "new_entities": 2,
  "changed_entities": 5,
  "unchanged_entities": 40,
  "edges_emitted": 63,
  "diffs": [
    {
      "entity_id": "model.my_project.fct_orders",
      "entity_type": "dbt_model",
      "diff": {
        "changed": {"description": ["V1 desc", "V2 desc — drift"]},
        "added": {"new_property_key": "value"}
      }
    },
    {
      "entity_id": "model.my_project.new_table",
      "entity_type": "dbt_model",
      "diff": {
        "new_entity": true,
        "added": {...}
      }
    }
  ]
}
```

The JSON form is what cron / GHA workflows parse for alerting and metrics. Exit code is 0 when the refresh completes (even with zero diffs); non-zero only for genuine errors (missing source, unreadable input, extractor exceptions).

---

## The diff payload

For each entity touched by a refresh, a `TAGS_REFRESHED` event is emitted into the EventLog with the structured diff. Schema:

```python
{
    "extractor_used": "dbt_manifest",
    "source_name": "jaffle-dbt",
    "diff": {
        # Mutually exclusive top-level markers:
        "new_entity": True,        # entity was not in graph before
        "deleted_entity": True,    # entity is gone in new extraction

        # Otherwise, property-level changes:
        "added":   {"key": value, ...},
        "removed": {"key": value, ...},
        "changed": {"key": [before, after], ...}
    }
}
```

The event's `source` field is `extract.refresh:<source_name>` so consumers can filter for extractor-driven refreshes vs classification-driven refreshes (which also use `TAGS_REFRESHED` with a different source prefix).

Agents that cache pack content keyed by entity_id subscribe to the EventLog stream and invalidate on relevant diff events. `PackBuilder` does *not* do this automatically — caching is a consumer concern, not a substrate concern. The substrate emits the events; consumers decide what to do with them.

---

## Wiring into your scheduler

Trellis is not a scheduler. Here are the canonical patterns for the most common ones.

### Cron

```cron
# /etc/cron.d/trellis-refresh
# Daily dbt + OpenLineage refresh at 03:00 UTC
0 3 * * *  trellis-user  /usr/local/bin/trellis extract refresh --source jaffle-dbt --format json >> /var/log/trellis/refresh.log 2>&1
5 3 * * *  trellis-user  /usr/local/bin/trellis extract refresh --source lineage-events --format json >> /var/log/trellis/refresh.log 2>&1
```

Add log monitoring on the file; alert on non-zero exit codes.

### GitHub Actions

```yaml
# .github/workflows/trellis-refresh.yml
name: Trellis refresh
on:
  schedule:
    - cron: "0 3 * * *"
  workflow_dispatch:

jobs:
  refresh:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.13" }
      - run: pip install trellis-cli
      - run: trellis extract refresh --source jaffle-dbt --format json
      - run: trellis extract refresh --source unity-catalog --format json
```

For push-on-dbt-build:

```yaml
on:
  push:
    paths: ["models/**", "dbt_project.yml"]

jobs:
  refresh:
    steps:
      - run: dbt parse  # produces target/manifest.json
      - run: trellis extract refresh --type dbt-manifest --path target/manifest.json --format json
```

### Airflow DAG

```python
from airflow import DAG
from airflow.operators.bash import BashOperator
from datetime import datetime, timedelta

with DAG(
    "trellis_refresh",
    start_date=datetime(2026, 5, 1),
    schedule="0 3 * * *",
    catchup=False,
) as dag:
    refresh_dbt = BashOperator(
        task_id="refresh_dbt",
        bash_command="trellis extract refresh --source jaffle-dbt --format json",
    )
    refresh_uc = BashOperator(
        task_id="refresh_unity_catalog",
        bash_command="trellis extract refresh --source unity-catalog --format json",
    )
    refresh_dbt >> refresh_uc
```

### Kubernetes CronJob

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: trellis-refresh
spec:
  schedule: "0 3 * * *"
  jobTemplate:
    spec:
      template:
        spec:
          containers:
            - name: trellis
              image: trellis-cli:0.6.0
              command:
                - sh
                - -c
                - >
                  trellis extract refresh --source jaffle-dbt --format json &&
                  trellis extract refresh --source unity-catalog --format json
              env:
                - name: TRELLIS_KNOWLEDGE_PG_DSN
                  valueFrom:
                    secretKeyRef:
                      name: trellis-config
                      key: knowledge_pg_dsn
          restartPolicy: OnFailure
```

Pattern across all four: a single command per source, run in sequence, exit-code-driven success signal.

---

## Curator workflows: keeping domain knowledge usable

Refresh handles the structural-fact axis (does the graph match the source?). Curation handles the semantic axis (is the content of the graph the right shape to *retrieve from*?). The two run on different cadences and serve different consumers.

### What curators do

| Activity | Trigger | Cadence |
|---|---|---|
| Annotate entities (`description`, `tags`, `confidence`) | New entity appears or existing description is wrong | Ongoing; per-PR |
| Promote curated nodes (`JoinPattern`, `Topic`, `Glossary`) | Analyzer script identifies candidates; curator approves | Weekly / monthly |
| Re-run analyzer scripts | Source data drifted; periodic refresh | Daily for hot scripts; weekly for slow ones |
| Demote noise (`signal_quality="noise"`) | Feedback signals consistent low value | Continuous via `apply_noise_tags()` |
| Mark deprecated entities (`Lifecycle.state="deprecated"`) | Source-system deprecation; planned removal | Per-event |
| Edit curated-node summaries | Domain expert review | Per-entity |

### The v1 curation pattern: edit YAML, re-ingest

For Reading B v1, the curator workflow is intentionally simple: curators edit Markdown / YAML files in their source repo, and the cold-start `trellis extract refresh` runs the changes through the normal pipeline. The `MutationExecutor`'s idempotency handles diffs cleanly — re-running with no edits is a no-op; re-running with edits updates only the changed entities.

Why YAML-edit-and-re-ingest:

- **Source of truth lives in version control.** Edits go through PR review; rollback is `git revert`.
- **No new CLI surface for v1.** A `trellis curate edit <eid>` interactive flow is deferred until a design partner specifically asks; until then, every interaction goes through the same path as every other ingest.
- **The refresh CLI handles drift detection.** Curator edits produce `TAGS_REFRESHED` events just like extractor drift; consumers don't need to distinguish.

Example: a curator wants to add a `description` to a `Dataset` extracted from Unity Catalog. They:

1. Add the description to the source-of-truth (a `descriptions.yaml` curated by hand) or to UC itself.
2. Run `trellis extract refresh --source unity-catalog`.
3. The refresh detects the description change, emits a `TAGS_REFRESHED` event, and updates the entity.

For sources without a hand-editable source-of-truth (raw SQL query logs, OpenLineage events), curators run analyzer scripts that emit curated entities directly — see the next section.

### Curated derivations: when curators ship code, not edits

Some curated entities (`JoinPattern`, `AccessPattern`, `HotDataset`, topic clusters) are produced by analyzer scripts that read the graph and emit derived entities. The analyzer is itself curator-owned code — not an extractor (extractors ingest from *external* sources; analyzers derive from the graph).

Pattern:

```python
# nightly/sql_log_analyzer.py
from trellis.mutate import build_curate_executor
from trellis.mutate.commands import Command, CommandBatch, Operation
from trellis.stores.registry import StoreRegistry

def analyze(lookback_days: int = 30) -> None:
    registry = StoreRegistry.from_config_dir()
    # 1. Read raw QueryExecution + Dataset entities from the graph
    raw_queries = registry.knowledge.graph_store.query_nodes(...)
    datasets = registry.knowledge.graph_store.query_nodes(node_type="Dataset")
    # 2. Compute patterns (the curator's domain logic)
    patterns = compute_join_patterns(raw_queries, datasets, min_count=50)
    # 3. Submit curated entities through MutationExecutor
    commands = [
        Command(
            operation=Operation.ENTITY_CREATE,
            args={"entity": pattern_to_entity(p)},
            target_id=p.entity_id,
            target_type="JoinPattern",
            requested_by="curator:sql_log_analyzer",
        )
        for p in patterns
    ]
    build_curate_executor(registry).execute_batch(CommandBatch(commands=commands))
```

Run on a schedule (same nightly cron as the refresh CLI, or a separate Airflow DAG). The analyzer is part of the curator's repo, versioned with their code, deployed alongside their other tooling.

### Lifecycle transitions

`Lifecycle.state` is the human-readable summary of an entity's freshness/usefulness:

| State | When to set | Who sets it | Retrieval impact |
|---|---|---|---|
| `active` | Default. Entity is current and useful. | Initial extract; restored by curator. | Normal ranking. |
| `superseded` | A newer entity replaces this one. | Extractor refresh detects replacement; curator. | Excluded from default retrieval; `supersedes` edge points at the replacement. |
| `deprecated` | Source system deprecated; planned removal. | Curator. | Demoted in ranking; not excluded unless `include_deprecated=False`. |
| `archived` | Removed from source but kept for audit. | Extractor refresh detects removal; curator. | Excluded from default retrieval. |
| `noise` | Feedback signals consistent low value. | `apply_noise_tags()` from feedback loop. | Excluded by default; consumer can opt in. |

Transitions are mutations through the governed pipeline, not direct property writes. The `EventLog` records every transition; effectiveness analysis tracks the rates so operators can spot drift.

---

## The variation → selection loop

Trellis's broader design is a *variation-selection* loop: extraction produces candidate context items, feedback grades them, the advisory and learning loops propagate or suppress.

| Path | Role | Cadence |
|---|---|---|
| **Extraction** | Produces candidate entities/edges from sources. | On refresh (pull) or on event (push). |
| **Pack assembly** | Selects relevant candidates for an agent task. | Per-task. |
| **Feedback** | Grades the candidates that were used. | Per-task completion. |
| **Effectiveness analysis** | Aggregates feedback into per-item / per-source signals. | Continuous (events) or batch (JSONL). |
| **Advisory loop** | Surfaces advisories ("this item underperforms") to retrievers. | Per pack assembly. |
| **Learning loop** | Promotes successful patterns (`PrecedentPromotion`); demotes consistently bad items (`apply_noise_tags`). | Continuous + human-reviewed batch. |

Refresh is the variation-side input. Feedback + `apply_noise_tags` is the selection-side output. They're complementary halves of the same loop — neither is sufficient on its own.

Two feedback paths exist for historical reasons; see [CLAUDE.md](../../CLAUDE.md#two-feedback-paths--eventlog-authoritative-vs-jsonl-file-based) for the EventLog-authoritative vs JSONL-file-based split. Refresh consumes neither directly — but extractors that re-extract over time benefit from `apply_noise_tags` having already demoted historically-irrelevant items.

### Running the selection-side loop: `trellis worker curate`

The selection-side half (effectiveness feedback → advisory generation → advisory fitness → learning candidates) runs as one operational command:

```bash
trellis worker curate --output-dir ./review --days 30
# or unattended, every 6 hours, until SIGINT/SIGTERM:
trellis worker curate --output-dir ./review --interval 21600
```

It calls the curation library functions directly and writes the promote-half review artifacts to `--output-dir`. **Promotion stays human-gated** — review `promotion_decisions.template.json`, approve rows, then run `trellis curate promote-learning` (Tier-2 of [`../design/adr-autonomy-ladder.md`](../design/adr-autonomy-ladder.md)). `--interval` is a plain-sleep convenience; Trellis introduces no scheduler dependency. See [operations.md](operations.md#trellis-worker-curate) for the full flag table. Per-stage `--skip-*` toggles let you run just the demote half (`--skip-advisories --skip-learning`) or just the promote-half scan.

### Reconciling the JSONL audit log: `trellis admin reconcile-feedback`

The curate cycle reads the **EventLog-authoritative** signal. When the `FEEDBACK_RECORDED` event failed to emit (sink unavailable, crash between the JSONL append and the event emit, file-only capture being promoted into the governed pipeline) the row lives only in `pack_feedback.jsonl` and the cycle never sees it. Backfill those rows into the EventLog:

```bash
trellis admin reconcile-feedback --log-dir ./data [--dry-run] [--format json]
```

This wraps `reconcile_feedback_log_to_event_log`: it scans `pack_feedback.jsonl`, matches each row against the EventLog by `feedback_id`, and emits only the missing ones — safe to run repeatedly. The JSON output reports `scanned` / `already_present` / `emitted` / `failed` counts (`--dry-run` reports `would_emit` and touches nothing). `worker curate --reconcile-first` runs this same backfill immediately before a cycle, so a scheduled curate pass never misses file-only feedback. The JSONL remains an **audit log, not a second decision path** — reconciliation feeds the authoritative EventLog; it does not create a parallel promote/demote route.

---

## Operational signals to watch

After the system is running, three EventLog patterns indicate health:

1. **`TAGS_REFRESHED` rate per source.** Steady-state is the source's natural mutation rate (a few per day for dbt; hundreds for query logs). A sudden spike means upstream churned; a sudden drop means a refresh job is silently failing.
2. **`EXTRACTOR_FALLBACK` events with `reason="empty_result"`.** A refresh that returns zero entities probably indicates a source-format change or a credentials problem. The dispatcher emits this exactly so cold-start gaps surface fast.
3. **`importance_scored_at` distribution.** A retrievable entity whose score hasn't been refreshed in > 30 days is suspect — either it's not being touched (probably stale) or the importance refresh hook has stopped firing.

Build a small dashboard that surfaces all three. The signals are already in the EventLog; you don't need a separate metrics pipeline.

---

## Further reading

- [modeling-guide.md](modeling-guide.md) — the freshness signals section, plus the curated-vs-raw distinction
- [extractor-authoring.md](extractor-authoring.md) — telemetry contract for the events this guide reads
- [source-modeling-cookbook.md](source-modeling-cookbook.md) — per-source refresh recommendations
- [`src/trellis_cli/extract_refresh.py`](../../src/trellis_cli/extract_refresh.py) — the refresh CLI source
- [`src/trellis/classify/refresh.py`](../../src/trellis/classify/refresh.py) — the classification refresh (companion to extractor refresh; same event, different source prefix)
