"""Learning promote loop — successful packs → reviewed promotion → graph precedent.

Proves the EventLog-authoritative promote half of the dual-loop closes
end-to-end at the public surface:

    seed corpus → run packs with successful feedback (3 rounds) →
    bridge events to learning observations → analyze →
    write review artifacts → auto-approve a candidate →
    prepare_learning_promotions → submit entity via
    POST /api/v1/entities → verify precedent node in the graph

Like the other loops in this directory, ingest + retrieval + entity
creation flow through REST, per-item feedback through the MCP
``record_feedback`` tool (today's REST surface doesn't accept
``helpful_item_ids``). Steps the system has no public surface for yet
— the EventLog-to-observations bridge, scorer, artifact writer,
decisions approval, and promotion preparation — run as direct Python
in the test process. ``trellis.learning.observations`` documents this
gap explicitly: the JSONL-only ``pack_feedback.jsonl`` bridge is
deferred because the file alone doesn't carry the per-item
``item_type`` / ``source_strategy`` details ``learning.scoring``
needs.

The test process opens its own ``StoreRegistry`` against the same
Postgres + Neo4j backends the spawned subprocesses write to
(``loop_env.config_dir`` is the cloud-config YAML; env vars are set
transiently so plane-aware DSN resolution finds the DSNs that
``build_subprocess_env`` gave the subprocesses).

Skipped when ``TRELLIS_TEST_NEO4J_URI`` or ``TRELLIS_TEST_PG_DSN``
is unset — same gating as the rest of the loop suite.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from tests.integration._live_server import NEO4J_URI, PG_DSN
from trellis.learning import (
    analyze_learning_observations,
    build_learning_observations_from_event_log,
    prepare_learning_promotions,
    write_learning_review_artifacts,
)
from trellis.stores.registry import StoreRegistry

if TYPE_CHECKING:
    from collections.abc import Iterator

    from tests.integration.loops.conftest import LoopEnvironment

pytestmark = pytest.mark.asyncio

# Single-token marker so Postgres FTS retrieves the seeded document
# for every pack round; multi-token intents tokenize to terms that
# aren't in the doc content. The noise-demote loop's docstring
# documents the same caveat.
_INTENT = "learnpromote"
_HELPFUL_DOC_ID = "lp:doc:helpful"
_DOMAIN = "learning-promote"

# Default ``min_support`` is 2; running 3 rounds gives one observation
# of headroom over the floor and avoids brittle exact-match assertions.
_PACK_ROUNDS = 3
_PACK_FETCH_LIMIT = 10
_PACK_TOKEN_BUDGET = 2000


@contextmanager
def _live_registry(config_dir: Path, data_dir: Path) -> Iterator[StoreRegistry]:
    """Yield a ``StoreRegistry`` pointing at the loop-test backends.

    Mirrors the env-var dance ``wipe_live_state_for_config`` performs:
    plane-aware DSN resolution reads ``TRELLIS_KNOWLEDGE_PG_DSN`` /
    ``TRELLIS_OPERATIONAL_PG_DSN`` from the process env, so we set
    them just for the registry's lifetime and restore on exit. The
    test process and the spawned subprocesses end up reading and
    writing the same Postgres tables and Neo4j database. The Neo4j
    credentials live in the cloud-config YAML, so they don't need an
    env-var bridge.
    """
    env_overrides = {
        "TRELLIS_CONFIG_DIR": str(config_dir),
        "TRELLIS_DATA_DIR": str(data_dir),
        "TRELLIS_KNOWLEDGE_PG_DSN": PG_DSN or "",
        "TRELLIS_OPERATIONAL_PG_DSN": PG_DSN or "",
    }
    saved = {k: os.environ.get(k) for k in env_overrides}
    try:
        os.environ.update(env_overrides)
        registry = StoreRegistry.from_config_dir(config_dir=config_dir)
        try:
            yield registry
        finally:
            registry.close()
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _seed_helpful_doc(api_url: str) -> None:
    """Seed a single document the loop will repeatedly retrieve as helpful."""
    with httpx.Client(base_url=api_url, timeout=30.0) as client:
        resp = client.post(
            "/api/v1/documents",
            json={
                "doc_id": _HELPFUL_DOC_ID,
                "content": (
                    f"{_INTENT} canonical reference document. Standard "
                    "workflow that should appear in successful packs."
                ),
                "metadata": {
                    "domain": _DOMAIN,
                    "source": "learning-promote-test",
                },
            },
        )
        assert resp.status_code == 200, resp.text


def _build_pack_or_skip(client: httpx.Client) -> dict:
    """Assemble one pack via REST and assert the helpful doc is in it."""
    resp = client.post(
        "/api/v1/packs",
        json={
            "intent": _INTENT,
            "domain": _DOMAIN,
            "max_items": _PACK_FETCH_LIMIT,
            "max_tokens": _PACK_TOKEN_BUDGET,
        },
    )
    assert resp.status_code == 200, resp.text
    pack = resp.json()
    item_ids = {item["item_id"] for item in pack["items"]}
    assert _HELPFUL_DOC_ID in item_ids, (
        f"helpful doc must appear in every pack to accumulate "
        f"times_served; got {sorted(item_ids)}"
    )
    return pack


async def _run_graded_pack_rounds(loop_env: LoopEnvironment) -> None:
    """Run ``_PACK_ROUNDS`` packs and grade each as a success via MCP."""
    with httpx.Client(base_url=loop_env.api_url, timeout=30.0) as client:
        for round_idx in range(_PACK_ROUNDS):
            pack = _build_pack_or_skip(client)
            feedback_result = await loop_env.mcp.call_tool(
                "record_feedback",
                {
                    "pack_id": pack["pack_id"],
                    "success": True,
                    "helpful_item_ids": [_HELPFUL_DOC_ID],
                    "notes": f"learning-promote loop probe round {round_idx}",
                },
            )
            feedback_text = getattr(feedback_result, "data", "") or ""
            assert "Feedback recorded" in feedback_text, feedback_text


def _score_observations(loop_env: LoopEnvironment) -> tuple[dict, dict]:
    """Bridge events → observations → score → return ``(report, helpful_candidate)``."""
    with _live_registry(loop_env.config_dir, loop_env.data_dir) as registry:
        observations = build_learning_observations_from_event_log(
            registry.operational.event_log, days=7
        )

    assert len(observations) >= _PACK_ROUNDS, (
        f"expected ≥{_PACK_ROUNDS} observations after {_PACK_ROUNDS} graded "
        f"packs; got {len(observations)}: {observations}"
    )

    report = analyze_learning_observations(observations=observations)
    assert report["candidate_count"] >= 1, (
        f"scorer produced no candidates from {len(observations)} observations: {report}"
    )
    helpful_candidate = next(
        (c for c in report["candidates"] if c["item_id"] == _HELPFUL_DOC_ID),
        None,
    )
    assert helpful_candidate is not None, (
        f"helpful doc {_HELPFUL_DOC_ID!r} missing from candidates "
        f"{[c['item_id'] for c in report['candidates']]}"
    )
    # success_rate=1.0 + retry_rate=0.0 over min_support=2 should clear
    # the promote thresholds (0.75 / 0.25); item_type comes back as
    # "document" so the recommendation is ``promote_guidance`` rather
    # than ``promote_precedent`` — both are accepted by
    # ``prepare_learning_promotions``.
    assert helpful_candidate["recommendation_type"] in {
        "promote_precedent",
        "promote_guidance",
    }, helpful_candidate
    assert helpful_candidate["metrics"]["times_served"] >= _PACK_ROUNDS
    assert helpful_candidate["metrics"]["success_rate"] == pytest.approx(1.0)
    assert helpful_candidate["metrics"]["retry_rate"] == pytest.approx(0.0)
    return report, helpful_candidate


def _approve_and_prepare_promotion(
    report: dict,
    helpful_candidate: dict,
    artifacts_dir: Path,
) -> tuple[dict, list[dict]]:
    """Write artifacts, auto-approve the candidate, prepare promotion payloads."""
    paths = write_learning_review_artifacts(report=report, output_dir=artifacts_dir)
    candidates_path = Path(paths["candidates_path"])
    decisions_path = Path(paths["decisions_template_path"])
    assert candidates_path.exists()
    assert decisions_path.exists()

    decisions = json.loads(decisions_path.read_text(encoding="utf-8"))
    candidate_id = helpful_candidate["candidate_id"]
    approved_count = 0
    for decision in decisions["decisions"]:
        if decision["candidate_id"] == candidate_id:
            decision["approved"] = True
            decision["promotion_name"] = (
                decision.get("promotion_name") or helpful_candidate["precedent_name"]
            )
            decision["rationale"] = "H2.3 loop-test auto-approval"
            approved_count += 1
    assert approved_count == 1, (
        f"candidate {candidate_id} not in decisions template: {decisions}"
    )
    decisions_path.write_text(
        json.dumps(decisions, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    candidates_payload = json.loads(candidates_path.read_text(encoding="utf-8"))
    decisions_payload = json.loads(decisions_path.read_text(encoding="utf-8"))
    promotion = prepare_learning_promotions(
        candidates_payload=candidates_payload,
        decisions_payload=decisions_payload,
    )
    assert promotion["approved_count"] == 1, promotion
    ready = [r for r in promotion["results"] if r["status"] == "ready"]
    assert len(ready) == 1, f"expected exactly one ready promotion: {promotion}"
    return ready[0]["entity_payload"], ready[0]["edge_payloads"]


def _submit_promotion(
    api_url: str,
    entity_payload: dict,
    edge_payloads: list[dict],
) -> str:
    """Submit the entity (and any edges) via REST. Returns the created ``node_id``.

    Today's PACK_ASSEMBLED telemetry doesn't stamp ``target_entity_ids``
    in its payload, so document-only loops produce candidates with
    empty ``target_entity_ids`` and zero edges. The empty-edges path
    is still exercised below so a future pack-builder change that
    starts populating the field doesn't silently break the link half
    of the loop.
    """
    with httpx.Client(base_url=api_url, timeout=30.0) as client:
        ent_resp = client.post(
            "/api/v1/entities",
            json={
                "entity_type": entity_payload["entity_type"],
                "entity_id": entity_payload["entity_id"],
                "name": entity_payload["name"],
                "properties": entity_payload["properties"],
            },
        )
        assert ent_resp.status_code == 200, ent_resp.text
        ent_body = ent_resp.json()
        assert ent_body["status"] == "ok", ent_body
        node_id = ent_body["node_id"]
        assert node_id, ent_body

        for edge in edge_payloads:
            link_resp = client.post(
                "/api/v1/links",
                json={
                    "source_id": edge["source_id"],
                    "target_id": edge["target_id"],
                    "edge_kind": edge["edge_kind"],
                    "properties": edge.get("properties") or {},
                },
            )
            assert link_resp.status_code == 200, link_resp.text
    return node_id


def _verify_precedent_in_graph(
    loop_env: LoopEnvironment,
    node_id: str,
    helpful_candidate: dict,
) -> None:
    """Open a fresh registry and assert the precedent landed in the graph store."""
    with _live_registry(loop_env.config_dir, loop_env.data_dir) as registry:
        node = registry.knowledge.graph_store.get_node(node_id)
    assert node is not None, (
        f"promoted precedent node_id={node_id} not found in graph store"
    )
    assert node["node_type"] == "precedent", node
    properties: dict[str, Any] = node.get("properties") or {}
    assert properties.get("source_item_id") == _HELPFUL_DOC_ID, properties
    assert properties.get("source_of_truth") == "reviewed_promotion", properties
    assert properties.get("intent_family") == helpful_candidate["intent_family"]


async def test_learning_promote_loop(loop_env: LoopEnvironment) -> None:
    """Successful feedback → learning candidate → approved → graph precedent."""
    if not NEO4J_URI or not PG_DSN:  # paranoia — loop_env already gates this
        pytest.skip("live infra creds missing")

    _seed_helpful_doc(loop_env.api_url)
    await _run_graded_pack_rounds(loop_env)

    report, helpful_candidate = _score_observations(loop_env)
    artifacts_dir = loop_env.data_dir / "learning_review"
    entity_payload, edge_payloads = _approve_and_prepare_promotion(
        report, helpful_candidate, artifacts_dir
    )
    node_id = _submit_promotion(loop_env.api_url, entity_payload, edge_payloads)
    _verify_precedent_in_graph(loop_env, node_id, helpful_candidate)
