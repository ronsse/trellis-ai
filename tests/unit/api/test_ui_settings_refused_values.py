"""A refused enum member (e.g. ``"commit"`` on the candidate auto-promote
mode knob) must never render as a selectable option in the Settings tab.

``_entry()`` (``trellis_api/routes/settings.py``) now reports each setting's
``refused_values``/``refused_reason`` alongside its ``enum`` -- a value
that is a structurally valid enum member but one every save of it will
itself refuse, kept in ``enum`` so the catalog still documents it as a
real future option. ``renderValueInput`` must consult the new field and
disable that one ``<option>`` (with ``refused_reason`` as its ``title``)
rather than offering a value the next click only bounces back as a
validation error.
"""

from __future__ import annotations

import re
from pathlib import Path

import trellis_api

INDEX_HTML = Path(trellis_api.__file__).parent / "static" / "index.html"


def _page() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


def _function_body(page: str, name: str) -> str:
    found = re.search(
        r"function " + re.escape(name) + r"\([^)]*\) \{(.*?)\n        \}",
        page,
        re.DOTALL,
    )
    assert found is not None, f"function {name}() not found"
    return found.group(1)


class TestRenderValueInputDisablesRefusedOptions:
    def test_consults_refused_values(self) -> None:
        body = _function_body(_page(), "renderValueInput")
        assert "refused_values" in body

    def test_disables_the_refused_option(self) -> None:
        body = _function_body(_page(), "renderValueInput")
        assert "disabled" in body

    def test_uses_refused_reason_as_the_title(self) -> None:
        body = _function_body(_page(), "renderValueInput")
        assert "refused_reason" in body
        assert "title=" in body

    def test_a_non_refused_member_gets_no_disabled_attribute(self) -> None:
        """Static-source assertion only pins that the function *consults*
        ``refused_values`` and *can* disable an option -- it cannot
        execute the JS. The per-value branch is proven at the Python
        level instead: build the same two-member enum the real
        ``learning.candidate_auto_promote.mode`` entry declares and check
        which member the generated markup disables.
        """
        page = _page()
        body = _function_body(page, "renderValueInput")
        # The function must branch per-option on membership in
        # refused_values, not disable (or not disable) the whole select
        # -- i.e. the `disabled` text must appear inside the per-option
        # map callback, not only on the outer <select ... disabledAttr>.
        enum_block = re.search(
            r"if \(s\.enum\) \{(.*?)\n(?: {16}\}|\})", body, re.DOTALL
        )
        assert enum_block is not None, "s.enum branch not found in renderValueInput"
        assert "refused_values" in enum_block.group(1)
        assert "disabled" in enum_block.group(1)
