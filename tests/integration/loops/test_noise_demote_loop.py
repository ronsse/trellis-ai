"""Noise demote loop — feedback closes at the next pack.

Proves the full closed loop the project shipped in PR #65 works end
to end at the public surface:

    seed corpus → pack 1 (distractor included) →
    record per-item feedback marking the distractor as never useful →
    apply-noise-tags →
    pack 2 (distractor excluded by signal_quality filter)

The inside-out version of this loop lives in
``eval/scenarios/agent_loop_convergence/scenario.py``. The point of
this file is to prove the **public surface** version works — the
REST API for ingest + retrieval + apply-noise-tags, and MCP for the
per-item feedback signal that today's REST surface doesn't expose.

Skipped when ``TRELLIS_TEST_NEO4J_URI`` or ``TRELLIS_TEST_PG_DSN``
is unset; runs against the same Neon + AuraDB cluster the API smoke
matrix uses.
"""

from __future__ import annotations

import pytest

from tests.integration.loops.conftest import (
    LoopEnvironment,
    build_pack,
    seed_distractor_corpus,
    trigger_apply_noise_tags,
)

pytestmark = pytest.mark.asyncio


# Single-token intent matches the seeded corpus' shared marker via
# Postgres full-text search. Multi-token queries like
# ``"noise-demote-loop probe"`` tokenize to terms that aren't present
# in the content and end up retrieving nothing.
_INTENT = "noisedemote"
_PACK_FETCH_LIMIT = 10
_PACK_TOKEN_BUDGET = 2000


def _item_ids(pack: dict) -> set[str]:
    """Pull ``item_id`` out of every entry in a pack response body."""
    return {item["item_id"] for item in pack["items"]}


async def test_noise_demote_loop(loop_env: LoopEnvironment) -> None:
    """Per-item feedback marks an unhelpful doc as noise; next pack drops it."""
    distractor_id, helpful_ids = seed_distractor_corpus(loop_env.api_url)

    pack_1 = build_pack(
        loop_env.api_url,
        intent=_INTENT,
        max_items=_PACK_FETCH_LIMIT,
        max_tokens=_PACK_TOKEN_BUDGET,
        tag_filters={},
    )
    pack_1_items = _item_ids(pack_1)
    assert distractor_id in pack_1_items, (
        f"the loop's value depends on the distractor showing up in pack 1; "
        f"got items={sorted(pack_1_items)}"
    )
    helpful_in_pack_1 = sorted(pack_1_items.intersection(helpful_ids))
    assert helpful_in_pack_1, (
        "expected at least one helpful doc in pack 1 — without one, the "
        "feedback signal carries no contrast"
    )

    # Record per-item feedback through the MCP surface. The REST API's
    # /api/v1/packs/{pack_id}/feedback route doesn't currently accept
    # ``helpful_item_ids`` / ``unhelpful_item_ids`` in its body; the MCP
    # tool emits the same FEEDBACK_RECORDED event with that payload,
    # which is what analyze_effectiveness reads. Keeps the loop honest
    # to today's surfaces — no new product code in this PR.
    feedback_result = await loop_env.mcp.call_tool(
        "record_feedback",
        {
            "pack_id": pack_1["pack_id"],
            "success": True,
            "helpful_item_ids": helpful_in_pack_1,
            "unhelpful_item_ids": [distractor_id],
            "notes": "noise-demote loop probe",
        },
    )
    feedback_text = getattr(feedback_result, "data", "") or ""
    assert "Feedback recorded" in feedback_text, feedback_text

    report = trigger_apply_noise_tags(loop_env.api_url)
    assert report["status"] == "ok"
    assert report["noise_candidates_tagged"] >= 1, (
        f"apply-noise-tags should have tagged at least the distractor: {report}"
    )
    noise_ids = set(report.get("noise_candidates", []))
    assert distractor_id in noise_ids, (
        f"distractor {distractor_id!r} not in noise list: {sorted(noise_ids)}"
    )

    pack_2 = build_pack(
        loop_env.api_url,
        intent=_INTENT,
        max_items=_PACK_FETCH_LIMIT,
        max_tokens=_PACK_TOKEN_BUDGET,
        # Empty dict opts in to the default ``signal_quality`` filter,
        # which is what excludes the just-tagged noise document.
        tag_filters={},
    )
    pack_2_items = _item_ids(pack_2)
    assert pack_2["pack_id"] != pack_1["pack_id"], (
        "second pack must be a fresh assembly, not a cached re-issue"
    )
    assert distractor_id not in pack_2_items, (
        f"the loop didn't close — distractor {distractor_id!r} still in pack 2: "
        f"{sorted(pack_2_items)}"
    )
