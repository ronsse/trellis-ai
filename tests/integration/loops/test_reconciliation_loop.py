"""Reconciliation loop — JSONL-only feedback heals into the EventLog.

Proves the divergence-recovery primitive ``trellis.feedback.recording.
reconcile_feedback_log_to_event_log`` works end-to-end against the
live infra: feedback that landed only in ``pack_feedback.jsonl``
(file-only capture, sink unavailable, process crash between writes,
etc.) can be backfilled into the authoritative EventLog and then
drives the same downstream analytics a live emission would have:

    seed corpus →
    N-1 × (pack → JSONL feedback ONLY, no ``event_log`` kwarg) →
    confirm the EventLog has no matching ``FEEDBACK_RECORDED`` →
    reconcile → the events land, and the demotion gate *counts them*
      but still refuses: one pack short of the coverage floor →
    one more file-only round → reconcile again (only the new row
      emits — the healed ones are idempotent) →
    apply-noise-tags via REST → distractor demoted →
    final pack (distractor excluded by signal_quality filter)

The test process writes JSONL via ``record_feedback`` from
``trellis.feedback`` directly. That's the same code path production
uses for file-only capture; the test deliberately skips the MCP
``record_feedback`` tool here so the EventLog stays empty for our
packs until ``reconcile_*`` runs. The reconciler itself opens its own
``StoreRegistry`` (scoped DSN env vars) so it reads / writes the
same Postgres EventLog the spawned uvicorn uses.

**What "indistinguishable from a live emission" now means.** The loop
used to end by relying on the distractor being served-but-never-cited,
which #380 removed as unsound — absence of praise is not evidence of
unhelpfulness. The JSONL rows now carry explicit
``unhelpful_item_ids``, which makes this a stricter test of
reconciliation than it was: the replayed payload has to carry per-item
*negative* attribution through ``PackFeedback.to_event_payload`` for
the gate downstream to see anything at all, and the intermediate
refusal pins that the reconciled events are the ones being counted.

Skipped when ``TRELLIS_TEST_NEO4J_URI`` or ``TRELLIS_TEST_PG_DSN``
is unset — same gating as the rest of the loop suite.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.integration._live_server import NEO4J_URI, PG_DSN
from tests.integration.loops.conftest import (
    DEMOTION_ROUNDS,
    assert_demotion_admitted,
    assert_demotion_withheld_below_floor,
    build_pack,
    build_pack_with_distractor,
    item_ids,
    live_registry,
    seed_distractor_corpus,
    trigger_apply_noise_tags,
)
from trellis.feedback import (
    PackFeedback,
    reconcile_feedback_log_to_event_log,
    record_feedback,
)
from trellis.feedback.recording import _feedback_id_in_event_log

if TYPE_CHECKING:
    from pathlib import Path

    from tests.integration.loops.conftest import LoopEnvironment

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.live,
    pytest.mark.slow,
    pytest.mark.neo4j,
    pytest.mark.postgres,
]

_INTENT = "reconcile"  # single-token; see conftest module docstring


def _grade_file_only(
    api_url: str,
    *,
    log_dir: Path,
    distractor_id: str,
    helpful_ids: list[str],
    round_index: int,
) -> tuple[str, PackFeedback]:
    """One graded round that lands in the JSONL log and nowhere else.

    ``record_feedback`` is called without an ``event_log`` kwarg, which
    is the file-only capture path production takes when the sink is
    unavailable. The returned ``(pack_id, feedback)`` pair is what the
    reconciler needs to replay the row with its pack association.
    """
    pack, helpful_in_pack = build_pack_with_distractor(
        api_url,
        intent=_INTENT,
        distractor_id=distractor_id,
        helpful_ids=helpful_ids,
    )
    feedback = PackFeedback(
        run_id=f"reconcile-test-run-{round_index}",
        phase="execute",
        intent=_INTENT,
        outcome="success",
        items_served=sorted(item_ids(pack)),
        items_referenced=helpful_in_pack,
        # Explicit rejection, not inferred from the omission above:
        # ``to_event_payload`` deliberately refuses to read "served but
        # not referenced" as unhelpful, and since #380 the demotion gate
        # reads nothing else.
        unhelpful_item_ids=[distractor_id],
        intent_family="general_context",
    )
    result = record_feedback(feedback, log_dir=log_dir)
    assert result.event_log_emitted is False, result
    assert result.outcome_emitted is False, result
    assert result.log_path.exists()
    return pack["pack_id"], feedback


async def test_reconciliation_loop(loop_env: LoopEnvironment) -> None:
    """JSONL-only feedback → reconcile → drives apply-noise-tags downstream."""
    if not NEO4J_URI or not PG_DSN:  # paranoia — loop_env already gates this
        pytest.skip("live infra creds missing")

    distractor_id, helpful_ids = seed_distractor_corpus(
        loop_env.api_url, intent_token=_INTENT
    )
    log_dir = loop_env.data_dir / "feedback_log"
    graded: list[tuple[str, PackFeedback]] = [
        _grade_file_only(
            loop_env.api_url,
            log_dir=log_dir,
            distractor_id=distractor_id,
            helpful_ids=helpful_ids,
            round_index=i,
        )
        for i in range(DEMOTION_ROUNDS - 1)
    ]

    # The divergence pre-check and the backfill share one registry so
    # they cost a single connect/teardown cycle. Reusing the production
    # ``_feedback_id_in_event_log`` helper means the pre-check uses the
    # same scan limit + match logic the reconciler enforces internally.
    with live_registry(loop_env.config_dir, loop_env.data_dir) as registry:
        event_log = registry.operational.event_log
        for _, feedback in graded:
            assert not _feedback_id_in_event_log(event_log, feedback.feedback_id), (
                f"feedback_id {feedback.feedback_id} leaked into the "
                f"EventLog before reconciliation"
            )
        first = reconcile_feedback_log_to_event_log(
            log_dir,
            event_log,
            pack_id_lookup={fb.feedback_id: pack_id for pack_id, fb in graded},
        )
    assert first.scanned == DEMOTION_ROUNDS - 1, first
    assert first.emitted == DEMOTION_ROUNDS - 1, first
    assert first.already_present == 0, first
    assert first.failed == 0, first

    # The reconciled events reach the effectiveness join carrying their
    # per-item attribution — ``attributed_packs`` counts only packs whose
    # feedback cited something, so reading it back at exactly
    # ``DEMOTION_ROUNDS - 1`` is a measurement of the replayed payloads,
    # not a restatement of the round count. They are still one pack short
    # of the coverage floor, and the gate says so rather than demoting.
    withheld = trigger_apply_noise_tags(loop_env.api_url)
    assert withheld["status"] == "ok"
    assert_demotion_withheld_below_floor(
        withheld, attributed_packs=DEMOTION_ROUNDS - 1
    )

    # One more file-only round, then reconcile again: the new row emits
    # and the already-healed rows do not. Mixing both in one call is a
    # stronger idempotency proof than re-running an all-present scan,
    # which cannot distinguish "skipped correctly" from "skipped
    # everything".
    graded.append(
        _grade_file_only(
            loop_env.api_url,
            log_dir=log_dir,
            distractor_id=distractor_id,
            helpful_ids=helpful_ids,
            round_index=DEMOTION_ROUNDS - 1,
        )
    )
    lookup = {fb.feedback_id: pack_id for pack_id, fb in graded}
    with live_registry(loop_env.config_dir, loop_env.data_dir) as registry:
        event_log = registry.operational.event_log
        second = reconcile_feedback_log_to_event_log(
            log_dir, event_log, pack_id_lookup=lookup
        )
        third = reconcile_feedback_log_to_event_log(
            log_dir, event_log, pack_id_lookup=lookup
        )
    assert second.scanned == DEMOTION_ROUNDS, second
    assert second.emitted == 1, second
    assert second.already_present == DEMOTION_ROUNDS - 1, second
    assert second.failed == 0, second
    assert third.scanned == DEMOTION_ROUNDS, third
    assert third.emitted == 0, third
    assert third.already_present == DEMOTION_ROUNDS, third

    report = trigger_apply_noise_tags(loop_env.api_url)
    assert report["status"] == "ok"
    assert distractor_id in set(report["noise_candidates"]), (
        f"reconciled feedback should have driven distractor "
        f"{distractor_id!r} into the noise list: report={report}"
    )
    assert_demotion_admitted(report, item_id=distractor_id)

    final_pack = build_pack(loop_env.api_url, intent=_INTENT, tag_filters={})
    final_items = item_ids(final_pack)
    assert final_pack["pack_id"] != graded[-1][0], (
        "the closing pack must be a fresh assembly, not a cached re-issue"
    )
    assert distractor_id not in final_items, (
        f"reconciliation→apply-noise-tags loop didn't close — distractor "
        f"{distractor_id!r} still served: {sorted(final_items)}"
    )
    assert final_items, (
        "the loop demoted the whole corpus — the helpful docs must survive, "
        "or 'excluded the distractor' is indistinguishable from 'excluded "
        "everything'"
    )
