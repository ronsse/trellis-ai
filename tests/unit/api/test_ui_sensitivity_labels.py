"""The Memories views show a document's sensitivity label and relabel it.

``POST /documents/{id}/sensitivity`` existed with no way to reach it from the
page, and the page showed no label although labels decide what each key may
read. These are SPELLING pins, like the neighbouring ``test_ui_*`` files: they
read ``index.html`` as text and fail when a line they name is reworded or
removed. They do not run the page, and a change that keeps the spelling and
breaks the behaviour (a confirmation that never fires, an escape that is
dropped, a handler that does nothing) passes them. The behaviour was checked by
hand in a real browser against a running server, per the pull request. What the
pins hold: the label comes from the server's ``label`` block and never from
``metadata`` (a public-only key's metadata carries none), the wording states
what each label means for an external key, and the relabel code names the
checks it makes.
"""

from __future__ import annotations

import re
from pathlib import Path

import trellis_api
from trellis.schemas.classification import SENSITIVITY_ORDER

INDEX_HTML = Path(trellis_api.__file__).parent / "static" / "index.html"


def _page() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


def _function(page: str, name: str) -> str:
    found = re.search(
        rf"(?:async )?function {name}\(.*?\n        \}}\n", page, re.DOTALL
    )
    assert found is not None, f"function {name} not found in index.html"
    return found.group(0)


def _js_list(page: str, name: str) -> list[str]:
    found = re.search(rf"const {name} = \[([^\]]*)\];", page)
    assert found is not None, f"{name} not found"
    return [v.strip().strip("'\"") for v in found.group(1).split(",")]


def _js_object_keys(page: str, name: str) -> list[str]:
    found = re.search(rf"const {name} = \{{(.*?)\n        \}};", page, re.DOTALL)
    assert found is not None, f"{name} not found"
    return re.findall(r"^\s{12}(\w+):", found.group(1), re.MULTILINE)


def test_the_vocabulary_is_the_servers_in_the_servers_order() -> None:
    page = _page()
    assert tuple(_js_list(page, "SENSITIVITY_LEVELS")) == SENSITIVITY_ORDER
    for table in ("SENSITIVITY_READERS", "SENSITIVITY_BADGE"):
        assert _js_object_keys(page, table) == list(SENSITIVITY_ORDER), table


def test_the_page_reads_the_label_from_the_label_block_not_from_metadata() -> None:
    page = _page()
    # A public-only key's metadata is cut to a title and a form, so a page that
    # read the classification out of it would call every row that key reads
    # unlabelled.
    assert "data_classification" not in page
    state = _function(page, "labelState")
    assert "label.stored" in state
    assert "label.external_readable" in state
    assert "label.corrupt" in state
    assert "metadata" not in state


def test_an_unlabelled_document_reads_as_internal_and_not_for_external_keys() -> None:
    state = _function(_page(), "labelState")
    assert "'unlabelled (internal)'" in state
    unlabelled = state.split("if (!label.stored && s === 'internal')", 1)[1]
    unlabelled = unlabelled.split("}\n", 1)[0]
    assert "reads as internal" in unlabelled
    assert "external key" in unlabelled


def test_a_row_with_no_label_of_its_own_is_worded_from_its_effective_label() -> None:
    state = _function(_page(), "labelState")
    # A chunk of a restricted document has no stored label and reads restricted:
    # only the default ``internal`` is "unlabelled", anything higher is named.
    assert "if (!label.stored && s === 'internal')" in state
    inherited = state.split("if (!label.stored) {", 1)[1].split("}\n", 1)[0]
    assert "its parent document's label" in inherited
    assert "unlabelled" not in inherited


def test_a_public_record_an_external_key_cannot_read_promises_no_relabel() -> None:
    state = _function(_page(), "labelState")
    title = state.split("if (s === 'public' && !label.external_readable)", 1)[1]
    title = title.split("return {", 1)[1].split("};", 1)[0]
    # The server refuses a relabel that would discard tags or other fields, so
    # the wording does not say "relabel it public".
    assert "Relabel it public" not in title
    assert "the server then refuses and names them" in title


def test_a_corrupt_record_says_so_and_is_not_a_no_op_label() -> None:
    page = _page()
    state = _function(page, "labelState")
    assert "'corrupt label (reads as restricted)'" in state
    begin = _function(page, "beginRelabelDocument")
    assert "label.corrupt && target === 'restricted'" in begin
    assert "!label.corrupt && label.sensitivity === target" in begin


def test_the_control_is_off_when_the_server_reported_no_label() -> None:
    control = _function(_page(), "relabelControlHtml")
    assert "const usable = !!labelState(d.label, d.doc_id);" in control
    assert "${off}" in control
    assert "' disabled'" in control


def test_a_late_relabel_does_not_touch_a_pane_opened_since() -> None:
    page = _page()
    relabel = _function(page, "relabelDocument")
    assert "const stillOpen = () => memDetailWanted === docId;" in relabel
    assert relabel.count("stillOpen()") >= 3
    detail = _function(page, "loadMemoryDetail")
    assert "memDetailWanted = docId;" in detail
    assert "if (memDetailWanted !== docId) return false;" in detail


def test_a_failed_reload_after_a_relabel_says_the_relabel_applied() -> None:
    page = _page()
    relabel = _function(page, "relabelDocument")
    assert "const loaded = await loadMemoryDetail(docId);" in relabel
    assert "but reloading the document failed" in relabel
    assert "refreshMemoryRowLabel(docId," in relabel
    detail = _function(page, "loadMemoryDetail")
    assert detail.count("return false;") >= 3
    assert "return true;" in detail


def test_the_list_and_the_detail_both_show_the_badge() -> None:
    page = _page()
    assert '<th scope="col">Label</th>' in page
    assert 'mem-label-cell">${sensitivityBadge(d.label, d.doc_id)}' in page
    detail = _function(page, "loadMemoryDetail")
    assert "sensitivityBadge(d.label, d.doc_id)" in detail
    assert "relabelControlHtml(d)" in detail
    # Both loaders count the Label column in their full-width message rows.
    loader = _function(page, "loadMemories")
    assert 'colspan="5"' not in loader
    assert loader.count('colspan="6"') == 3


def test_relabel_posts_to_the_existing_route_and_shows_the_servers_refusal() -> None:
    relabel = _function(_page(), "relabelDocument")
    assert (
        "postJSON(`/documents/${encodeURIComponent(docId)}/sensitivity`, "
        "{ sensitivity: target })"
    ) in relabel
    # The refusal text is the server's, escaped, and the control comes back.
    assert "escHtml(err.message)" in relabel
    assert "again.disabled = false" in relabel


def test_lowering_and_opening_to_external_keys_ask_first() -> None:
    begin = _function(_page(), "beginRelabelDocument")
    lower = "SENSITIVITY_LEVELS.indexOf(target) < SENSITIVITY_LEVELS.indexOf("
    assert lower + "label.sensitivity)" in begin
    assert "opensToExternal" in begin
    assert "openConfirmDialog({" in begin
    # What changes is named: who can read it after, and who could before.
    assert "After: readable by" in begin
    assert "Before: readable by" in begin
    # Raising a label applies without the dialog, so the only unconditional
    # call is the one guarded by "neither".
    assert (
        "if (!lowering && !opensToExternal) { relabelDocument(docId, target); return; }"
        in begin
    )


def test_the_relabel_action_goes_through_the_id_action_table() -> None:
    page = _page()
    table = re.search(r"const ID_ACTIONS = \{(.*?)\n        \};", page, re.DOTALL)
    assert table is not None
    assert "beginRelabelDocument" in table.group(1)
    assert 'data-action="beginRelabelDocument" data-id="${escHtml(d.doc_id)}"' in page


def test_a_chunk_gets_no_relabel_control() -> None:
    control = _function(_page(), "relabelControlHtml")
    assert "CHUNK_DOC_ID.test" in control
    assert "reads its parent document" in control
