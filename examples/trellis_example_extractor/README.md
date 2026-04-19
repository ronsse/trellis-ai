# trellis_example_extractor

A skeleton client extractor package — fork this to build your own
Unity Catalog, dbt, OpenLineage, or domain-specific extractor that
submits drafts to a running Trellis server.

**Design principles** (see [TODO.md Step 4](../../TODO.md#step-4--trellis_sdkextract-module--post-apiv1extractdrafts)):

- **Client-side extraction.** Your reader lives in *your* package,
  with *your* dependencies (the Unity Catalog SDK, a dbt manifest
  parser, whatever you need).  No server-side code change when you
  add new types or tweak extraction logic.
- **Namespaced types.** Use `entity_type="your_domain.resource"` not
  bare `"resource"`.  Core accepts any string; namespacing keeps
  domains from colliding.
- **Idempotency keys for safe retries.** Combine a stable extractor
  identifier with a source snapshot ID (git SHA, warehouse snapshot
  ID, ISO timestamp).  Re-running the same sync is a no-op.
- **Extraction is pure.** The reader reads, emits drafts, returns.
  No store access, no HTTP calls.  Submission happens separately so
  each piece can be tested, cached, or retried independently.

## Files

- [`reader.py`](reader.py) — the extractor.  Reads "widgets" from an
  in-memory source, emits `EntityDraft` / `EdgeDraft` records.
- [`sync.py`](sync.py) — the submission script.  Constructs a
  `TrellisClient`, calls `reader.extract(...)`, and submits.
- [`types.py`](types.py) — optional typed Pydantic models for your
  domain's `properties` dict.  Validates client-side; the server
  accepts the `properties` escape hatch opaquely.

## Quick start

```bash
pip install trellis-ai
python -m examples.trellis_example_extractor.sync --server http://localhost:8420
```

Or against an in-memory test fixture:

```python
from pathlib import Path
from trellis.testing import in_memory_client
from examples.trellis_example_extractor.reader import ExampleExtractor
from examples.trellis_example_extractor.sync import SAMPLE_DATA

with in_memory_client(Path("/tmp/trellis-stores")) as client:
    result = client.submit_drafts(ExampleExtractor().extract(SAMPLE_DATA))
    print(f"Submitted {result.succeeded}/{result.entities_submitted} entities")
```

## What to change when you fork

1. Rename the package (`trellis_example_extractor` → `trellis_your_system`).
2. Replace `SAMPLE_DATA` with a real source fetcher (`boto3`,
   `databricks-sdk`, a manifest loader, …).
3. Change the type namespace (`example.*` → `your_domain.*`).
4. Expand `types.py` with the full schema of your source system's
   metadata.
5. Wire up CI to run `python -m your_package.sync` on a schedule
   (GitHub Actions, Argo, Airflow, whatever you use).

## Why not add my extractor to core?

Core ships server-side extractors for agent-centric sources
(`trellis_workers.extract`: dbt manifest, OpenLineage).  Domain
extractors belong in their own packages because:

- They have domain-specific dependencies (Unity Catalog SDK, AWS SDK, etc).
- They release on their own cadence, not Trellis's.
- They can be private / internal to your org without forking Trellis.
- They benefit from running in your environment (IAM credentials,
  network ACLs) rather than on the Trellis runtime.

The `POST /api/v1/extract/drafts` route exists so you *don't* have
to touch the server to add a new source.
