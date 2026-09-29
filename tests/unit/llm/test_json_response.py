"""Strict shared parsing for JSON-shaped LLM responses."""

import pytest

from trellis.llm.json_response import (
    JSONParseOutcome,
    coerce_finite_float,
    parse_json_response,
    strip_code_fence,
)


def test_strip_code_fence_keeps_last_json_line_without_closing_fence() -> None:
    raw = '```JSON\n[{"title": "first"},\n{"title": "second"}]'

    stripped = strip_code_fence(raw)

    assert stripped == '[{"title": "first"},\n{"title": "second"}]'
    parsed = parse_json_response(raw)
    assert parsed.outcome is JSONParseOutcome.VALUE
    assert parsed.value == [{"title": "first"}, {"title": "second"}]


def test_non_json_fence_is_not_stripped() -> None:
    result = parse_json_response("```javascript\n{}\n```")

    assert result.outcome is JSONParseOutcome.MALFORMED


def test_parse_json_response_distinguishes_empty_from_value() -> None:
    empty = parse_json_response("[]")
    value = parse_json_response('{"decision": "add"}')

    assert empty.outcome is JSONParseOutcome.EMPTY
    assert empty.value == []
    assert value.outcome is JSONParseOutcome.VALUE
    assert value.value == {"decision": "add"}


def test_parse_json_response_is_strict_beyond_fence_removal() -> None:
    result = parse_json_response('Sure! {"decision": "add"}')

    assert result.outcome is JSONParseOutcome.MALFORMED
    assert result.value is None
    assert result.error


def test_parse_json_response_reads_nesting_past_the_recursion_limit_as_malformed() -> (
    None
):
    # json.loads raises RecursionError, not a ValueError, once nesting passes
    # the interpreter's limit: about 1,000 deep on 3.11 and about 10,000 on
    # 3.12/3.13, so 100,000 reaches it on each with a 10x margin.
    result = parse_json_response("[" * 100_000)

    assert result.outcome is JSONParseOutcome.MALFORMED
    assert result.value is None
    assert result.error is not None
    assert result.error.startswith("RecursionError")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param(0.3, 0.3, id="float"),
        pytest.param("0.7", 0.7, id="numeric-string"),
        # Finite but out of range: clamping is each site's job.
        pytest.param(7, 7.0, id="int-above-one"),
        pytest.param(-2, -2.0, id="negative-int"),
        # bool is an int; each site applies its own bool policy.
        pytest.param(True, 1.0, id="bool"),
    ],
)
def test_coerce_finite_float_reads_finite_numbers(
    value: object, expected: float
) -> None:
    assert coerce_finite_float(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(float("nan"), id="nan"),
        pytest.param(float("inf"), id="inf"),
        pytest.param(float("-inf"), id="-inf"),
        pytest.param("nan", id="nan-string"),
        pytest.param("Infinity", id="infinity-string"),
        pytest.param("1e999", id="overflowing-exponent-string"),
        pytest.param(10**310, id="overflowing-int"),
        pytest.param(-(10**310), id="overflowing-negative-int"),
        pytest.param("high", id="word"),
        pytest.param(None, id="none"),
        pytest.param([0.9], id="list"),
        pytest.param({"v": 1}, id="dict"),
    ],
)
def test_coerce_finite_float_rejects_non_finite_and_non_numeric(
    value: object,
) -> None:
    assert coerce_finite_float(value) is None
