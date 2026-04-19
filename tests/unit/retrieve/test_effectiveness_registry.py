"""Verify effectiveness analysers honour ParameterRegistry overrides."""

from __future__ import annotations

from pathlib import Path

import pytest

from trellis.ops import ParameterRegistry
from trellis.retrieve.effectiveness import (
    analyze_effectiveness,
    run_advisory_fitness_loop,
)
from trellis.schemas.parameters import ParameterScope, ParameterSet
from trellis.stores.advisory_store import AdvisoryStore
from trellis.stores.sqlite.event_log import SQLiteEventLog
from trellis.stores.sqlite.parameter import SQLiteParameterStore


@pytest.fixture
def tmp_stores(tmp_path: Path):
    event_log = SQLiteEventLog(tmp_path / "events.db")
    param_store = SQLiteParameterStore(tmp_path / "parameters.db")
    advisory_store = AdvisoryStore(tmp_path / "advisories.json")
    try:
        yield event_log, param_store, advisory_store
    finally:
        event_log.close()
        param_store.close()


def test_analyze_effectiveness_no_registry_uses_defaults(tmp_stores):
    event_log, _, _ = tmp_stores
    # No data; just verify the function accepts registry=None.
    report = analyze_effectiveness(event_log)
    assert report.total_packs == 0
    assert report.total_feedback == 0


def test_analyze_effectiveness_with_registry_override(tmp_stores):
    event_log, param_store, _ = tmp_stores
    reg = ParameterRegistry(param_store)

    # Override should not blow up on an empty event log; this just
    # exercises the resolve path.
    param_store.put(
        ParameterSet(
            scope=ParameterScope(component_id="retrieve.effectiveness.items"),
            values={
                "success_rating_threshold": 0.8,
                "noise_rate_threshold": 0.2,
            },
        )
    )

    report = analyze_effectiveness(event_log, registry=reg)
    assert report.total_packs == 0


def test_advisory_fitness_loop_honours_registry_defaults(tmp_stores):
    event_log, param_store, advisory_store = tmp_stores
    reg = ParameterRegistry(param_store)

    param_store.put(
        ParameterSet(
            scope=ParameterScope(component_id="retrieve.effectiveness.advisory"),
            values={
                "min_presentations": 10,
                "suppress_confidence": 0.05,
                "blend_weight": 0.5,
            },
        )
    )

    # Empty event log; call should resolve defaults from registry and
    # return an empty report without raising.
    report = run_advisory_fitness_loop(
        event_log,
        advisory_store,
        registry=reg,
    )
    assert report.total_feedback == 0
    assert report.advisory_scores == []
