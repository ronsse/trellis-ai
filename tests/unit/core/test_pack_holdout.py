"""The pack holdout assignment and the reader-side helpers.

The assignment is a pure function of ``(pack_id, rate)``: a stable hash of
the pack id mapped to ``[0, 1)``, withheld when it falls below the rate.
It must give the same arm in every process and on every Python version,
which is why it is SHA-256 and never :func:`hash` (salted per process by
``PYTHONHASHSEED``). See :mod:`trellis.core.pack_holdout`.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.integration._live_server import repo_root, repo_src_pythonpath
from trellis.core import pack_holdout
from trellis.core.pack_holdout import (
    drop_holdout,
    holdout_draw,
    is_held_out,
    is_holdout,
)
from trellis.stores.base.event_log import Event, EventType

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

#: Two-sided 99.9% normal quantile, for the binomial bound.
_Z_999 = 3.2905


def _synthetic_pack_id(i: int) -> str:
    """A ULID-shaped id: 26 Crockford characters, distinct for each ``i``.

    A golden-ratio stride spreads consecutive ``i`` over the whole 80-bit
    tail, so the population is neither sequential nor random — the share
    tests below are deterministic and cannot flake.
    """
    value = (i * 0x9E3779B97F4A7C15 + 0x1234567) % (32**16)
    tail = ""
    for _ in range(16):
        tail = _CROCKFORD[value % 32] + tail
        value //= 32
    return "01K7PACKTE" + tail


PACK_IDS = [_synthetic_pack_id(i) for i in range(4000)]

#: Draws pinned from the shipped function. A change to the hash, its
#: domain prefix or the bit extraction moves every one of them, and would
#: silently re-randomise every pack already recorded under the old rule.
GOLDEN_DRAWS = {
    "01K7PACKTE0000000000000000": 0.7574268728321744,
    "01K7PACKTEZZZZZZZZZZZZZZZZ": 0.46078745695407497,
    "01JHX3B9Q2M4N6P8R0S2T4V6W8": 0.695578924092413,
    "01K6W2ZPF9QX8V4C3N7M5T1R0G": 0.7788327802936185,
}


class TestAssignment:
    def test_the_population_is_distinct(self) -> None:
        assert len(set(PACK_IDS)) == len(PACK_IDS) == 4000

    def test_rate_zero_never_withholds(self) -> None:
        assert not any(is_held_out(pid, 0.0) for pid in PACK_IDS)

    def test_rate_one_always_withholds(self) -> None:
        assert all(is_held_out(pid, 1.0) for pid in PACK_IDS)

    def test_draws_lie_in_the_unit_interval_and_spread_over_it(self) -> None:
        draws = [holdout_draw(pid) for pid in PACK_IDS]
        assert all(0.0 <= d < 1.0 for d in draws)
        assert len(set(draws)) == len(draws)
        # 4000 uniform draws: P(min > 0.01) = 0.99**4000, about e**-40.
        assert min(draws) < 0.01
        assert max(draws) > 0.99

    @pytest.mark.parametrize("rate", [0.1, 0.5, 0.9])
    def test_share_withheld_is_within_a_binomial_999_bound(self, rate: float) -> None:
        n = len(PACK_IDS)
        withheld = sum(is_held_out(pid, rate) for pid in PACK_IDS)
        bound = _Z_999 * math.sqrt(n * rate * (1 - rate))
        assert abs(withheld - n * rate) <= bound, (withheld, n * rate, bound)

    def test_the_draw_is_pinned(self) -> None:
        assert {pid: holdout_draw(pid) for pid in GOLDEN_DRAWS} == GOLDEN_DRAWS

    def test_the_rate_is_a_strict_upper_bound(self) -> None:
        """``draw < rate``, never ``<=``: a rate equal to the draw serves.

        With ``<=`` a rate of 0 would withhold a pack whose draw is exactly
        0.0 — a 2**-53 event no test can construct by search, so the
        boundary is pinned at every draw instead, and at 0.0 by stubbing.
        """
        for pid in PACK_IDS[:200]:
            draw = holdout_draw(pid)
            assert is_held_out(pid, draw) is False
            assert is_held_out(pid, math.nextafter(draw, 1.0)) is True

    def test_rate_zero_serves_even_a_zero_draw(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(pack_holdout, "holdout_draw", lambda _pid: 0.0)
        assert is_held_out(PACK_IDS[0], 0.0) is False
        assert is_held_out(PACK_IDS[0], 1e-12) is True


class TestAcrossProcesses:
    """Same pack id, same arm, in a fresh interpreter with another hash seed."""

    _CHILD = (
        "import json, sys\n"
        "import trellis.core.pack_holdout as m\n"
        "ids = json.loads(sys.stdin.read())\n"
        "print(json.dumps({'file': m.__file__,"
        " 'draws': [m.holdout_draw(i) for i in ids]}))\n"
    )

    @pytest.mark.parametrize("hash_seed", ["1", "2"])
    def test_a_child_process_draws_the_same_values(
        self, hash_seed: str, tmp_path: Path
    ) -> None:
        ids = PACK_IDS[:300] + list(GOLDEN_DRAWS)
        env = {
            **os.environ,
            "PYTHONPATH": repo_src_pythonpath(),
            "PYTHONHASHSEED": hash_seed,
        }
        result = subprocess.run(  # noqa: S603 — argv is this interpreter + a literal
            [sys.executable, "-c", self._CHILD],
            input=json.dumps(ids),
            capture_output=True,
            text=True,
            env=env,
            cwd=tmp_path,
            check=False,
            timeout=120,
        )
        assert result.returncode == 0, result.stderr[-2000:]
        child = json.loads(result.stdout)
        # The child imported *this* checkout, not the venv's editable install.
        expected_file = repo_root() / "src" / "trellis" / "core" / "pack_holdout.py"
        assert Path(child["file"]).resolve() == expected_file.resolve()
        assert Path(pack_holdout.__file__).resolve() == expected_file.resolve()
        assert child["draws"] == [holdout_draw(i) for i in ids]
        assert [d < 0.5 for d in child["draws"]] == [is_held_out(i, 0.5) for i in ids]


def _event(
    event_type: EventType,
    entity_id: str | None,
    payload: dict[str, object],
) -> Event:
    return Event(
        event_type=event_type,
        source="test",
        entity_id=entity_id,
        entity_type="pack" if event_type is EventType.PACK_ASSEMBLED else None,
        payload=payload,
    )


class TestIsHoldout:
    @pytest.mark.parametrize(
        "payload",
        [None, {}, {"holdout": False}, {"holdout": "true"}, {"holdout": 1}],
    )
    def test_anything_but_a_true_flag_reads_as_served(
        self, payload: dict[str, object] | None
    ) -> None:
        assert is_holdout(payload) is False

    def test_a_true_flag_reads_as_withheld(self) -> None:
        assert is_holdout({"holdout": True, "holdout_rate": 0.1}) is True


class TestDropHoldout:
    """Readers analyse the served arm: holdout packs and their feedback go."""

    def test_holdout_packs_and_the_feedback_naming_them_are_dropped(self) -> None:
        served_a = _event(EventType.PACK_ASSEMBLED, PACK_IDS[0], {"holdout": False})
        legacy = _event(EventType.PACK_ASSEMBLED, PACK_IDS[1], {})
        held = _event(EventType.PACK_ASSEMBLED, PACK_IDS[2], {"holdout": True})
        fb_served = _event(
            EventType.FEEDBACK_RECORDED, PACK_IDS[0], {"pack_id": PACK_IDS[0]}
        )
        fb_held_payload = _event(
            EventType.FEEDBACK_RECORDED, "fb-1", {"pack_id": f" {PACK_IDS[2]} "}
        )
        fb_held_entity = _event(
            EventType.FEEDBACK_RECORDED, PACK_IDS[2], {"target_id": PACK_IDS[2]}
        )
        fb_unkeyed = _event(EventType.FEEDBACK_RECORDED, None, {"rating": 0.9})

        packs, feedback = drop_holdout(
            [served_a, legacy, held],
            [fb_served, fb_held_payload, fb_held_entity, fb_unkeyed],
        )

        assert packs == [served_a, legacy]
        assert feedback == [fb_served, fb_unkeyed]

    def test_without_holdout_packs_nothing_is_dropped(self) -> None:
        packs_in = [
            _event(EventType.PACK_ASSEMBLED, pid, {"holdout": False})
            for pid in PACK_IDS[:3]
        ]
        feedback_in = [
            _event(EventType.FEEDBACK_RECORDED, pid, {"pack_id": pid})
            for pid in PACK_IDS[:3]
        ]
        packs, feedback = drop_holdout(packs_in, feedback_in)
        assert packs == packs_in
        assert feedback == feedback_in
