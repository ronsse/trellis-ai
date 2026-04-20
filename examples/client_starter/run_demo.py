"""End-to-end demo: extract → ingest → retrieve, all in one script.

Two modes:

* ``python -m examples.client_starter.run_demo``
      In-memory — no server required. Spins up an ASGI-backed
      TrellisClient in-process, ingests sample data, retrieves a pack.

* ``python -m examples.client_starter.run_demo --server http://<bastion>:8420``
      Remote — hits a real Trellis deployment. Same code path as the
      in-memory run; only the client factory differs.

This is what your first-client integration looks like end-to-end: a
dataclass of source data, an extractor that produces a draft batch, a
client that submits the batch, then a retrieval call that answers an
agent intent. Everything else is variation on these five steps.
"""

from __future__ import annotations

import argparse
import uuid

from examples.client_starter.client import factory
from examples.client_starter.extractor import (
    Dataset,
    Service,
    ServiceCatalogExtractor,
    ServiceCatalogSnapshot,
    Team,
)
from examples.client_starter.retrieve import get_context

SAMPLE_SNAPSHOT = ServiceCatalogSnapshot(
    snapshot_id=str(uuid.uuid4()),
    teams=[
        Team(id="t1", name="Payments", slack_channel="#payments-oncall"),
        Team(id="t2", name="Data Platform", slack_channel="#data-platform"),
    ],
    datasets=[
        Dataset(
            id="d1", name="orders", platform="postgres", row_estimate=50_000_000
        ),
        Dataset(
            id="d2",
            name="users",
            platform="postgres",
            row_estimate=8_000_000,
            pii=True,
        ),
        Dataset(
            id="d3",
            name="order_events",
            platform="s3",
            row_estimate=2_000_000_000,
        ),
    ],
    services=[
        Service(
            id="s1",
            name="checkout-api",
            language="python",
            repo_url="https://git.example/mycompany/checkout-api",
            criticality="tier1",
            owner_team_id="t1",
            reads_dataset_ids=["d1", "d2"],
            writes_dataset_ids=["d1"],
        ),
        Service(
            id="s2",
            name="orders-stream-processor",
            language="go",
            repo_url="https://git.example/mycompany/orders-stream",
            criticality="tier2",
            owner_team_id="t2",
            reads_dataset_ids=["d1"],
            writes_dataset_ids=["d3"],
        ),
    ],
)


def run(server: str | None) -> None:
    extractor = ServiceCatalogExtractor()
    batch = extractor.extract(SAMPLE_SNAPSHOT)
    print(
        f"Extracted: {len(batch.entities)} entities, {len(batch.edges)} edges"
        f" (idempotency_key={batch.idempotency_key})"
    )

    with factory(in_memory=server is None, base_url=server) as client:
        result = client.submit_drafts(batch)
        print(
            f"\nSubmitted batch {result.batch_id}\n"
            f"  entities_submitted = {result.entities_submitted}"
            f" succeeded={result.succeeded} duplicates={result.duplicates}\n"
            f"  edges_submitted    = {result.edges_submitted}"
        )

        # Entities/edges populate the graph. Pack retrieval reaches into
        # the document store (the searchable semantic content), so we
        # also ingest a piece of Evidence per service. In production
        # this is where you'd ingest runbooks, incident reports, or
        # agent traces — any text the pack builder can search.
        for svc in SAMPLE_SNAPSHOT.services:
            client.ingest_evidence(
                {
                    "evidence_type": "document",
                    "source_origin": "manual",
                    "content": (
                        f"{svc.name} is a {svc.language} service "
                        f"(criticality {svc.criticality}) "
                        f"owned by team {svc.owner_team_id}. "
                        f"Reads from {svc.reads_dataset_ids or 'nothing'}; "
                        f"writes to {svc.writes_dataset_ids or 'nothing'}."
                    ),
                    "metadata": {"domain": "backend", "service_id": svc.id},
                }
            )
        print(f"\nIngested {len(SAMPLE_SNAPSHOT.services)} evidence documents")

        hits = client.search("checkout", limit=5)
        print(f"\nDocument search for 'checkout': {len(hits)} hits")
        for h in hits[:3]:
            preview = (h.get("content") or "")[:80]
            print(f"  - {h.get('doc_id', '?')}: {preview}")

        pack = get_context(client, intent="checkout")
        print(f"\n{pack.summarize()}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--server",
        default=None,
        help=(
            "Trellis base URL (e.g. http://localhost:8420). "
            "Omit to run in-memory with no external server."
        ),
    )
    args = parser.parse_args()
    run(args.server)


if __name__ == "__main__":
    main()
