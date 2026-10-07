"""A stamp failure's exception text is sanitized before it reaches an MCP
caller (trellis-ai#793 follow-up 5).

``supersede_document``/``supersede_entity`` (``trellis.mcp.supersession``)
catch any exception their stamp raises and return
``f"{type(exc).__name__}: {exc}"`` as an error string — never raising
themselves. That string reaches an MCP caller verbatim: embedded in an
``McpError`` message by ``save_knowledge``'s ``_raise_if_supersede_failed``
and by one of ``save_memory``'s two callers (the exact-hash-hit path), and
in a successful reply's warning text by the other (the post-store path).
Every other caught-exception site in ``trellis.mcp.server`` renders through
``_exception_detail``, which lets a ``TrellisError``'s own text through but
sanitizes anything else (a store driver's text can carry a DSN, a
credential, a row value); the two stamp functions skipped that, so a raw
driver exception reaches the caller unfiltered.
"""

from __future__ import annotations

from typing import Any

import pytest
from mcp.shared.exceptions import McpError

import trellis.mcp.server as server_mod
from tests.unit.mcp.conftest import unwrap_tool
from trellis.core.error_sanitize import SUPPRESSED_MARKER
from trellis.stores.registry import StoreRegistry

save_memory = unwrap_tool(server_mod.save_memory)
save_knowledge = unwrap_tool(server_mod.save_knowledge)

#: Trips the secret-shaped-assignment leak heuristic in
#: ``trellis.core.error_sanitize`` (same shape as its own
#: ``test_secret_assignment_suppressed``) — synthetic, not a real credential.
LEAK_TEXT = "connect failed: api_key=sk-FAKE0011223344"

#: Clean text: no leak heuristic trips on it, so it should read through
#: unchanged, the way ``_exception_detail`` lets a timeout or connection
#: refusal through.
CLEAN_TEXT = "request timed out after 30s"

T_TEXT = "zebrafish calibration note: settle window forty milliseconds"
X_TEXT = "zebrafish calibration note: settle window twenty-five milliseconds"
NAME = "zebrafish settle window probe"


def _doc_id(reply: str) -> str:
    return reply.splitlines()[0].rsplit(": ", 1)[1].strip()


def _node_id(reply: str) -> str:
    import re

    match = re.search(r"Entity created: (\S+)", reply)
    assert match is not None, reply
    return match.group(1)


def _patch_get_raises(
    monkeypatch: pytest.MonkeyPatch,
    obj: Any,
    attr: str,
    target_arg: str,
    exc: Exception,
    *,
    skip: int = 0,
) -> None:
    """Replace ``obj.attr`` with a wrapper that raises ``exc`` on the call
    whose first positional argument equals ``target_arg``, after ``skip``
    such calls have already been let through to the real method.

    Both ``save_memory`` and ``save_knowledge`` look the target up twice:
    once in the pre-write eligibility check (``check_memory_target`` /
    ``plan_entity_supersession``, a plain read with no try/except — an
    exception there is a refusal-path bug, not this PR's subject) and again
    inside the stamp itself (``_stamp_document`` / ``_stamp_entity``, wrapped
    in ``supersede_document``/``supersede_entity``'s ``except Exception``).
    ``skip=1`` lets the first (check) call through and raises on the second
    (stamp) call, which is the one this module's fix covers.
    """
    real = getattr(obj, attr)
    seen = 0

    def wrapper(first_arg: str, *args: Any, **kwargs: Any) -> Any:
        nonlocal seen
        if first_arg == target_arg:
            if seen >= skip:
                raise exc
            seen += 1
        return real(first_arg, *args, **kwargs)

    monkeypatch.setattr(obj, attr, wrapper)


def test_document_stamp_leak_reaches_mcp_caller_sanitized(
    temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A document stamp's non-Trellis exception comes back as the
    sanitizer's marker, type name still present, via save_memory's
    exact-hash-hit path (``_memory_exists_response``)."""
    reg = temp_registry
    t = _doc_id(save_memory(T_TEXT))
    save_memory(X_TEXT)  # exact-hash target for the re-send below

    _patch_get_raises(
        monkeypatch,
        reg.knowledge.document_store,
        "get",
        t,
        RuntimeError(LEAK_TEXT),
        skip=1,
    )

    with pytest.raises(McpError) as excinfo:
        save_memory(X_TEXT, supersedes=t)

    message = excinfo.value.error.message
    assert "RuntimeError" in message
    assert SUPPRESSED_MARKER in message
    assert "sk-FAKE0011223344" not in message


def test_entity_stamp_leak_reaches_mcp_caller_sanitized(
    temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An entity stamp's non-Trellis exception comes back as the
    sanitizer's marker, type name still present, via save_knowledge's
    ``_raise_if_supersede_failed``."""
    reg = temp_registry
    n_old = _node_id(save_knowledge(name=NAME, content=T_TEXT))

    _patch_get_raises(
        monkeypatch,
        reg.knowledge.graph_store,
        "get_node",
        n_old,
        RuntimeError(LEAK_TEXT),
        skip=1,
    )

    with pytest.raises(McpError) as excinfo:
        save_knowledge(name=NAME, content=X_TEXT, supersedes=n_old)

    message = excinfo.value.error.message
    assert "RuntimeError" in message
    assert SUPPRESSED_MARKER in message
    assert "sk-FAKE0011223344" not in message


def test_trellis_error_stamp_text_reaches_mcp_caller_unchanged(
    temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``TrellisError`` raised by a stamp keeps its own text verbatim —
    Trellis wrote it, same carve-out ``_exception_detail`` gives every other
    caught-exception site in server.py. Uses leak-shaped text on purpose to
    prove the pass-through is type-gated, not content-gated."""
    from trellis.errors import TrellisError

    reg = temp_registry
    n_old = _node_id(save_knowledge(name=NAME, content=T_TEXT))

    _patch_get_raises(
        monkeypatch,
        reg.knowledge.graph_store,
        "get_node",
        n_old,
        TrellisError(LEAK_TEXT),
        skip=1,
    )

    with pytest.raises(McpError) as excinfo:
        save_knowledge(name=NAME, content=X_TEXT, supersedes=n_old)

    message = excinfo.value.error.message
    assert "TrellisError" in message
    assert LEAK_TEXT in message
    assert SUPPRESSED_MARKER not in message


def test_clean_stamp_exception_reads_through_to_mcp_caller(
    temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clean message (no leak heuristic trips) stays readable, the way
    ``_exception_detail`` lets a timeout or connection refusal through."""
    reg = temp_registry
    n_old = _node_id(save_knowledge(name=NAME, content=T_TEXT))

    _patch_get_raises(
        monkeypatch,
        reg.knowledge.graph_store,
        "get_node",
        n_old,
        TimeoutError(CLEAN_TEXT),
        skip=1,
    )

    with pytest.raises(McpError) as excinfo:
        save_knowledge(name=NAME, content=X_TEXT, supersedes=n_old)

    message = excinfo.value.error.message
    assert f"TimeoutError: {CLEAN_TEXT}" in message
    assert SUPPRESSED_MARKER not in message
