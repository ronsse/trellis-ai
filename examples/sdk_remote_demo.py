"""SDK remote-mode demo: ingest a trace and assemble a pack over the REST API.

STATUS: PREVIEW — examples are in flux while parallel work lands. Expect
breaking changes before the next minor release.

Unlike sdk_local_demo (which talks to the in-memory API client, no server
process), this one is `TrellisClient(base_url=...)` over HTTP against a
real running `trellis-api` server. Use this mode when:
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

    trace_id = client.ingest_trace(
        {
            "source": "agent",
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
