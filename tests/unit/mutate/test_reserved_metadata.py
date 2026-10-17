"""Tests for :mod:`trellis.mutate.reserved_metadata`.

``reject_reserved_metadata`` is the shared guard every boundary that
copies caller-supplied metadata onto a ``Command`` must call before
merging in its own server-derived stamp. See the module's docstring for
which reads treat each reserved key as authoritative.
"""

from __future__ import annotations

import re

import pytest

import trellis.mutate.reserved_metadata as reserved_metadata_module
import trellis_api.routes.mutations as mutations_module
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


def _backtick_tokens(text: str) -> set[str]:
    return set(re.findall(r"``([a-z_]+)``", text))


def _enumerated_keys_after(text: str, anchor: str) -> set[str]:
    """Backtick-wrapped tokens inside the parenthesised list right after
    *anchor* -- the shape both of ``mutations.py``'s prose blocks use for
    their one-line enumeration of the reserved keys.

    *anchor*'s own words may themselves be wrapped onto separate source
    lines (docstrings and the 400 ``description`` tuple-of-literals both
    reflow at whatever column the prose happens to hit), so the words are
    matched with ``\\s+`` between them rather than the literal substring.
    """
    anchor_pattern = r"\s+".join(re.escape(word) for word in anchor.split())
    match = re.search(rf"{anchor_pattern}\s*\(([^)]*)\)", text)
    assert match, f"could not find {anchor!r} in:\n{text}"
    return _backtick_tokens(match.group(1))


def _bulleted_keys(text: str) -> set[str]:
    """One key (or a key/key pair) per leading ``* ``key`` -- ...`` bullet
    line -- the shape ``reserved_metadata.py``'s own module docstring uses,
    one reserved key (or pair) documented per bullet rather than in a
    single inline list."""
    keys: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("* ") and "--" in stripped:
            head = stripped.split("--", 1)[0]
            keys |= _backtick_tokens(head)
    return keys


class TestReservedKeyDocstringsNameExactlyTheFrozenset:
    """``RESERVED_COMMAND_METADATA_KEYS`` cannot generate this prose, so
    nothing keeps it in sync by construction. These are the three places
    that stay hand-written on purpose (the route's OpenAPI-facing 400
    description, its function docstring, and this module's own docstring)
    -- each must name every key in the frozenset, and no key outside it,
    or a future change to the set silently stops matching what an agent
    reading the docs, or the generated OpenAPI spec, is told.
    """

    def test_route_responses_400_description(self) -> None:
        route = next(
            r for r in mutations_module.router.routes if r.path == "/commands/batch"
        )
        description = route.responses[400]["description"]
        assert (
            _enumerated_keys_after(description, "server-side audit stamping")
            == RESERVED_COMMAND_METADATA_KEYS
        )

    def test_route_function_docstring(self) -> None:
        doc = mutations_module.execute_batch.__doc__
        assert doc is not None
        assert (
            _enumerated_keys_after(doc, "server-side audit stamping")
            == RESERVED_COMMAND_METADATA_KEYS
        )

    def test_reserved_metadata_module_docstring(self) -> None:
        doc = reserved_metadata_module.__doc__
        assert doc is not None
        assert _bulleted_keys(doc) == RESERVED_COMMAND_METADATA_KEYS
