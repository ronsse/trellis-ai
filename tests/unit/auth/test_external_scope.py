"""The ``external`` scope: a key that reads only what someone labelled ``public``.

It is a scope of its own, not an entry in ``ALL_SCOPES``: that set is also what
every trusted-local context (auth off, no credential on an optional surface) is
granted, and none of those may read as an external caller.
"""

from __future__ import annotations

import pytest

from trellis.auth import (
    ALL_SCOPES,
    KEY_SCOPES,
    SCOPE_ADMIN,
    SCOPE_CONFIDENTIAL,
    SCOPE_EXTERNAL,
    SCOPE_INGEST,
    SCOPE_MUTATE,
    SCOPE_READ,
    SCOPE_RESTRICTED,
    clearance_of,
    external_scope_refusal,
    generate_api_key,
    scopes_satisfy,
)
from trellis.retrieve.sensitivity import is_public_only, permits


def test_external_is_a_key_scope_but_not_a_trusted_local_scope() -> None:
    assert SCOPE_EXTERNAL == "external"
    assert SCOPE_EXTERNAL in KEY_SCOPES
    assert SCOPE_EXTERNAL not in ALL_SCOPES
    assert ALL_SCOPES < KEY_SCOPES


def test_an_external_key_reads_at_the_new_lowest_rung() -> None:
    assert clearance_of({SCOPE_READ, SCOPE_EXTERNAL}) == "public"


@pytest.mark.parametrize(
    "scopes",
    [
        {SCOPE_READ},
        {SCOPE_READ, SCOPE_CONFIDENTIAL},
        {SCOPE_READ, SCOPE_RESTRICTED},
        {SCOPE_ADMIN},
        set(ALL_SCOPES),
        set(),
    ],
)
def test_no_other_key_reads_public(scopes: set[str]) -> None:
    assert clearance_of(scopes) != "public"


def test_the_stored_lowest_clearance_wins_over_a_higher_one() -> None:
    """A key that somehow holds both is the narrower of the two, never the wider."""
    stored = {SCOPE_READ, SCOPE_EXTERNAL, SCOPE_RESTRICTED}
    assert clearance_of(stored) == "public"
    assert clearance_of(stored | {SCOPE_ADMIN}) == "public"


def test_external_satisfies_read_and_nothing_else() -> None:
    granted = {SCOPE_READ, SCOPE_EXTERNAL}
    assert scopes_satisfy(granted, SCOPE_READ)
    for needed in (SCOPE_INGEST, SCOPE_MUTATE, SCOPE_ADMIN):
        assert not scopes_satisfy(granted, needed)


def test_a_stored_admin_beside_external_does_not_satisfy_a_write() -> None:
    granted = {SCOPE_READ, SCOPE_EXTERNAL, SCOPE_ADMIN, SCOPE_MUTATE, SCOPE_INGEST}
    assert scopes_satisfy(granted, SCOPE_READ)
    for needed in (SCOPE_INGEST, SCOPE_MUTATE, SCOPE_ADMIN):
        assert not scopes_satisfy(granted, needed)


def test_a_read_key_still_satisfies_read_and_admin_still_everything() -> None:
    assert scopes_satisfy({SCOPE_READ}, SCOPE_READ)
    assert scopes_satisfy({SCOPE_ADMIN}, SCOPE_MUTATE)


@pytest.mark.parametrize("spelling", ["External", "EXTERNAL", " external", "external "])
def test_a_stored_external_scope_fails_closed_whatever_its_case(spelling: str) -> None:
    """A hand-edited or imported record reading ``read,External`` is still the
    narrow key: a mis-cased narrowing scope must not read as a plain read key."""
    granted = frozenset({SCOPE_READ, spelling})
    assert clearance_of(granted) == "public"
    assert is_public_only(clearance_of(granted))
    assert scopes_satisfy(granted, SCOPE_READ)
    for needed in (SCOPE_INGEST, SCOPE_MUTATE, SCOPE_ADMIN):
        assert not scopes_satisfy(granted, needed)


def test_a_stored_mis_cased_external_beside_admin_is_still_read_only() -> None:
    granted = frozenset({SCOPE_READ, "External", SCOPE_ADMIN})
    assert clearance_of(granted) == "public"
    assert not scopes_satisfy(granted, SCOPE_ADMIN)
    assert not scopes_satisfy(granted, SCOPE_MUTATE)


def test_other_scopes_are_still_matched_exactly() -> None:
    """Only ``external`` is normalised: ``Admin`` is not ``admin``."""
    assert clearance_of({"Admin"}) == "internal"
    assert clearance_of({SCOPE_READ, "Restricted"}) == "internal"
    assert not scopes_satisfy(frozenset({"Admin"}), SCOPE_MUTATE)


def test_the_only_legal_spelling_is_read_and_external() -> None:
    assert external_scope_refusal({SCOPE_READ, SCOPE_EXTERNAL}) is None
    assert external_scope_refusal({SCOPE_READ}) is None
    assert external_scope_refusal(set(ALL_SCOPES)) is None


@pytest.mark.parametrize(
    "scopes",
    [
        {SCOPE_EXTERNAL},
        {SCOPE_READ, SCOPE_EXTERNAL, SCOPE_ADMIN},
        {SCOPE_READ, SCOPE_EXTERNAL, SCOPE_INGEST},
        {SCOPE_READ, SCOPE_EXTERNAL, SCOPE_MUTATE},
        {SCOPE_READ, SCOPE_EXTERNAL, SCOPE_CONFIDENTIAL},
        {SCOPE_READ, SCOPE_EXTERNAL, SCOPE_RESTRICTED},
    ],
)
def test_minting_external_beside_anything_else_is_refused_with_a_reason(
    scopes: set[str],
) -> None:
    reason = external_scope_refusal(scopes)
    assert reason is not None
    assert "read,external" in reason
    with pytest.raises(ValueError, match="read,external"):
        generate_api_key("partner", scopes)


def test_a_legal_external_key_mints_and_round_trips_its_scopes() -> None:
    _token, record = generate_api_key("partner", {SCOPE_READ, SCOPE_EXTERNAL})
    assert set(record.scopes) == {SCOPE_READ, SCOPE_EXTERNAL}


def test_an_unknown_scope_is_still_refused() -> None:
    with pytest.raises(ValueError, match="scope"):
        generate_api_key("partner", {SCOPE_READ, "externl"})


class TestPublicRung:
    def test_a_public_clearance_reads_only_public_labels(self) -> None:
        assert permits("public", "public")
        for above in ("internal", "confidential", "restricted"):
            assert not permits("public", above)  # type: ignore[arg-type]

    @pytest.mark.parametrize("unknown", ["", "PUBLIC", "external", "root", None, 3])
    def test_an_unknown_clearance_never_ranks_above_public(
        self, unknown: object
    ) -> None:
        assert is_public_only(unknown)  # type: ignore[arg-type]
        assert permits(unknown, "public")  # type: ignore[arg-type]
        assert not permits(unknown, "internal")  # type: ignore[arg-type]

    def test_a_read_keys_view_of_a_public_document_is_unchanged(self) -> None:
        for held in ("internal", "confidential", "restricted"):
            assert permits(held, "public")  # type: ignore[arg-type]
            assert not is_public_only(held)  # type: ignore[arg-type]
