"""Verify RRF and MMR rerankers honour ParameterRegistry overrides."""

from __future__ import annotations

from pathlib import Path

import pytest

from trellis.ops import ParameterRegistry
from trellis.retrieve.rerankers import MMRReranker, RRFReranker, build_reranker
from trellis.retrieve.rerankers.mmr import (
    DEFAULT_MMR_LAMBDA,
    DEFAULT_MMR_SHINGLE_SIZE,
)
from trellis.retrieve.rerankers.rrf import DEFAULT_RRF_K
from trellis.schemas.parameters import ParameterScope, ParameterSet
from trellis.stores.sqlite.parameter import SQLiteParameterStore


@pytest.fixture
def param_store(tmp_path: Path):
    s = SQLiteParameterStore(tmp_path / "parameters.db")
    yield s
    s.close()


def test_rrf_default_k_without_registry():
    reranker = RRFReranker()
    assert reranker._k == DEFAULT_RRF_K
    assert DEFAULT_RRF_K == 60


def test_rrf_honours_registry_override(param_store: SQLiteParameterStore):
    reg = ParameterRegistry(param_store)
    param_store.put(
        ParameterSet(
            scope=ParameterScope(component_id="retrieve.rerankers.RRFReranker"),
            values={"k": 10},
        )
    )
    reranker = RRFReranker(registry=reg)
    assert reranker._k == 10


def test_rrf_caller_default_wins_when_no_snapshot(
    param_store: SQLiteParameterStore,
):
    reg = ParameterRegistry(param_store)  # no snapshot stored
    reranker = RRFReranker(k=99, registry=reg)
    assert reranker._k == 99


def test_mmr_defaults_without_registry():
    reranker = MMRReranker()
    assert reranker._lambda == DEFAULT_MMR_LAMBDA
    assert reranker._shingle_size == DEFAULT_MMR_SHINGLE_SIZE


def test_mmr_honours_registry_overrides(param_store: SQLiteParameterStore):
    reg = ParameterRegistry(param_store)
    param_store.put(
        ParameterSet(
            scope=ParameterScope(component_id="retrieve.rerankers.MMRReranker"),
            values={"lambda_param": 0.4, "shingle_size": 5},
        )
    )
    reranker = MMRReranker(registry=reg)
    assert reranker._lambda == 0.4
    assert reranker._shingle_size == 5


def test_build_reranker_factory(param_store: SQLiteParameterStore):
    reg = ParameterRegistry(param_store)
    assert isinstance(build_reranker("rrf"), RRFReranker)
    assert isinstance(build_reranker("mmr"), MMRReranker)
    # Registry threads through.
    param_store.put(
        ParameterSet(
            scope=ParameterScope(component_id="retrieve.rerankers.RRFReranker"),
            values={"k": 42},
        )
    )
    r = build_reranker("rrf", parameter_registry=reg)
    assert isinstance(r, RRFReranker)
    assert r._k == 42


def test_build_reranker_rejects_unknown():
    with pytest.raises(ValueError, match="Unknown reranker kind"):
        build_reranker("does-not-exist")
