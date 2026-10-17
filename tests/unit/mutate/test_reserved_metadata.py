"""Tests for :mod:`trellis.mutate.reserved_metadata`.

``reject_reserved_metadata`` is the shared guard every boundary that
copies caller-supplied metadata onto a ``Command`` must call before
merging in its own server-derived stamp. See the module's docstring for
which reads treat each reserved key as authoritative.
"""

from __future__ import annotations

import pytest

from trellis.mutate.reserved_metadata import (
    RESERVED_COMMAND_METADATA_KEYS,
    ReservedMetadataKeyError,
    reject_reserved_metadata,
)


class TestEachReservedKeyIsRefused:
    @pytest.mark.parametrize("key", sorted(RESERVED_COMMAND_METADATA_KEYS))
    def test_a_planted_key_raises_naming_itself(self, key: str) -> None:
        with pytest.raises(ReservedMetadataKeyError) as excinfo:
            reject_reserved_metadata({key: "forged-value"})
        assert excinfo.value.key == key
        assert key in str(excinfo.value)

    def test_the_reserved_set_is_exactly_the_five_documented_keys(self) -> None:
        # Pinned so a future addition or removal is a deliberate edit to
        # this test, not a silent drift in the guard's coverage.
        assert (
            frozenset(
                {
                    "requested_by_claim",
                    "key_id",
                    "key_name",
                    "write_provenance",
                    "handler_warnings",
                }
            )
            == RESERVED_COMMAND_METADATA_KEYS
        )

    def test_a_reserved_key_among_other_decoration_still_raises(self) -> None:
        with pytest.raises(ReservedMetadataKeyError) as excinfo:
            reject_reserved_metadata({"note": "ordinary", "key_id": "forged"})
        assert excinfo.value.key == "key_id"


class TestOrdinaryDecorationPassesThrough:
    def test_empty_metadata_passes(self) -> None:
        assert reject_reserved_metadata({}) == {}

    def test_ordinary_decoration_is_returned_unchanged(self) -> None:
        decoration = {"note": "operator context", "source": "cli"}
        assert reject_reserved_metadata(decoration) == decoration

    def test_the_return_value_is_a_copy_not_the_same_object(self) -> None:
        decoration = {"note": "operator context"}
        result = reject_reserved_metadata(decoration)
        assert result == decoration
        assert result is not decoration
        result["note"] = "mutated after the fact"
        assert decoration["note"] == "operator context"


class TestGuardOrderingSupportsServerSideStamping:
    """The guard runs on caller input *before* the server merges its own
    stamp -- never on the already-merged dict, which would reject the
    server's own legitimate ``requested_by_claim``/``key_id``/``key_name``.
    This is the ordering ``trellis_api.routes.mutations._build_command``
    relies on."""

    def test_guard_then_merge_lets_a_server_stamp_through(self) -> None:
        caller_metadata = {"note": "decoration"}
        checked = reject_reserved_metadata(caller_metadata)
        checked["requested_by_claim"] = "api:key-123"  # the server's own stamp
        assert checked == {
            "note": "decoration",
            "requested_by_claim": "api:key-123",
        }

    def test_merge_then_guard_would_wrongly_reject_the_servers_own_stamp(self) -> None:
        """Documents the trap the ordering avoids: applying the guard
        *after* the merge rejects the server's own legitimate key. This
        is not the code path any boundary takes -- it exists to make the
        ordering requirement observable in a test, not just in prose."""
        caller_metadata = {"note": "decoration"}
        already_merged = {**caller_metadata, "requested_by_claim": "api:key-123"}
        with pytest.raises(ReservedMetadataKeyError):
            reject_reserved_metadata(already_merged)
