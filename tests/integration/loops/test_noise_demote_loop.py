"""Noise demote loop — feedback closes at the next pack.

Proves the full closed loop the project shipped in PR #65 works end
to end at the public surface:

    seed corpus →
    DEMOTION_ROUNDS rounds of (pack → feedback rejecting the distractor) →
    apply-noise-tags →
    pack N+1 (distractor excluded by signal_quality filter)

The inside-out version of this loop lives in
``eval/scenarios/agent_loop_convergence/scenario.py``. The point of
this file is to prove the **public surface** version works — the
REST API for ingest + retrieval + apply-noise-tags, and MCP for the
per-item feedback signal that today's REST surface doesn't expose.

**Why the loop runs several rounds.** It used to seed one pack and one
feedback event, and relied on the distractor being *never cited
helpful* to demote it. #380 removed that rule: absence of praise is not
evidence of unhelpfulness, and the version of the rule this fixture
encoded flagged 81% of scored items on the live corpus. Demotion now
requires explicit ``unhelpful_item_ids`` citations plus enough
attributed packs in the window to have a population to reason over.

The fixture supplies that evidence rather than lowering the bar. A
lowered ``min_attributed_packs`` would leave the loop green while
testing a configuration no deployment runs — and the floor is the
thing most worth having an end-to-end test of, because it is what
stands between a grading outage and a corpus-wide demotion.

Skipped when ``TRELLIS_TEST_NEO4J_URI`` or ``TRELLIS_TEST_PG_DSN``
is unset; runs against the same Neon + AuraDB cluster the API smoke
matrix uses.
"""

from __future__ import annotations

import pytest

from tests.integration.loops.conftest import (
    DEMOTION_ROUNDS,
    LoopEnvironment,
    assert_demotion_admitted,
    assert_demotion_withheld_below_floor,
    build_pack,
    build_pack_with_distractor,
    item_ids,
    seed_distractor_corpus,
    trigger_apply_noise_tags,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.live,
    pytest.mark.slow,
    pytest.mark.neo4j,
    pytest.mark.postgres,
]


_INTENT = "noisedemote"  # single-token; see conftest module docstring


async def _serve_and_reject(
    loop_env: LoopEnvironment,
    *,
    distractor_id: str,
    helpful_ids: list[str],
) -> dict:
    """One graded round: assemble a pack, cite the distractor unhelpful.

    Records per-item feedback through the MCP surface. The REST API's
    ``/api/v1/packs/{pack_id}/feedback`` route doesn't currently accept
    ``helpful_item_ids`` / ``unhelpful_item_ids`` in its body; the MCP
    tool emits the same ``FEEDBACK_RECORDED`` event with that payload,
    which is what ``analyze_effectiveness`` reads. Keeps the loop honest
    to today's surfaces — no new product code in this test.
    """
    pack, helpful_in_pack = build_pack_with_distractor(
        loop_env.api_url,
        intent=_INTENT,
        distractor_id=distractor_id,
        helpful_ids=helpful_ids,
    )
    result = await loop_env.mcp.call_tool(
        "record_feedback",
        {
            "pack_id": pack["pack_id"],
            "success": True,
            "helpful_item_ids": helpful_in_pack,
            "unhelpful_item_ids": [distractor_id],
            "notes": "noise-demote loop probe",
        },
    )
    text = getattr(result, "data", "") or ""
    # Reported against the whole result, not against ``text``: a tool
    # call that came back without a ``data`` attribute at all would
    # otherwise fail with an empty message.
    assert "Feedback recorded" in text, f"unexpected record_feedback result: {result!r}"
    return pack


async def test_noise_demote_loop(loop_env: LoopEnvironment) -> None:
    """Per-item feedback marks a doc unhelpful; the next pack drops it."""
    distractor_id, helpful_ids = seed_distractor_corpus(loop_env.api_url)

    # --- One round short of the coverage floor: the gate refuses. ---
    #
    # Asserted before the loop closes, not after, because "the gate
    # still refuses under-evidenced demotions" is the governed
    # behaviour #380 shipped. A test that only proved demotion happens
    # would pass just as well against a gate that had been removed.
    for _ in range(DEMOTION_ROUNDS - 1):
        await _serve_and_reject(
            loop_env, distractor_id=distractor_id, helpful_ids=helpful_ids
        )

    withheld = trigger_apply_noise_tags(loop_env.api_url)
    assert withheld["status"] == "ok"
    assert_demotion_withheld_below_floor(withheld, attributed_packs=DEMOTION_ROUNDS - 1)

    withheld_pack = build_pack(loop_env.api_url, intent=_INTENT, tag_filters={})
    still_served = item_ids(withheld_pack)
    assert distractor_id in still_served, (
        f"a withheld demotion must not tag anything — distractor "
        f"{distractor_id!r} disappeared anyway: {sorted(still_served)}"
    )

    # --- One more graded round clears the floor: the gate admits. ---
    last_pack = await _serve_and_reject(
        loop_env, distractor_id=distractor_id, helpful_ids=helpful_ids
    )

    report = trigger_apply_noise_tags(loop_env.api_url)
    assert report["status"] == "ok"
    assert report["noise_candidates_tagged"] >= 1, (
        f"apply-noise-tags should have tagged at least the distractor: {report}"
    )
    assert distractor_id in set(report["noise_candidates"]), (
        f"distractor {distractor_id!r} not proposed: {report['noise_candidates']}"
    )
    assert_demotion_admitted(report, item_id=distractor_id)

    # Empty ``tag_filters`` dict opts in to the default ``signal_quality``
    # filter, which excludes the just-tagged noise document.
    pack_final = build_pack(loop_env.api_url, intent=_INTENT, tag_filters={})
    final_items = item_ids(pack_final)
    assert pack_final["pack_id"] != last_pack["pack_id"], (
        "the closing pack must be a fresh assembly, not a cached re-issue"
    )
    assert distractor_id not in final_items, (
        f"the loop didn't close — distractor {distractor_id!r} still served: "
        f"{sorted(final_items)}"
    )
    assert final_items, (
        "the loop demoted the whole corpus — the helpful docs must survive, "
        "or 'excluded the distractor' is indistinguishable from 'excluded "
        "everything'"
    )
