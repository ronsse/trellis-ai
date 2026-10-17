"""The Reports tab renders a typed control per param, not one text box each.

``onReportPickerChange()`` used to render every report param as a plain
``<input type="text">`` regardless of its actual type, with no bounds and
no enum options, so an operator had to already know a report's valid
range (or read the source) before running it. ``GET /api/v1/analyze`` now
carries each report's params DTO as JSON Schema (``params_schema``, see
``tests/unit/api/test_analyze_routes.py``), and this file pins the UI side
that consumes it:

* an integer/number field renders a ``<input type="number">`` carrying its
  schema bounds as ``data-min``/``data-max``/``data-min-exclusive``/
  ``data-max-exclusive``;
* a boolean field renders a checkbox;
* a field with ``enum`` in its schema (``outcome`` on the holdout report)
  renders a ``<select>``;
* an ``Optional`` field (``anyOf`` with a null branch) is unwrapped to its
  non-null branch before any of the above;
* ``validateReportParamInput`` enforces those same bounds client-side
  before ``runReport()`` submits, and a 422 that still reaches the server
  (a bound the client cannot see, such as the holdout report's
  ``outcome`` allow-list enforced by a ``@field_validator``) renders
  legibly through ``fetchReportJSON``, not a bare status line.

Static assertions on ``index.html``'s own markup/script, the same style as
``test_reports_tab_ui.py`` -- no browser, no JS engine.
"""

from __future__ import annotations

import re
from pathlib import Path

import trellis_api

INDEX_HTML = Path(trellis_api.__file__).parent / "static" / "index.html"


def _page() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


def _between(page: str, start: str, end: str) -> str:
    found = re.search(re.escape(start) + r"(.*?)" + re.escape(end), page, re.DOTALL)
    assert found is not None, f"could not find region between {start!r} and {end!r}"
    return found.group(1)


def _function_body(page: str, name: str) -> str:
    found = re.search(
        r"function " + re.escape(name) + r"\([^)]*\) \{(.*?)\n        \}",
        page,
        re.DOTALL,
    )
    assert found is not None, f"function {name}() not found"
    return found.group(1)


# ---------------------------------------------------------------------------
# reportSchemas is captured and threaded into onReportPickerChange.
# ---------------------------------------------------------------------------


def test_report_schemas_captured_from_the_list_response() -> None:
    page = _page()
    assert "let reportSchemas = {};" in page
    load_body = _between(
        page,
        "async function loadReportsTab() {",
        "\n        function onReportPickerChange() {",
    )
    assert "reportSchemas[r.name] = r.params_schema || {};" in load_body


def test_on_report_picker_change_passes_each_fields_schema_to_the_renderer() -> None:
    body = _function_body(_page(), "onReportPickerChange")
    assert "reportSchemas[name] && reportSchemas[name].properties" in body
    assert "reportParamFieldHtml(key, val, properties[key] || {})" in body


# ---------------------------------------------------------------------------
# reportParamInnerSchema unwraps Optional (anyOf w/ a null branch).
# ---------------------------------------------------------------------------


def test_inner_schema_unwraps_optional_anyof() -> None:
    body = _function_body(_page(), "reportParamInnerSchema")
    assert "Array.isArray(fieldSchema.anyOf)" in body
    assert "branch.type !== 'null'" in body


# ---------------------------------------------------------------------------
# reportParamFieldHtml renders the right control per schema shape.
# ---------------------------------------------------------------------------


def test_enum_schema_renders_a_select_with_options() -> None:
    body = _function_body(_page(), "reportParamFieldHtml")
    assert "Array.isArray(inner.enum)" in body
    select_branch = _between(body, "Array.isArray(inner.enum)) {", "\n            }")
    # pass 6: the inline font-size style on this control became .fs-3
    assert '<select class="report-param-input fs-3" data-kind="enum"' in select_branch
    assert "opt === val ? 'selected' : ''" in select_branch
    assert "escHtml(opt)" in select_branch


def test_boolean_schema_renders_a_checkbox() -> None:
    body = _function_body(_page(), "reportParamFieldHtml")
    assert "inner.type === 'boolean'" in body
    bool_branch = _between(body, "inner.type === 'boolean') {", "\n            }")
    assert 'type="checkbox"' in bool_branch
    assert 'data-kind="boolean"' in bool_branch
    assert "${val ? 'checked' : ''}" in bool_branch


def test_numeric_schema_renders_a_number_input_carrying_its_bounds() -> None:
    body = _function_body(_page(), "reportParamFieldHtml")
    assert "inner.type === 'integer' || inner.type === 'number'" in body
    numeric_branch = _between(
        body,
        "inner.type === 'integer' || inner.type === 'number') {",
        "\n            }",
    )
    assert 'type="number"' in numeric_branch
    assert "data-min=" in numeric_branch
    assert "data-min-exclusive=" in numeric_branch
    assert "data-max=" in numeric_branch
    assert "data-max-exclusive=" in numeric_branch
    # An integer field steps by whole numbers; any other numeric field
    # (a float) accepts a fractional value.
    assert "inner.type === 'integer' ? '1' : 'any'" in numeric_branch


def test_fallback_schema_renders_a_plain_text_input() -> None:
    body = _function_body(_page(), "reportParamFieldHtml")
    tail = body[body.rindex("\n            }") :]
    assert 'type="text"' in tail
    assert 'data-kind="string"' in tail


# ---------------------------------------------------------------------------
# validateReportParamInput: client-side bounds checking before submit.
# ---------------------------------------------------------------------------


def test_validate_skips_boolean_and_enum_kinds() -> None:
    body = _function_body(_page(), "validateReportParamInput")
    assert "kind === 'boolean' || kind === 'enum'" in body


def test_validate_treats_an_empty_optional_field_as_valid() -> None:
    body = _function_body(_page(), "validateReportParamInput")
    assert "raw === ''" in body


def test_validate_rejects_non_finite_and_non_integer() -> None:
    body = _function_body(_page(), "validateReportParamInput")
    assert "Number.isFinite(num)" in body
    assert "Number.isInteger(num)" in body


def test_validate_checks_all_four_bound_shapes() -> None:
    # ge/le (inclusive, `minimum`/`maximum`) and gt/lt (exclusive,
    # `exclusiveMinimum`/`exclusiveMaximum`) are different fields in the
    # schema and must both be checked -- a field using gt=0 (planned_mde)
    # would otherwise silently accept 0 if only the inclusive check ran.
    body = _function_body(_page(), "validateReportParamInput")
    assert "num < min" in body
    assert "num <= minExclusive" in body
    assert "num > max" in body
    assert "num >= maxExclusive" in body


def test_run_report_blocks_submission_on_a_validation_error() -> None:
    body = _function_body(_page(), "runReport")
    assert "inputs.map(validateReportParamInput).filter(Boolean)" in body
    guard = _between(body, "errors.length > 0) {", "\n            }")
    assert "errorEl.textContent = errors.join" in guard
    assert "return;" in guard


def test_run_report_sends_an_explicit_true_false_for_a_boolean_param() -> None:
    # Unlike a text/number param (omitted from the query string when
    # empty, so the server applies its own default), a checkbox always
    # has a definite state and must always be sent -- an unchecked box
    # left out would be indistinguishable from "use the default", which
    # for a report whose boolean default is True would silently flip it.
    body = _function_body(_page(), "runReport")
    bool_branch = _between(
        body, "input.dataset.kind === 'boolean') {", "\n                    return;"
    )
    assert "input.checked ? 'true' : 'false'" in bool_branch


# ---------------------------------------------------------------------------
# fetchReportJSON: a 422/409 still renders legibly.
# ---------------------------------------------------------------------------


def test_run_report_uses_the_detail_extracting_fetch() -> None:
    body = _function_body(_page(), "runReport")
    assert "fetchReportJSON(" in body
    assert "fetchJSON(" not in body


def test_fetch_report_json_renders_a_422_list_legibly() -> None:
    page = _page()
    body = re.search(
        r"async function fetchReportJSON\(path\) \{(.*?)\n        \}",
        page,
        re.DOTALL,
    )
    assert body is not None
    text = body.group(1)
    assert "res.status === 422" in text
    assert "formatPatchValidationDetail(j.detail)" in text


def test_fetch_report_json_falls_back_to_a_typed_error_message() -> None:
    page = _page()
    body = re.search(
        r"async function fetchReportJSON\(path\) \{(.*?)\n        \}",
        page,
        re.DOTALL,
    )
    assert body is not None
    text = body.group(1)
    assert "typeof j.message === 'string'" in text


# ---------------------------------------------------------------------------
# Markup: an inline error element exists for the validation message.
# ---------------------------------------------------------------------------


def test_report_param_error_container_exists_in_markup() -> None:
    page = _page()
    assert 'id="report-param-error"' in page
    # pass 6: color:var(--danger) became the shared .text-danger class
    assert "text-danger" in _between(page, '<p id="report-param-error"', "</p>")
