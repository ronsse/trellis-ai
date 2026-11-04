"""``active_variants`` stamps the holdout arm and takes no draw of its own."""

from __future__ import annotations

import pytest

from trellis.core.pack_holdout import holdout_draw, is_held_out
from trellis.core.variants import (
    HOLDOUT_ARM_HELD_OUT,
    HOLDOUT_ARM_SERVED,
    PACK_HOLDOUT_EXPERIMENT,
    VARIANTS_KEY,
    active_variants,
)


def test_the_key_and_experiment_names_are_the_documented_spelling() -> None:
    assert VARIANTS_KEY == "variants"
    assert PACK_HOLDOUT_EXPERIMENT == "pack_holdout"
    assert (HOLDOUT_ARM_HELD_OUT, HOLDOUT_ARM_SERVED) == ("held_out", "served")


@pytest.mark.parametrize("held_out", [False, True])
def test_a_zero_rate_lists_no_experiment(held_out: bool) -> None:
    # ``held_out=True`` at rate 0 cannot happen; the rate still decides.
    assert active_variants(holdout_rate=0.0, held_out=held_out) == {}


@pytest.mark.parametrize(("held_out", "arm"), [(True, "held_out"), (False, "served")])
@pytest.mark.parametrize("rate", [0.01, 0.5, 1.0])
def test_a_positive_rate_names_the_arm(rate: float, held_out: bool, arm: str) -> None:
    assert active_variants(holdout_rate=rate, held_out=held_out) == {
        "pack_holdout": arm
    }


def test_each_call_returns_its_own_mapping() -> None:
    first = active_variants(holdout_rate=0.5, held_out=False)
    first["mutated"] = "x"
    assert active_variants(holdout_rate=0.5, held_out=False) == {
        "pack_holdout": "served"
    }


class TestHoldoutDrawIsUnchanged:
    """Pinned values: the draw is a recorded assignment, so it must not move.

    Every ``PACK_ASSEMBLED`` row already written was assigned by this function;
    a change to it re-randomises history. These literals were computed from
    ``sha256(b"trellis/pack-holdout/v1:" + pack_id)`` before ``variants``
    existed.
    """

    def test_the_draw_for_fixed_pack_ids(self) -> None:
        assert holdout_draw("pack-fixed-0001") == 0.027614797282425996
        assert holdout_draw("01HZZZZZZZZZZZZZZZZZZZZZZZ") == 0.3833878470750097
        assert holdout_draw("pack-fixed-0002") == 0.5882332463840987

    def test_the_arm_for_fixed_pack_ids_at_a_fixed_rate(self) -> None:
        assert is_held_out("pack-fixed-0001", 0.5) is True
        assert is_held_out("pack-fixed-0002", 0.5) is False
        assert is_held_out("pack-fixed-0001", 0.0) is False

    def test_stamping_an_arm_does_not_move_the_draw(self) -> None:
        before = holdout_draw("pack-fixed-0001")
        active_variants(holdout_rate=0.5, held_out=is_held_out("pack-fixed-0001", 0.5))
        assert holdout_draw("pack-fixed-0001") == before
