"""Guard: a TrellisError describes a caught exception, never quotes it (Q8).

Nine raise sites in ``src/trellis/mutate`` and ``src/trellis/stores`` used to
paste a caught ``OSError``/``JSONDecodeError``/pydantic ``ValidationError``/
``ImportError`` straight into a ``TrellisError`` subclass's message, by
f-string or ``str()``. That text can carry pydantic's ``input_value=`` (the
caller's own field value) or arbitrary driver/OS text. Both land
caller-facing: the immutable ``mutation.rejected`` audit event
(``trellis.mutate.executor``), MCP's ``_exception_detail`` (which passes a
``TrellisError``'s text through on the convention that Trellis composes it
without raw driver text), and the REST 400/409 detail at the sites that
bypass the sanitizing middleware.

The fix (``src/trellis/core/error_sanitize.py``'s ``describe_os_error``,
``describe_json_error``, ``describe_validation_error``,
``describe_import_error``) keeps the sentence Trellis composes and drops the
raw exception text, describing it from structured fields instead
(errno/strerror/filename; msg/lineno/colno; ``loc``/``type`` pairs with
``include_input=False``; the module name). This file is the regression
guard: a ``TrellisError``-family raise inside an ``except ... as name:``
handler, anywhere in ``src/trellis``, must not interpolate ``name`` into its
message by f-string, ``str()``/``repr()``, ``.format()``, or
``%``-formatting.

**Scope note, a deliberate deviation from the plan's literal numbers.** The
plan (``q8-describe-dont-quote.md``) says "set its floor with
``assert_hand_read_floor``: 9, or 11 with the SDK sites" -- but that count is
how many sites were *violating* at measurement time (the nine in the Q8
table, scoped to three files), not the population this scan's predicate
reaches. The rule as the plan states it ("a TrellisError subclass raised
inside except ... as name must not interpolate name") is general over all of
``src/trellis``, and a guard confined to those three files would never see a
violation introduced anywhere else -- the exact failure mode
``tests/ast_rules.py``'s header catalogues (#457, #464, #466, #488). The
hand-read floor below is therefore the count of *every* such raise-in-except
site at HEAD, read by hand (every site printed by ``_scan`` was inspected;
see the per-file breakdown in this repo's PR description), not the nine
known violations. Per that same header, "the population floor must come
from outside the measurement": 35 is a number a person counted, not a
quantity the scan below computes for itself.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

from tests.ast_rules import assert_hand_read_floor, iter_modules, name_of

_SRC = Path(__file__).resolve().parents[2] / "src" / "trellis"

#: Hand count at HEAD, 2026-10-09: every ``except ... as name:`` handler in
#: ``src/trellis`` whose body raises a TrellisError-family exception, across
#: 14 files (``llm/providers/openai.py`` x1, ``mutate/handlers.py`` x3,
#: ``mutate/policy_source.py`` x3, ``stores/arcadedb/base.py`` x1,
#: ``stores/bolt_opencypher/base.py`` x1, ``stores/bolt_opencypher/graph.py``
#: x3, ``stores/pgvector/store.py`` x1, ``stores/postgres/api_key.py`` x1,
#: ``stores/postgres/event_log.py`` x1, ``stores/postgres/graph.py`` x3,
#: ``stores/postgres/trace.py`` x1, ``stores/registry.py`` x16,
#: ``stores/sqlite/api_key.py`` x1, ``stores/sqlite/trace.py`` x1). All 35
#: are, at HEAD, either a ``describe_*`` call, a static sentence, an
#: interpolation of the *caller's own* data (a record/trace id, never the
#: exception), or ``type(exc).__name__`` -- never a bare ``{exc}``.
_HAND_READ_SITE_COUNT = 35


def _trellis_error_family(modules: list[tuple[Path, ast.Module]]) -> frozenset[str]:
    """Every class name reaching ``TrellisError`` through ``src/trellis``.

    A single-module resolver (``construction_names``) cannot see a subclass
    defined in another file -- documented in ``tests/ast_rules.py`` as the
    ``cross_module_subclass`` residue shape. ``LLMRoutingError``
    (``src/trellis/llm/routing.py``) is exactly that case: a ``ConfigError``
    subclass outside ``errors.py``. This resolves it with the same
    fixed-point idea, run once across every parsed module instead of one.
    """
    bases_by_name: dict[str, set[str]] = {}
    for _, tree in modules:
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                bases_by_name.setdefault(node.name, set()).update(
                    base for base in (name_of(b) for b in node.bases) if base
                )
    family = {"TrellisError"}
    changed = True
    while changed:
        changed = False
        for name, bases in bases_by_name.items():
            if name not in family and bases & family:
                family.add(name)
                changed = True
    return frozenset(family)


def _bare_name_interpolation(node: ast.AST, exc_name: str) -> bool:
    """Does *node* stringify the bare name ``exc_name`` into text?

    Four shapes, matching the plan's "by f-string or str(name)" plus the two
    other ways Python turns a value into text: ``repr()`` and ``%``. Each
    checks a *bare* ``ast.Name`` -- ``type(exc).__name__``, ``exc.strerror``
    and ``describe_os_error(exc)`` all contain a ``Name(id=exc_name)`` node
    too, but not as the direct, unwrapped operand of a stringification, so
    none of them match. That is the discriminating line this rule exists to
    hold (see ``test_safe_patterns_are_not_flagged`` below): passing the
    exception object itself, or a structured field off it, is the fix this
    guard is meant to require, not a shape it should also forbid.
    """
    for n in ast.walk(node):
        if isinstance(n, ast.FormattedValue) and (
            isinstance(n.value, ast.Name) and n.value.id == exc_name
        ):
            return True
        if isinstance(n, ast.Call):
            fn = name_of(n.func)
            if (
                fn in ("str", "repr")
                and len(n.args) == 1
                and isinstance(n.args[0], ast.Name)
                and n.args[0].id == exc_name
            ):
                return True
            if isinstance(n.func, ast.Attribute) and n.func.attr == "format":
                for arg in (*n.args, *(kw.value for kw in n.keywords)):
                    if isinstance(arg, ast.Name) and arg.id == exc_name:
                        return True
        if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Mod):
            right = n.right
            if isinstance(right, ast.Name) and right.id == exc_name:
                return True
            if isinstance(right, (ast.Tuple, ast.List)) and any(
                isinstance(elt, ast.Name) and elt.id == exc_name for elt in right.elts
            ):
                return True
    return False


@dataclass(frozen=True)
class _RaiseSite:
    """One ``raise <TrellisErrorFamily>(...)`` inside an ``except ... as name:``."""

    path: Path
    lineno: int
    exc_class: str
    exc_name: str
    violates: bool

    def describe(self, root: Path) -> str:
        where = self.path.relative_to(root)
        mark = " VIOLATION" if self.violates else ""
        return (
            f"{where}:{self.lineno} except as {self.exc_name} "
            f"-> raise {self.exc_class}{mark}"
        )


def _resolve_one_hop(expr: ast.expr, handler: ast.ExceptHandler) -> ast.expr:
    """If *expr* is a bare ``Name`` bound by a top-level ``x = ...`` in the
    handler's own body, return that assignment's value instead.

    One hop, not a fixed point: every real site in this tree builds its
    message as ``msg = f"..."; raise X(msg) from exc``, and a resolver that
    chases further is reaching past what a handler's own body actually
    contains.
    """
    if not isinstance(expr, ast.Name):
        return expr
    found: ast.expr | None = None
    for stmt in handler.body:
        if (
            isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], ast.Name)
            and stmt.targets[0].id == expr.id
        ):
            found = stmt.value
    return found if found is not None else expr


def _scan(tree: ast.Module, path: Path, family: frozenset[str]) -> list[_RaiseSite]:
    """Every TrellisError-family raise inside an ``except ... as name:`` in *tree*.

    Walks every node (not just statements) for the same reason
    ``calls_to_any`` does: #457's scanner descended only ``ast.stmt`` and so
    never entered an ``ast.ExceptHandler`` at all.
    """
    parent_of: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parent_of[child] = parent

    def enclosing_handler(node: ast.AST) -> ast.ExceptHandler | None:
        cur = node
        while cur in parent_of:
            cur = parent_of[cur]
            if isinstance(cur, ast.ExceptHandler) and cur.name:
                return cur
        return None

    sites: list[_RaiseSite] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)):
            continue
        exc_class = name_of(node.exc.func)
        if exc_class not in family:
            continue
        handler = enclosing_handler(node)
        if handler is None:
            continue
        assert handler.name is not None
        exprs = [
            _resolve_one_hop(arg, handler)
            for arg in (*node.exc.args, *(kw.value for kw in node.exc.keywords))
        ]
        violates = any(_bare_name_interpolation(e, handler.name) for e in exprs)
        sites.append(
            _RaiseSite(
                path=path,
                lineno=node.lineno,
                exc_class=exc_class,
                exc_name=handler.name,
                violates=violates,
            )
        )
    return sites


def _scan_all(root: Path) -> list[_RaiseSite]:
    modules = list(iter_modules(root))
    family = _trellis_error_family(modules)
    sites: list[_RaiseSite] = []
    for path, tree in modules:
        sites.extend(_scan(tree, path, family))
    return sites


def test_trellis_error_family_reaches_a_cross_module_subclass() -> None:
    """The family resolver is not confined to ``errors.py``.

    ``LLMRoutingError`` is a ``ConfigError`` defined in ``llm/routing.py``.
    A frozen, hand-copied list of the 13 names declared in ``errors.py``
    would silently exclude it (and any future cross-module subclass) from
    every check below -- the exact "exemption that empties the roster"
    failure this repo's other AST rules were built to refuse.
    """
    family = _trellis_error_family(list(iter_modules(_SRC)))
    assert "LLMRoutingError" in family
    assert family >= {
        "TrellisError",
        "ValidationError",
        "ConfigError",
        "BackendNotInstalledError",
        "StoreError",
        "StoreWriteRefusedError",
        "DegradedStoreWriteError",
        "StaleStoreWriteError",
        "NotFoundError",
        "MutationError",
        "PolicyViolationError",
        "ApprovalRequiredError",
        "IdempotencyError",
    }


def test_scan_is_not_vacuous() -> None:
    """The scan's reach, hand-read against today's tree, has not narrowed."""
    sites = _scan_all(_SRC)
    assert_hand_read_floor(
        len(sites),
        _HAND_READ_SITE_COUNT,
        subject="TrellisError-family raise inside an except...as handler",
        hint=(
            "Re-run the scan and re-read its printed sites by hand "
            "(pytest -q -s -k test_scan_is_not_vacuous) rather than "
            "lowering this number to match a narrower scan."
        ),
    )


def test_no_trellis_error_interpolates_the_caught_exception() -> None:
    """The real guard: zero raw-interpolation violations in src/trellis today."""
    sites = _scan_all(_SRC)
    violations = [s for s in sites if s.violates]
    assert not violations, "\n".join(
        [
            (
                f"{len(violations)} site(s) interpolate a caught exception's text "
                "directly into a TrellisError message instead of describing it "
                "via src/trellis/core/error_sanitize.py:"
            ),
            *(v.describe(_SRC.parent.parent) for v in violations),
        ]
    )


def test_safe_patterns_are_not_flagged() -> None:
    """Negative control: every shape the fix actually uses stays clean.

    An over-broad predicate (treating any appearance of the bound name as a
    violation, rather than only a bare stringification of it) would flag
    ``type(exc).__name__``, a structured attribute read, a describe_*
    helper call, and passing the exception object itself -- all of which
    are exactly the patterns this fix introduces or relies on. If any of
    these trips the scan, the predicate has regressed toward the
    over-collecting scanner this file's docstring (and the real site list
    inspected by hand) explicitly rules out.
    """
    tree = ast.parse(
        """
def f():
    try:
        pass
    except OSError as exc:
        msg = f"operation failed: {type(exc).__name__}"
        raise StoreError(msg) from exc
    except ValueError as exc:
        msg = f"could not parse: {describe_json_error(exc)}"
        raise ConfigError(msg) from exc
    except UnicodeDecodeError as exc:
        msg = f"not valid text at byte {exc.start}"
        raise ConfigError(msg) from None
    except Exception as exc:
        raise StoreError("operation failed", exc) from exc
    except KeyError as exc:
        raise StoreError("static message, no exception text at all")
"""
    )
    family = frozenset({"StoreError", "ConfigError"})
    sites = _scan(tree, Path("<synthetic>"), family)
    assert len(sites) == 5, sites
    assert not any(s.violates for s in sites), [s for s in sites if s.violates]


def test_fstring_interpolation_is_caught_on_a_defective_subject() -> None:
    """Prove the guard on a deliberately defective subject (f-string shape)."""
    tree = ast.parse(
        """
def f():
    try:
        pass
    except OSError as exc:
        raise StoreError(f"operation failed: {exc}") from exc
"""
    )
    sites = _scan(tree, Path("<synthetic>"), frozenset({"StoreError"}))
    assert len(sites) == 1
    assert sites[0].violates


def test_str_call_interpolation_is_caught_on_a_defective_subject() -> None:
    """Prove the guard on a deliberately defective subject (``str(exc)`` shape)."""
    tree = ast.parse(
        """
def f():
    try:
        pass
    except OSError as exc:
        msg = "operation failed: " + str(exc)
        raise StoreError(msg) from exc
"""
    )
    sites = _scan(tree, Path("<synthetic>"), frozenset({"StoreError"}))
    assert len(sites) == 1
    assert sites[0].violates


def test_format_call_interpolation_is_caught_on_a_defective_subject() -> None:
    """Prove the guard on a deliberately defective subject (``.format()`` shape)."""
    tree = ast.parse(
        """
def f():
    try:
        pass
    except OSError as exc:
        msg = "operation failed: {}".format(exc)
        raise StoreError(msg) from exc
"""
    )
    sites = _scan(tree, Path("<synthetic>"), frozenset({"StoreError"}))
    assert len(sites) == 1
    assert sites[0].violates


def test_percent_interpolation_is_caught_on_a_defective_subject() -> None:
    """Prove the guard on a deliberately defective subject (``%`` shape)."""
    tree = ast.parse(
        """
def f():
    try:
        pass
    except OSError as exc:
        msg = "operation failed: %s" % exc
        raise StoreError(msg) from exc
"""
    )
    sites = _scan(tree, Path("<synthetic>"), frozenset({"StoreError"}))
    assert len(sites) == 1
    assert sites[0].violates


def test_reverting_a_real_site_is_caught() -> None:
    """Reverting one real site to its pre-fix shape makes the scan flag it.

    This is the exact text ``src/trellis/mutate/handlers.py`` raised before
    this fix (Q8 table, ``q-review.md``): pydantic's rendered
    ``ValidationError`` text, which can carry ``input_value=`` -- the
    caller's own field value -- pasted straight into the message. The real
    file now builds this message with ``describe_validation_error(exc)``
    instead (confirmed clean by
    ``test_no_trellis_error_interpolates_the_caught_exception`` above); this
    test parses the old shape as a standalone synthetic snippet, never
    touching the real file, and confirms the scan would have caught it.
    """
    tree = ast.parse(
        """
def handle(self, command, context):
    try:
        pass
    except PydanticValidationError as exc:
        msg = f"Measurement validation failed: {exc}"
        raise ValidationError(msg) from exc
"""
    )
    sites = _scan(tree, Path("<synthetic>"), frozenset({"ValidationError"}))
    assert len(sites) == 1
    assert sites[0].violates
