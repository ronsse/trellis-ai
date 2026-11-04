"""Which arm of which experiment a pack was assembled under.

Serving changes run as concurrent flags once the current experiment reads out,
and a reader of ``PACK_ASSEMBLED`` has to be able to say, from the row alone,
which arm of each running experiment produced it. Every row carries one
``variants`` mapping, ``{experiment: arm}``, built by :func:`active_variants`.

**An experiment is listed only while it is running.** The pack holdout is the
only experiment today, and it is running when its rate is above zero; at rate
``0`` the mapping is ``{}``, so a row from a build that predates the key and a
row from a build with no experiment on read the same way: no arm. A reader
branches on ``variants.get("pack_holdout")``, never on the key's presence
alone.

**Stamping is telemetry, never serving.** :func:`active_variants` reads the
decisions the builder already made and takes no draw of its own, so adding an
experiment here cannot change what a pack serves.

Add an experiment by giving :func:`active_variants` one more keyword argument
for its inputs and one more entry for its arm; every ``PACK_ASSEMBLED`` row,
flat and sectioned, then carries it.

Standard library only, like :mod:`trellis.core.pack_holdout`.
"""

from __future__ import annotations

#: ``PACK_ASSEMBLED`` payload key holding the ``{experiment: arm}`` mapping.
VARIANTS_KEY = "variants"

#: The pack holdout's experiment name, and its two arms.
PACK_HOLDOUT_EXPERIMENT = "pack_holdout"
HOLDOUT_ARM_HELD_OUT = "held_out"
HOLDOUT_ARM_SERVED = "served"


def active_variants(*, holdout_rate: float, held_out: bool) -> dict[str, str]:
    """The ``{experiment: arm}`` stamp for one assembled pack.

    ``holdout_rate`` is the rate the builder drew at and ``held_out`` is the
    draw's outcome for this pack. The holdout is listed only when the rate is
    above zero: at ``0`` nothing is withheld, there is no second arm, and a
    ``served`` stamp would claim an experiment that is not running. Returns a
    new dict each call.
    """
    variants: dict[str, str] = {}
    if holdout_rate > 0.0:
        variants[PACK_HOLDOUT_EXPERIMENT] = (
            HOLDOUT_ARM_HELD_OUT if held_out else HOLDOUT_ARM_SERVED
        )
    return variants


__all__ = [
    "HOLDOUT_ARM_HELD_OUT",
    "HOLDOUT_ARM_SERVED",
    "PACK_HOLDOUT_EXPERIMENT",
    "VARIANTS_KEY",
    "active_variants",
]
