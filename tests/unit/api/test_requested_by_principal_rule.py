"""Every route-layer audit-trail construction must resolve ``requested_by``
through the shared principal helper.

``routes/mutations.py``'s ``_resolve_requested_by`` (#889) got this right
for ``/commands/batch`` and left every other body-supplied ``requested_by``
route trusting the caller: a mutate-scoped key could ``POST /precedents``
with ``requested_by="auto:learning_candidate"`` and forge the auto-promote
origin ``_was_auto_promoted`` trusts. The fix (``routes/_principal.py``)
is a shared ``resolve_requested_by(ctx, claimed)`` every route must call;
this rule is the thing that keeps a *new* route (or a regression in an
existing one) from silently reopening that hole.

**What the rule polices.** Every call in ``src/trellis_api/routes/`` to
one of :data:`_SUBJECT_NAMES` -- ``Command``, ``CommandBatch``, and the
three factories that build a ``Command`` from a ``requested_by=`` kwarg of
their own (``result_to_batch``, ``build_evidence_ingest_command``,
``build_evidence_ingest_command_from_args``) -- that carries a
``requested_by=`` keyword. The value must be a string literal (an
``f-string`` counts: it is a template, not a passthrough of a body value
by itself), or a name that traces back to ``resolve_requested_by`` --
either bound directly by a tuple-unpack in the same function
(``requested_by, claim = resolve_requested_by(ctx, ...)``), or, one level
removed, a parameter of a module-local "builder" function (the
``_entity_command(item, requested_by, ...)`` shape ``ingest.py`` and
``mutations.py`` both use) whose *every* call site in the module passes a
safe value for that parameter.

**Deliberately bounded, not a general dataflow analysis.** The one-level
indirection handles this repo's actual shape; a second hop, a value
threaded through a dict or a class attribute, or a builder called from
outside the module, all read as unsafe (fail closed) rather than being
chased further -- cheaper to flag a false positive by hand than to grow
an under-tested resolver.

The population floor below is hand-read from ``git show`` at the commit
this test was written against, not computed by the scan it guards (the
lesson #466 is cited for throughout ``tests/ast_rules.py``).
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

_RESOLVER_NAME = "resolve_requested_by"

#: Every callable whose ``requested_by=`` keyword ends up on a
#: ``Command``/``CommandBatch``'s audit trail. ``result_to_batch`` and the
#: two ``build_evidence_ingest_command*`` factories take the value as
#: their own kwarg and stamp it onto every ``Command`` they build -- the
#: forgery risk is identical to constructing the ``Command`` directly.
_SUBJECT_NAMES = frozenset(
    {
        "Command",
        "CommandBatch",
        "result_to_batch",
        "build_evidence_ingest_command",
        "build_evidence_ingest_command_from_args",
    }
)

#: Hand-read 2026-10-17 against this branch: every call in
#: ``src/trellis_api/routes/*.py`` to a name in ``_SUBJECT_NAMES`` that
#: carries a ``requested_by=`` keyword --
#:   mutations.py: _build_command's build_evidence_ingest_command_from_args
#:                 call, _build_command's Command(...) fallback,
#:                 execute_batch's CommandBatch(...)                     (3)
#:   curate.py:    promote, demote, reinstate, create_link, create_entity,
#:                 create_document's build_evidence_ingest_command,
#:                 record_feedback                                      (7)
#:   ingest.py:    ingest_trace, ingest_evidence's
#:                 build_evidence_ingest_command, _entity_command,
#:                 _edge_command, _alias_command                        (5)
#:   extract.py:   submit_drafts's result_to_batch(...)                 (1)
#:   classify_rules.py: apply-rule, revoke-rule                         (2)
#:   settings.py:  update_setting                                      (1)
#:   observations.py: record_observation, record_measurement           (2)
#:   admin.py:     schedule-update, job-run-request                    (2)
#: (admin.py's ``submit_learning_promotion(..., requested_by=...)`` is
#: excluded: that helper is not in ``_SUBJECT_NAMES`` and its value is
#: already a hardcoded literal, so it is not part of the population this
#: rule is checking.)
#: Total: 3 + 7 + 5 + 1 + 2 + 1 + 2 + 2 = 23
_HAND_READ_FLOOR = 23


def _subject_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for subject in _SUBJECT_NAMES:
        names |= construction_names(subject, tree)
    return names


def _requested_by_calls(tree: ast.Module) -> list[ast.Call]:
    names = _subject_names(tree)
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and name_of(node.func) in names
        and any(kw.arg == "requested_by" for kw in node.keywords)
    ]


def _requested_by_value(call: ast.Call) -> ast.expr:
    return next(kw.value for kw in call.keywords if kw.arg == "requested_by")


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


def _resolver_bound_names(
    func: ast.FunctionDef | ast.AsyncFunctionDef, resolver_names: set[str]
) -> set[str]:
    """Names bound, within *func*'s own body, by a tuple-unpack whose
    value is a call to ``resolve_requested_by`` (however imported,
    aliased or rebound at module scope -- *resolver_names* already
    resolved that)."""
    bound: set[str] = set()
    for node in ast.walk(func):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Tuple)
            and isinstance(node.value, ast.Call)
            and name_of(node.value.func) in resolver_names
        ):
            elts = node.targets[0].elts
            if elts and isinstance(elts[0], ast.Name):
                bound.add(elts[0].id)
    return bound


def _call_arg_for_param(
    call: ast.Call, func_def: ast.FunctionDef | ast.AsyncFunctionDef, param_name: str
) -> ast.expr | None:
    for kw in call.keywords:
        if kw.arg == param_name:
            return kw.value
    param_names = [a.arg for a in func_def.args.args]
    if param_name in param_names:
        idx = param_names.index(param_name)
        if idx < len(call.args):
            return call.args[idx]
    return None


def _is_safe_value(  # noqa: PLR0911 — each return is a distinct verdict in a flat safety check, not nested branching.
    value: ast.expr,
    *,
    tree: ast.Module,
    parents: dict[ast.AST, ast.AST],
    resolver_names: set[str],
    enclosing: ast.FunctionDef | ast.AsyncFunctionDef | None,
    depth: int = 0,
) -> bool:
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return True
    if isinstance(value, ast.JoinedStr):
        # An f-string template (``f"api:settings:{principal}"``) is not a
        # passthrough of a body-supplied value by itself.
        return True
    if not isinstance(value, ast.Name) or enclosing is None:
        return False
    if value.id in _resolver_bound_names(enclosing, resolver_names):
        return True
    if depth >= 2:
        return False
    param_names = {a.arg for a in enclosing.args.args}
    if value.id not in param_names:
        return False
    call_sites = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and name_of(node.func) == enclosing.name
    ]
    if not call_sites:
        # No evidence either way -- fail closed rather than trust an
        # unexercised (or cross-module-called) builder.
        return False
    for call in call_sites:
        arg = _call_arg_for_param(call, enclosing, value.id)
        if arg is None:
            return False
        caller = _enclosing_function(call, parents)
        if not _is_safe_value(
            arg,
            tree=tree,
            parents=parents,
            resolver_names=resolver_names,
            enclosing=caller,
            depth=depth + 1,
        ):
            return False
    return True


def _unsafe_sites(tree: ast.Module) -> list[ast.Call]:
    resolver_names = construction_names(_RESOLVER_NAME, tree)
    parents = _parent_map(tree)
    unsafe = []
    for call in _requested_by_calls(tree):
        value = _requested_by_value(call)
        enclosing = _enclosing_function(call, parents)
        if not _is_safe_value(
            value,
            tree=tree,
            parents=parents,
            resolver_names=resolver_names,
            enclosing=enclosing,
        ):
            unsafe.append(call)
    return unsafe


def test_every_requested_by_construction_site_is_hand_counted() -> None:
    sites = [
        call
        for _, tree in iter_modules(_ROUTES_ROOT)
        for call in _requested_by_calls(tree)
    ]
    assert_hand_read_floor(
        len(sites),
        _HAND_READ_FLOOR,
        subject="Command()/CommandBatch()/factory requested_by= construction",
    )


def test_no_route_passes_a_body_supplied_requested_by_without_the_helper() -> None:
    offenders = [
        f"{path.name}:{call.lineno}: {ast.unparse(call).splitlines()[0][:80]}"
        for path, tree in iter_modules(_ROUTES_ROOT)
        for call in _unsafe_sites(tree)
    ]
    assert not offenders, (
        "route(s) pass requested_by to Command()/CommandBatch() (or a "
        "factory that does) without resolving it through "
        "trellis_api.routes._principal.resolve_requested_by first:\n"
        + "\n".join(offenders)
    )


def _parse(source: str) -> ast.Module:
    return ast.parse(textwrap.dedent(source))


class TestRuleCatchesADefectiveRoute:
    """Prove the rule against a deliberately defective subject, and that
    it does not reject the real pattern it is meant to allow."""

    def test_a_raw_body_passthrough_is_flagged(self) -> None:
        tree = _parse(
            """
            from trellis.mutate import Command, Operation

            def promote(req, ctx):
                cmd = Command(
                    operation=Operation.PRECEDENT_PROMOTE,
                    requested_by=req.requested_by,
                )
                return cmd
            """
        )
        assert len(_unsafe_sites(tree)) == 1

    def test_a_resolved_local_is_not_flagged(self) -> None:
        tree = _parse(
            """
            from trellis.mutate import Command, Operation
            from trellis_api.routes._principal import resolve_requested_by

            def promote(req, ctx):
                requested_by, requested_by_claim = resolve_requested_by(
                    ctx, req.requested_by
                )
                cmd = Command(
                    operation=Operation.PRECEDENT_PROMOTE,
                    requested_by=requested_by,
                )
                return cmd
            """
        )
        assert _unsafe_sites(tree) == []

    def test_a_literal_label_is_not_flagged(self) -> None:
        tree = _parse(
            """
            from trellis.mutate import Command, Operation

            def create_link(req):
                return Command(operation=Operation.LINK_CREATE, requested_by="api:link")
            """
        )
        assert _unsafe_sites(tree) == []

    def test_a_resolved_value_threaded_through_a_builder_is_not_flagged(self) -> None:
        tree = _parse(
            """
            from trellis.mutate import Command, Operation
            from trellis_api.routes._principal import resolve_requested_by

            def _entity_command(item, requested_by):
                return Command(
                    operation=Operation.ENTITY_CREATE, requested_by=requested_by
                )

            def ingest_bulk(req, ctx):
                requested_by, claim = resolve_requested_by(ctx, req.requested_by)
                for entity in req.entities:
                    _entity_command(entity, requested_by)
            """
        )
        assert _unsafe_sites(tree) == []

    def test_an_unresolved_value_threaded_through_a_builder_is_flagged(self) -> None:
        """The same shape as above, but the caller forwards the raw claim
        instead of a resolved value -- the regression this rule exists to
        catch in ``_entity_command``/``_edge_command``/``_alias_command``
        and ``mutations.py``'s ``_build_command``."""
        tree = _parse(
            """
            from trellis.mutate import Command, Operation

            def _entity_command(item, requested_by):
                return Command(
                    operation=Operation.ENTITY_CREATE, requested_by=requested_by
                )

            def ingest_bulk(req, ctx):
                for entity in req.entities:
                    _entity_command(entity, req.requested_by)
            """
        )
        assert len(_unsafe_sites(tree)) == 1

    def test_a_builder_with_no_call_site_in_module_fails_closed(self) -> None:
        tree = _parse(
            """
            from trellis.mutate import Command, Operation

            def _entity_command(item, requested_by):
                return Command(
                    operation=Operation.ENTITY_CREATE, requested_by=requested_by
                )
            """
        )
        assert len(_unsafe_sites(tree)) == 1

    def test_a_call_with_no_requested_by_keyword_is_not_a_site(self) -> None:
        """Negative control: a Command() call that doesn't carry
        requested_by at all must not be reported as a site, let alone an
        offender -- otherwise the scan is trivially non-vacuous for the
        wrong reason."""
        tree = _parse(
            """
            from trellis.mutate import Command, Operation

            def helper():
                return Command(operation=Operation.ENTITY_CREATE)
            """
        )
        assert _requested_by_calls(tree) == []
        assert _unsafe_sites(tree) == []
