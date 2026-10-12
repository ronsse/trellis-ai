"""A ``supersedes=`` stamp failure reaches an MCP caller with its exception
text rendered by ``render_exception_detail``.

``supersede_document``/``supersede_entity`` return the stamp's exception as
an error string, which ``save_knowledge`` and ``save_memory`` put into an
``McpError`` or a reply's warning. A ``TrellisError`` keeps its text; any
other exception's text goes through the sanitizer. Clean text reading
through on both paths is pinned by ``test_supersedes_param.py``'s
``test_a_stamp_that_raises_is_reported_not_raised``.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from mcp.shared.exceptions import McpError

import trellis.mcp.server as server_mod
from tests.unit.mcp.conftest import unwrap_tool
from trellis.core.error_sanitize import SUPPRESSED_MARKER
from trellis.errors import TrellisError
from trellis.stores.registry import StoreRegistry

save_memory = unwrap_tool(server_mod.save_memory)
save_knowledge = unwrap_tool(server_mod.save_knowledge)

#: Trips the secret-shaped-assignment leak heuristic in
#: ``trellis.core.error_sanitize`` (same shape as its own
#: ``test_secret_assignment_suppressed``) — synthetic, not a real credential.
LEAK_TEXT = "connect failed: api_key=sk-FAKE0011223344"

T_TEXT = "zebrafish calibration note: settle window forty milliseconds"
X_TEXT = "zebrafish calibration note: settle window twenty-five milliseconds"
NAME = "zebrafish settle window probe"


def _doc_id(reply: str) -> str:
    return reply.splitlines()[0].rsplit(": ", 1)[1].strip()


def _node_id(reply: str) -> str:
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
    """Make ``obj.attr`` raise ``exc`` on the call whose first argument is
    ``target_arg``, after ``skip`` such calls have reached the real method.

    Both tools read the target twice: in the pre-write eligibility check,
    which has no ``except``, and again inside the stamp. ``skip=1`` raises
    in the stamp.
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
