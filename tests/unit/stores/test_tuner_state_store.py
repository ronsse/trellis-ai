"""Tests for SQLiteTunerStateStore."""

from __future__ import annotations

from pathlib import Path

import pytest

from trellis.schemas.parameters import ParameterProposal, ParameterScope
from trellis.stores.sqlite.tuner_state import SQLiteTunerStateStore


@pytest.fixture
def store(tmp_path: Path):
    s = SQLiteTunerStateStore(tmp_path / "tuner_state.db")
    yield s
    s.close()


def _proposal(**kw) -> ParameterProposal:
    defaults: dict = {
        "scope": ParameterScope(component_id="c", domain="d"),
        "tuner": "rule_tuner",
        "proposed_values": {"alpha": 0.8},
        "sample_size": 25,
        "effect_size": 0.2,
    }
    defaults.update(kw)
    return ParameterProposal(**defaults)


def test_put_and_get_proposal(store: SQLiteTunerStateStore):
    prop = _proposal()
    store.put_proposal(prop)
    got = store.get_proposal(prop.proposal_id)
    assert got is not None
    assert got.tuner == "rule_tuner"
    assert got.proposed_values == {"alpha": 0.8}


def test_list_proposals_by_tuner(store: SQLiteTunerStateStore):
    store.put_proposal(_proposal(tuner="a"))
    store.put_proposal(_proposal(tuner="b"))
    assert len(store.list_proposals(tuner="a")) == 1
    assert len(store.list_proposals()) == 2


def test_list_proposals_by_status(store: SQLiteTunerStateStore):
    store.put_proposal(_proposal(status="pending"))
    store.put_proposal(_proposal(status="canary"))
    assert len(store.list_proposals(status="canary")) == 1


def test_update_status(store: SQLiteTunerStateStore):
    prop = _proposal()
    store.put_proposal(prop)
    updated = store.update_status(prop.proposal_id, "canary", notes="running A/B")
    assert updated is not None
    assert updated.status == "canary"
    assert updated.notes == "running A/B"


def test_update_status_missing(store: SQLiteTunerStateStore):
    assert store.update_status("nope", "canary") is None


def test_replace_proposal(store: SQLiteTunerStateStore):
    prop = _proposal()
    store.put_proposal(prop)
    replaced = ParameterProposal(
        proposal_id=prop.proposal_id,
        scope=prop.scope,
        tuner="rule_tuner",
        proposed_values={"alpha": 0.9},
        status="canary",
    )
    store.put_proposal(replaced)
    got = store.get_proposal(prop.proposal_id)
    assert got is not None
    assert got.proposed_values == {"alpha": 0.9}
    assert got.status == "canary"


def test_cursor(store: SQLiteTunerStateStore):
    assert store.get_cursor("rule_tuner") is None
    store.set_cursor("rule_tuner", "01HF-cursor-01")
    assert store.get_cursor("rule_tuner") == "01HF-cursor-01"

    store.set_cursor("rule_tuner", "01HF-cursor-02")
    assert store.get_cursor("rule_tuner") == "01HF-cursor-02"


def test_cursor_independent_per_tuner(store: SQLiteTunerStateStore):
    store.set_cursor("a", "cur_a")
    store.set_cursor("b", "cur_b")
    assert store.get_cursor("a") == "cur_a"
    assert store.get_cursor("b") == "cur_b"
