"""Curate cycle loop — seeded events → one ``worker curate`` cycle → artifacts.

Proves the Tier-2 curation cycle (``trellis worker curate``) closes
end-to-end against real (SQLite) stores. Unlike the live loops in this
directory, this test does NOT need Neo4j / Postgres: it seeds the
EventLog directly, invokes :func:`trellis_cli.worker.run_curation_cycle`
as a function against tmp_path SQLite stores, and asserts the cycle
produced:

* noise tags on a consistently-unhelpful item (demote half),
* advisory store updates (advisory generation + fitness),
* learning-candidate review artifacts on disk (promote half — surface
  only; promotion stays human-gated via ``trellis curate
  promote-learning``).

This is the in-process counterpart to the live ``noise_demote`` and
``learning_promote`` loops: same authoritative EventLog signal, no live
infra, so it runs in the default unit-equivalent selection.
"""

from __future__ import annotations

import json
from pathlib import Path

from trellis.learning import (
    LEARNING_NOISE_RETRY_KEY,
    LEARNING_NOISE_SUCCESS_KEY,
    LEARNING_PROMOTE_RETRY_KEY,
    LEARNING_PROMOTE_SUCCESS_KEY,
    LEARNING_SCORING_COMPONENT,
)
from trellis.ops import ParameterRegistry
from trellis.schemas.parameters import ParameterScope, ParameterSet
from trellis.stores.advisory_store import AdvisoryStore
from trellis.stores.base.event_log import EventType
from trellis.stores.sqlite.document import SQLiteDocumentStore
from trellis.stores.sqlite.event_log import SQLiteEventLog
from trellis.stores.sqlite.parameter import SQLiteParameterStore
from trellis_cli.worker import run_curation_cycle

_HELPFUL = "cc:doc:helpful"
_NOISY = "cc:doc:noisy"
_INTENT_FAMILY = "curate_cycle"


def _seed_events(event_log: SQLiteEventLog, *, rounds: int = 4) -> None:
    """Seed graded packs.

    The helpful doc is served + referenced + successful every round (a
    promote candidate). The noisy doc is served every round but never
    referenced and the pack fails (a noise candidate for the demote half).
    """
    for i in range(rounds):
        pack_id = f"cc-pack-{i}"
        success = True
        event_log.emit(
            EventType.PACK_ASSEMBLED,
            source="test",
            entity_id=pack_id,
            entity_type="pack",
            payload={
                "intent": "curate cycle probe",
                "intent_family": _INTENT_FAMILY,
                "domain": "cc-test",
                "injected_item_ids": [_HELPFUL, _NOISY],
                "injected_items": [
                    {
                        "item_id": _HELPFUL,
                        "item_type": "document",
                        "rank": 0,
                        "strategy_source": "document",
                    },
                    {
                        "item_id": _NOISY,
                        "item_type": "document",
                        "rank": 1,
                        "strategy_source": "document",
                    },
                ],
            },
        )
        event_log.emit(
            EventType.FEEDBACK_RECORDED,
            source="test",
            entity_id=pack_id,
            entity_type="pack",
            payload={
                "pack_id": pack_id,
                "run_id": f"run-{i}",
                "intent_family": _INTENT_FAMILY,
                "outcome": "success" if success else "failure",
                "success": success,
                # Helpful doc referenced; noisy doc never referenced => the
                # usage-first noise path flags the noisy doc.
                "helpful_item_ids": [_HELPFUL],
            },
        )


def test_worker_curate_cycle_end_to_end(tmp_path: Path) -> None:
    event_log = SQLiteEventLog(tmp_path / "events.db")
    document_store = SQLiteDocumentStore(tmp_path / "docs.db")
    parameter_store = SQLiteParameterStore(tmp_path / "params.db")
    advisory_store = AdvisoryStore(tmp_path / "advisories.json")
    output_dir = tmp_path / "review"

    try:
        # Seed the learning.scoring parameters the promote-half scorer
        # requires (mirrors ``trellis admin init-learning-params``).
        parameter_store.put(
            ParameterSet(
                scope=ParameterScope(component_id=LEARNING_SCORING_COMPONENT),
                values={
                    LEARNING_PROMOTE_SUCCESS_KEY: 0.75,
                    LEARNING_PROMOTE_RETRY_KEY: 0.25,
                    LEARNING_NOISE_SUCCESS_KEY: 0.4,
                    LEARNING_NOISE_RETRY_KEY: 0.5,
                },
                source="test",
                notes="curate-cycle integration seed",
            )
        )

        # Seed the noisy doc so apply_noise_tags has something to tag.
        document_store.put(_NOISY, "noisy content nobody uses", {})
        _seed_events(event_log)

        result = run_curation_cycle(
            event_log=event_log,
            document_store=document_store,
            advisory_store=advisory_store,
            learning_registry=ParameterRegistry(parameter_store),
            output_dir=output_dir,
            days=30,
            no_meta_trace=True,
        )

        # --- Demote half: noise tag applied to the unreferenced doc. ---
        assert result.noise_tagged >= 1
        noisy_doc = document_store.get(_NOISY)
        assert noisy_doc is not None
        assert noisy_doc["metadata"]["content_tags"]["signal_quality"] == "noise"

        # --- Advisory half ran (generation + fitness, no crash). ---
        assert "advisories" not in result.skipped_stages

        # --- Promote half: review artifacts written to output_dir. ---
        assert result.candidates_path is not None
        candidates_path = Path(result.candidates_path)
        decisions_path = Path(result.decisions_path)
        assert candidates_path.exists()
        assert decisions_path.exists()

        report = json.loads(candidates_path.read_text(encoding="utf-8"))
        assert report["observation_count"] >= 4
        # The helpful doc should surface as a promote candidate.
        item_ids = {c["item_id"] for c in report["candidates"]}
        assert _HELPFUL in item_ids

        decisions = json.loads(decisions_path.read_text(encoding="utf-8"))
        # All review rows default to not-approved — promotion is human-gated.
        assert all(d["approved"] is False for d in decisions["decisions"])
    finally:
        event_log.close()
        document_store.close()
        parameter_store.close()
