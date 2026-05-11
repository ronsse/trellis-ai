# Sample Query-Engine Agent on Trellis

A self-contained worked example that:

1. Loads the cold-start fixture (dbt manifest + OpenLineage events) into an in-memory Trellis through the real extractor + governed mutation pipeline.
2. Asks Trellis where customer data lives.
3. Reads cross-database routing properties from dataset-shaped entities — the metadata an agent needs to dispatch a real query without hardcoding credentials in its prompt.
4. Sketches the closing of the feedback loop.

## Run it

From the repo root:

```bash
make -C examples/docker-demo demo
```

Total runtime: under 60 seconds on a laptop. No external services required.

## What this is and isn't

- **Is**: a minimum-viable end-to-end demo proving the cold-start path works and producing real, inspectable routing properties.
- **Is not**: a docker-compose stack. The in-process ASGI shim (`trellis.testing.in_memory_client`) achieves the same goal with much less ceremony. A real docker-compose deployment (Trellis API container + Postgres + agent sidecar) is a v2 follow-up — see [`docs/agent-guide/quickstart-query-agent.md`](../../docs/agent-guide/quickstart-query-agent.md) "Deployment shape" for the production-shape walkthrough.

## Files

- [`sample_query_agent.py`](sample_query_agent.py) — the agent script, annotated, short enough to read end-to-end.
- [`Makefile`](Makefile) — convenience wrapper for `make demo` and `make install`.

## Further reading

- [`docs/agent-guide/quickstart-query-agent.md`](../../docs/agent-guide/quickstart-query-agent.md) — the deeper walkthrough this script supports.
- [`examples/cold-start-fixture/`](../cold-start-fixture/) — the fixture this script loads.
- [`examples/client_starter/`](../client_starter/) — a heavier example showing the client-side extractor pattern (submit_drafts via wire DTOs).
