"""Verify learning scoring thresholds honour ParameterRegistry overrides."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.unit.learning._registry_fixture import build_seeded_registry
from trellis.learning.scoring import _recommend_learning_action
from trellis.ops import ParameterRegistry
from trellis.schemas.parameters import ParameterScope, ParameterSet
from trellis.stores.sqlite.parameter import SQLiteParameterStore


@pytest.fixture
def param_store(tmp_path: Path):
    s = SQLiteParameterStore(tmp_path / "parameters.db")
    yield s
    s.close()


def test_recommend_learning_action_seeded_defaults() -> None:
    """With a registry seeded with the historical defaults, the four
    recommendation buckets fire exactly as before the registry-required
    refactor. Proves the seed values preserve behaviour."""
    registry = build_seeded_registry()
    # High success + low retry -> promote_guidance (default thresholds).
    result = _recommend_learning_action(
        item_type="guidance",
        success_rate=0.9,
        retry_rate=0.1,
        registry=registry,
    )
    assert result == "promote_guidance"

    result = _recommend_learning_action(
        item_type="precedent",
        success_rate=0.9,
        retry_rate=0.1,
        registry=registry,
    )
    assert result == "promote_precedent"

    # Low success -> noise.
    assert (
        _recommend_learning_action(
            item_type="guidance",
            success_rate=0.2,
            retry_rate=0.1,
            registry=registry,
        )
        == "investigate_noise"
    )

    # In the neutral band -> None.
    assert (
        _recommend_learning_action(
            item_type="guidance",
            success_rate=0.5,
            retry_rate=0.3,
            registry=registry,
        )
        is None
    )


def test_recommend_learning_action_registry_override(
    param_store: SQLiteParameterStore,
):
    reg = ParameterRegistry(param_store)
    param_store.put(
        ParameterSet(
            scope=ParameterScope(component_id="learning.scoring"),
            values={
                "promote_success_threshold": 0.95,  # tighter bar
                "promote_retry_threshold": 0.05,
                "noise_success_threshold": 0.1,
                "noise_retry_threshold": 0.9,
            },
        )
    )

    # A candidate that defaults would promote, now falls short.
    assert (
        _recommend_learning_action(
            item_type="guidance",
            success_rate=0.8,
            retry_rate=0.1,
            registry=reg,
        )
        is None
    )

    # A candidate meeting the new tighter bar still promotes.
    assert (
        _recommend_learning_action(
            item_type="guidance",
            success_rate=0.96,
            retry_rate=0.04,
            registry=reg,
        )
        == "promote_guidance"
    )
