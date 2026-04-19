"""Tests for ParameterScope, ParameterSet, ParameterProposal."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from trellis.schemas.parameters import (
    ParameterProposal,
    ParameterScope,
    ParameterSet,
)


def test_scope_key_and_specificity():
    s0 = ParameterScope(component_id="c")
    assert s0.key() == ("c", None, None, None)
    assert s0.specificity() == 0

    s3 = ParameterScope(
        component_id="c",
        domain="d",
        intent_family="plan",
        tool_name="t",
    )
    assert s3.key() == ("c", "d", "plan", "t")
    assert s3.specificity() == 3


def test_parameter_set_defaults():
    ps = ParameterSet(scope=ParameterScope(component_id="c"))
    assert ps.values == {}
    assert ps.source == "default"
    assert ps.params_version


def test_parameter_set_forbids_extra():
    with pytest.raises(ValidationError):
        ParameterSet(
            scope=ParameterScope(component_id="c"),
            nope="boom",
        )


def test_parameter_set_mixed_value_types():
    ps = ParameterSet(
        scope=ParameterScope(component_id="c"),
        values={"k": 60, "alpha": 0.7, "enabled": True, "label": "baseline"},
    )
    assert ps.values["k"] == 60
    assert ps.values["alpha"] == 0.7
    assert ps.values["enabled"] is True
    assert ps.values["label"] == "baseline"


def test_parameter_proposal_defaults():
    prop = ParameterProposal(
        scope=ParameterScope(component_id="c"),
        tuner="rule_tuner",
    )
    assert prop.status == "pending"
    assert prop.sample_size == 0
    assert prop.effect_size is None
