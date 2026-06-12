"""Tests for ``preview_promotion`` and ``reject_proposal`` (WP10).

``preview_promotion`` forecasts the promotion decision without mutating
or emitting; ``reject_proposal`` is the human-gated manual rejection path
the Review-queue UI calls. Both are factored out of / added to the
promotion module so the API and CLI share one decision path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from trellis.learning.tuners import (
    PromotionPreview,
    PromotionResult,
    preview_promotion,
    reject_proposal,
)
from trellis.schemas.parameters import (
    ParameterProposal,
    ParameterScope,
    ParameterSet,
)
from trellis.stores.base.event_log import EventType
from trellis.stores.sqlite.event_log import SQLiteEventLog
from trellis.stores.sqlite.parameter import SQLiteParameterStore
from trellis.stores.sqlite.tuner_state import SQLiteTunerStateStore


@pytest.fixture
def stores(tmp_path: Path):
    params = SQLiteParameterStore(tmp_path / "parameters.db")
    state = SQLiteTunerStateStore(tmp_path / "tuner_state.db")
    events = SQLiteEventLog(tmp_path / "events.db")
    try:
        yield params, state, events
    finally:
        params.close()
        state.close()
        events.close()


def _proposal(**kw) -> ParameterProposal:
    defaults: dict = {
        "proposal_id": "prop_test",
        "scope": ParameterScope(component_id="retrieve.packer", domain="a"),
        "tuner": "rule_tuner",
        "proposed_values": {"max_items": 20.0},
        "sample_size": 30,
    }
    defaults.update(kw)
    return ParameterProposal(**defaults)


# -- preview_promotion ------------------------------------------------------


def test_preview_missing_proposal(stores):
    params, state, events = stores
    pv = preview_promotion(
        "nope", tuner_state=state, parameter_store=params
    )
    assert isinstance(pv, PromotionPreview)
    assert pv.status == "skipped"
    assert pv.reason == "proposal_not_found"
    # Pure read — nothing emitted.
    assert events.count() == 0


def test_preview_predicts_promote(stores):
    params, state, events = stores
    params.put(ParameterSet(scope=_proposal().scope, values={"max_items": 10.0}))
    p = _proposal()
    state.put_proposal(p)
    pv = preview_promotion(
        p.proposal_id, tuner_state=state, parameter_store=params
    )
    assert pv.status == "promoted"
    assert pv.reason == "ok"
    assert pv.proposed_values == {"max_items": 20.0}
    assert pv.baseline_values == {"max_items": 10.0}
    # Forecast must not have mutated the proposal or emitted anything.
    assert state.get_proposal(p.proposal_id).status == "pending"
    assert events.count() == 0


def test_preview_predicts_reject(stores):
    params, state, _ = stores
    params.put(ParameterSet(scope=_proposal().scope, values={"max_items": 10.0}))
    p = _proposal(sample_size=1)
    state.put_proposal(p)
    pv = preview_promotion(
        p.proposal_id, tuner_state=state, parameter_store=params
    )
    assert pv.status == "rejected"
    assert "sample_size" in pv.reason


def test_preview_matches_promote_outcome(stores):
    """The predicted status equals what promote_proposal then produces."""
    from trellis.learning.tuners import promote_proposal

    params, state, events = stores
    params.put(ParameterSet(scope=_proposal().scope, values={"max_items": 10.0}))
    p = _proposal()
    state.put_proposal(p)
    pv = preview_promotion(
        p.proposal_id, tuner_state=state, parameter_store=params
    )
    result = promote_proposal(
        p.proposal_id,
        tuner_state=state,
        parameter_store=params,
        event_log=events,
    )
    assert pv.status == result.status == "promoted"


# -- reject_proposal --------------------------------------------------------


def test_reject_missing_proposal(stores):
    _, state, events = stores
    r = reject_proposal("nope", tuner_state=state, event_log=events)
    assert isinstance(r, PromotionResult)
    assert r.status == "skipped"
    assert events.count() == 0


def test_reject_persists_and_emits(stores):
    _, state, events = stores
    p = _proposal()
    state.put_proposal(p)
    r = reject_proposal(
        p.proposal_id,
        tuner_state=state,
        event_log=events,
        reason="reviewer says no",
    )
    assert r.status == "rejected"
    assert state.get_proposal(p.proposal_id).status == "rejected"
    emitted = events.get_events(
        event_type=EventType.TUNER_PROPOSAL_REJECTED, limit=10
    )
    assert len(emitted) == 1
    assert emitted[0].payload["reason"] == "reviewer says no"
    assert emitted[0].payload["manual"] is True


def test_reject_terminal_proposal_is_skipped(stores):
    _, state, events = stores
    p = _proposal()
    state.put_proposal(p)
    state.update_status(p.proposal_id, "promoted")
    r = reject_proposal(p.proposal_id, tuner_state=state, event_log=events)
    assert r.status == "skipped"
    assert r.reason == "proposal_already_promoted"
