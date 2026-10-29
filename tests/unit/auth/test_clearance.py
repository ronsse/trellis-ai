"""Read clearance is derived from key scopes, never from a new key field (#194)."""

from __future__ import annotations

import pytest

from trellis.auth import (
    ALL_SCOPES,
    SCOPE_ADMIN,
    SCOPE_CONFIDENTIAL,
    SCOPE_READ,
    SCOPE_RESTRICTED,
    clearance_of,
    scopes_satisfy,
)


@pytest.mark.parametrize(
    ("scopes", "expected"),
    [
        (set(), "internal"),
        ({SCOPE_READ}, "internal"),
        ({SCOPE_READ, "ingest"}, "internal"),
        ({SCOPE_CONFIDENTIAL}, "confidential"),
        ({SCOPE_RESTRICTED}, "restricted"),
        ({SCOPE_ADMIN}, "restricted"),
        ({SCOPE_CONFIDENTIAL, SCOPE_RESTRICTED}, "restricted"),
    ],
)
def test_clearance_follows_scopes(scopes: set[str], expected: str) -> None:
    assert clearance_of(scopes) == expected


def test_the_new_scopes_are_known() -> None:
    assert {SCOPE_CONFIDENTIAL, SCOPE_RESTRICTED} <= ALL_SCOPES


def test_restricted_implies_confidential_but_not_the_reverse() -> None:
    assert scopes_satisfy({SCOPE_RESTRICTED}, SCOPE_CONFIDENTIAL)
    assert not scopes_satisfy({SCOPE_CONFIDENTIAL}, SCOPE_RESTRICTED)
    assert not scopes_satisfy({SCOPE_READ}, SCOPE_CONFIDENTIAL)
