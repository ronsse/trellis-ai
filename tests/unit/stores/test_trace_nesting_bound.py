"""The nesting bound on a trace's free-form payload (#1186).

``append`` once stored a trace nested about 200 levels deep that ``query`` then
could not parse. The per-backend behaviour is pinned in the trace-store contract
suite; this file pins the helper itself and that every backend calls it.
"""

from __future__ import annotations

import ast
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from tests.ast_rules import assert_hand_read_floor
from trellis.errors import ValidationError
from trellis.schemas.enums import TraceSource
from trellis.schemas.trace import Trace, TraceContext, TraceStep
from trellis.stores.base.trace import (
    MAX_TRACE_PAYLOAD_DEPTH,
    TRACE_TOO_DEEP_CODE,
    _nests_deeper_than,
    check_trace_nesting,
)

STORES_ROOT = Path(__file__).resolve().parents[3] / "src" / "trellis" / "stores"


def _nested(levels: int) -> dict[str, Any]:
    value: dict[str, Any] = {"leaf": 1}
    for _ in range(levels - 1):
        value = {"k": value}
    return value


class _Box(BaseModel):
    """A pydantic model that holds one free-form value, as a payload might."""

    inner: Any = None


def _chain(levels: int, wrap: Callable[[Any], Any]) -> Any:
    """``levels`` containers of one kind around a scalar, built with *wrap*."""
    value: Any = 1
    for _ in range(levels):
        value = wrap(value)
    return value


#: One wrapper per container the JSON writer turns into an object or an array,
#: besides the dict and list the helper has always counted.
_WRAPPERS: dict[str, Callable[[Any], Any]] = {
    "model": lambda v: _Box(inner=v),
    "tuple": lambda v: (v,),
    "frozenset": lambda v: frozenset({v}),
}


def _trace(**kwargs: Any) -> Trace:
    return Trace(
        source=TraceSource.AGENT,
        intent="x",
        context=TraceContext(agent_id="a"),
        **kwargs,
    )


class TestNestsDeeperThan:
    def test_a_scalar_and_an_empty_container_are_shallow(self) -> None:
        assert not _nests_deeper_than(1, 0)
        assert not _nests_deeper_than("text", 0)
        assert _nests_deeper_than({}, 0)  # one level, past a limit of zero
        assert not _nests_deeper_than({}, 1)
        assert not _nests_deeper_than([], 1)

    @pytest.mark.parametrize("levels", [1, 2, 7, 50])
    def test_the_limit_is_inclusive(self, levels: int) -> None:
        assert not _nests_deeper_than(_nested(levels), levels)
        assert _nests_deeper_than(_nested(levels), levels - 1)

    def test_a_payload_past_the_recursion_limit_is_measured_not_crashed_on(
        self,
    ) -> None:
        assert _nests_deeper_than(_nested(20_000), MAX_TRACE_PAYLOAD_DEPTH)

    @pytest.mark.parametrize("kind", sorted(_WRAPPERS))
    @pytest.mark.parametrize("levels", [1, 2, 7, 50])
    def test_the_limit_is_inclusive_for_a_model_a_tuple_and_a_frozenset(
        self, kind: str, levels: int
    ) -> None:
        value = _chain(levels, _WRAPPERS[kind])
        assert not _nests_deeper_than(value, levels)
        assert _nests_deeper_than(value, levels - 1)

    @pytest.mark.parametrize("kind", sorted(_WRAPPERS))
    def test_a_container_of_each_kind_counts_past_the_production_bound(
        self, kind: str
    ) -> None:
        deep = _chain(MAX_TRACE_PAYLOAD_DEPTH + 1, _WRAPPERS[kind])
        assert _nests_deeper_than(deep, MAX_TRACE_PAYLOAD_DEPTH)

    def test_a_set_counts_as_a_level(self) -> None:
        # A set cannot hold a set or a dict, so it is the outer level over a
        # chain of frozensets.
        inner = _chain(4, _WRAPPERS["frozenset"])
        assert not _nests_deeper_than({inner}, 5)
        assert _nests_deeper_than({inner}, 4)

    def test_every_kind_in_one_chain_counts_each_level_once(self) -> None:
        mixed = {"a": _Box(inner=[(frozenset({1}),)])}
        # dict, model, list, tuple, frozenset
        assert not _nests_deeper_than(mixed, 5)
        assert _nests_deeper_than(mixed, 4)

    def test_a_model_is_measured_by_its_fields_and_not_by_itself(self) -> None:
        assert not _nests_deeper_than(_Box(inner=1), 1)
        assert _nests_deeper_than(_Box(inner=1), 0)
        assert _nests_deeper_than(_Box(inner={"k": 1}), 1)

    def test_a_model_that_contains_itself_ends(self) -> None:
        box = _Box()
        box.inner = box
        assert _nests_deeper_than(box, MAX_TRACE_PAYLOAD_DEPTH)

    def test_a_structure_that_contains_itself_ends(self) -> None:
        loop: dict[str, Any] = {}
        loop["self"] = loop
        assert _nests_deeper_than(loop, MAX_TRACE_PAYLOAD_DEPTH)

    def test_a_wide_flat_payload_is_not_deep(self) -> None:
        wide = {f"k{i}": [i, {"a": i}] for i in range(10_000)}
        assert not _nests_deeper_than(wide, 3)


class TestCheckTraceNesting:
    def test_an_ordinary_trace_passes(self) -> None:
        check_trace_nesting(
            _trace(
                steps=[TraceStep(step_type="t", name="n", args={"a": {"b": [1, 2]}})],
                metadata={"repo": "x"},
            )
        )

    def test_the_error_names_the_field_and_the_bound_and_no_value(self) -> None:
        secret = {"token": "sekrit-value"}
        payload: dict[str, Any] = secret
        for _ in range(MAX_TRACE_PAYLOAD_DEPTH):
            payload = {"k": payload}
        trace = _trace(
            steps=[
                TraceStep(step_type="t", name="ok"),
                TraceStep(step_type="t", name="deep", result=payload),
            ]
        )
        with pytest.raises(ValidationError) as caught:
            check_trace_nesting(trace)
        err = caught.value
        assert err.code == TRACE_TOO_DEEP_CODE
        assert "steps[1].result" in err.message
        assert str(MAX_TRACE_PAYLOAD_DEPTH) in err.message
        assert "sekrit-value" not in err.message
        assert err.errors == [err.message]


class TestCheckTraceNestingSeesModelsAndSets:
    """``metadata`` is ``dict[str, Any]``, so it takes a model or a set as it
    takes a dict; ``model_dump_json`` then writes them as an object and an array."""

    @pytest.mark.parametrize("kind", sorted(_WRAPPERS))
    def test_metadata_deeper_than_the_bound_is_refused_whatever_it_nests(
        self, kind: str
    ) -> None:
        trace = _trace(
            metadata={"payload": _chain(MAX_TRACE_PAYLOAD_DEPTH, _WRAPPERS[kind])}
        )
        with pytest.raises(ValidationError) as caught:
            check_trace_nesting(trace)
        assert caught.value.code == TRACE_TOO_DEEP_CODE
        assert "metadata" in caught.value.message

    @pytest.mark.parametrize("kind", sorted(_WRAPPERS))
    def test_a_step_result_at_the_bound_is_accepted(self, kind: str) -> None:
        # ``result`` is the dict level, so the chain may be one shorter.
        trace = _trace(
            steps=[
                TraceStep(
                    step_type="t",
                    name="n",
                    result={
                        "payload": _chain(MAX_TRACE_PAYLOAD_DEPTH - 1, _WRAPPERS[kind])
                    },
                )
            ]
        )
        check_trace_nesting(trace)

    def test_the_depth_is_the_depth_the_writer_produces(self) -> None:
        """The helper's count for a model chain is the JSON depth on disk."""
        trace = _trace(metadata={"payload": _chain(3, _WRAPPERS["model"])})
        written = json.loads(trace.model_dump_json())["metadata"]
        levels = 0
        node: Any = written
        while isinstance(node, dict):
            levels += 1
            node = next(iter(node.values()))
        assert levels == 4  # metadata, then three models
        assert not _nests_deeper_than(trace.metadata, levels)
        assert _nests_deeper_than(trace.metadata, levels - 1)


def _store_classes_calling_the_check() -> tuple[list[str], list[str]]:
    """``(every TraceStore subclass found, those whose append calls the check)``.

    Read from source, not by import: the Postgres module needs a driver this
    suite does not always have.
    """
    found: list[str] = []
    calling: list[str] = []
    for path in sorted(STORES_ROOT.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.ClassDef):
                continue
            if not any(
                isinstance(b, ast.Name) and b.id == "TraceStore" for b in node.bases
            ):
                continue
            found.append(f"{path.relative_to(STORES_ROOT)}::{node.name}")
            append = next(
                (
                    f
                    for f in node.body
                    if isinstance(f, ast.FunctionDef) and f.name == "append"
                ),
                None,
            )
            calls = [
                c
                for c in ast.walk(append or ast.Module(body=[], type_ignores=[]))
                if isinstance(c, ast.Call)
                and isinstance(c.func, ast.Name)
                and c.func.id == "check_trace_nesting"
            ]
            if calls:
                calling.append(found[-1])
    return found, calling


def test_every_trace_store_checks_nesting_before_it_writes() -> None:
    """A new backend cannot skip the bound and quietly store what cannot be read."""
    found, calling = _store_classes_calling_the_check()
    # Hand read: SQLiteTraceStore and PostgresTraceStore.
    assert_hand_read_floor(len(found), 2, subject="TraceStore subclass")
    assert sorted(set(found) - set(calling)) == []
