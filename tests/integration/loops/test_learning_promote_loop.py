"""Learning promote loop — successful packs → reviewed promotion → graph precedent.

Proves the EventLog-authoritative promote half of the dual-loop closes
end-to-end at the public surface:

    seed corpus → run packs with successful feedback (3 rounds) →
    trellis analyze learning-candidates → operator approves a row →
    trellis curate promote-learning → verify precedent in graph

Like the other loops in this directory, ingest + retrieval flow
through REST and per-item feedback through the MCP ``record_feedback``
tool. The score-and-approve and promotion steps run as **CLI
subprocesses** — the same ``trellis analyze learning-candidates`` and
``trellis curate promote-learning`` operators will use in production
— so the loop is genuinely outside-in: every step crosses a process
boundary and uses a public surface.

Two pieces still cross a tier boundary that has no public CLI surface:

* The pre-flight Postgres-only state wipe done by ``loop_env``.
* The post-promotion graph-store assertion (verifying the precedent
  node landed) — opens its own ``StoreRegistry`` against the same
  cloud config so it reads the same Neo4j the spawned API server
  wrote to.

Skipped when ``TRELLIS_TEST_NEO4J_URI`` or ``TRELLIS_TEST_PG_DSN``
is unset — same gating as the rest of the loop suite.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from tests.integration._live_server import (
    NEO4J_URI,
    PG_DSN,
    build_subprocess_env,
    find_console_script,
    run_cli,
)
from tests.integration.loops.conftest import live_registry
from trellis.learning import PROMOTE_RECOMMENDATIONS

if TYPE_CHECKING:
    from tests.integration.loops.conftest import LoopEnvironment

pytestmark = pytest.mark.asyncio

_INTENT = "learnpromote"  # single-token; see conftest module docstring
_HELPFUL_DOC_ID = "lp:doc:helpful"
_DOMAIN = "learning-promote"

# Default ``min_support`` is 2; running 3 rounds gives one observation
# of headroom over the floor and avoids brittle exact-match assertions.
_PACK_ROUNDS = 3


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
    """Assemble one pack via REST and assert the helpful doc is in it.

    Inlines the POST rather than calling ``conftest.build_pack`` so the
    three pack rounds in :func:`_run_graded_pack_rounds` reuse a single
    httpx connection. ``max_items=10`` / ``max_tokens=2000`` match
    ``build_pack``'s defaults.
    """
    resp = client.post(
        "/api/v1/packs",
        json={
            "intent": _INTENT,
            "domain": _DOMAIN,
            "max_items": 10,
            "max_tokens": 2000,
        },
    )
    assert resp.status_code == 200, resp.text
    pack = resp.json()
    served = {item["item_id"] for item in pack["items"]}
    assert _HELPFUL_DOC_ID in served, (
        f"helpful doc must appear in every pack to accumulate "
        f"times_served; got {sorted(served)}"
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


def _score_via_cli(
    trellis_bin: str,
    subprocess_env: dict[str, str],
    review_dir: Path,
) -> dict[str, Any]:
    """Run ``trellis analyze learning-candidates`` and return the JSON payload.

    The caller reads ``candidates_path`` / ``decisions_template_path``
    from the payload to feed the next step; the test never has to know
    the artifact filenames.
    """
    _, payload = run_cli(
        trellis_bin,
        [
            "analyze",
            "learning-candidates",
            "--output-dir",
            str(review_dir),
            "--days",
            "7",
            "--format",
            "json",
        ],
        subprocess_env,
    )
    assert payload["status"] == "ok", payload
    assert payload["observation_count"] >= _PACK_ROUNDS, (
        f"expected ≥{_PACK_ROUNDS} observations after {_PACK_ROUNDS} graded "
        f"packs; got {payload['observation_count']}"
    )
    assert payload["candidate_count"] >= 1, f"scorer produced no candidates: {payload}"
    helpful = next(
        (c for c in payload["candidates"] if c["item_id"] == _HELPFUL_DOC_ID),
        None,
    )
    assert helpful is not None, (
        f"helpful doc {_HELPFUL_DOC_ID!r} missing from candidates: "
        f"{[c['item_id'] for c in payload['candidates']]}"
    )
    assert helpful["recommendation_type"] in PROMOTE_RECOMMENDATIONS, helpful
    assert helpful["metrics"]["times_served"] >= _PACK_ROUNDS
    assert helpful["metrics"]["success_rate"] == pytest.approx(1.0)
    assert helpful["metrics"]["retry_rate"] == pytest.approx(0.0)
    return payload


def _auto_approve(decisions_path: Path, candidate_id: str) -> None:
    """Mark the candidate as approved in the decisions template, in place."""
    decisions = json.loads(decisions_path.read_text(encoding="utf-8"))
    approved = 0
    for decision in decisions["decisions"]:
        if decision["candidate_id"] == candidate_id:
            decision["approved"] = True
            decision["rationale"] = "H2.3 loop-test auto-approval"
            approved += 1
    assert approved == 1, (
        f"candidate {candidate_id} not found in decisions template: {decisions}"
    )
    decisions_path.write_text(
        json.dumps(decisions, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _promote_via_cli(
    trellis_bin: str,
    subprocess_env: dict[str, str],
    candidates_path: Path,
    decisions_path: Path,
) -> str:
    """Run ``trellis curate promote-learning`` and return the created node_id."""
    _, payload = run_cli(
        trellis_bin,
        [
            "curate",
            "promote-learning",
            "--candidates",
            str(candidates_path),
            "--decisions",
            str(decisions_path),
            "--format",
            "json",
        ],
        subprocess_env,
    )
    assert payload["status"] == "ok", payload
    assert payload["dry_run"] is False
    assert payload["promoted_count"] == 1, payload
    assert len(payload["results"]) == 1, payload
    result = payload["results"][0]
    assert result["status"] == "promoted", result
    assert result["node_id"], result
    return result["node_id"]


def _verify_precedent_in_graph(
    loop_env: LoopEnvironment,
    node_id: str,
    helpful_candidate: dict,
) -> None:
    """Open a fresh registry and assert the precedent landed in the graph store."""
    with live_registry(loop_env.config_dir, loop_env.data_dir) as registry:
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
    """Feedback → CLI score → operator approval → CLI promote → graph precedent."""
    if not NEO4J_URI or not PG_DSN:  # paranoia — loop_env already gates this
        pytest.skip("live infra creds missing")

    trellis_bin = find_console_script(
        "trellis", install_hint="install with `pip install -e .`"
    )
    subprocess_env = build_subprocess_env(loop_env.config_dir, loop_env.data_dir)

    _seed_helpful_doc(loop_env.api_url)
    await _run_graded_pack_rounds(loop_env)

    review_dir = loop_env.data_dir / "learning_review"
    score_payload = _score_via_cli(trellis_bin, subprocess_env, review_dir)
    candidates_path = Path(score_payload["candidates_path"])
    decisions_path = Path(score_payload["decisions_template_path"])
    helpful_candidate = next(
        c for c in score_payload["candidates"] if c["item_id"] == _HELPFUL_DOC_ID
    )
    _auto_approve(decisions_path, helpful_candidate["candidate_id"])
    node_id = _promote_via_cli(
        trellis_bin, subprocess_env, candidates_path, decisions_path
    )
    _verify_precedent_in_graph(loop_env, node_id, helpful_candidate)
