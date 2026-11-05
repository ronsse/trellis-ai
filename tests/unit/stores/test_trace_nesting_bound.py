"""The nesting bound on a trace's free-form payload (#1186).

``append`` once stored a trace nested about 200 levels deep that ``query`` then
could not parse. The per-backend behaviour is pinned in the trace-store contract
suite; this file pins the helper itself and that every backend calls it.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

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
