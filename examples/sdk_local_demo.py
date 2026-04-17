"""SDK local-mode demo: ingest a trace, search, assemble a context pack.

Local mode opens the SQLite stores directly through StoreRegistry — no server
process required. This is the lightest possible way to embed Trellis into a
Python agent or batch job.

Run:
    trellis admin init   # one-time
    python examples/sdk_local_demo.py
"""

from __future__ import annotations

from trellis_sdk import TrellisClient


def main() -> None:
    client = TrellisClient()  # no base_url -> local mode

    # 1. Ingest a trace describing some completed work.
    trace_id = client.ingest_trace(
        {
            "source": "examples.sdk_local_demo",
            "intent": "Add retry logic to the payments client",
            "steps": [
                {
                    "step_type": "tool_call",
                    "name": "edit_file",
                    "result": {"path": "src/payments/client.py"},
                },
                {
                    "step_type": "tool_call",
                    "name": "run_tests",
                    "result": {"passed": 12, "failed": 0},
                },
            ],
            "outcome": {
                "status": "success",
                "summary": "Exponential backoff with jitter, max 3 retries.",
            },
            "context": {"domain": "backend"},
        }
    )
    print(f"Ingested trace: {trace_id}")

    # 2. Create a knowledge-graph entity and link a related concept.
    service_id = client.create_entity(
        "payments-client", entity_type="service", properties={"language": "python"}
    )
    pattern_id = client.create_entity("retry-with-backoff", entity_type="pattern")
    client.create_link(service_id, pattern_id, edge_kind="entity_uses")
    print(f"Created entities: {service_id}, {pattern_id}")

    # 3. Search documents for related context.
    hits = client.search("retry backoff", limit=5)
    print(f"Search returned {len(hits)} hits")

    # 4. Assemble a token-budgeted context pack for the next task.
    pack = client.assemble_pack(
        intent="harden the orders service against transient failures",
        domain="backend",
        max_items=10,
        max_tokens=2000,
    )
    print(f"Pack {pack['pack_id']} -> {pack['count']} items")

    client.close()


if __name__ == "__main__":
    main()
