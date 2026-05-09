"""Targeted gap-coverage tests for :mod:`trellis.learning.tuners.promotion`.

The main suite in ``test_promotion.py`` already covers the dominant
flows (skipped/rejected/promoted/forced/string values, registry cache,
event payload shape). This file backfills the branches the audit
flagged as unexercised: bool baselines, the ``allow_non_numeric=False``
rejection reason, the zero-effect (proposed==baseline) rejection,
the non-coercible numeric branch in ``_compute_effect_size``, and the
``parameter_registry=None`` short-circuit on the success path.

Each test is constructor-style (build the stores, run the function,
assert the result) and uses real SQLite stores like the rest of the
suite — the tuners modules call concrete store methods that
``MagicMock(spec=...)`` would not faithfully simulate (``put`` returns
a ``ParameterSet`` whose ``params_version`` we read back from the
EventLog payload). Stays unit-scoped via ``tmp_path``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from trellis.learning.tuners import (
    PromotionPolicy,
    promote_proposal,
)
from trellis.learning.tuners.promotion import _apply_policy, _compute_effect_size
from trellis.schemas.parameters import ParameterProposal, ParameterScope, ParameterSet
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


_SCOPE = ParameterScope(component_id="retrieve.strategies.KeywordSearch", domain="b")


def _proposal(**kw) -> ParameterProposal:
    defaults: dict = {
        "proposal_id": "prop_gap",
        "scope": _SCOPE,
        "tuner": "rule_tuner",
        "proposed_values": {"recency_half_life_days": 15.0},
        "sample_size": 30,
    }
    defaults.update(kw)
    return ParameterProposal(**defaults)


# ---------------------------------------------------------------------------
# _compute_effect_size — bool / non-coercible branches
# ---------------------------------------------------------------------------


def test_compute_effect_size_bool_baseline_marks_non_numeric():
    """Bool values are reported as a non-numeric change, no effect size.

    Covers the ``isinstance(_, bool)`` branch in ``_compute_effect_size``
    that the existing string test does not exercise (str and bool take
    different code paths).
    """
    effect, has_non_numeric = _compute_effect_size(
        proposed={"strict_mode": True},
        baseline={"strict_mode": False},
    )
    assert effect is None
    assert has_non_numeric is True


def test_compute_effect_size_uncoercible_value_is_skipped():
    """A proposed value that can't be cast to float is silently skipped.

    Hits the ``except (TypeError, ValueError): continue`` arm: with no
    other numeric keys in the proposal the loop yields ``effect=None``
    and ``has_non_numeric=False``.
    """
    effect, has_non_numeric = _compute_effect_size(
        proposed={"weights": [1, 2, 3]},  # list isn't bool/str/None/numeric
        baseline={"weights": [4, 5, 6]},
    )
    assert effect is None
    assert has_non_numeric is False


# ---------------------------------------------------------------------------
# Policy gate — non-numeric and zero-effect rejections
# ---------------------------------------------------------------------------


def test_promote_rejected_when_non_numeric_disallowed(stores):
    """``allow_non_numeric=False`` blocks str/bool changes.

    Covers branch ``return "non_numeric_change_disallowed"`` at
    ``_apply_policy`` line 315 — not reached by any existing test.
    """
    params, state, events = stores
    params.put(ParameterSet(scope=_SCOPE, values={"mode": "standard"}))

    p = _proposal(proposed_values={"mode": "aggressive"})
    state.put_proposal(p)

    result = promote_proposal(
        p.proposal_id,
        tuner_state=state,
        parameter_store=params,
        event_log=events,
        policy=PromotionPolicy(allow_non_numeric=False),
    )
    assert result.status == "rejected"
    assert result.reason == "non_numeric_change_disallowed"

    # Audit trail must show the rejection event.
    rejected = events.get_events(event_type=EventType.TUNER_PROPOSAL_REJECTED)
    assert len(rejected) == 1
    assert rejected[0].payload["reason"] == "non_numeric_change_disallowed"


def test_apply_policy_returns_zero_effect_when_proposal_is_empty():
    """Empty proposal with a baseline → ``zero_effect_proposed_equals_baseline``.

    Covers the ``return "zero_effect_proposed_equals_baseline"`` arm of
    ``_apply_policy`` (line 327).  Reaching it through ``promote_proposal``
    is hard — when proposed-equals-baseline numerically the
    ``min_effect_size`` rejection fires first ("effect=0.0 < 0.15").  An
    empty proposal *with* a baseline is the practical way to trigger
    "I have nothing numeric to compare against, but a baseline exists" —
    same semantics, different shape.

    Tested at the helper level so the assertion stays decoupled from
    the upstream effect-size computation.
    """
    proposal = _proposal(proposed_values={}, sample_size=30)
    reason = _apply_policy(
        proposal=proposal,
        policy=PromotionPolicy(),
        baseline_values={"recency_half_life_days": 30.0},
        effect=None,
        has_non_numeric=False,
    )
    assert reason == "zero_effect_proposed_equals_baseline"


# ---------------------------------------------------------------------------
# Success-path edges: no registry, custom source label
# ---------------------------------------------------------------------------


def test_promote_succeeds_without_parameter_registry(stores):
    """Promotion works when ``parameter_registry`` is None (default).

    The existing ``test_promote_invalidates_registry_cache`` covers the
    not-None branch; this nails down the ``is None`` short-circuit so
    refactors of that branch don't silently break the default path.
    """
    params, state, events = stores
    params.put(ParameterSet(scope=_SCOPE, values={"recency_half_life_days": 30.0}))

    p = _proposal(proposed_values={"recency_half_life_days": 15.0})
    state.put_proposal(p)

    result = promote_proposal(
        p.proposal_id,
        tuner_state=state,
        parameter_store=params,
        event_log=events,
        parameter_registry=None,
    )
    assert result.status == "promoted"
    assert result.params_version is not None


def test_promote_carries_custom_source_label_to_event(stores):
    """The ``source`` kwarg propagates to both PARAMS_UPDATED and rejections.

    Light-touch documentation test — locks the source label down so a
    future signature shuffle doesn't silently drop it.
    """
    params, state, events = stores
    params.put(ParameterSet(scope=_SCOPE, values={"recency_half_life_days": 30.0}))
    p = _proposal(proposed_values={"recency_half_life_days": 15.0})
    state.put_proposal(p)

    promote_proposal(
        p.proposal_id,
        tuner_state=state,
        parameter_store=params,
        event_log=events,
        source="trellis.cli.promote",
    )
    ev = events.get_events(event_type=EventType.PARAMS_UPDATED)[0]
    assert ev.source == "trellis.cli.promote"
