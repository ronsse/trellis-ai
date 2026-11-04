"""Knob-off differential for the knowledge link-or-mint knob.

With ``TRELLIS_KNOWLEDGE_LINK_OR_MINT`` unset the branch must write exactly
what ``main`` wrote: the same nodes, edges, aliases, documents and events,
and the same reply text, for the same scripted calls. The golden next to
this file was captured from ``main`` (eca22318) before the knob existed,
by running this test with ``UPDATE_GOLDEN=1`` on an unmodified tree. Ids
are normalised to first-appearance placeholders and timestamps and
provenance stamps are excluded, so the comparison is about what was
written, not when.

Regenerating the golden on a changed tree defeats the test. Do it only
when ``main``'s knob-off behaviour is changed on purpose, and say so in
the PR.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any

import pytest

from trellis.extract.commands import result_to_batch
from trellis.mcp.server import save_knowledge
from trellis.mutate import build_curate_executor
from trellis.schemas.well_known import CONCEPT, SOFTWARE_APPLICATION
from trellis.stores.registry import StoreRegistry
from trellis_workers.extract.dbt_manifest import DbtManifestExtractor
from trellis_workers.extract.openlineage import OpenLineageExtractor

GOLDEN = Path(__file__).parent / "golden" / "knowledge_link_or_mint_knob_off.json"
_ULID = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")

SAVE_SCRIPT: list[dict[str, Any]] = [
    {"name": "Alpha gotcha", "properties": {"domain": "trellis"}},
    {
        "name": "Beta",
        "properties": {"domain": ["Finance", "trellis"]},
        "relates_to": "Bash",
    },
    {"name": "Alpha gotcha"},
    {"name": "Gamma", "relates_to": "nonexistent_id"},
    {
        "name": "Delta",
        "content": "Some evidence prose about delta.",
        "properties": {"domain": "!!!"},
    },
    {"name": "Eps", "relates_to": "model.shop.orders"},
    {"name": "Zeta", "entity_type": "person", "relates_to": "Alpha gotcha"},
    {"name": "Alpha  GOTCHA ", "properties": {"domain": "Trellis"}},
]

MANIFEST: dict[str, Any] = {
    "metadata": {"adapter_type": "postgres"},
    "nodes": {
        "model.shop.orders": {
            "unique_id": "model.shop.orders",
            "resource_type": "model",
            "name": "orders",
            "schema": "analytics",
            "database": "warehouse",
            "depends_on": {"nodes": ["source.shop.raw.orders"]},
        }
    },
    "sources": {
        "source.shop.raw.orders": {
            "unique_id": "source.shop.raw.orders",
            "resource_type": "source",
            "name": "orders",
            "source_name": "raw",
            "schema": "raw",
            "database": "warehouse",
        }
    },
}

LINEAGE_EVENTS: list[dict[str, Any]] = [
    {
        "job": {"namespace": "airflow", "name": "load_orders"},
        "inputs": [{"namespace": "postgres://db:5432", "name": "raw.orders"}],
        "outputs": [{"namespace": "postgres://db:5432", "name": "mart.orders"}],
    }
]


def _seed(registry: StoreRegistry) -> None:
    graph = registry.knowledge.graph_store
    graph.upsert_node("domain:trellis", CONCEPT, {"name": "trellis"})
    graph.upsert_node("tool:bash", SOFTWARE_APPLICATION, {"name": "Bash"})
    existing = graph.upsert_node("01SEEDNOTE", CONCEPT, {"name": "Seed note"})
    graph.bind_alias_if_absent(existing, "dbt", "model.shop.orders")


def _snapshot(registry: StoreRegistry, replies: list[str]) -> dict[str, Any]:
    graph = registry.knowledge.graph_store
    nodes = sorted(graph.query(limit=1000), key=lambda n: str(n["node_id"]))
    node_rows = [
        {
            key: node.get(key)
            for key in ("node_id", "node_type", "properties", "node_role", "document_ids")
        }
        for node in nodes
    ]
    edges: list[dict[str, Any]] = []
    aliases: list[dict[str, Any]] = []
    for node in nodes:
        node_id = str(node["node_id"])
        for edge in graph.get_edges(node_id, direction="outgoing"):
            edges.append(
                {
                    key: edge.get(key)
                    for key in ("source_id", "target_id", "edge_type", "properties")
                }
            )
        aliases.extend(
            {
                key: alias.get(key)
                for key in ("entity_id", "source_system", "raw_id", "raw_name")
            }
            for alias in graph.get_aliases(node_id)
        )
    events = [
        {
            "event_type": event.event_type.value,
            "source": event.source,
            "entity_id": event.entity_id,
            "entity_type": event.entity_type,
            "payload": event.payload,
        }
        for event in registry.operational.event_log.get_events(limit=1000)
    ]
    documents = [
        {"doc_id": doc.get("doc_id"), "content": doc.get("content")}
        for doc in registry.knowledge.document_store.list_documents(limit=1000)
    ]
    return {
        "replies": replies,
        "nodes": node_rows,
        "edges": edges,
        "aliases": aliases,
        "events": events,
        "documents": documents,
    }


# Order-insensitive collections: ULIDs minted in one millisecond do not sort
# in creation order, so these are sorted after the ids are replaced.
_UNORDERED = ("nodes", "edges", "aliases", "documents")


def _normalise(snapshot: dict[str, Any], replies: list[str]) -> dict[str, Any]:
    mapping: dict[str, str] = {}
    for reply in replies:
        for found in _ULID.findall(reply):
            mapping.setdefault(found, f"<id{len(mapping) + 1}>")
    text = json.dumps(snapshot, sort_keys=True, default=str)

    def sub(match: re.Match[str]) -> str:
        return mapping.setdefault(match.group(0), f"<id{len(mapping) + 1}>")

    out: dict[str, Any] = json.loads(_ULID.sub(sub, text))
    for key in _UNORDERED:
        out[key] = sorted(
            out[key], key=lambda row: json.dumps(row, sort_keys=True, default=str)
        )
    return out


def _run_saves(registry: StoreRegistry) -> dict[str, Any]:
    _seed(registry)
    replies = [save_knowledge(**call) for call in SAVE_SCRIPT]
    return _normalise(_snapshot(registry, replies), replies)


def _run_extractors(registry: StoreRegistry) -> dict[str, Any]:
    executor = build_curate_executor(registry, clearance="restricted")
    commands: list[dict[str, Any]] = []
    for extractor, raw, hint, who in (
        (DbtManifestExtractor(), MANIFEST, "dbt-manifest", "cli:dbt-manifest"),
        (OpenLineageExtractor(), LINEAGE_EVENTS, "openlineage", "cli:openlineage"),
    ):
        result = asyncio.run(extractor.extract(raw, source_hint=hint))
        batch = result_to_batch(result, requested_by=who)
        commands.extend(
            {
                "operation": c.operation.value,
                "args": json.loads(json.dumps(c.args, default=str)),
                "requested_by": c.requested_by,
            }
            for c in batch.commands
        )
        executor.execute_batch(batch)
    snap = _snapshot(registry, [])
    snap["commands"] = commands
    return _normalise(snap, [])


def _check(name: str, actual: dict[str, Any]) -> None:
    golden = json.loads(GOLDEN.read_text()) if GOLDEN.exists() else {}
    if os.environ.get("UPDATE_GOLDEN") == "1":
        golden[name] = actual
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps(golden, indent=1, sort_keys=True) + "\n")
        pytest.skip("golden rewritten")
    assert actual == golden[name]


@pytest.fixture(autouse=True)
def _knob_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRELLIS_KNOWLEDGE_LINK_OR_MINT", raising=False)


def test_save_knowledge_knob_off_matches_main(temp_registry: StoreRegistry) -> None:
    _check("save_knowledge", _run_saves(temp_registry))


def test_extractor_batches_knob_off_match_main(temp_registry: StoreRegistry) -> None:
    _check("extractors", _run_extractors(temp_registry))


@pytest.mark.parametrize("spelling", ["0", "false", "off", "", "no", "garbage"])
def test_save_knowledge_knob_off_spellings_match_main(
    temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch, spelling: str
) -> None:
    monkeypatch.setenv("TRELLIS_KNOWLEDGE_LINK_OR_MINT", spelling)
    _check("save_knowledge", _run_saves(temp_registry))
