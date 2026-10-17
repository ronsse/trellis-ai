"""Direct coverage of ``_principal.py``'s claim handling (items 4 and 5 of
the #895 review fix-round): a DTO default must not be recorded as a claim,
and a recorded claim must be bounded and made safe for ``jsonb`` storage
before it reaches ``Command.metadata``.

Route-level tests (``test_mutations_authz_followups.py``,
``test_requested_by_claim_route_coverage.py``) prove the real routes call
through correctly; this file proves the two behaviours in isolation so a
future route can be checked against them without standing up a full app.
"""

from __future__ import annotations

from trellis_api.auth import AuthContext
from trellis_api.routes._principal import (
    _CLAIM_MAX_CHARS,
    _scrub_claim,
    resolve_requested_by,
)

_CTX_WITH_KEY = AuthContext(
    key_id="abc123", name=None, scopes=["mutate"], mode="required"
)
_CTX_WITH_NAME_ONLY = AuthContext(
    key_id=None, name="legacy-secret", scopes=["mutate"], mode="required"
)


# ---------------------------------------------------------------------------
# Item 4: a DTO default is not a claim
# ---------------------------------------------------------------------------


class TestClaimWasSetGating:
    def test_a_claim_the_body_actually_sent_is_recorded(self) -> None:
        requested_by, claim = resolve_requested_by(
            _CTX_WITH_KEY, "attacker-chosen-label", claim_was_set=True
        )
        assert requested_by == "api:abc123"
        assert claim == "attacker-chosen-label"

    def test_a_dto_default_the_body_never_set_is_not_recorded(self) -> None:
        # Same string as the "claim" above would carry, but claim_was_set
        # is False -- this is what every route passes when
        # "requested_by" not in req.model_fields_set, i.e. the DTO's own
        # default (e.g. "api:promote") rather than something the caller
        # wrote into the body.
        requested_by, claim = resolve_requested_by(
            _CTX_WITH_KEY, "api:promote", claim_was_set=False
        )
        assert requested_by == "api:abc123"
        assert claim is None

    def test_claim_was_set_defaults_to_true_for_backward_compatible_callers(
        self,
    ) -> None:
        # /commands/batch's DTO has no server-side default for
        # requested_by that would need gating historically, so callers
        # that don't pass claim_was_set keep the pre-existing behaviour.
        requested_by, claim = resolve_requested_by(_CTX_WITH_KEY, "someone-else")
        assert requested_by == "api:abc123"
        assert claim == "someone-else"

    def test_gating_applies_with_a_name_only_principal_too(self) -> None:
        requested_by, claim = resolve_requested_by(
            _CTX_WITH_NAME_ONLY, "api:settings", claim_was_set=False
        )
        assert requested_by == "api:legacy-secret"
        assert claim is None

    def test_with_no_principal_claim_was_set_is_irrelevant(self) -> None:
        from trellis.auth import ALL_SCOPES

        ctx = AuthContext(key_id=None, name=None, scopes=ALL_SCOPES, mode="off")
        requested_by, claim = resolve_requested_by(
            ctx, "anon-claim", claim_was_set=False
        )
        assert requested_by == "anon-claim"
        assert claim is None


# ---------------------------------------------------------------------------
# Item 5: the claim is bounded and scrubbed before it reaches metadata
# ---------------------------------------------------------------------------


class TestScrubClaim:
    def test_a_nul_byte_becomes_the_replacement_character(self) -> None:
        scrubbed = _scrub_claim("before\x00after")
        assert "\x00" not in scrubbed
        assert scrubbed == "before�after"

    def test_a_lone_surrogate_becomes_the_replacement_character(self) -> None:
        lone_high_surrogate = "before" + "\ud800" + "after"
        scrubbed = _scrub_claim(lone_high_surrogate)
        assert "\ud800" not in scrubbed
        assert scrubbed == "before�after"

    def test_a_10000_char_claim_is_truncated(self) -> None:
        claim = "x" * 10_000
        scrubbed = _scrub_claim(claim)
        assert len(scrubbed) < 10_000
        # elide_text's marker rides on top of the cap, so the kept prefix
        # is exactly _CLAIM_MAX_CHARS chars before the marker is appended.
        assert scrubbed.startswith("x" * _CLAIM_MAX_CHARS)
        assert not scrubbed.startswith("x" * (_CLAIM_MAX_CHARS + 1))

    def test_a_short_clean_claim_is_returned_unchanged(self) -> None:
        assert _scrub_claim("a normal claim") == "a normal claim"

    def test_resolve_requested_by_scrubs_a_recorded_claim(self) -> None:
        _requested_by, claim = resolve_requested_by(
            _CTX_WITH_KEY, "nul\x00here", claim_was_set=True
        )
        assert claim == "nul�here"

    def test_resolve_requested_by_does_not_scrub_a_claim_it_drops(self) -> None:
        # claim_was_set=False short-circuits before _scrub_claim runs at
        # all -- there is nothing to scrub because nothing is recorded.
        _requested_by, claim = resolve_requested_by(
            _CTX_WITH_KEY, "nul\x00here", claim_was_set=False
        )
        assert claim is None
