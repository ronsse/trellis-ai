"""The Neo4j vector suite runs in live-infra CI, and its SEARCH cases are gated.

Those two facts hold each other up. ``tests/unit/stores/test_neo4j_vector.py``
is named in ``.github/workflows/live-infra.yml`` — 24 of its 28 tests that no
workflow had ever executed — and that is only safe because the four cases issuing the
AuraDB-grade ``SEARCH ... IN (VECTOR INDEX ...)`` clause skip themselves on the
``neo4j:2025.12`` container the job provisions.

So the rule is derived, not declared: find every test in that suite which calls
``Neo4jVectorStore.query``, and require each to take the capability gate. A
roster of today's four test names would go stale the first time someone adds a
fifth — silently, and in the direction that turns a skip into a red CI on a
workflow nobody runs locally.

The second invariant here is about **sharing one container**, and it is the one
that nearly shipped broken. Putting this suite in the same pytest invocation as
``tests/integration/test_neo4j_e2e.py`` points both at one Neo4j, and Neo4j
allows exactly one vector index per ``(label, property)`` pair. The two suites
named different indexes on ``(:Node, embedding)``, which cost nothing while no
workflow ran them together — and a second ``CREATE VECTOR INDEX ... IF NOT
EXISTS`` is rejected **silently**: measured on ``neo4j:2025.12``, success is
returned, the index never appears in ``SHOW INDEXES``, and every test then dies
30s later in ``wait_for_vector_index_online``. Nothing raises at the point of
the mistake, so the only durable guard is to pin that both suites resolve to the
same name.

The contract subclass ``tests/unit/stores/contracts/test_neo4j_vector_contract.py``
is under both rules too. It rides the ``contracts/`` directory selection into
the same invocation, so it must resolve to the same index name; and because its
bodies are inherited from ``VectorStoreContractTests`` it cannot take the gate
in their signatures, so it gates by a hand-read roster of names —
``SEARCH_ISSUING_TESTS`` — which is pinned here, both directions, against the
same AST scan run over the contract.
"""

from __future__ import annotations

import ast
import shlex
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.ast_rules import name_of

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "live-infra.yml"
SUITE = REPO_ROOT / "tests" / "unit" / "stores" / "test_neo4j_vector.py"
STORE_SOURCE = REPO_ROOT / "src" / "trellis" / "stores" / "neo4j" / "vector.py"
INTEGRATION_CONFTEST = REPO_ROOT / "tests" / "integration" / "conftest.py"
CONTRACTS_DIR = REPO_ROOT / "tests" / "unit" / "stores" / "contracts"
#: The shared vector contract, whose inherited bodies the subclass below runs.
CONTRACT = CONTRACTS_DIR / "vector_store_contract.py"
#: ``Neo4jVectorStore``'s subclass of that contract. It cannot put the gate in
#: the inherited signatures, so it names the gated cases in a module constant.
CONTRACT_SUBCLASS = CONTRACTS_DIR / "test_neo4j_vector_contract.py"
CONTRACT_ROSTER_CONSTANT = "SEARCH_ISSUING_TESTS"

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

#: The fixture (in ``tests/conftest.py``) whose presence in a test's signature
#: is the gate. Requesting it by name is the whole mechanism — see that
#: fixture's docstring for why it is a fixture rather than an inline skip.
GATE = "require_neo4j_vector_search"

#: The store method that emits the ``SEARCH`` clause, and therefore the call
#: this rule scans for.
QUERY_METHOD = "query"

#: Tests that call :data:`QUERY_METHOD` and legitimately need no gate, each
#: with the reason. A new query-calling test is gated or named here; there is
#: no third option, and a name that stops being a query caller fails below
#: rather than sitting here doing nothing.
UNGATED_QUERY_TESTS = {
    "test_query_dimension_mismatch_raises": (
        "the ValueError is raised in Python from the length check at the top "
        "of Neo4jVectorStore.query, before any Cypher is built or sent"
    ),
}


#: Contract cases that call :data:`QUERY_METHOD` and are deliberately *not* in
#: the subclass's roster, each with the reason. Same both-directions rule as
#: :data:`UNGATED_QUERY_TESTS`: a query caller is rostered or named here.
CONTRACT_UNGATED_QUERY_TESTS = {
    "test_reset_storage_matches_supports_reset": (
        "it queries only after a successful reset_storage(); Neo4jVectorStore "
        "does not implement one, so the case takes the NotImplementedError "
        "branch and returns before any Cypher is built or sent"
    ),
}

#: The method whose absence on the store is the premise of the exemption above.
RESET_METHOD = "reset_storage"


def _live_test_step() -> dict[str, Any]:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    [step] = [
        step
        for step in workflow["jobs"]["live-infra"]["steps"]
        if step.get("name") == "Run live + contract suites against the containers"
    ]
    return step


def _query_calling_tests(source: str) -> dict[str, set[str]]:
    """Map each ``test_*`` function that calls ``.query(...)`` to its fixtures.

    Keyed on the attribute name rather than the receiver, so it does not care
    whether the store is unpacked as ``vector``, ``_, vector`` or anything
    else. The value is the function's parameter names, which is where the gate
    shows up.
    """
    found: dict[str, set[str]] = {}
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.FunctionDef) or not node.name.startswith("test_"):
            continue
        calls_query = any(
            isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Attribute)
            and sub.func.attr == QUERY_METHOD
            for sub in ast.walk(node)
        )
        if calls_query:
            found[node.name] = {arg.arg for arg in node.args.args}
    return found


def test_live_infra_selects_the_neo4j_vector_suite() -> None:
    """The 24 ungated tests in that file are the point of #356."""
    tokens = shlex.split(_live_test_step()["run"].replace("\\\n", " "))
    targets = {token.rstrip("/") for token in tokens if token.startswith("tests/")}

    assert "tests/unit/stores/test_neo4j_vector.py" in targets


def test_every_search_issuing_test_is_capability_gated() -> None:
    callers = _query_calling_tests(SUITE.read_text(encoding="utf-8"))
    ungated = {name for name, params in callers.items() if GATE not in params}

    assert ungated == set(UNGATED_QUERY_TESTS), (
        f"tests calling .{QUERY_METHOD}() without the {GATE!r} gate: "
        f"{sorted(ungated - set(UNGATED_QUERY_TESTS))}. Add the fixture to the "
        f"signature, or name the test in UNGATED_QUERY_TESTS with its reason."
    )


def test_the_exemptions_are_still_query_callers() -> None:
    """A stale exemption must fail, not quietly permit a future ungated test."""
    callers = _query_calling_tests(SUITE.read_text(encoding="utf-8"))

    assert set(UNGATED_QUERY_TESTS) <= set(callers), (
        f"UNGATED_QUERY_TESTS names tests that no longer call "
        f".{QUERY_METHOD}(): {sorted(set(UNGATED_QUERY_TESTS) - set(callers))}"
    )


def _contract_roster(source: str) -> set[str]:
    """The subclass's hand-read :data:`CONTRACT_ROSTER_CONSTANT`, read as text.

    Read rather than imported: the subclass module ``importorskip``s the
    ``neo4j`` driver, and this rule has to hold in the default suite.
    """
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Assign):
            continue
        targets = {t.id for t in node.targets if isinstance(t, ast.Name)}
        if CONTRACT_ROSTER_CONSTANT in targets:
            return {
                sub.value
                for sub in ast.walk(node.value)
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str)
            }
    msg = f"{CONTRACT_ROSTER_CONSTANT} not found in {CONTRACT_SUBCLASS.name}"
    raise AssertionError(msg)


def test_every_search_issuing_contract_case_is_rostered() -> None:
    """The contract's query callers and the subclass's roster, both directions.

    The subclass gates by name, so the two failures are opposite and both
    silent until live-infra runs: a new contract case that calls ``query``
    and is missing from the roster goes red there on ``Invalid input
    'SEARCH'``; a rostered name that no longer calls ``query`` (or no longer
    exists) skips a case that would have passed, which is coverage lost with
    nothing reporting it.
    """
    callers = set(_query_calling_tests(CONTRACT.read_text(encoding="utf-8")))
    roster = _contract_roster(CONTRACT_SUBCLASS.read_text(encoding="utf-8"))
    expected = callers - set(CONTRACT_UNGATED_QUERY_TESTS)

    missing = sorted(expected - roster)
    stale = sorted(roster - expected)
    assert not missing, (
        f"contract cases calling .{QUERY_METHOD}() that "
        f"{CONTRACT_SUBCLASS.name} does not gate: {missing}. Add each to "
        f"{CONTRACT_ROSTER_CONSTANT}, or name it in "
        f"CONTRACT_UNGATED_QUERY_TESTS with its reason."
    )
    assert not stale, (
        f"{CONTRACT_ROSTER_CONSTANT} gates cases that do not call "
        f".{QUERY_METHOD}() (or are exempted here): {stale}. A gated case "
        f"that needs no gate is a passing test skipped on every run."
    )


def test_the_contract_subclass_requests_the_gate() -> None:
    """The roster only matters if something hands its names to the gate.

    A roster pinned perfectly against the contract is still inert if the
    fixture reading it stops calling ``getfixturevalue`` on the gate — every
    SEARCH case then runs ungated and live-infra goes red. Read as a call,
    not as a string, so a docstring naming the gate does not satisfy it.
    """
    tree = ast.parse(CONTRACT_SUBCLASS.read_text(encoding="utf-8"))
    requests = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and name_of(node.func) == "getfixturevalue"
        and any(
            isinstance(arg, ast.Constant) and arg.value == GATE for arg in node.args
        )
    ]
    roster_reads = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Compare)
        and any(
            isinstance(sub, ast.Name) and sub.id == CONTRACT_ROSTER_CONSTANT
            for sub in ast.walk(node)
        )
    ]

    assert requests, (
        f"{CONTRACT_SUBCLASS.name} never calls getfixturevalue({GATE!r}), so "
        f"{CONTRACT_ROSTER_CONSTANT} gates nothing."
    )
    assert roster_reads, (
        f"{CONTRACT_SUBCLASS.name} never tests a name against "
        f"{CONTRACT_ROSTER_CONSTANT}, so the gate is not selected by it."
    )


def test_the_contract_exemptions_are_still_query_callers() -> None:
    callers = _query_calling_tests(CONTRACT.read_text(encoding="utf-8"))

    assert set(CONTRACT_UNGATED_QUERY_TESTS) <= set(callers), (
        f"CONTRACT_UNGATED_QUERY_TESTS names contract cases that no longer "
        f"call .{QUERY_METHOD}(): "
        f"{sorted(set(CONTRACT_UNGATED_QUERY_TESTS) - set(callers))}"
    )


def test_the_reset_exemption_premise_holds() -> None:
    """The reset case is ungated only because the store has no reset.

    If ``Neo4jVectorStore`` ever implements ``reset_storage``, the contract
    case takes its other branch and queries — so the exemption has to be
    re-read, not left standing. Read as text for the same reason as above.
    """
    source = STORE_SOURCE.read_text(encoding="utf-8")
    [cls] = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ClassDef) and node.name == VECTOR_STORE_CLASS
    ]
    methods = {
        item.name
        for item in cls.body
        if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef)
    }

    assert QUERY_METHOD in methods, "the class scan found the wrong class"
    assert RESET_METHOD not in methods, (
        f"{VECTOR_STORE_CLASS} now defines {RESET_METHOD}(), so "
        f"test_reset_storage_matches_supports_reset reaches .{QUERY_METHOD}() "
        f"on this backend. Move it into {CONTRACT_ROSTER_CONSTANT} and drop "
        f"its CONTRACT_UNGATED_QUERY_TESTS entry."
    )


def test_the_scanned_method_is_the_one_that_emits_search() -> None:
    """Anchor the scan's key to the clause it exists to find.

    :data:`QUERY_METHOD` is the whole basis of the rule above: if the method
    that issues ``SEARCH`` is renamed, or the clause moves to a helper, the
    scan matches nothing and every assertion here passes on an empty set. Read
    as text rather than imported, so this runs without the ``neo4j`` driver
    installed — the rule has to hold in the default suite, not only where the
    optional extra is present.

    The clause is looked for in the method's **string literals**, not in its
    source text. That distinction is not fastidiousness: the first cut asserted
    over ``ast.get_source_segment`` and survived a mutant that broke the emitted
    keyword, because the four comments in the method body still say "SEARCH".
    A guard satisfied by a comment about the thing is not a guard on the thing.
    """
    source = STORE_SOURCE.read_text(encoding="utf-8")
    [method] = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef) and node.name == QUERY_METHOD
    ]
    docstring = ast.get_docstring(method)
    emitted = [
        node.value
        for node in ast.walk(method)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value != docstring
    ]

    assert any("SEARCH" in literal for literal in emitted)


@pytest.mark.parametrize(
    ("signature", "expected_gated"),
    [
        (f"self, {GATE}, stores", True),
        (f"self, stores, {GATE}", True),
        ("self, stores", False),
    ],
)
def test_the_scan_separates_gated_from_ungated(
    signature: str, expected_gated: bool
) -> None:
    """Prove the predicate discriminates, on a synthetic tree.

    Without this, every assertion above is satisfiable by a scan that has
    stopped finding anything — which is how a derived rule rots into a rule
    that passes because it is blind.
    """
    synthetic = (
        "class TestQuery:\n"
        f"    def test_synthetic({signature}):\n"
        "        _, vector = stores\n"
        "        assert vector.query([1.0], top_k=1) == []\n"
    )
    callers = _query_calling_tests(synthetic)

    assert set(callers) == {"test_synthetic"}
    assert (GATE in callers["test_synthetic"]) is expected_gated


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
    Measured on ``neo4j:2025.12`` — the image the job provisions — which is also
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
