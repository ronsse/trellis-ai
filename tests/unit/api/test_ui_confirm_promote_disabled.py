"""The Review queue's tuner-proposal Confirm step must not offer a click
that only wastes the proposal.

``confirmPromoteProposal()`` (``index.html``) shows the dry-run preview
before swapping the Approve button into a "Confirm approve" action. Before
this fix that button was always rendered enabled, including when the
preview predicted a rejection. Confirming on a *reachable* no-baseline
proposal rejected it terminally (pre-``terminal``-flag behaviour), after
which ``promote_proposal(force=True)`` answered ``skipped
proposal_already_rejected`` from every surface — unrecoverable (#823's
gate finding, the "approve-button trap"). These tests pin that the
button is disabled whenever the preview does not predict a promotion, and
that a no-baseline refusal additionally renders the CLI bootstrap hint.
"""

from __future__ import annotations

import re
from pathlib import Path

import trellis_api

INDEX_HTML = Path(trellis_api.__file__).parent / "static" / "index.html"


def _load_confirm_promote_proposal_body(page: str) -> str:
    found = re.search(
        r"async function confirmPromoteProposal\(id\) \{(.*?)"
        r"async function promoteProposal\(id\) \{",
        page,
        re.DOTALL,
    )
    assert found is not None, "confirmPromoteProposal() not found"
    return found.group(1)


def test_confirm_button_is_disabled_on_predicted_rejection() -> None:
    body = _load_confirm_promote_proposal_body(INDEX_HTML.read_text(encoding="utf-8"))

    # The predicted-outcome flag the rest of the function already computes.
    assert "const ok = pv.predicted_status === 'promoted';" in body

    actions = re.search(r"actions\.innerHTML =\s*(.*?);\n", body, re.DOTALL)
    assert actions is not None, "actions.innerHTML assignment not found"
    clause = actions.group(1)

    # The button markup is built from a ternary on `ok`: the enabled arm
    # is empty, the disabled arm adds the `disabled` attribute. Anything
    # else (a `removed` button, an `if` that skips rendering it) would
    # not pin "disabled", specifically, the way the plan asked for.
    assert re.search(r"\(ok \? '' : ' disabled", clause), (
        "Confirm approve button markup is not gated on `ok` with a "
        "disabled-attribute ternary"
    )
    # The disabled arm must land adjacent to the actual Confirm-approve
    # button tag, not merely somewhere in the clause — a mutant that moves
    # the ternary onto the Cancel button instead leaves every substring
    # above present while always enabling Confirm.
    assert re.search(
        r'data-action="promoteProposal"[^`]*`\s*\+\s*'
        r"\(ok \? '' : ' disabled[^)]*\)\s*\+\s*"
        r"`>Confirm approve</button>",
        clause,
    ), "disabled ternary is not adjacent to the Confirm approve button tag"


def test_no_baseline_rejection_shows_the_cli_bootstrap_hint() -> None:
    body = _load_confirm_promote_proposal_body(INDEX_HTML.read_text(encoding="utf-8"))

    # Detects the bootstrap-shaped refusal by its reason prefix, matching
    # the two `_apply_policy` reasons promotion.py's `_NO_BASELINE_REASON_PREFIX`
    # covers: no snapshot at all, and a snapshot missing the proposed keys.
    assert "pv.reason.startsWith('no_baseline_')" in body

    # The hint names the exact remedy the plan specified, not a vaguer
    # restatement that an operator would have to decode on their own.
    assert "trellis metrics promote --allow-no-baseline" in body


def test_predict_panel_still_shows_predicted_status_and_reason() -> None:
    """Regression guard: the new disabled-button/hint logic must not have
    replaced the existing prediction text the operator reads first."""
    body = _load_confirm_promote_proposal_body(INDEX_HTML.read_text(encoding="utf-8"))
    assert "Predicted: ${escHtml(pv.predicted_status)}" in body
    assert "${escHtml(pv.reason)}" in body
