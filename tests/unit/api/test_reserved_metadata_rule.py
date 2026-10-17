"""Every boundary that copies caller metadata onto a ``Command`` must pass
it through :func:`trellis.mutate.reject_reserved_metadata` first.

``POST /api/v1/commands/batch`` built each item's ``Command.metadata`` as
``dict(item.metadata)`` -- a caller's own dict, copied in whole. Several
keys on ``Command.metadata`` are read as authoritative rather than as
opaque decoration (``requested_by_claim`` by the executor,
``key_id``/``key_name`` by classify-rule and schedule handlers,
``write_provenance`` by the write-provenance stamp, ``handler_warnings``
surfaced verbatim on ``CommandResult.warnings`` -- see
``src/trellis/mutate/reserved_metadata.py``'s module docstring for the
read site of each), so an unguarded copy let any caller -- even one with
only the base scope -- plant one of those keys and forge who the audit
trail blames, which build wrote the event, or what warning a handler
raised. ``trellis_api.routes.mutations._build_command`` now calls
``reject_reserved_metadata(item.metadata)`` before merging in its own
server-derived ``requested_by_claim``; this rule is what keeps a *new*
site (or a regression in this one) from reopening the hole.

**What the rule polices.** Every call in ``src/trellis_api/routes/`` or
``src/trellis/mcp/`` to ``Command`` or ``CommandBatch`` carrying a
``metadata=`` keyword, or to
``build_evidence_ingest_command``/``build_evidence_ingest_command_from_args``
carrying a ``command_metadata=`` keyword. ``build_evidence_ingest_command``'s
own ``metadata=`` keyword is the one exclusion: it sets the *document's*
content metadata, a caller-owned field with no relation to audit identity.
``CommandBatch.metadata`` is read by no handler today
(``MutationExecutor.execute_batch`` never reads it) -- but it is
caller-supplied the same way a command's own metadata is, so
``trellis_api.routes.mutations.execute_batch`` guards it the same way
before building the batch, rather than leaving an exemption that would
need revisiting the day something starts reading it.

A site is safe only when its value is the guard call itself -- directly,
or one local-variable hop away (``m = item.metadata`` is still a bare
passthrough one hop later; the guard call is checked on the keyword's
value itself, and once more on the single local it resolves to, matching
``mutations.py``'s own ``metadata = reject_reserved_metadata(item.metadata)``
shape) -- or contains no read of a caller mapping's ``metadata`` attribute
anywhere in its expression tree, by any shape: a bare ``X.metadata``
attribute access, ``dict(X.metadata)``, a dict-unpack ``{**X.metadata,
...}``, ``getattr(X, "metadata")``, a subscript (``X.metadata["key_id"]``),
or a ``.get()`` call (``X.metadata.get("key_id")``) -- including any of
those buried inside an otherwise-fresh dict literal
(``{"note": item.metadata.get("key_id")}`` is unsafe: the dict is
literal, but one of its values still reads the caller's bag). This is an
allowlist, not a denylist of known-dangerous shapes: a freshly built dict
literal (``{"requested_by_claim": claim} if claim else {}``) or a call to
a different, server-only helper (``_audit_identity(ctx)``) passes only
because neither one touches ``.metadata`` at all, not because either
shape is named safe. The guard would reject the server's own legitimate
stamp if applied to either (see ``reject_reserved_metadata``'s
docstring), which is why the guard call itself is checked for structurally
rather than by re-running it.

The population floor below is hand-read against this branch, not
computed by the scan it guards (the lesson #466 is cited for throughout
``tests/ast_rules.py``).
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

from tests.ast_rules import (
    assert_hand_read_floor,
    construction_names,
    iter_modules,
    name_of,
)

_ROUTES_ROOT = Path(__file__).resolve().parents[3] / "src" / "trellis_api" / "routes"
_MCP_ROOT = Path(__file__).resolve().parents[3] / "src" / "trellis" / "mcp"

_GUARD_NAME = "reject_reserved_metadata"

#: Hand-read 2026-10-17 against this branch: every call under
#: ``src/trellis_api/routes/*.py`` or ``src/trellis/mcp/*.py`` to
#: ``Command`` or ``CommandBatch`` carrying ``metadata=``, or to
#: ``build_evidence_ingest_command``/``build_evidence_ingest_command_from_args``
#: carrying ``command_metadata=`` --
#:   admin.py: schedule-update, job-run-request
#:             (Command(metadata=_audit_identity(ctx)))                      (2)
#:   classify_rules.py: apply-rule, revoke-rule (same shape)                 (2)
#:   curate.py: promote, demote, reinstate
#:              (Command(metadata={...} if claim else {}))                  (3)
#:   ingest.py: ingest_trace x3 (Command(metadata=_claim_metadata(claim)))   (3)
#:   mutations.py: _build_command's build_evidence_ingest_command_from_args
#:                 call, _build_command's Command(...) fallback,
#:                 execute_batch's CommandBatch(...) call                   (3)
#:   mcp/server.py: execute_mutation's build_evidence_ingest_command_from_args
#:                  call, execute_mutation's Command(...) fallback          (2)
#: Total: 2 + 2 + 3 + 3 + 3 + 2 = 15
_HAND_READ_FLOOR = 15

#: ``Command``'s and ``CommandBatch``'s own keyword.
#: ``build_evidence_ingest_command``'s ``metadata=`` is not in this map at
#: all -- see the module docstring for why it is out of scope.
_SUBJECT_KEYWORDS: dict[str, str] = {
    "Command": "metadata",
    "CommandBatch": "metadata",
    "build_evidence_ingest_command": "command_metadata",
    "build_evidence_ingest_command_from_args": "command_metadata",
}


def _subject_names(tree: ast.Module) -> dict[str, str]:
    """``{local call name: keyword to check}`` for this module."""
    resolved: dict[str, str] = {}
    for subject, keyword in _SUBJECT_KEYWORDS.items():
        for name in construction_names(subject, tree):
            resolved[name] = keyword
    return resolved


def _reserved_metadata_calls(tree: ast.Module) -> list[ast.Call]:
    names = _subject_names(tree)
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and name_of(node.func) in names
        and any(kw.arg == names[name_of(node.func)] for kw in node.keywords)
    ]


def _metadata_value(call: ast.Call, tree: ast.Module) -> ast.expr:
    names = _subject_names(tree)
    keyword = names[name_of(call.func)]
    return next(kw.value for kw in call.keywords if kw.arg == keyword)


def _parent_map(tree: ast.Module) -> dict[ast.AST, ast.AST]:
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    return parents


def _enclosing_function(
    node: ast.AST, parents: dict[ast.AST, ast.AST]
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    cur = parents.get(node)
    while cur is not None:
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return cur
        cur = parents.get(cur)
    return None


def _is_guard_call(expr: ast.expr, guard_names: set[str]) -> bool:
    return isinstance(expr, ast.Call) and name_of(expr.func) in guard_names


def _is_getattr_metadata_call(node: ast.AST) -> bool:
    """``getattr(X, "metadata")`` (with or without a default arg) --
    the one caller-metadata read that is a plain ``Call``, not an
    ``ast.Attribute``, so it needs its own check."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "getattr"
        and len(node.args) >= 2
        and isinstance(node.args[1], ast.Constant)
        and node.args[1].value == "metadata"
    )


def _contains_metadata_attr_read(node: ast.AST, guard_names: set[str]) -> bool:
    """Whether *node*'s expression tree reads a caller mapping's
    ``metadata`` attribute anywhere, by any shape -- a bare attribute
    access, ``getattr(X, "metadata")``, or either buried under a
    subscript, a ``.get()`` call, ``dict(...)``, a dict literal/unpack,
    or any other wrapper.

    The one place a ``.metadata`` read is sanctioned is inside a call to
    the guard itself: recursion does not descend into a nested guard
    call's own arguments, so ``{"x": reject_reserved_metadata(m)["x"]}``
    is not flagged for the read the guard call performs internally.
    """
    if _is_guard_call(node, guard_names):
        return False
    if isinstance(node, ast.Attribute) and node.attr == "metadata":
        return True
    if _is_getattr_metadata_call(node):
        return True
    return any(
        _contains_metadata_attr_read(child, guard_names)
        for child in ast.iter_child_nodes(node)
    )


def _local_assignment_value(
    func: ast.FunctionDef | ast.AsyncFunctionDef, name: str
) -> ast.expr | None:
    """The value of a plain ``name = <value>`` assignment in *func*'s own
    body (not a nested function's). Flow-insensitive, matching
    ``test_requested_by_principal_rule.py``'s style: the single real
    shape this rule resolves one hop for is ``metadata = reject_reserved_metadata(...)``
    assigned exactly once, so the first match is the only one that
    matters in practice.
    """
    for node in ast.iter_child_nodes(func):
        for stmt in ast.walk(node):
            if (
                isinstance(stmt, ast.Assign)
                and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name)
                and stmt.targets[0].id == name
            ):
                return stmt.value
    return None


def _is_unsafe_metadata_value(
    value: ast.expr,
    *,
    func: ast.FunctionDef | ast.AsyncFunctionDef | None,
    guard_names: set[str],
) -> bool:
    if _is_guard_call(value, guard_names):
        return False
    if isinstance(value, ast.Name) and func is not None:
        rhs = _local_assignment_value(func, value.id)
        if rhs is not None:
            if _is_guard_call(rhs, guard_names):
                return False
            return _contains_metadata_attr_read(rhs, guard_names)
    return _contains_metadata_attr_read(value, guard_names)


def _unsafe_sites(tree: ast.Module) -> list[ast.Call]:
    guard_names = construction_names(_GUARD_NAME, tree)
    parents = _parent_map(tree)
    unsafe = []
    for call in _reserved_metadata_calls(tree):
        value = _metadata_value(call, tree)
        enclosing = _enclosing_function(call, parents)
        if _is_unsafe_metadata_value(value, func=enclosing, guard_names=guard_names):
            unsafe.append(call)
    return unsafe


def _roots() -> list[Path]:
    return [_ROUTES_ROOT, _MCP_ROOT]


def test_every_reserved_metadata_construction_site_is_hand_counted() -> None:
    sites = [
        call
        for root in _roots()
        for _, tree in iter_modules(root)
        for call in _reserved_metadata_calls(tree)
    ]
    assert_hand_read_floor(
        len(sites),
        _HAND_READ_FLOOR,
        subject=(
            "Command(metadata=...) / build_evidence_ingest_command*"
            "(command_metadata=...) construction"
        ),
    )


def test_no_route_or_mcp_tool_passes_caller_metadata_without_the_guard() -> None:
    offenders = [
        f"{path.name}:{call.lineno}: {ast.unparse(call).splitlines()[0][:80]}"
        for root in _roots()
        for path, tree in iter_modules(root)
        for call in _unsafe_sites(tree)
    ]
    assert not offenders, (
        "site(s) copy a caller's metadata mapping onto a Command without "
        "resolving it through trellis.mutate.reject_reserved_metadata "
        "first:\n" + "\n".join(offenders)
    )


def _parse(source: str) -> ast.Module:
    return ast.parse(textwrap.dedent(source))


class TestRuleCatchesADefectiveRoute:
    """Prove the rule against a deliberately defective subject, and that
    it does not reject the real patterns it is meant to allow."""

    def test_a_bare_attribute_passthrough_is_flagged(self) -> None:
        tree = _parse(
            """
            from trellis.mutate import Command, Operation

            def handler(item, requested_by):
                return Command(
                    operation=Operation.ENTITY_CREATE,
                    metadata=item.metadata,
                    requested_by=requested_by,
                )
            """
        )
        assert len(_unsafe_sites(tree)) == 1

    def test_a_dict_wrapped_passthrough_is_flagged(self) -> None:
        tree = _parse(
            """
            from trellis.mutate import Command, Operation

            def handler(item, requested_by):
                return Command(
                    operation=Operation.ENTITY_CREATE,
                    metadata=dict(item.metadata),
                    requested_by=requested_by,
                )
            """
        )
        assert len(_unsafe_sites(tree)) == 1

    def test_a_dict_unpack_passthrough_is_flagged(self) -> None:
        tree = _parse(
            """
            from trellis.mutate import Command, Operation

            def handler(item, requested_by):
                return Command(
                    operation=Operation.ENTITY_CREATE,
                    metadata={**item.metadata, "note": "x"},
                    requested_by=requested_by,
                )
            """
        )
        assert len(_unsafe_sites(tree)) == 1

    def test_a_getattr_passthrough_is_flagged(self) -> None:
        tree = _parse(
            """
            from trellis.mutate import Command, Operation

            def handler(item, requested_by):
                return Command(
                    operation=Operation.ENTITY_CREATE,
                    metadata=getattr(item, "metadata"),
                    requested_by=requested_by,
                )
            """
        )
        assert len(_unsafe_sites(tree)) == 1

    def test_a_get_call_buried_in_a_dict_literal_is_flagged(self) -> None:
        tree = _parse(
            """
            from trellis.mutate import Command, Operation

            def handler(item, requested_by):
                return Command(
                    operation=Operation.ENTITY_CREATE,
                    metadata={"key_id": item.metadata.get("key_id")},
                    requested_by=requested_by,
                )
            """
        )
        assert len(_unsafe_sites(tree)) == 1

    def test_a_subscript_read_is_flagged(self) -> None:
        tree = _parse(
            """
            from trellis.mutate import Command, Operation

            def handler(item, requested_by):
                return Command(
                    operation=Operation.ENTITY_CREATE,
                    metadata=item.metadata["key_id"],
                    requested_by=requested_by,
                )
            """
        )
        assert len(_unsafe_sites(tree)) == 1

    def test_a_one_hop_unguarded_local_is_flagged(self) -> None:
        tree = _parse(
            """
            from trellis.mutate import Command, Operation

            def handler(item, requested_by):
                metadata = dict(item.metadata)
                return Command(
                    operation=Operation.ENTITY_CREATE,
                    metadata=metadata,
                    requested_by=requested_by,
                )
            """
        )
        assert len(_unsafe_sites(tree)) == 1

    def test_the_real_guarded_pattern_is_not_flagged(self) -> None:
        tree = _parse(
            """
            from trellis.mutate import Command, Operation, reject_reserved_metadata

            def handler(item, requested_by, claim):
                metadata = reject_reserved_metadata(item.metadata)
                if claim is not None:
                    metadata["requested_by_claim"] = claim
                return Command(
                    operation=Operation.ENTITY_CREATE,
                    metadata=metadata,
                    requested_by=requested_by,
                )
            """
        )
        assert _unsafe_sites(tree) == []

    def test_a_direct_inline_guard_call_is_not_flagged(self) -> None:
        tree = _parse(
            """
            from trellis.mutate import Command, Operation, reject_reserved_metadata

            def handler(item, requested_by):
                return Command(
                    operation=Operation.ENTITY_CREATE,
                    metadata=reject_reserved_metadata(item.metadata),
                    requested_by=requested_by,
                )
            """
        )
        assert _unsafe_sites(tree) == []

    def test_an_aliased_guard_import_is_still_recognised(self) -> None:
        tree = _parse(
            """
            from trellis.mutate import Command, Operation
            from trellis.mutate import reject_reserved_metadata as guard

            def handler(item, requested_by):
                metadata = guard(item.metadata)
                return Command(
                    operation=Operation.ENTITY_CREATE,
                    metadata=metadata,
                    requested_by=requested_by,
                )
            """
        )
        assert _unsafe_sites(tree) == []

    def test_a_fresh_serverside_dict_is_not_flagged(self) -> None:
        tree = _parse(
            """
            from trellis.mutate import Command, Operation

            def handler(ctx, requested_by, claim):
                metadata = {"requested_by_claim": claim} if claim else {}
                return Command(
                    operation=Operation.ENTITY_CREATE,
                    metadata=metadata,
                    requested_by=requested_by,
                )
            """
        )
        assert _unsafe_sites(tree) == []

    def test_a_different_serverside_helper_is_not_flagged(self) -> None:
        tree = _parse(
            """
            from trellis.mutate import Command, Operation

            def handler(ctx, requested_by):
                return Command(
                    operation=Operation.ENTITY_CREATE,
                    metadata=_audit_identity(ctx),
                    requested_by=requested_by,
                )
            """
        )
        assert _unsafe_sites(tree) == []

    def test_command_batch_metadata_passthrough_is_flagged(self) -> None:
        """``CommandBatch.metadata`` is read by no handler today, but it is
        caller-supplied the same way a command's own metadata is, so an
        unguarded passthrough is flagged the same way (see
        ``trellis_api.routes.mutations.execute_batch``)."""
        tree = _parse(
            """
            from trellis.mutate import CommandBatch

            def handler(req, requested_by):
                return CommandBatch(
                    commands=[],
                    strategy="sequential",
                    requested_by=requested_by,
                    metadata=req.metadata,
                )
            """
        )
        assert len(_unsafe_sites(tree)) == 1

    def test_command_batch_metadata_through_the_guard_is_not_flagged(self) -> None:
        tree = _parse(
            """
            from trellis.mutate import CommandBatch, reject_reserved_metadata

            def handler(req, requested_by):
                batch_metadata = reject_reserved_metadata(req.metadata)
                return CommandBatch(
                    commands=[],
                    strategy="sequential",
                    requested_by=requested_by,
                    metadata=batch_metadata,
                )
            """
        )
        assert _unsafe_sites(tree) == []

    def test_evidence_document_metadata_keyword_is_out_of_scope(self) -> None:
        """``build_evidence_ingest_command``'s ``metadata=`` sets the
        created document's own content metadata, not ``Command.metadata``
        -- a caller passthrough there is legitimate and out of this
        rule's population."""
        tree = _parse(
            """
            from trellis.mutate import build_evidence_ingest_command

            def handler(req, requested_by):
                return build_evidence_ingest_command(
                    content=req.content,
                    requested_by=requested_by,
                    metadata=req.metadata,
                )
            """
        )
        assert _reserved_metadata_calls(tree) == []

    def test_a_subclassed_command_is_still_resolved(self) -> None:
        tree = _parse(
            """
            from trellis.mutate import Command, Operation

            class MyCommand(Command):
                pass

            def handler(item, requested_by):
                return MyCommand(
                    operation=Operation.ENTITY_CREATE,
                    metadata=item.metadata,
                    requested_by=requested_by,
                )
            """
        )
        assert len(_unsafe_sites(tree)) == 1
