"""Reconciliation loop — JSONL-only feedback heals into the EventLog.

Proves the divergence-recovery primitive ``trellis.feedback.recording.
reconcile_feedback_log_to_event_log`` works end-to-end against the
live infra: feedback that landed only in ``pack_feedback.jsonl``
(file-only capture, sink unavailable, process crash between writes,
etc.) can be backfilled into the authoritative EventLog and then
drives the same downstream analytics a live emission would have:

    seed corpus → pack 1 (distractor included) →
    write JSONL feedback ONLY (no ``event_log`` kwarg) →
    confirm EventLog has no matching ``FEEDBACK_RECORDED`` →
    reconcile_feedback_log_to_event_log →
    EventLog now has the events; rerun reconcile is a no-op (idempotency) →
    apply-noise-tags via REST → distractor tagged →
    pack 2 (distractor excluded by signal_quality filter)

The test process writes JSONL via ``record_feedback`` from
``trellis.feedback`` directly. That's the same code path production
uses for file-only capture; the test deliberately skips the MCP
``record_feedback`` tool here so the EventLog stays empty for our
pack until ``reconcile_*`` runs. The reconciler itself opens its own
``StoreRegistry`` (scoped DSN env vars) so it reads / writes the
same Postgres EventLog the spawned uvicorn uses.

Skipped when ``TRELLIS_TEST_NEO4J_URI`` or ``TRELLIS_TEST_PG_DSN``
is unset — same gating as the rest of the loop suite.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import TYPE_CHECKING

import pytest

from tests.integration._live_server import NEO4J_URI, PG_DSN
from tests.integration.loops.conftest import (
    build_pack,
    seed_distractor_corpus,
    trigger_apply_noise_tags,
)
from trellis.feedback import (
    PackFeedback,
    reconcile_feedback_log_to_event_log,
    record_feedback,
)
from trellis.feedback.recording import _feedback_id_in_event_log
from trellis.stores.registry import StoreRegistry

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from tests.integration.loops.conftest import LoopEnvironment

pytestmark = pytest.mark.asyncio

# Single-token marker matches the noise-demote loop's caveat: Postgres
# FTS tokenizes multi-word intents to terms that have to all be
# present in document content, which would empty the result set.
_INTENT = "reconcile"
_PACK_FETCH_LIMIT = 10
_PACK_TOKEN_BUDGET = 2000


@contextmanager
def _live_registry(config_dir: Path, data_dir: Path) -> Iterator[StoreRegistry]:
    """Yield a ``StoreRegistry`` pointing at the loop-test backends.

    Mirrors the env-var dance ``wipe_live_state_for_config`` performs:
    plane-aware DSN resolution reads ``TRELLIS_KNOWLEDGE_PG_DSN`` /
    ``TRELLIS_OPERATIONAL_PG_DSN`` from the process env, so we set
    them just for the registry's lifetime and restore on exit. The
    Neo4j credentials live in the cloud-config YAML, so they don't
    need an env-var bridge.
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


async def test_reconciliation_loop(loop_env: LoopEnvironment) -> None:
    """JSONL-only feedback → reconcile → drives apply-noise-tags downstream."""
    if not NEO4J_URI or not PG_DSN:  # paranoia — loop_env already gates this
        pytest.skip("live infra creds missing")

    distractor_id, helpful_ids = seed_distractor_corpus(
        loop_env.api_url, intent_token=_INTENT
    )
    pack_1 = build_pack(
        loop_env.api_url,
        intent=_INTENT,
        max_items=_PACK_FETCH_LIMIT,
        max_tokens=_PACK_TOKEN_BUDGET,
        tag_filters={},
    )
    pack_1_set = {item["item_id"] for item in pack_1["items"]}
    helpful_in_pack = sorted(pack_1_set.intersection(helpful_ids))
    assert distractor_id in pack_1_set, (
        f"distractor must be in pack 1 to drive the noise tag downstream; "
        f"got items={sorted(pack_1_set)}"
    )
    assert helpful_in_pack, (
        "expected at least one helpful doc — without one, the JSONL "
        "feedback carries no positive label and analyze_effectiveness "
        "won't have a contrastive signal"
    )

    log_dir = loop_env.data_dir / "feedback_log"
    feedback = PackFeedback(
        run_id="reconcile-test-run",
        phase="execute",
        intent=_INTENT,
        outcome="success",
        items_served=sorted(pack_1_set),
        items_referenced=helpful_in_pack,  # distractor deliberately omitted
        intent_family="general_context",
    )
    record_result = record_feedback(feedback, log_dir=log_dir)
    assert record_result.event_log_emitted is False, record_result
    assert record_result.outcome_emitted is False, record_result
    assert record_result.log_path.exists()

    # All three EventLog touches share one registry so the divergence
    # pre-check, the backfill, and the idempotency rerun cost a single
    # connect/teardown cycle. Reusing the production
    # ``_feedback_id_in_event_log`` helper means the pre-check uses the
    # same scan limit + match logic the reconciler enforces internally.
    pack_id = pack_1["pack_id"]
    pack_id_lookup = {feedback.feedback_id: pack_id}
    with _live_registry(loop_env.config_dir, loop_env.data_dir) as registry:
        event_log = registry.operational.event_log
        assert not _feedback_id_in_event_log(event_log, feedback.feedback_id), (
            "feedback_id leaked into EventLog before reconciliation"
        )
        first = reconcile_feedback_log_to_event_log(
            log_dir,
            event_log,
            pack_id_lookup=pack_id_lookup,
        )
        second = reconcile_feedback_log_to_event_log(
            log_dir,
            event_log,
            pack_id_lookup=pack_id_lookup,
        )
    assert first.scanned == 1, first
    assert first.emitted == 1, first
    assert first.already_present == 0, first
    assert first.failed == 0, first
    assert second.scanned == 1, second
    assert second.emitted == 0, second
    assert second.already_present == 1, second

    # The reconciled FEEDBACK_RECORDED must look indistinguishable from
    # a live emission to ``analyze_effectiveness``: distractor was
    # served but never referenced, so usage-rate flagging picks it as
    # noise.
    report = trigger_apply_noise_tags(loop_env.api_url)
    assert report["status"] == "ok"
    noise_ids = set(report.get("noise_candidates", []))
    assert distractor_id in noise_ids, (
        f"reconciled feedback should have driven distractor "
        f"{distractor_id!r} into the noise list: report={report}"
    )

    pack_2 = build_pack(
        loop_env.api_url,
        intent=_INTENT,
        max_items=_PACK_FETCH_LIMIT,
        max_tokens=_PACK_TOKEN_BUDGET,
        tag_filters={},
    )
    pack_2_items = {item["item_id"] for item in pack_2["items"]}
    assert pack_2["pack_id"] != pack_1["pack_id"], (
        "second pack must be a fresh assembly, not a cached re-issue"
    )
    assert distractor_id not in pack_2_items, (
        f"reconciliation→apply-noise-tags loop didn't close — distractor "
        f"{distractor_id!r} still in pack 2: {sorted(pack_2_items)}"
    )
