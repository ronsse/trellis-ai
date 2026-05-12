"""Shared registry-fixture helper for learning tests.

Tests that exercise :func:`trellis.learning.scoring.analyze_learning_observations`
(or :func:`trellis.learning.scoring._recommend_learning_action`) must supply
a :class:`ParameterRegistry` populated with the four learning thresholds —
the production library carries no hard-coded defaults per the POC directive
in ``docs/design/plan-self-improvement-program.md`` §2.

Build a seeded registry with :func:`build_seeded_registry` and pass it as
``registry=``. Override values via ``overrides=`` to test boundary
conditions.
"""

from __future__ import annotations

from trellis.learning import (
    LEARNING_NOISE_RETRY_KEY,
    LEARNING_NOISE_SUCCESS_KEY,
    LEARNING_PROMOTE_RETRY_KEY,
    LEARNING_PROMOTE_SUCCESS_KEY,
)
from trellis.ops import ParameterRegistry
from trellis.schemas.parameters import ParameterScope, ParameterSet
from trellis_cli.analyze import _InMemoryParameterStore

# Historical defaults from the deleted module-level constants. Tests pin
# these values so behaviour is preserved after the registry-required
# refactor; production wires the same defaults from the CLI seed (see
# ``trellis_cli.analyze.LEARNING_PARAMETER_SEED_DEFAULTS``).
TEST_DEFAULTS: dict[str, float] = {
    LEARNING_PROMOTE_SUCCESS_KEY: 0.75,
    LEARNING_PROMOTE_RETRY_KEY: 0.25,
    LEARNING_NOISE_SUCCESS_KEY: 0.4,
    LEARNING_NOISE_RETRY_KEY: 0.5,
}


def build_seeded_registry(
    *,
    overrides: dict[str, float] | None = None,
) -> ParameterRegistry:
    """Construct an in-memory ParameterRegistry for learning.scoring.

    By default seeds the four required keys with their historical values.
    Pass ``overrides`` to replace individual values; missing keys retain
    the default so tests stay terse.
    """
    values: dict[str, float | int | str | bool] = dict(TEST_DEFAULTS)
    if overrides:
        values.update(overrides)
    store = _InMemoryParameterStore()
    store.put(
        ParameterSet(
            scope=ParameterScope(component_id="learning.scoring"),
            values=values,
            source="test:_registry_fixture",
        )
    )
    return ParameterRegistry(store=store)


def build_empty_registry() -> ParameterRegistry:
    """Return a registry whose snapshot is missing required keys.

    Used to verify the scoring module raises ``KeyError`` rather than
    silently substituting hard-coded defaults.
    """
    store = _InMemoryParameterStore()
    store.put(
        ParameterSet(
            scope=ParameterScope(component_id="learning.scoring"),
            values={},
            source="test:_registry_fixture.empty",
        )
    )
    return ParameterRegistry(store=store)
