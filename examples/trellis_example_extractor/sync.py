"""CLI entry point that runs the example extractor against a server.

Usage::

    python -m examples.trellis_example_extractor.sync \
        --server http://localhost:8420 \
        --snapshot-id 2026-04-18T14:00:00Z

Or against a tmp in-process server for the demo::

    python -m examples.trellis_example_extractor.sync --in-memory

Fork notes: replace ``SAMPLE_DATA`` with whatever your real source
fetcher returns (``boto3.client("glue").get_tables()``,
``databricks.sdk.WorkspaceClient().tables.list(...)``, a dbt
manifest load, etc).
"""

from __future__ import annotations

import argparse
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from examples.trellis_example_extractor.reader import (
    Container,
    ExampleExtractor,
    SourceData,
    Widget,
)
from trellis_sdk import TrellisClient
from trellis_sdk.extract import DraftSubmissionResult

# Replace with a real fetcher.
SAMPLE_DATA = SourceData(
    snapshot_id=str(uuid.uuid4()),
    widgets=[
        Widget(id="w1", name="Red Gear", color="red", weight_grams=42.5, tags=["mech"]),
        Widget(id="w2", name="Blue Cog", color="blue", weight_grams=13.0),
        Widget(id="w3", name="Green Spring", color="green", weight_grams=4.2),
    ],
    containers=[
        Container(
            id="c1",
            name="Assembly Box",
            capacity=10,
            location="shelf-a",
            widget_ids=["w1", "w2"],
        ),
        Container(
            id="c2",
            name="Spares Tray",
            capacity=20,
            location="shelf-b",
            widget_ids=["w3"],
        ),
    ],
)


@contextmanager
def _build_client(server: str | None) -> Iterator[TrellisClient]:
    if server is None:
        # In-memory mode — useful for local demos without running a server.
        from trellis.testing import in_memory_client  # noqa: PLC0415

        with (
            tempfile.TemporaryDirectory() as scratch,
            in_memory_client(Path(scratch) / "stores") as client,
        ):
            yield client
        return
    client = TrellisClient(base_url=server)
    try:
        yield client
    finally:
        client.close()


def run(server: str | None) -> DraftSubmissionResult:
    extractor = ExampleExtractor()
    batch = extractor.extract(SAMPLE_DATA)
    with _build_client(server) as client:
        return client.submit_drafts(batch)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--server",
        default=None,
        help="Trellis base URL (e.g. http://localhost:8420). "
        "Omit to use an in-memory test fixture.",
    )
    args = parser.parse_args()

    result = run(args.server)
    print(
        f"Submitted batch {result.batch_id}\n"
        f"  extractor       : {result.extractor}\n"
        f"  idempotency_key : {result.idempotency_key}\n"
        f"  entities        : {result.entities_submitted}\n"
        f"  edges           : {result.edges_submitted}\n"
        f"  succeeded       : {result.succeeded}\n"
        f"  failed          : {result.failed}\n"
        f"  duplicates      : {result.duplicates}"
    )


if __name__ == "__main__":
    main()
