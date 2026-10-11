"""The Packs detail view can submit pack feedback, not just display it.

``POST /packs/{pack_id}/feedback`` (``trellis_api.routes.curate.pack_feedback``)
was the only learning-loop input the dashboard could add, and it never called
it: ``loadPackDetail`` rendered ``injected_items`` and existing feedback rows
but had no form to submit a new one. This pins the load-bearing pieces of
the form added to close that gap:

- a Helpful/Unhelpful checkbox per injected-item row, keyed by an opaque
  ``item_id`` carried in ``data-item-id`` (never spliced into a selector or
  an inline handler, matching the house rule in ``test_ui_inline_handlers``
  and ``test_ui_attribute_escaping``);
- checking one verdict for a row clears the other, resolved through the row
  the checkbox sits in rather than a selector built from the item's id;
- the submitted payload names ``success``/``comment``/``helpful_item_ids``/
  ``unhelpful_item_ids`` and never invents a ``rating`` -- the REST/MCP
  surfaces already derive it from ``success`` when omitted
  (``PackFeedbackRequest.rating``);
- a failed submit shows the error and re-enables the buttons instead of
  resetting the form silently (the #849 "absence must not read as success"
  standard);
- a successful submit reports ``event_log_in_sync`` and re-loads the pack so
  the new row and the Feedback count reflect it.
"""

from __future__ import annotations

import re
from pathlib import Path

import trellis_api

INDEX_HTML = Path(trellis_api.__file__).parent / "static" / "index.html"


def _load_pack_detail_body(page: str) -> str:
    found = re.search(
        r"async function loadPackDetail\(packId\) \{\n(.*?)\n        \}\n",
        page,
        re.DOTALL,
    )
    assert found is not None, "loadPackDetail not found in index.html"
    return found.group(1)


def _submit_pack_feedback_body(page: str) -> str:
    found = re.search(
        r"async function submitPackFeedback\(success\) \{\n(.*?)\n                \}\n",
        page,
        re.DOTALL,
    )
    assert found is not None, "submitPackFeedback not found in index.html"
    return found.group(1)


def test_each_injected_row_carries_a_helpful_and_unhelpful_checkbox() -> None:
    body = _load_pack_detail_body(INDEX_HTML.read_text(encoding="utf-8"))
    assert 'class="fb-helpful-cb" data-item-id="${escHtml(it.item_id)}"' in body
    assert 'class="fb-unhelpful-cb" data-item-id="${escHtml(it.item_id)}"' in body


def test_checking_one_verdict_clears_the_other_via_the_row_not_a_selector() -> None:
    body = _load_pack_detail_body(INDEX_HTML.read_text(encoding="utf-8"))
    # The pairing goes through the row the checkbox lives in. A selector
    # built from the item's own id (e.g. a template literal splicing
    # it.item_id into querySelector) would be the exact mistake
    # test_ui_inline_handlers.py exists to catch one level up (inline
    # handlers) -- this is the same hazard one level down (CSS selectors).
    assert "cb.closest('tr')" in body
    assert "other.checked = false" in body
    assert re.search(r"querySelector\(`[^`]*\$\{", body) is None, (
        "a checkbox pairing selector interpolates a value instead of "
        "resolving through the row"
    )


def test_submit_payload_has_the_contract_fields_and_no_invented_rating() -> None:
    body = _submit_pack_feedback_body(INDEX_HTML.read_text(encoding="utf-8"))
    post_call = re.search(
        r"postJSON\(`/packs/\$\{encodeURIComponent\(packId\)\}/feedback`, \{(.*?)\}\);",
        body,
        re.DOTALL,
    )
    assert post_call is not None, "no postJSON call to the pack feedback route"
    payload = post_call.group(1)
    required_fields = (
        "success",
        "comment: comment || null",
        "helpful_item_ids",
        "unhelpful_item_ids",
    )
    for field in required_fields:
        assert field in payload, f"payload is missing {field!r}:\n{payload}"
    # rating is derived server-side from success when omitted
    # (PackFeedbackRequest.rating) -- the dashboard must not re-derive it.
    assert not re.search(r"\brating\s*:", payload), f"payload sets rating:\n{payload}"


def test_failed_submit_shows_the_error_and_reenables_the_buttons() -> None:
    body = _submit_pack_feedback_body(INDEX_HTML.read_text(encoding="utf-8"))
    catch_block = re.search(r"\} catch \(err\) \{\n(.*?)\n\s*\}\Z", body, re.DOTALL)
    assert catch_block is not None, "submitPackFeedback has no catch block"
    catch_body = catch_block.group(1)
    assert "escHtml(err.message)" in catch_body
    assert "b.disabled = false" in catch_body
    # The failure path must not quietly reload the pack as if nothing
    # happened -- that would be the #849 "absence reads as success" bug.
    assert "loadPackDetail(packId)" not in catch_body


def test_buttons_disable_before_the_request_and_success_reports_sync_state() -> None:
    body = _submit_pack_feedback_body(INDEX_HTML.read_text(encoding="utf-8"))
    disable_idx = body.index("b.disabled = true")
    post_idx = body.index("await postJSON(")
    assert disable_idx < post_idx, "buttons are not disabled before the request fires"
    assert "resp.event_log_in_sync" in body
    assert "await loadPackDetail(packId)" in body


def test_feedback_card_shows_capped_total_when_truncated() -> None:
    # feedback.length is always <=50 (GET /packs/{pack_id}'s display cap)
    # and used to be the whole card -- reading as "that's everything" even
    # when far more feedback exists. The card must branch on the route's
    # new feedback_total/feedback_truncated fields (P1-9) instead of the
    # raw array length alone.
    body = _load_pack_detail_body(INDEX_HTML.read_text(encoding="utf-8"))
    assert "data.feedback_truncated" in body
    assert "data.feedback_total" in body
    assert (
        '<div class="value">${feedback.length}</div>'
        '<div class="sub">signals recorded</div>' not in body
    )


def test_pack_feedback_form_is_wired_through_a_local_data_act_dispatch() -> None:
    # Scoped to this view's own container, not the global data-action
    # registry (ID_ACTIONS) that test_ui_inline_handlers.py pins for
    # id-carrying buttons -- these two buttons carry no id at all, so they
    # follow the graph node-detail panel's local `data-act` precedent
    # instead.
    page = INDEX_HTML.read_text(encoding="utf-8")
    assert 'data-act="submitPackFeedbackHelpful"' in page
    assert 'data-act="submitPackFeedbackUnhelpful"' in page
    assert "submitPackFeedbackHelpful: () => submitPackFeedback(true)" in page
    assert "submitPackFeedbackUnhelpful: () => submitPackFeedback(false)" in page
