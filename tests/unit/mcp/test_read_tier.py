"""The read-key tool tier: a read-only key lists three tools, others keep all theirs.

``trellis_scope(SCOPE_READ, advanced=True)`` hides a tool from, and refuses it
to, a credential that holds no ingest, mutate or admin scope. FastMCP filters
``tools/list`` and gates calls on that one check, so the listing and the call
cannot disagree. The oracle for every other key is the scope rule that existed
before the tier (``scopes_satisfy``), so a tier that leaked into a writing key's
list fails here.
"""

from __future__ import annotations

from itertools import combinations
from unittest.mock import MagicMock

import pytest
from fastmcp.server.auth import AccessToken, AuthContext

from tests.unit.mcp.test_auth import EXPECTED_TOOL_SCOPES, READ_TIER
from trellis.auth import (
    KEY_SCOPES,
    SCOPE_ADMIN,
    SCOPE_INGEST,
    SCOPE_MUTATE,
    SCOPE_READ,
    is_read_only_key,
    scopes_satisfy,
)
from trellis.mcp.auth import set_auth_enforced, trellis_scope
from trellis.mcp.server import mcp

#: The read-scoped tools a read-only key no longer lists. Naming them keeps the
#: tier a decision: moving a tool in or out of it changes this set on purpose.
ADVANCED_READ_TOOLS = {
    "get_graph",
    "get_lessons",
    "get_file_context",
    "query_observations",
    "get_objective_context",
    "get_task_context",
    "get_sectioned_context",
}


def _ctx(*scopes: str) -> AuthContext:
    return AuthContext(
        token=AccessToken(
            token="opaque",  # noqa: S106 -- placeholder, never read
            client_id="key-id",
            scopes=list(scopes),
            expires_at=None,
        ),
        component=MagicMock(),
    )


async def _tools() -> dict[str, object]:
    set_auth_enforced(enforced=False)
    try:
        return {t.name: t for t in await mcp.list_tools()}
    finally:
        set_auth_enforced(enforced=True)


def _visible(tools: dict[str, object], *scopes: str) -> set[str]:
    ctx = _ctx(*scopes)
    return {n for n, t in tools.items() if t.auth(ctx)}  # type: ignore[attr-defined]


def _writing_scope_sets() -> list[tuple[str, ...]]:
    """Every combination of the grantable scopes that is not a read-only key."""
    grantable = [s for s in KEY_SCOPES if s != "external"]
    out: list[tuple[str, ...]] = []
    for size in range(1, len(grantable) + 1):
        out.extend(combinations(grantable, size))
    return [scopes for scopes in out if not is_read_only_key(scopes)]


class TestReadOnlyKeys:
    @pytest.mark.parametrize(
        "scopes",
        [
            (SCOPE_READ,),
            (SCOPE_READ, "confidential"),
            (SCOPE_READ, "restricted"),
            (SCOPE_READ, "confidential", "restricted"),
            (SCOPE_READ, "external"),
        ],
    )
    async def test_a_read_only_key_lists_exactly_the_read_tier(
        self, scopes: tuple[str, ...]
    ) -> None:
        assert is_read_only_key(scopes)
        assert _visible(await _tools(), *scopes) == READ_TIER

    async def test_the_tier_costs_a_read_only_key_exactly_the_named_seven(self) -> None:
        tools = await _tools()
        before = {n for n, s in EXPECTED_TOOL_SCOPES.items() if s == SCOPE_READ}
        assert before - _visible(tools, SCOPE_READ) == ADVANCED_READ_TOOLS
        assert len(ADVANCED_READ_TOOLS) == 7

    async def test_an_advanced_tool_is_refused_not_only_hidden(self) -> None:
        tools = await _tools()
        for name in ADVANCED_READ_TOOLS:
            assert tools[name].auth(_ctx(SCOPE_READ)) is False  # type: ignore[attr-defined]


class TestEveryOtherKeyIsUnchanged:
    @pytest.mark.parametrize("scopes", _writing_scope_sets(), ids="+".join)
    async def test_the_list_is_what_the_scope_rule_gave_before_the_tier(
        self, scopes: tuple[str, ...]
    ) -> None:
        oracle = {
            name
            for name, needed in EXPECTED_TOOL_SCOPES.items()
            if scopes_satisfy(frozenset(scopes), needed)
        }
        assert _visible(await _tools(), *scopes) == oracle

    @pytest.mark.parametrize("writer", [SCOPE_INGEST, SCOPE_MUTATE, SCOPE_ADMIN])
    async def test_any_writing_key_keeps_the_seven_tools(self, writer: str) -> None:
        assert _visible(await _tools(), SCOPE_READ, writer) >= ADVANCED_READ_TOOLS

    async def test_nothing_is_hidden_from_everyone(self) -> None:
        # get_file_context and the deprecated context trio stay listed for a
        # key that can write; hiding them for all keys would change what the
        # server offers everyone, and is a separate decision.
        listed = _visible(await _tools(), SCOPE_READ, SCOPE_INGEST)
        assert {
            "get_file_context",
            "get_objective_context",
            "get_task_context",
            "get_sectioned_context",
        } <= listed


class TestTheCheckItself:
    def test_a_plain_scope_check_is_unchanged_for_read_only_keys(self) -> None:
        assert trellis_scope(SCOPE_READ)(_ctx(SCOPE_READ)) is True

    def test_advanced_needs_the_scope_and_a_writing_scope(self) -> None:
        check = trellis_scope(SCOPE_READ, advanced=True)
        assert check(_ctx(SCOPE_READ)) is False
        assert check(_ctx(SCOPE_READ, SCOPE_INGEST)) is True
        assert check(_ctx(SCOPE_ADMIN)) is True  # admin satisfies read
        assert check(_ctx(SCOPE_INGEST)) is False  # ingest alone does not read

    def test_the_check_keeps_its_scope_name(self) -> None:
        assert trellis_scope(SCOPE_READ, advanced=True).__name__ == "require_scope_read"

    def test_an_anonymous_caller_is_denied(self) -> None:
        check = trellis_scope(SCOPE_READ, advanced=True)
        assert check(AuthContext(token=None, component=MagicMock())) is False

    def test_enforcement_off_lets_everything_through(self) -> None:
        set_auth_enforced(enforced=False)
        try:
            check = trellis_scope(SCOPE_READ, advanced=True)
            assert check(AuthContext(token=None, component=MagicMock())) is True
        finally:
            set_auth_enforced(enforced=True)
