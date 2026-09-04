"""Strict shared parsing for JSON-shaped LLM responses."""

from trellis.llm.json_response import (
    JSONParseOutcome,
    parse_json_response,
    strip_code_fence,
)


def test_strip_code_fence_keeps_last_json_line_without_closing_fence() -> None:
    raw = '```json\n[{"title": "first"},\n{"title": "second"}]'

    stripped = strip_code_fence(raw)

    assert stripped == '[{"title": "first"},\n{"title": "second"}]'
    parsed = parse_json_response(raw)
    assert parsed.outcome is JSONParseOutcome.VALUE
    assert parsed.value == [{"title": "first"}, {"title": "second"}]


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
