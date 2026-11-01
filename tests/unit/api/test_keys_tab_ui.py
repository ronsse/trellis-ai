"""The Keys tab: static checks on ``index.html``, like the other tab tests."""

from __future__ import annotations

import re
from pathlib import Path

import trellis_api

PAGE = (Path(trellis_api.__file__).parent / "static" / "index.html").read_text(
    encoding="utf-8"
)


def _body(name: str) -> str:
    found = re.search(
        r"function " + re.escape(name) + r"\([^)]*\) \{(.*?)\n        \}",
        PAGE,
        re.DOTALL,
    )
    assert found is not None, f"{name}() not found"
    return found.group(1)


def test_the_tab_is_in_the_nav_and_marked_admin_only() -> None:
    assert 'data-view="keys" data-label="Keys"' in PAGE
    assert 'id="view-keys"' in PAGE
    assert re.search(r"ADMIN_ONLY_VIEWS = \[[^\]]*'keys'", PAGE)
    assert "if (name === 'keys') loadKeys();" in PAGE


def test_the_one_time_token_notice_says_it_will_not_be_shown_again() -> None:
    assert "You will not see this token again" in _body("mintApiKey")


def test_every_server_value_is_escaped_and_ids_ride_data_attributes() -> None:
    row = _body("renderKeyRow")
    for field in ("k.key_id", "k.name", "k.created_at"):
        assert f"escHtml({field})" in row
    assert "escHtml(sc)" in row
    assert 'data-action="confirmRevokeApiKey"' in row
    assert "onclick" not in row
    assert "confirmRevokeApiKey" in PAGE.split("const ID_ACTIONS = {")[1].split("};")[0]
    mint = _body("mintApiKey")
    assert "escHtml(out.token)" in mint
    assert "escHtml(out.key_id)" in mint
    assert "escHtml(out.name)" in mint


def test_revoke_asks_for_a_typed_confirmation() -> None:
    body = _body("confirmRevokeApiKey")
    assert "openConfirmDialog" in body
    assert "confirmWord: 'revoke'" in body


def test_a_403_dims_to_a_note_and_hides_the_mint_form() -> None:
    body = _body("loadKeys")
    assert "isAdminScopeError(err)" in body
    assert "loadFailureText(err, 'API keys')" in body
    assert "mintPanel.style.display = 'none'" in body


def test_the_token_is_never_written_into_an_attribute() -> None:
    mint = _body("mintApiKey")
    assert not re.search(r'="[^"]*\$\{[^}]*token[^}]*\}"', mint)


def test_a_revoke_button_carries_the_id_escaped_in_data_id() -> None:
    row = _body("renderKeyRow")
    assert 'data-action="confirmRevokeApiKey" data-id="${escHtml(k.key_id)}"' in row


def test_dismissing_the_token_drops_the_value_and_empties_the_panel() -> None:
    body = _body("dismissMintedKey")
    assert "mintedToken = null;" in body
    assert "getElementById('keys-token-panel').innerHTML = ''" in body


def test_a_failed_mint_or_revoke_re_lists_so_no_stale_live_row_stays() -> None:
    mint_failure = _body("mintApiKey").split("catch (err)")[1]
    assert "await loadKeys();" in mint_failure
    revoke = _body("revokeApiKey")
    assert revoke.index("await loadKeys();") > revoke.index("catch (err)")


def test_a_refusal_is_explained_rather_than_shown_bare() -> None:
    text = _body("keyWriteFailureText")
    assert "identified admin API key" in text
    assert "trellis admin api-keys" in text
    assert 'id="keys-identity-note"' in PAGE
    assert "keys-identity-note').hidden = !!getApiKey()" in _body("loadKeys")
