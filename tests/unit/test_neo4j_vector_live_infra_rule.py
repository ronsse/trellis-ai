"""The Neo4j vector suites run in live-infra CI, against one vector index.

``tests/unit/stores/test_neo4j_vector.py`` is named in
``.github/workflows/live-infra.yml``; before #356 no workflow executed it.

The rule here is about **sharing one container**, and it is the one that
nearly shipped broken. Putting this suite in the same pytest invocation as
``tests/integration/test_neo4j_e2e.py`` points both at one Neo4j, and Neo4j
allows exactly one vector index per ``(label, property)`` pair. The two suites
named different indexes on ``(:Node, embedding)``, which cost nothing while no
workflow ran them together — and a second ``CREATE VECTOR INDEX ... IF NOT
EXISTS`` is rejected **silently**: measured on ``neo4j:2025.12``, success is
returned, the index never appears in ``SHOW INDEXES``, and every test then dies
30s later in ``wait_for_vector_index_online``. Nothing raises at the point of
the mistake, so the only durable guard is to pin that both suites resolve to the
same name. The contract subclass
``tests/unit/stores/contracts/test_neo4j_vector_contract.py`` rides the
``contracts/`` directory selection into the same invocation, so it is pinned
to the same name.

This module once also pinned a capability gate that skipped the cases
reaching the store's ``SEARCH`` clause, because ``neo4j:2025.12`` refuses it.
The store now falls back to ``db.index.vector.queryNodes`` on that refusal,
so those cases run on the job's image through the procedure, the gate is
gone, and ``scripts/check_live_floor.py`` fails the job if the contract's
cases start skipping again. The ``SEARCH`` form itself runs on no CI server;
``tests/unit/stores/test_neo4j_vector_search_fallback.py`` covers the switch
between the two without one.
"""

from __future__ import annotations

import ast
import shlex
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "live-infra.yml"
SUITE = REPO_ROOT / "tests" / "unit" / "stores" / "test_neo4j_vector.py"
STORE_SOURCE = REPO_ROOT / "src" / "trellis" / "stores" / "neo4j" / "vector.py"
INTEGRATION_CONFTEST = REPO_ROOT / "tests" / "integration" / "conftest.py"
CONTRACTS_DIR = REPO_ROOT / "tests" / "unit" / "stores" / "contracts"
#: ``Neo4jVectorStore``'s subclass of the shared vector contract.
CONTRACT_SUBCLASS = CONTRACTS_DIR / "test_neo4j_vector_contract.py"

#: The e2e suite this file now shares a container with. Named as a path rather
#: than inferred, because it is the *premise* of the index-name rule below: if
#: it ever leaves the live-infra invocation, that rule stops describing a real
#: hazard and should be re-read rather than left passing.
E2E_SUITE_PATH = "tests/integration/test_neo4j_e2e.py"

#: The class both suites instantiate, and the keyword that names its index.
VECTOR_STORE_CLASS = "Neo4jVectorStore"
INDEX_NAME_KWARG = "index_name"

#: The module constant ``tests/integration/conftest.py`` feeds to the registry.
INTEGRATION_INDEX_CONSTANT = "INTEGRATION_VECTOR_INDEX"


def _live_test_step() -> dict[str, Any]:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    [step] = [
        step
        for step in workflow["jobs"]["live-infra"]["steps"]
        if step.get("name") == "Run live + contract suites against the containers"
    ]
    return step


def test_live_infra_selects_the_neo4j_vector_suite() -> None:
    """No workflow ran this suite before #356; it must not fall out again."""
    tokens = shlex.split(_live_test_step()["run"].replace("\\\n", " "))
    targets = {token.rstrip("/") for token in tokens if token.startswith("tests/")}

    assert "tests/unit/stores/test_neo4j_vector.py" in targets


def _store_default_index_name(source: str) -> str | None:
    """The ``index_name`` default on ``Neo4jVectorStore.__init__``.

    This is what a caller gets by *omitting* the keyword, which is what the unit
    suite now does, so it has to be read rather than assumed.
    """
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.ClassDef) or node.name != VECTOR_STORE_CLASS:
            continue
        for item in node.body:
            if not isinstance(item, ast.FunctionDef) or item.name != "__init__":
                continue
            args = item.args
            positional = args.posonlyargs + args.args
            padded: list[ast.expr | None] = [
                *([None] * (len(positional) - len(args.defaults))),
                *args.defaults,
            ]
            by_name = dict(zip([a.arg for a in positional], padded, strict=True))
            by_name.update(
                dict(
                    zip(
                        [a.arg for a in args.kwonlyargs],
                        args.kw_defaults,
                        strict=True,
                    )
                )
            )
            default = by_name.get(INDEX_NAME_KWARG)
            if isinstance(default, ast.Constant) and isinstance(default.value, str):
                return default.value
    return None


def _constructed_index_names(source: str, default: str | None) -> list[str | None]:
    """Resolve the index every ``Neo4jVectorStore(...)`` in ``source`` will use.

    An explicit ``index_name=`` wins; omitting it resolves to the store's own
    signature default. Both spellings have to resolve, because the fix for the
    collision was to *stop* passing the keyword — a scan that only understood
    the explicit form would report "no names found" on the fixed source and
    pass by finding nothing.
    """
    resolved: list[str | None] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            name = func.attr
        else:
            name = getattr(func, "id", None)
        if name != VECTOR_STORE_CLASS:
            continue
        explicit = [kw for kw in node.keywords if kw.arg == INDEX_NAME_KWARG]
        if not explicit:
            resolved.append(default)
        elif isinstance(explicit[0].value, ast.Constant):
            resolved.append(explicit[0].value.value)
        else:
            resolved.append(None)
    return resolved


def _integration_index_name(source: str) -> str | None:
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Assign):
            continue
        targets = {t.id for t in node.targets if isinstance(t, ast.Name)}
        if INTEGRATION_INDEX_CONSTANT not in targets:
            continue
        if isinstance(node.value, ast.Constant):
            return node.value.value
    return None


def test_live_infra_runs_both_neo4j_vector_suites_in_one_invocation() -> None:
    """The premise of the rule below: one pytest run, therefore one container."""
    tokens = shlex.split(_live_test_step()["run"].replace("\\\n", " "))
    targets = {token.rstrip("/") for token in tokens if token.startswith("tests/")}

    assert {
        "tests/unit/stores/test_neo4j_vector.py",
        "tests/unit/stores/contracts",
        E2E_SUITE_PATH,
    } <= targets


def test_both_suites_resolve_to_one_vector_index_name() -> None:
    """One index per ``(:Node, embedding)``, so one name across both suites.

    The failure this prevents has no error at the point of the mistake. Neo4j
    accepts the second ``CREATE ... IF NOT EXISTS`` under a different name,
    reports success, and simply does not create it; the suite that asked then
    fails every test 30s later on a timeout that names an index nobody can see.
    Measured on ``neo4j:2025.12`` (the job's image then), which is also
    where the pre-#356 arrangement was caught, running the two suites back to
    back against one container.
    """
    default = _store_default_index_name(STORE_SOURCE.read_text(encoding="utf-8"))
    integration_name = _integration_index_name(
        INTEGRATION_CONFTEST.read_text(encoding="utf-8")
    )
    assert integration_name is not None

    for suite in (SUITE, CONTRACT_SUBCLASS):
        unit_names = _constructed_index_names(
            suite.read_text(encoding="utf-8"), default
        )
        assert unit_names, f"no {VECTOR_STORE_CLASS}(...) call found in {suite}"
        assert set(unit_names) == {integration_name}, (
            f"{suite.name} provisions {sorted(set(unit_names))} while "
            f"{INTEGRATION_CONFTEST.parent.name}/{INTEGRATION_CONFTEST.name} "
            f"pins {integration_name!r}. All run against one Neo4j in "
            f"live-infra, which holds one vector index per (label, property) "
            f"— the second CREATE is accepted and discarded without an error."
        )


def test_the_store_default_is_readable_and_is_a_real_name() -> None:
    """Guard the resolver's fallback, which the unit suite now depends on.

    ``_store_default_index_name`` returning ``None`` would make the unit suite
    resolve to ``None`` and the equality above fail loudly — but only as long as
    the integration constant is a string. Assert the default is really there, so
    a signature change is read as a signature change rather than as a mismatch.
    """
    default = _store_default_index_name(STORE_SOURCE.read_text(encoding="utf-8"))

    assert isinstance(default, str)
    assert default


@pytest.mark.parametrize(
    ("call", "expected"),
    [
        ("Neo4jVectorStore(URI, dimensions=3)", "the_default"),
        ("Neo4jVectorStore(URI, dimensions=3, index_name='private')", "private"),
        ("Neo4jVectorStore(URI, index_name=SOME_CONSTANT)", None),
    ],
)
def test_the_index_scan_reads_both_spellings(call: str, expected: str | None) -> None:
    """Prove the resolver discriminates, on a synthetic tree.

    The middle case is the state this rule was written to catch and the first is
    the fix, so a scan that collapsed them — always returning the default, or
    only seeing explicit keywords — would pass the rule above while enforcing
    nothing. The third pins that a non-literal name is reported as unresolved
    rather than silently skipped, since that is also a divergence nobody can see.
    """
    assert _constructed_index_names(call, "the_default") == [expected]
