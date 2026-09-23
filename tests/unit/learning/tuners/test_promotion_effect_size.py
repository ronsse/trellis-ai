"""The effect-size gate separates "small change" from "nothing to compare".

Three defects shared one cause: ``_compute_effect_size`` encoded "this key
has no baseline" as ``float("inf")`` and put it in the same ``float`` slot
as a measured relative delta.

1. ``inf`` is not representable in the audit log. Event payloads are
   ``json.dumps``'d into a ``JSONB`` column and Postgres rejects the
   resulting ``Infinity`` literal. Because the store write and the
   tuner-state update both precede the emit, such a promotion applied the
   parameter, recorded ``status="promoted"``, raised at the caller, and
   wrote no ``parameters.updated`` row. *Every first promotion for a
   scope* has that shape.
2. ``inf`` was assigned rather than maxed, so one unbaselined key
   laundered every other key in the proposal past ``min_effect_size``.
3. The guard meant to contain it (``effect != float("inf") and effect <
   min``) was dead code — ``inf < x`` is already ``False``.

The Postgres half is pinned here with ``json.dumps(..., allow_nan=False)``
rather than a live server: ``allow_nan=False`` raises on exactly the
values that make ``JSONB`` reject the document, so the property is checked
on every PR instead of only on the ``live-infra`` legs. Reproduced against
a real ``postgres:16`` before it was written this way — the raise is
``InvalidTextRepresentation: invalid input syntax for type json``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trellis.learning.tuners import PromotionPolicy, promote_proposal
from trellis.learning.tuners.promotion import _apply_policy, _compute_effect_size
from trellis.schemas.parameters import ParameterProposal, ParameterScope, ParameterSet
from trellis.stores.base.event_log import EventType
from trellis.stores.sqlite.event_log import SQLiteEventLog
from trellis.stores.sqlite.parameter import SQLiteParameterStore
from trellis.stores.sqlite.tuner_state import SQLiteTunerStateStore

SCOPE = ParameterScope(component_id="retrieve.strategies.KeywordSearch", domain="a")


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
        "scope": SCOPE,
        "tuner": "rule_tuner",
        "proposed_values": {"recency_half_life_days": 15.0},
        "sample_size": 30,
    }
    defaults.update(kw)
    return ParameterProposal(**defaults)


# ---------------------------------------------------------------------------
# The encoding: a key with no baseline is a key, not a magnitude
# ---------------------------------------------------------------------------


def test_unbaselined_key_is_named_not_scored():
    effect = _compute_effect_size(proposed={"brand_new": 4.0}, baseline={"other": 1.0})
    assert effect.comparable_max is None
    assert effect.unbaselined_keys == ("brand_new",)


def test_unbaselined_key_does_not_raise_the_comparable_max():
    """The laundering mechanism, at the computation.

    ``mature`` moves 0.4 %. Before the fix ``brand_new`` assigned
    ``inf`` to the same slot and the 0.4 % became unreadable.
    """
    effect = _compute_effect_size(
        proposed={"mature": 100.4, "brand_new": 4.0},
        baseline={"mature": 100.0},
    )
    assert effect.comparable_max == pytest.approx(0.004)
    assert effect.unbaselined_keys == ("brand_new",)


@pytest.mark.parametrize(
    ("proposed", "baseline"),
    [
        ({"a": 1.0}, None),
        ({"a": 1.0}, {}),
        ({"a": 1.0}, {"b": 2.0}),
        ({"a": 1.0, "b": 2.0}, {"b": 2.0}),
    ],
)
def test_comparable_max_is_never_infinite(proposed, baseline):
    """The property the audit log depends on, over every no-baseline shape."""
    effect = _compute_effect_size(proposed=proposed, baseline=baseline)
    assert effect.comparable_max != float("inf")
    assert json.dumps({"effect_size": effect.comparable_max}, allow_nan=False)


# ---------------------------------------------------------------------------
# The gate: the floor applies to what it can measure, and says what it can't
# ---------------------------------------------------------------------------


def test_small_move_beside_a_new_key_is_rejected_and_names_both():
    """#617's example: 0.4 % on a mature key no longer rides in free."""
    reason = _apply_policy(
        proposal=_proposal(proposed_values={"mature": 100.4, "brand_new": 4.0}),
        policy=PromotionPolicy(),
        baseline_values={"mature": 100.0},
        effect=_compute_effect_size(
            {"mature": 100.4, "brand_new": 4.0}, {"mature": 100.0}
        ),
    )
    assert reason is not None
    assert "effect_size=0.0040" in reason
    assert "uncomparable keys: brand_new" in reason


def test_every_key_new_is_a_bootstrap_not_a_no_op():
    """A snapshot exists but carries none of the proposed keys.

    Must not reuse ``zero_effect_proposed_equals_baseline``, which would
    describe a wholly new proposal as a no-op.
    """
    effect = _compute_effect_size({"brand_new": 4.0}, {"other": 1.0})
    assert (
        _apply_policy(
            proposal=_proposal(proposed_values={"brand_new": 4.0}),
            policy=PromotionPolicy(),
            baseline_values={"other": 1.0},
            effect=effect,
        )
        is None
    )
    strict = _apply_policy(
        proposal=_proposal(proposed_values={"brand_new": 4.0}),
        policy=PromotionPolicy(allow_no_baseline=False),
        baseline_values={"other": 1.0},
        effect=effect,
    )
    assert strict == "no_baseline_for_proposed_keys=brand_new"
    assert strict != "zero_effect_proposed_equals_baseline"


def test_exact_match_still_reads_as_a_no_op():
    """The arm the bootstrap case must not steal.

    Reachable only for a *non-numeric* key equal to its baseline. A
    numeric exact match is comparable — its delta is ``0.0``, not
    ``None`` — so it is rejected by the floor instead, as it was
    before this change (pinned below so the two are not confused).
    """
    assert (
        _apply_policy(
            proposal=_proposal(proposed_values={"mode": "hybrid"}),
            policy=PromotionPolicy(),
            baseline_values={"mode": "hybrid"},
            effect=_compute_effect_size({"mode": "hybrid"}, {"mode": "hybrid"}),
        )
        == "zero_effect_proposed_equals_baseline"
    )


def test_numeric_exact_match_is_comparable_zero_not_uncomparable():
    """Zero is a measurement. It must not be routed to a bootstrap arm."""
    effect = _compute_effect_size({"mature": 100.0}, {"mature": 100.0})
    assert effect.comparable_max == 0.0
    assert effect.unbaselined_keys == ()
    reason = _apply_policy(
        proposal=_proposal(proposed_values={"mature": 100.0}),
        policy=PromotionPolicy(),
        baseline_values={"mature": 100.0},
        effect=effect,
    )
    assert reason == "effect_size=0.0000 < min_effect_size=0.15"
    assert "uncomparable keys" not in reason


def test_a_real_move_beside_a_new_key_still_promotes():
    """The fix tightens the floor; it does not close the gate."""
    assert (
        _apply_policy(
            proposal=_proposal(proposed_values={"mature": 150.0, "brand_new": 4.0}),
            policy=PromotionPolicy(),
            baseline_values={"mature": 100.0},
            effect=_compute_effect_size(
                {"mature": 150.0, "brand_new": 4.0}, {"mature": 100.0}
            ),
        )
        is None
    )


# ---------------------------------------------------------------------------
# The audit log: every emitted payload survives a strict JSON round trip
# ---------------------------------------------------------------------------


def test_first_promotion_for_a_scope_emits_a_storable_payload(stores):
    """The shape that lost its audit row on Postgres.

    No baseline at all, so every proposed key is unbaselined — before the
    fix this emitted ``effect_size: Infinity`` and the insert failed
    *after* the parameter had already been written.
    """
    params, state, events = stores
    state.put_proposal(_proposal(proposal_id="prop_first"))

    result = promote_proposal(
        proposal_id="prop_first",
        parameter_store=params,
        tuner_state=state,
        event_log=events,
    )

    assert result.status == "promoted"
    emitted = events.get_events(event_type=EventType.PARAMS_UPDATED)
    assert len(emitted) == 1
    payload = emitted[0].payload

    # First, and on its own line: this is the assertion the production
    # failure maps onto. Anything asserted before it can shadow it, and a
    # guard that cannot be watched to fire is not a guard.
    json.dumps(payload, allow_nan=False)

    assert payload["effect_size"] is None
    assert payload["uncomparable_keys"] == ["recency_half_life_days"]
    assert result.effect_size is None  # nothing was comparable — not `inf`


def test_rejection_payload_is_storable_and_names_what_it_could_not_weigh(stores):
    params, state, events = stores
    params.put(ParameterSet(scope=SCOPE, values={"mature": 100.0}, source="test"))
    state.put_proposal(
        _proposal(
            proposal_id="prop_small",
            proposed_values={"mature": 100.4, "brand_new": 4.0},
        )
    )

    result = promote_proposal(
        proposal_id="prop_small",
        parameter_store=params,
        tuner_state=state,
        event_log=events,
    )

    assert result.status == "rejected"
    emitted = events.get_events(event_type=EventType.TUNER_PROPOSAL_REJECTED)
    assert len(emitted) == 1
    payload = emitted[0].payload

    json.dumps(payload, allow_nan=False)

    assert payload["uncomparable_keys"] == ["brand_new"]
    assert result.effect_size == pytest.approx(0.004)
