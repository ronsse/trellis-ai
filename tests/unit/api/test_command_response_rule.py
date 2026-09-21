"""The one projection of ``CommandResult`` onto the wire (M1).

``curate``, ``extract`` and ``mutations`` each hand-wrote the same
five-field copy, and all three dropped ``CommandResult.warnings`` -- so
``Enforcement.WARN``, whose entire contract is "allow, but say so", said
nothing to any REST caller. That is #456's shape exactly: N hand-written
copies of one projection, each an independent chance to mistype, omit or
copy off the wrong object, and the fix is to stop there being copies.

Consolidation on its own is not the durable half. Nothing prevents a
fourth copy appearing in the next route, so two properties are pinned
structurally here:

* ``CommandResponse`` is constructed in exactly one file, and
* that one construction passes **every** field the DTO declares.

The second is what makes the first worth having. A single projection
that quietly stops setting ``warnings`` is the same defect with a
smaller blast radius, and a rule that only counted construction sites
would stay green through it.

Known limit, stated rather than implied: this is a rule about
*constructions*, so a route returning a bare ``dict`` that FastAPI
coerces into a ``CommandResponse`` is invisible to it. What closes that
today is ``mypy``, not this file -- every route that answers with one is
annotated ``-> CommandResponse`` and a ``dict`` return fails the
typecheck CI runs.
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests.ast_rules import (
    assert_hand_read_floor,
    assert_scan_is_not_vacuous,
    calls_to_any,
    construction_names,
    iter_modules,
)
from trellis_api.routes._results import command_response
from trellis_wire.dtos import CommandResponse

#: Path, relative to ``src/``, allowed to construct a ``CommandResponse``.
#: A set of one rather than an "everything except" check, so a second
#: projection is a deliberate edit to this line with a reviewer looking at
#: it. Matched on the ``src``-relative path and not on ``path.name``,
#: which would exempt any file called ``_results.py`` anywhere under
#: ``src/`` -- a one-``mkdir`` hole in a rule whose whole job is to stop a
#: fourth copy appearing somewhere new.
_ALLOWED_CONSTRUCTION_SITES = frozenset({"trellis_api/routes/_results.py"})

#: Fields the projection deliberately does not set. Empty today, and kept
#: as an explicit name rather than a ``>=`` comparison: a field added to
#: the DTO and forgotten in the projection is precisely the drift this
#: rule exists to catch, so not-projecting one has to be written down.
#: Widening it is bounded rather than free: naming ``warnings`` here and
#: dropping it from the projection still fails
#: :func:`test_the_projection_is_the_callable_the_routes_import`, which
#: asserts a real warning survives -- proved by mutant, not assumed.
_UNPROJECTED_FIELDS: frozenset[str] = frozenset()

#: Hand-read floor on the module discovery. Counted on 2026-09-20: 341
#: ``*.py`` under ``src/``. Every other guard below divides by this scan's
#: own output, so a scan that merely *shrinks* satisfies all of them --
#: #457, #464 and #466 are three shipped rules with that one root cause.
#: The slack is loose enough to survive ordinary churn; what it cannot
#: survive is a narrowed ``rglob``. It is deliberately not the number
#: ``test_builder_factory.py`` uses: the helper's contract is that the
#: floor is a number *a person read off the tree*, and inheriting someone
#: else's count is the one thing that cannot satisfy it.
_MIN_SRC_MODULES = 320

#: Every distributed package the discovery must reach. The numeric floor
#: alone cannot catch dropping a small one -- ``trellis_api`` is 20
#: modules against 21 of slack, and it is where this rule lives.
#: ``trellis_wire``, six modules, is where ``CommandResponse`` is
#: *defined*.
_PACKAGES = frozenset(
    {
        "trellis",
        "trellis_api",
        "trellis_cli",
        "trellis_sdk",
        "trellis_wire",
        "trellis_workers",
    }
)


def _src_root() -> Path:
    root = Path(__file__).resolve().parents[3] / "src"
    assert root.is_dir(), f"src/ not found at {root}"
    return root


def _modules(root: Path) -> list[tuple[Path, ast.Module]]:
    """The one discovery, parsed once, used by the scan and by its floor.

    Not two independent ``rglob``\\ s: counters that look independent and
    quietly share a narrowing are #464 exactly, where eight real sites
    became six with every guard green. One function means there is one
    place to narrow it, and the floor below reads the same list the scan
    walked -- so a narrowing shrinks the count that is checked against a
    number a person read off the tree.
    """
    return list(iter_modules(root))


def _scanned_modules() -> list[Path]:
    """The paths the scan itself walked, for the floor."""
    return [path for path, _tree in _modules(_src_root())]


def _construction_sites(root: Path) -> list[tuple[Path, ast.Call]]:
    """Every ``CommandResponse(...)`` construction under *root*.

    A call, not a mention: the class is named in annotations, in
    ``response_model=`` decorators and in ``__all__``, and none of those
    constructs anything -- which is why this parses rather than greps.
    Aliases, rebindings and subclasses resolve through
    :func:`tests.ast_rules.construction_names`, because the bare-name-only
    version of this scan reproduces *inside the rule* the defect the rule
    removes from the code (#488).
    """
    sites: list[tuple[Path, ast.Call]] = []
    for py_file, tree in _modules(root):
        names = construction_names("CommandResponse", tree)
        sites.extend((py_file, node) for node in calls_to_any(names, tree))
    return sites


def _offenders(root: Path) -> list[tuple[str, int]]:
    """Constructions under *root* that are not the allowed one.

    One function, called by the rule **and** by every guard that claims
    something about the rule: a guard that re-writes the comparison it is
    checking proves a property of its own copy, which is how a basename
    allowlist once survived an explicit test naming it.
    """
    return [
        (path.relative_to(root).as_posix(), node.lineno)
        for path, node in _construction_sites(root)
        if path.relative_to(root).as_posix() not in _ALLOWED_CONSTRUCTION_SITES
    ]


def test_command_response_is_built_in_exactly_one_place() -> None:
    """Three routes, one projection.

    Two readers of one seam that each silently report a constant is this
    repo's recurring defect (#325/#326, #443, #456); the fix is not to
    correct the copies but to stop there being copies.
    """
    offenders = _offenders(_src_root())
    assert not offenders, (
        "CommandResponse is constructed outside the single projection: "
        f"{offenders}. Call trellis_api.routes._results.command_response "
        "instead -- a fourth copy of the field list is a fourth chance to "
        "drop a field the way all three of the first ones dropped warnings."
    )


def test_the_scan_sees_every_shape_in_the_shared_roster(tmp_path: Path) -> None:
    """The shipped predicate, run over #490's roster of evasions.

    Module and class scope, ``async def``, decorators, ``except`` bodies,
    closures, lambda bodies, and a second file. Every one is a way this
    scan could narrow without any guard here noticing, because every
    other guard divides by its output. Run through :func:`_offenders` --
    the same function the rule calls -- so a regression in the shipped
    predicate turns this red rather than leaving it green against a copy.
    """
    assert_scan_is_not_vacuous(
        lambda root: [lineno for _path, lineno in _offenders(root)],
        subject="CommandResponse",
        tmp_path=tmp_path,
        live_population=len(_scanned_modules()),
        floor=_MIN_SRC_MODULES,
        exempt={
            "partial_binding": (
                "residue: the binding's value is a call, so there is no "
                "name for construction_names to resolve. Nothing in src/ "
                "builds a CommandResponse through functools.partial, and a "
                "fourth copy of the field list would have to land "
                "somewhere this scan can see it"
            ),
            "cross_module_subclass": (
                "residue: a CommandResponse subclass defined in one module "
                "and constructed in another is invisible to a per-module "
                "fixed point. The construction is still flagged when the "
                "subclass is defined where it is built, which is the "
                "common case and the only one present in src/"
            ),
        },
    )


def test_the_scan_sees_the_whole_tree() -> None:
    """The floor the scan cannot compute for itself, and the packages."""
    modules = _scanned_modules()
    assert_hand_read_floor(
        len(modules),
        _MIN_SRC_MODULES,
        subject="module under src/",
        hint="a narrowed discovery shrinks the population and every count over it.",
    )
    top_level = {p.relative_to(_src_root()).parts[0] for p in modules}
    assert top_level >= _PACKAGES, sorted(top_level)


def test_the_allowlist_is_a_path_not_a_basename(tmp_path: Path) -> None:
    """``_results.py`` *somewhere else* is still an offender.

    Asserted through the same relative-path comparison the rule performs,
    over a tree whose only construction sits at a decoy path.
    """
    fake = tmp_path / "src"
    (fake / "trellis_elsewhere" / "routes").mkdir(parents=True)
    (fake / "trellis_elsewhere" / "routes" / "_results.py").write_text(
        "from trellis_wire.dtos import CommandResponse\n"
        "def go(r):\n"
        "    return CommandResponse(status=r.status.value)\n"
    )
    assert _offenders(fake) == [("trellis_elsewhere/routes/_results.py", 3)]


def test_the_projection_still_carries_the_field_it_was_built_for() -> None:
    """The property that makes this rule necessary at all.

    A rule counting construction sites stays green through a single
    projection that has quietly stopped setting ``warnings`` -- the
    original defect, with a smaller blast radius. So the field is
    asserted to exist, and to default to an empty list rather than to
    ``None``: an always-present ``[]`` is what distinguishes "the gate
    ran and nothing warned" from "this build predates the field", and it
    is the reason the DTO key is unconditional where the audit event's
    ``policy_warnings`` key is not.
    """
    assert "warnings" in CommandResponse.model_fields
    bare = CommandResponse(
        status="success", command_id="cmd_1", operation="entity.create", message=""
    )
    assert bare.warnings == []


def test_the_projection_sets_every_field_the_dto_declares() -> None:
    """A field added to the DTO and forgotten in the projection.

    This is the drift the consolidation was for, and the only guard that
    catches it: ``warnings`` had a default, so the three hand-written
    copies that omitted it were valid ``CommandResponse``\\ s, valid
    OpenAPI, and silently wrong. Read off the single construction the
    shipped scan finds, so a projection moved to another file fails the
    rule above rather than quietly passing this one.
    """
    sites = _construction_sites(_src_root())
    assert len(sites) == 1, [
        (p.relative_to(_src_root()).as_posix(), n.lineno) for p, n in sites
    ]
    path, node = sites[0]
    assert path.relative_to(_src_root()).as_posix() in _ALLOWED_CONSTRUCTION_SITES

    passed = {kw.arg for kw in node.keywords if kw.arg is not None}
    splatted = any(kw.arg is None for kw in node.keywords)
    assert not splatted, (
        "the projection passes **kwargs, so no static check can tell "
        "which fields it sets. Spell them out -- the whole point of one "
        "projection is that one file can be read."
    )
    expected = set(CommandResponse.model_fields) - _UNPROJECTED_FIELDS
    assert passed == expected, (
        f"the projection sets {sorted(passed)} but CommandResponse declares "
        f"{sorted(CommandResponse.model_fields)}. A field with a default "
        "that nothing sets is valid, valid OpenAPI, and silently wrong -- "
        "which is exactly how warnings was lost. Either project it, or add "
        "it to _UNPROJECTED_FIELDS with a reviewer looking."
    )


def test_the_projection_is_the_callable_the_routes_import() -> None:
    """The scan and the behaviour are about the same function.

    Without this, ``_results.py`` could hold a construction the routes
    never call and every structural guard above would still pass.
    """
    from trellis.mutate import CommandResult, CommandStatus

    response = command_response(
        CommandResult(
            status=CommandStatus.SUCCESS,
            command_id="cmd_1",
            operation="entity.create",
            message="ok",
            warnings=["Policy warning (pol-warn): unusual write"],
        )
    )
    assert response.warnings == ["Policy warning (pol-warn): unusual write"]
