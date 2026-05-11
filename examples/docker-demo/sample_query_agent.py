"""Sample query-engine agent against a Trellis instance.

Demonstrates the closed loop from the *agent's* side:

  1. Trellis is pre-seeded with the cold-start fixture (run
     ``trellis demo load`` once before this script — the Makefile does
     it for you).
  2. The agent receives a task intent.
  3. It searches the graph for relevant entities and reads their
     cross-database routing properties — the metadata it needs to
     dispatch a real query without hardcoding credentials in its
     prompt.
  4. It records lightweight feedback.

Run via the Makefile in this directory (``make demo``), or directly
after ``trellis demo load`` succeeds:

    python examples/docker-demo/sample_query_agent.py

For a real deployment, swap ``in_memory_client(...)`` for
``TrellisClient(base_url="https://trellis.your-domain")`` and the rest
of the script stays the same.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# Quiet down structlog so the demo output stays readable. Set
# TRELLIS_LOG_LEVEL=DEBUG to see the full audit trail of every mutation.
os.environ.setdefault("TRELLIS_LOG_LEVEL", "WARNING")

from trellis.logging import configure_stderr_logging  # noqa: E402
from trellis.testing import in_memory_client  # noqa: E402
from trellis_sdk.client import TrellisClient  # noqa: E402

configure_stderr_logging()


def _print_section(title: str) -> None:
    print()
    print("=" * 64)
    print(f" {title}")
    print("=" * 64)


def _seed_via_extractor_path(_client: TrellisClient) -> None:
    """Load the cold-start fixture via the in-process extractor pipeline.

    Reaches through the in-memory client's shared ``StoreRegistry`` to
    submit the dbt + OpenLineage extractor output through the same
    governed mutation path a real deployment uses. Keeps the demo
    self-contained — no separate CLI invocation needed.

    For a *real* deployment, the equivalent path is:

        client.submit_drafts(wire_batch, ...)

    where ``wire_batch`` is the ``trellis_wire.ExtractionBatch`` produced
    by a client-side extractor. See ``examples/client_starter/`` for the
    canonical client-side extractor pattern.
    """
    import asyncio
    import json

    import trellis_api.app as api_app_module
    from trellis.extract.commands import result_to_batch
    from trellis.extract.dispatcher import ExtractionDispatcher
    from trellis.extract.registry import ExtractorRegistry
    from trellis.mutate import build_curate_executor
    from trellis_workers.extract import (
        DbtManifestExtractor,
        OpenLineageExtractor,
    )

    repo_root = Path(__file__).resolve().parents[2]
    fixture = repo_root / "examples" / "cold-start-fixture"
    manifest = json.loads((fixture / "manifest.json").read_text(encoding="utf-8"))
    events_text = (fixture / "openlineage-events.jsonl").read_text(encoding="utf-8")
    events = [json.loads(line) for line in events_text.splitlines() if line.strip()]

    registry = api_app_module._registry  # the shared StoreRegistry
    assert registry is not None, "in_memory_client must be active before seeding"

    ext_registry = ExtractorRegistry()
    ext_registry.register(DbtManifestExtractor())
    ext_registry.register(OpenLineageExtractor())
    dispatcher = ExtractionDispatcher(
        ext_registry, event_log=registry.operational.event_log
    )

    from trellis.mutate.commands import CommandStatus  # noqa: PLC0415

    executor = build_curate_executor(registry)
    for raw, hint in [(manifest, "dbt-manifest"), (events, "openlineage")]:
        result = asyncio.run(dispatcher.dispatch(raw, source_hint=hint))
        batch = result_to_batch(result, requested_by=f"sample-agent:{hint}")
        results = executor.execute_batch(batch)
        success = sum(1 for r in results if r.status == CommandStatus.SUCCESS)
        print(f"  {hint}: {success}/{len(results)} mutations applied")


def run() -> int:
    """Execute the worked example and return a process exit code."""
    with (
        tempfile.TemporaryDirectory() as scratch,
        in_memory_client(Path(scratch) / "stores") as client,
    ):
        _print_section("1. Seed the graph (cold-start fixture)")
        _seed_via_extractor_path(client)

        _print_section("2. Agent task: where does customer data live?")
        intent = "I need to query customer order history for analytics."
        print(f"  Intent: {intent}")

        # Document-store search returns nothing here because the
        # extractors don't write descriptions to the doc store by default
        # (the CLI ``trellis ingest dbt-manifest`` does so as a side
        # channel; we skip that here to keep the script focused on the
        # graph-only path). A real agent uses the SDK's
        # ``get_objective_context`` to assemble a sectioned pack that
        # combines graph + document + vector search.
        hits = client.search("customers", limit=5)
        print(f"  Doc-store search hits: {len(hits)} (expected 0 in this minimal demo)")

        _print_section("3. Routing properties on the dataset entities")
        # The cross-database routing properties land on dataset-shaped
        # entities so an agent can dispatch SQL to the right warehouse
        # without hardcoding the connection in its prompt.
        for entity_id in (
            "model.jaffle_shop.dim_customers",
            "model.jaffle_shop.fct_orders",
        ):
            entity = client.get_entity(entity_id)
            if entity is None:
                print(f"  {entity_id}: not found")
                continue
            props = entity.get("properties") or {}
            print(f"  {entity_id}:")
            for key in (
                "source_system",
                "database_name",
                "schema_name",
                "physical_uri",
                "description",
            ):
                value = props.get(key)
                if value is not None:
                    print(f"    {key}: {value}")

        _print_section("4. Closing the loop")
        print(
            "  An agent now has enough to dispatch a real query:\n"
            "    1. Dataset entities the search surfaced.\n"
            "    2. Cross-database routing metadata on each (source_system,\n"
            "       database, schema, physical_uri).\n"
            "    3. Descriptions and lineage edges (visible via the graph\n"
            "       traversal calls - omitted here for brevity).\n"
        )
        print(
            "  Real-deployment additions:\n"
            "    * Use client.get_objective_context(intent=...) for a\n"
            "      sectioned pack with advisories.\n"
            "    * On task completion, send a feedback record through the\n"
            "      MCP feedback tool or the REST POST /api/v1/feedback path\n"
            "      so apply_noise_tags can downrank low-signal items over\n"
            "      time.\n"
            "    * Hook the EventLog for TAGS_REFRESHED events to know\n"
            "      when cached context is stale - see\n"
            "      docs/agent-guide/freshness-and-curation.md.\n"
        )

    return 0


if __name__ == "__main__":
    sys.exit(run())
