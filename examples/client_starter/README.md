# Client integration starter

Copy this directory into your own repo as the skeleton for a Trellis
integration. It's the minimum viable shape: an extractor, a wrapped
SDK client, a retrieval helper, and an end-to-end demo that wires
them together.

## When to use this vs. the other examples

| If you want to…                                        | Use                                 |
|--------------------------------------------------------|-------------------------------------|
| See a one-file SDK call (ingest a trace, get a pack)   | [`sdk_remote_demo.py`](../sdk_remote_demo.py) |
| Build a production-shaped extractor in isolation       | [`trellis_example_extractor/`](../trellis_example_extractor/) |
| **Scaffold a new client repo around Trellis**          | **this directory**                  |
| Hook Trellis into an agent framework (LangGraph etc.)  | [`integrations/`](../integrations/) |

## What's in the box

```
client_starter/
├── __init__.py     docstring mapping each file to its role
├── types.py        namespaced entity/edge type constants + typed properties
├── client.py       factory() that returns a TrellisClient (remote or in-memory)
├── extractor.py    ServiceCatalogExtractor (pure DraftExtractor impl)
├── retrieve.py     get_context() → tight ContextPack dataclass
└── run_demo.py     end-to-end: extract → submit_drafts → get_context
```

## Run it

**In-memory** (no server required):

```bash
pip install trellis-ai
python -m examples.client_starter.run_demo
```

You should see:

```
Extracted: 7 entities, 7 edges (idempotency_key=catalog-sync-...)
Submitted batch ... entities_submitted=7 succeeded=14 duplicates=0
Ingested 2 evidence documents
Document search for 'checkout': 1 hits
  - 01KPP...: checkout-api is a python service (criticality tier1)...
Pack ...: 8 items
  - [document] 01KPP... (score=0.02) checkout-api is a python service...
  - [entity] s1... (score=0.02) checkout-api
  - [entity] t1... (score=0.01) Payments
  ...
```

Two things to notice:

- **Entities and edges populate the graph**; they surface in packs as
  graph-expansion results around matched seeds.
- **Documents (Evidence)** are what full-text keyword search finds. If
  you only `submit_drafts()` and never `ingest_evidence()` /
  `ingest_trace()` / `save_memory()`, packs will be empty even though
  the graph is rich.

**Against a real server** (POC bastion, AWS, local compose stack):

```bash
export TRELLIS_URL=http://localhost:8420    # or your ECS endpoint
python -m examples.client_starter.run_demo --server $TRELLIS_URL
```

Same output, same code path — the factory just hands back a
network-backed `TrellisClient` instead of the in-process one.

## The four replace-me hotspots

When you copy this into your own repo, these are the things you own:

1. **`types.py`** — rename the namespace (`mycompany.*` → your prefix),
   list the entity types and edge kinds you actually have, and define
   Pydantic property shapes for each. The server accepts anything; the
   typed models save you from typos and drift.

2. **`extractor.py`** — replace `ServiceCatalogSnapshot` with whatever
   your real source returns (dbt manifest, Backstage, Unity Catalog,
   internal API, YAML registry). Keep `extract()` pure — no I/O inside.

3. **`client.py`** — set the default `TRELLIS_URL` for your org, add
   auth headers here when native auth ships, swap the timeout to
   match your SLA.

4. **`run_demo.py`** — replace `SAMPLE_SNAPSHOT` with a real fetcher
   (the first thing that calls out to your source of truth). Wire
   the rest into your scheduler (cron, Airflow, GitHub Action — the
   extractor doesn't care).

## Production wiring (what changes vs. demo)

```python
# cron / airflow / cloudwatch-scheduled lambda
def nightly_catalog_sync() -> None:
    snapshot = fetch_from_our_source_of_truth()
    batch = ServiceCatalogExtractor().extract(snapshot)
    with factory() as client:  # TRELLIS_URL env var
        client.submit_drafts(batch)
```

```python
# in your agent's "before starting a task" hook
def load_context(intent: str, domain: str | None = None) -> str:
    with factory() as client:
        pack = get_context(client, intent, domain=domain)
    return pack.summarize()
```

Two ~10-line functions, one scheduled, one called by agents. That's
the shape of a first-client integration.

## Versioning

Pin `trellis-ai==<X.Y.Z>` in your client's dependencies. The SDK
calls `/api/version` on first use and raises
`TrellisVersionMismatchError` if the server's `api_major` disagrees
with the SDK's expected major — so incompatibilities surface on boot,
not silently at the wire.

## What this example does NOT cover (yet)

- Custom classifiers — see [`../custom_classifier.py`](../custom_classifier.py)
- Sectioned packs (agent-specific budgets per section) — covered in the
  SDK method `assemble_sectioned_pack()`; add when your agent needs it.
- Feedback recording — `client.record_feedback(...)` closes the loop
  after an agent uses a pack; add once you have a signal to send back.
- Auth — deferred until Trellis ships native API keys; today this
  integration assumes VPN / network-level access control.
