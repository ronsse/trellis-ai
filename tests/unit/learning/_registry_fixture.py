"""Shared registry-fixture helper for learning tests.

Tests that exercise :func:`trellis.learning.scoring.analyze_learning_observations`
(or :func:`trellis.learning.scoring._recommend_learning_action`) must supply
a :class:`ParameterRegistry` populated with the four learning thresholds —
the production library carries no hard-coded defaults per the POC directive
in ``docs/design/plan-self-improvement-program.md`` §2.

Build a seeded registry with :func:`build_seeded_registry` and pass it as
``registry=``. Override values via ``overrides=``. Pass ``replace=True``
to discard the seed defaults entirely — used by the missing-key tests
that verify the scoring layer raises ``KeyError`` instead of silently
substituting defaults.
"""

from __future__ import annotations

from trellis.learning import LEARNING_SCORING_COMPONENT
from trellis.ops import ParameterRegistry
from trellis.schemas.parameters import ParameterScope, ParameterSet
from trellis_cli.analyze import (
    LEARNING_PARAMETER_SEED_DEFAULTS,
    _InMemoryParameterStore,
)


def build_seeded_registry(
    *,
    overrides: dict[str, float] | None = None,
    replace: bool = False,
) -> ParameterRegistry:
    """Construct an in-memory ParameterRegistry for ``learning.scoring``.

    Seeds the four required keys with the production seed defaults
    (:data:`trellis_cli.analyze.LEARNING_PARAMETER_SEED_DEFAULTS`) so
    behaviour matches a fresh install. ``overrides`` replaces individual
    values; ``replace=True`` discards the defaults entirely (use to
    verify the scoring layer raises ``KeyError`` on a missing key).
    """
    values: dict[str, float | int | str | bool] = (
        {} if replace else dict(LEARNING_PARAMETER_SEED_DEFAULTS)
    )
    if overrides:
        values.update(overrides)
    store = _InMemoryParameterStore()
    store.put(
        ParameterSet(
            scope=ParameterScope(component_id=LEARNING_SCORING_COMPONENT),
            values=values,
            source="test:_registry_fixture",
        )
    )
    return ParameterRegistry(store=store)
