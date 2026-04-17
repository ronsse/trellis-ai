"""SDK remote-mode demo: same flow as sdk_local_demo, but over the REST API.

The only difference is `base_url=...` on the client. Use this mode when:
- Multiple agents share one substrate (a centrally hosted Trellis).
- You want process isolation between agents and stores.
- You need to deploy stores on Postgres / pgvector / S3 behind a service.

Run:
    trellis admin init                        # one-time
    trellis admin serve --port 8420           # in another terminal
    python examples/sdk_remote_demo.py
"""

from __future__ import annotations

from trellis_sdk import TrellisClient


def main() -> None:
    client = TrellisClient(base_url="http://localhost:8420")
    assert client.is_remote, "Client should be in remote mode"

    trace_id = client.ingest_trace(
        {
            "source": "examples.sdk_remote_demo",
            "intent": "Investigate slow checkout endpoint",
            "steps": [
                {
                    "step_type": "tool_call",
                    "name": "query_db",
                    "result": {"slow_query": "SELECT * FROM orders WHERE ..."},
                }
            ],
            "outcome": {
                "status": "success",
                "summary": "Added composite index on (user_id, created_at).",
            },
            "context": {"domain": "backend"},
        }
    )
    print(f"Ingested trace: {trace_id}")

    pack = client.assemble_pack(
        intent="improve database query performance",
        domain="backend",
        max_tokens=1500,
    )
    print(f"Pack {pack['pack_id']} -> {pack['count']} items")

    recent = client.list_traces(domain="backend", limit=5)
    print(f"Recent backend traces: {len(recent)}")

    client.close()


if __name__ == "__main__":
    main()
