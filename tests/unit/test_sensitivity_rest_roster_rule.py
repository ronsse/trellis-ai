"""Every REST route is behind the sensitivity gate or says why it needs none (#194).

The MCP twin is ``test_sensitivity_surface_rule.py``. Each route handler under
``src/trellis_api/routes`` is rostered below with the claim made for it, and the
rule checks the claim against the module's call graph:

* ``gated``: the handler's in-module callees include a clearance call
  (``request_clearance``, or a producer helper built on it), the only source of
  a REST caller's clearance.
* ``no-document-content``: nothing the handler reaches names ``document_store``
  or ``vector_store`` (a bare ``.count`` is not content), and it does not hand
  the registry itself to a helper, unless a person read that helper
  (``HANDS_REGISTRY``). Handing over means ``registry``, ``knowledge``,
  ``registry.knowledge`` or ``get_registry()`` as a positional or keyword
  argument, and ``getattr`` with a string constant naming a document or vector
  store counts as naming it. A local bound to one of those in a plain assignment
  is followed (``k = registry.knowledge; helper(k)``, also ``registry.operational``,
  tuple, annotated and ``:=`` forms), in the function that binds it. Known
  limits: a binding made by ``for``, ``with``, a comprehension or a call's
  result is not followed, and ``gated`` shows a clearance call is reached, not
  that its result is applied; the producers' own tests carry that.
* ``admin-only``: the router is mounted behind ``admin_auth`` in ``app.py``, or
  the decorator carries an admin dependency. Admin clears ``restricted``.
* ``mutation``: the handler builds the governed ``MutationExecutor``
  (``build_curate_executor``), and its writes go through it. It keeps every
  check ``no-document-content`` makes (no document or vector store named, no
  registry handed to any other helper); the one exemption is the arguments of
  the ``build_curate_executor(...)`` call itself, which has to be given the
  registry. The executor enforces the caller's clearance on a command aimed at a
  document above it (Stage 2) and on the label a write carries (the
  ``evidence.ingest`` and ``classification.set`` handlers), and ``clearance`` is
  a required keyword of ``build_curate_executor``, so a route passes
  ``request_clearance(...)`` or names ``"restricted"`` with a reason. This rule
  shows the route reads no document content itself and builds the executor; that
  it passes the right clearance is carried by the routes' own tests
  (``tests/unit/api/test_sensitivity_mutations.py``), which fail when it does
  not, not by this rule.
* ``left-out``: a known gap, named in the PR that shipped this rule. The set may
  only shrink.

A route added without a roster line fails, and so does a stale line.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

from tests.ast_rules import assert_hand_read_floor

ROUTES = Path(__file__).resolve().parents[2] / "src" / "trellis_api" / "routes"
APP = ROUTES.parent / "app.py"

GATED = "gated"
FREE = "no-document-content"
ADMIN = "admin-only"
MUTATION = "mutation"
LEFT_OUT = "left-out"

#: A mutation route builds the governed executor and writes through it. The
#: executor enforces the caller's clearance, so this names only the one call
#: whose registry argument a ``mutation`` route is excused for.
EXECUTOR_CALL = "build_curate_executor"

GATE_CALLS = frozenset(
    {
        "request_clearance",
        "refuse_document_overwrite",
        "refuse_vector_write",
        "stamp_request_sensitivity",
    }
)

#: Hand read: a ``no-document-content`` route that passes the registry to a
#: helper, and the helper reads no document body. ``readyz`` probes counts,
#: ``ingest_trace`` and ``ingest_bulk`` only write, and the rules helper reads
#: the rule file.
HANDS_REGISTRY = frozenset(
    {
        "classify_rules:list_classify_rules",
        "health:readyz",
        "ingest:ingest_trace",
        "ingest:ingest_bulk",
    }
)

#: Routes left out of the gate for now, like graph entity names; remove an
#: entry when it is gated. None today: ``POST /vectors`` is gated and
#: ``GET /classify/candidates`` is admin-only.
KNOWN_GAPS: frozenset[str] = frozenset()

ROSTER: dict[str, str] = {
    "access_log:key_reads": ADMIN,  # GET /access-log/keys
    "admin:health": ADMIN,  # GET /health
    "admin:stats": ADMIN,  # GET /stats
    "admin:runtime": ADMIN,  # GET /runtime
    "admin:loop_health": ADMIN,  # GET /loops
    "admin:effectiveness": ADMIN,  # GET /effectiveness
    "admin:apply_noise_tags": ADMIN,  # POST /effectiveness/apply-noise-tags
    "admin:metrics_timeseries": ADMIN,  # GET /metrics/timeseries
    "admin:generate_advisories": ADMIN,  # POST /advisories/generate
    "admin:list_advisories": ADMIN,  # GET /advisories
    "admin:reset_vectors": ADMIN,  # POST /vectors/reset
    "admin:list_pending_proposals": ADMIN,  # GET /proposals
    "admin:preview_proposal": ADMIN,  # GET /proposals/{proposal_id}/preview
    "admin:promote_proposal_route": ADMIN,  # POST /proposals/{proposal_id}/promote
    "admin:reject_proposal_route": ADMIN,  # POST /proposals/{proposal_id}/reject
    "admin:list_learning_candidates": ADMIN,  # GET /learning/candidates
    "admin:promote_learning_candidates": ADMIN,  # POST /learning/promotions
    "admin:list_schema_evolution_candidates": ADMIN,  # GET /schema-evolution/candidates
    "admin:draft_schema_evolution_adr": ADMIN,  # POST /schema-evolution/.../draft-adr
    "admin:list_code_proposals": ADMIN,  # GET /code-proposals
    "admin:list_admin_jobs": ADMIN,  # GET /admin/jobs
    "admin:patch_admin_job": ADMIN,  # PATCH /admin/jobs/{name}
    "admin:run_admin_job": ADMIN,  # POST /admin/jobs/{name}/run
    "analyze:list_analyze_reports": ADMIN,  # GET /analyze
    "analyze:get_analyze_report": ADMIN,  # GET /analyze/{report}
    "classify_rules:list_classify_candidates": ADMIN,  # GET /classify/candidates
    "classify_rules:mine_classify_candidates": ADMIN,  # POST /classify/candidates/mine
    "classify_rules:apply_classify_rule": ADMIN,  # POST /classify/rules
    "classify_rules:revoke_classify_rule": ADMIN,  # DELETE /classify/rules/{kind}/{key}
    "classify_rules:dismiss_classify_candidate": ADMIN,  # POST /classify/.../dismiss
    "classify_rules:list_classify_rules": FREE,  # GET /classify/rules
    "curate:promote": MUTATION,  # POST /precedents
    "curate:demote": MUTATION,  # POST /precedents/{target_id:path}/demote
    "curate:reinstate": MUTATION,  # POST /precedents/{target_id:path}/reinstate
    "curate:create_link": MUTATION,  # POST /links
    "curate:create_entity": MUTATION,  # POST /entities
    "curate:create_document": GATED,  # POST /documents
    "curate:set_document_sensitivity": GATED,  # POST /documents/.../sensitivity
    "curate:record_feedback": MUTATION,  # POST /feedback
    "curate:pack_feedback": FREE,  # POST /packs/{pack_id}/feedback
    "explore:list_documents": GATED,  # GET /documents
    "explore:get_document": GATED,  # GET /documents/{doc_id:path}
    "explore:list_events": ADMIN,  # GET /events
    "explore:get_event": ADMIN,  # GET /events/{event_id}
    "explore:list_packs": ADMIN,  # GET /packs
    "explore:get_pack": ADMIN,  # GET /packs/{pack_id}
    "explore:get_node_history": GATED,  # GET /graph/history
    "extract:submit_drafts": MUTATION,  # POST /extract/drafts
    "health:healthz": FREE,  # GET /healthz
    "health:readyz": FREE,  # GET /readyz
    "ingest:ingest_trace": FREE,  # POST /traces
    "ingest:ingest_evidence": GATED,  # POST /evidence
    "ingest:upsert_vectors": GATED,  # POST /vectors
    "ingest:ingest_bulk": FREE,  # POST /ingest/bulk
    "mutations:execute_batch": GATED,  # POST /commands/batch
    "observations:record_observation": MUTATION,  # POST /observations
    "observations:list_observations": GATED,  # GET /observations
    "observations:record_measurement": MUTATION,  # POST /measurements
    "observations:list_measurements": FREE,  # GET /measurements
    "policies:list_policies": FREE,  # GET /policies
    "policies:get_policy": FREE,  # GET /policies/{policy_id}
    "policies:create_policy": ADMIN,  # POST /policies
    "policies:delete_policy": ADMIN,  # DELETE /policies/{policy_id}
    "retrieve:search": GATED,  # GET /search
    "retrieve:assemble_pack": GATED,  # POST /packs
    "retrieve:assemble_sectioned_pack": GATED,  # POST /packs/sectioned
    "retrieve:search_entities": GATED,  # GET /graph/search
    "retrieve:search_entity_facets": FREE,  # GET /graph/search/facets (counts only)
    "retrieve:get_entity": GATED,  # GET /entities/{entity_id:path}
    "retrieve:list_traces": FREE,  # GET /traces
    "retrieve:list_trace_domains": FREE,  # GET /traces/domains
    "retrieve:get_trace": GATED,  # GET /traces/{trace_id}
    "retrieve:list_precedents": GATED,  # GET /precedents
    "retrieve:list_precedent_domains": FREE,  # GET /precedents/domains
    "settings:list_settings": ADMIN,  # GET /settings
    "settings:update_setting": ADMIN,  # PUT /settings/{name}
    "version:api_version": FREE,  # GET /api/version
}


def _module_functions(source: str) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    return {
        n.name: n
        for n in ast.parse(source).body
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
    }


def _route_decorators(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.Call]:
    return [
        d
        for d in fn.decorator_list
        if isinstance(d, ast.Call)
        and isinstance(d.func, ast.Attribute)
        and isinstance(d.func.value, ast.Name)
        and d.func.value.id == "router"
    ]


def _reach(
    name: str, functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef]
) -> set[str]:
    seen: set[str] = set()
    todo = [name]
    while todo:
        current = todo.pop()
        if current in seen or current not in functions:
            continue
        seen.add(current)
        todo.extend(
            n.func.id
            for n in ast.walk(functions[current])
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        )
    return seen


def _admin_mounted(app_source: str) -> set[str]:
    """Modules whose router is included with ``dependencies=admin_auth``."""
    mounted: set[str] = set()
    for node in ast.walk(ast.parse(app_source)):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "include_router"
            and node.args
        ):
            continue
        target = node.args[0]
        admin = any(
            k.arg == "dependencies"
            and isinstance(k.value, ast.Name)
            and k.value.id == "admin_auth"
            for k in node.keywords
        )
        if (
            admin
            and isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
        ):
            mounted.add(target.value.id)
    return mounted


def _admin_decorated(decorators: list[ast.Call]) -> bool:
    return any(
        k.arg == "dependencies"
        and (
            "_ADMIN_ONLY" in ast.unparse(k.value)
            or "SCOPE_ADMIN" in ast.unparse(k.value)
        )
        for d in decorators
        for k in d.keywords
    )


def route_names(sources: dict[str, str]) -> set[str]:
    return {
        f"{module}:{name}"
        for module, source in sources.items()
        for name, fn in _module_functions(source).items()
        if _route_decorators(fn)
    }


_DOCUMENT_STORES = frozenset({"document_store", "vector_store"})
_REGISTRY_ROOTS = frozenset({"registry", "knowledge", "operational"})


def _chain(expr: ast.expr) -> tuple[ast.expr, list[str]]:
    """The root of an attribute/subscript chain and the attributes along it."""
    attrs: list[str] = []
    while isinstance(expr, ast.Attribute | ast.Subscript):
        if isinstance(expr, ast.Attribute):
            attrs.append(expr.attr)
        expr = expr.value
    return expr, attrs[::-1]


def _aliases(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> dict[str, list[ast.expr]]:
    """Every expression each local of ``fn`` is bound to in a plain assignment.

    ``k = registry.knowledge``, ``a = b = x``, ``k: T = x``, ``(k := x)`` and the
    element-wise tuple form ``k, o = registry.knowledge, registry.operational``.
    A name bound more than once keeps every binding, since the walk does not
    know which one reaches a call.
    """
    bound: dict[str, list[ast.expr]] = {}

    def bind(target: ast.expr, value: ast.expr) -> None:
        if isinstance(target, ast.Name):
            bound.setdefault(target.id, []).append(value)
        elif (
            isinstance(target, ast.Tuple)
            and isinstance(value, ast.Tuple)
            and len(target.elts) == len(value.elts)
        ):
            for t, v in zip(target.elts, value.elts, strict=True):
                bind(t, v)

    for node in ast.walk(fn):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                bind(target, node.value)
        elif isinstance(node, ast.AnnAssign | ast.NamedExpr) and node.value is not None:
            bind(node.target, node.value)
    return bound


def _is_registry_handoff(
    expr: ast.expr,
    aliases: dict[str, list[ast.expr]] | None = None,
    _seen: frozenset[str] = frozenset(),
) -> bool:
    """Whether ``expr`` is the registry, or a whole plane bundle of it.

    ``registry``, ``knowledge``, ``operational``, ``registry.knowledge``,
    ``registry.operational``, ``get_registry()`` and ``get_registry().knowledge``
    or ``.operational``: whatever receives one can reach a document store
    without naming it. ``registry.knowledge.graph_store`` is not one; it hands
    over a single non-document store. A local in ``aliases`` is the registry
    wherever one of its bindings is, with the attributes read through it
    appended, so ``k = registry.knowledge`` makes ``k`` a handoff and
    ``k.graph_store`` not.
    """
    root, attrs = _chain(expr)
    if (
        isinstance(root, ast.Name)
        and aliases
        and root.id in aliases
        and root.id not in _seen
    ):
        for binding in aliases[root.id]:
            extended = binding
            for attr in attrs:
                extended = ast.Attribute(value=extended, attr=attr, ctx=ast.Load())
            if _is_registry_handoff(extended, aliases, _seen | {root.id}):
                return True
        return False
    if (
        isinstance(root, ast.Call)
        and isinstance(root.func, ast.Name)
        and root.func.id == "get_registry"
    ):
        return attrs in ([], ["knowledge"], ["operational"])
    if isinstance(root, ast.Name) and root.id in _REGISTRY_ROOTS:
        return attrs == [] or (
            root.id == "registry" and attrs in (["knowledge"], ["operational"])
        )
    return False


def _is_constant_getattr(call: ast.Call) -> bool:
    """``getattr(x, "name")``: it names one attribute, so ``x`` is not handed on.

    A document or vector store named this way is caught by
    ``_touched_document_stores``; a computed name is not constant and stays
    flagged.
    """
    return (
        isinstance(call.func, ast.Name)
        and call.func.id == "getattr"
        and len(call.args) >= 2
        and isinstance(call.args[1], ast.Constant)
        and isinstance(call.args[1].value, str)
    )


def _calls(call: ast.Call, name: str) -> bool:
    """Whether ``call`` is a direct call of the function called ``name``."""
    return isinstance(call.func, ast.Name) and call.func.id == name


def _touched_document_stores(nodes: list[ast.AST]) -> list[str]:
    """Document or vector stores named other than for a bare ``.count``.

    Both spellings count: ``x.document_store`` and ``getattr(x,
    "document_store")``.
    """
    counted = {
        id(n.value) for n in nodes if isinstance(n, ast.Attribute) and n.attr == "count"
    }
    named = {
        n.attr
        for n in nodes
        if isinstance(n, ast.Attribute)
        and n.attr in _DOCUMENT_STORES
        and id(n) not in counted
    }
    named |= {
        n.args[1].value
        for n in nodes
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "getattr"
        and len(n.args) >= 2
        and isinstance(n.args[1], ast.Constant)
        and n.args[1].value in _DOCUMENT_STORES
    }
    return sorted(named)


def violations(
    sources: dict[str, str],
    roster: dict[str, str],
    admin_modules: set[str],
) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for module, source in sources.items():
        functions = _module_functions(source)
        for name, fn in functions.items():
            decorators = _route_decorators(fn)
            if not decorators:
                continue
            key = f"{module}:{name}"
            seen.add(key)
            claim = roster.get(key)
            if claim is None:
                found.append(f"{key}: route is not rostered")
                continue
            reached = _reach(name, functions)
            nodes = [n for f in reached for n in ast.walk(functions[f])]
            calls = {
                n.func.id
                for n in nodes
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            }
            touched = _touched_document_stores(nodes)
            hands_registry = any(
                _is_registry_handoff(a, _aliases(functions[f]))
                for f in reached
                for n in ast.walk(functions[f])
                if isinstance(n, ast.Call)
                and not _is_constant_getattr(n)
                and not (claim == MUTATION and _calls(n, EXECUTOR_CALL))
                for a in [*n.args, *(k.value for k in n.keywords)]
            )
            reads_nothing = claim in {FREE, MUTATION}
            what = "no document content" if claim == FREE else "mutation"
            if claim == GATED and not calls & GATE_CALLS:
                found.append(f"{key}: claims gated but never reaches a clearance call")
            elif reads_nothing and touched:
                found.append(f"{key}: claims {what} but touches {touched}")
            elif reads_nothing and hands_registry and key not in HANDS_REGISTRY:
                found.append(f"{key}: claims {what} but hands the registry to a helper")
            elif (
                claim == ADMIN
                and module not in admin_modules
                and not _admin_decorated(decorators)
            ):
                found.append(
                    f"{key}: claims admin-only but is not behind an admin dependency"
                )
            elif claim == MUTATION and EXECUTOR_CALL not in calls:
                found.append(f"{key}: claims mutation but never builds the executor")
            elif claim == LEFT_OUT and key not in KNOWN_GAPS:
                found.append(f"{key}: is left out without being a named known gap")
            elif claim not in {GATED, FREE, ADMIN, MUTATION, LEFT_OUT}:
                found.append(f"{key}: unknown claim {claim!r}")
    found.extend(
        f"{key}: rostered but no such route" for key in sorted(set(roster) - seen)
    )
    return found


def _sources() -> dict[str, str]:
    return {
        p.stem: p.read_text(encoding="utf-8")
        for p in sorted(ROUTES.glob("*.py"))
        if not p.name.startswith("_")
    }


def test_every_rest_route_is_rostered_and_its_claim_holds() -> None:
    sources = _sources()
    # Hand read: 77 ``@router.<verb>`` decorators across the modules, on 76
    # handler functions (one handler carries two routes).
    assert_hand_read_floor(
        len(route_names(sources)),
        76,
        subject="REST route handlers",
        hint="a route module was skipped, or the decorator spelling changed",
    )
    found = violations(sources, ROSTER, _admin_mounted(APP.read_text(encoding="utf-8")))
    assert found == [], "\n".join(found)


def test_the_known_gaps_only_shrink() -> None:
    assert {k for k, v in ROSTER.items() if v == LEFT_OUT} == KNOWN_GAPS


#: Each spelling binds a local to the registry or a plane of it, then hands the
#: local to a helper. Bodies sit one indent deep inside a route function.
LOCAL_HANDOFFS = {
    "knowledge": "k = registry.knowledge\n    return helper(k)",
    "operational": "o = registry.operational\n    return helper(o)",
    "keyword": "k = registry.knowledge\n    return helper(store=k)",
    "get_registry": "r = get_registry()\n    return helper(r)",
    "get_registry_knowledge": ("k = get_registry().knowledge\n    return helper(k)"),
    "alias_of_an_alias": "k = registry.knowledge\n    j = k\n    helper(j)",
    "chained_targets": "a = b = registry.knowledge\n    return helper(b)",
    "annotated": "k: Any = registry.knowledge\n    return helper(k)",
    "walrus": "return helper(k := registry.knowledge)\n    helper(k)",
    "tuple_unpack": "x, k = 1, registry.knowledge\n    return helper(k)",
    "attribute_of_a_registry_alias": (
        "r = get_registry()\n    return helper(r.knowledge)"
    ),
    "bound_twice": "k = registry.knowledge\n    k = other\n    return helper(k)",
}


class TestTheRuleCatchesADefectiveSubject:
    ADMIN_APP = (
        "app.include_router(admin.router, dependencies=admin_auth)\n"
        "app.include_router(ingest.router)\n"
    )
    READS = (
        "@router.get('/x')\n"
        "def reads():\n"
        "    return registry.knowledge.document_store.get('a')\n"
    )
    GATE = (
        "@router.get('/x')\n"
        "def reads(ctx):\n"
        "    request_clearance(ctx)\n"
        "    return registry.knowledge.document_store.get('a')\n"
    )

    def test_an_unrostered_route_fails(self) -> None:
        assert "m:reads: route is not rostered" in violations(
            {"m": self.READS}, {}, set()
        )

    def test_a_gated_claim_without_a_gate_fails(self) -> None:
        found = violations({"m": self.READS}, {"m:reads": GATED}, set())
        assert any("never reaches a clearance call" in f for f in found)

    def test_a_gate_reached_through_a_helper_passes(self) -> None:
        source = (
            "def helper(ctx):\n    return request_clearance(ctx)\n\n"
            "@router.get('/x')\ndef reads(ctx):\n    return helper(ctx)\n"
        )
        assert violations({"m": source}, {"m:reads": GATED}, set()) == []
        assert violations({"m": self.GATE}, {"m:reads": GATED}, set()) == []

    def test_a_free_claim_that_touches_documents_fails(self) -> None:
        found = violations({"m": self.READS}, {"m:reads": FREE}, set())
        assert any("touches ['document_store']" in f for f in found)

    def test_a_free_claim_that_only_counts_passes(self) -> None:
        source = (
            "@router.get('/x')\ndef n():\n"
            "    return registry.knowledge.document_store.count()\n"
        )
        assert violations({"m": source}, {"m:n": FREE}, set()) == []

    def test_a_free_claim_handing_over_the_registry_fails(self) -> None:
        source = "@router.get('/x')\ndef n():\n    return helper(registry)\n"
        found = violations({"m": source}, {"m:n": FREE}, set())
        assert any("hands the registry" in f for f in found)

    @pytest.mark.parametrize(
        ("shape", "body", "expect"),
        [
            ("positional", "helper(registry)", "hands the registry"),
            ("keyword", "helper(registry=registry)", "hands the registry"),
            ("keyword_knowledge", "helper(k=registry.knowledge)", "hands the registry"),
            ("attribute_chain", "helper(registry.knowledge)", "hands the registry"),
            ("knowledge_name", "helper(knowledge)", "hands the registry"),
            ("get_registry_arg", "helper(get_registry())", "hands the registry"),
            (
                "get_registry_keyword",
                "helper(registry=get_registry())",
                "hands the registry",
            ),
            (
                "get_registry_knowledge",
                "helper(get_registry().knowledge)",
                "hands the registry",
            ),
            (
                "getattr_string",
                "getattr(registry.knowledge, 'document_store').get('a')",
                "touches ['document_store']",
            ),
            (
                "getattr_vector",
                "getattr(registry.knowledge, 'vector_store').get('a')",
                "touches ['vector_store']",
            ),
        ],
    )
    def test_a_free_claim_cannot_slip_the_registry_past_the_rule(
        self, shape: str, body: str, expect: str
    ) -> None:
        source = f"@router.get('/x')\ndef n():\n    return {body}\n"
        found = violations({"m": source}, {"m:n": FREE}, set())
        assert any(expect in f for f in found), (shape, found)

    def test_a_computed_getattr_on_the_registry_fails(self) -> None:
        source = (
            "@router.get('/x')\ndef n(name):\n"
            "    return getattr(registry.knowledge, name)\n"
        )
        found = violations({"m": source}, {"m:n": FREE}, set())
        assert any("hands the registry" in f for f in found)

    @pytest.mark.parametrize(
        "body",
        [
            "helper(registry.knowledge.graph_store)",
            "helper(registry.operational.event_log)",
            "helper(get_registry().knowledge.graph_store)",
            "getattr(registry.knowledge, 'graph_store')",
            "helper(request)",
        ],
    )
    def test_a_free_claim_naming_a_non_document_store_passes(self, body: str) -> None:
        source = f"@router.get('/x')\ndef n():\n    return {body}\n"
        assert violations({"m": source}, {"m:n": FREE}, set()) == []

    @pytest.mark.parametrize("body", LOCAL_HANDOFFS.values(), ids=list(LOCAL_HANDOFFS))
    def test_a_free_claim_cannot_slip_the_registry_past_the_rule_by_a_local(
        self, body: str
    ) -> None:
        source = f"@router.get('/x')\ndef n():\n    {body}\n"
        found = violations({"m": source}, {"m:n": FREE}, set())
        assert any("hands the registry" in f for f in found), found

    @pytest.mark.parametrize(
        "body",
        [
            "g = registry.knowledge.graph_store\n    return helper(g)",
            "k = registry.knowledge\n    return helper(k.graph_store)",
            "k = registry.knowledge\n    return getattr(k, 'graph_store')",
            "e = registry.operational.event_log\n    return helper(e)",
            "n = len(registry.knowledge.graph_store.query())\n    return helper(n)",
            "k = registry.knowledge\n    k = k.graph_store\n    return helper(k.x)",
        ],
    )
    def test_a_local_bound_to_a_non_document_store_passes(self, body: str) -> None:
        source = f"@router.get('/x')\ndef n():\n    {body}\n"
        assert violations({"m": source}, {"m:n": FREE}, set()) == []

    def test_a_local_in_a_reached_helper_is_followed(self) -> None:
        source = (
            "def dig():\n    k = registry.knowledge\n    return helper(k)\n\n"
            "@router.get('/x')\ndef n():\n    return dig()\n"
        )
        found = violations({"m": source}, {"m:n": FREE}, set())
        assert any("hands the registry" in f for f in found)

    def test_a_local_is_scoped_to_the_function_that_binds_it(self) -> None:
        source = (
            "def dig():\n    k = registry.knowledge\n    return k.graph_store\n\n"
            "@router.get('/x')\ndef n(k):\n    dig()\n    return helper(k)\n"
        )
        assert violations({"m": source}, {"m:n": FREE}, set()) == []

    def test_a_mutation_claim_cannot_hand_a_local_to_another_helper(self) -> None:
        source = (
            "@router.post('/x')\ndef w(cmd):\n"
            "    k = get_registry()\n"
            "    build_curate_executor(k).execute(cmd)\n"
            "    return dump_everything(k)\n"
        )
        found = violations({"m": source}, {"m:w": MUTATION}, set())
        assert any("claims mutation but hands the registry" in f for f in found)

    def test_a_mutation_claim_may_give_a_local_to_the_executor(self) -> None:
        source = (
            "@router.post('/x')\ndef w(cmd):\n"
            "    k = get_registry()\n"
            "    return build_curate_executor(k).execute(cmd)\n"
        )
        assert violations({"m": source}, {"m:w": MUTATION}, set()) == []

    def test_a_hand_read_registry_handoff_passes(self) -> None:
        source = "@router.get('/x')\ndef n():\n    return helper(registry)\n"
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(sys.modules[__name__], "HANDS_REGISTRY", frozenset({"m:n"}))
            assert violations({"m": source}, {"m:n": FREE}, set()) == []

    def test_a_mutation_claim_without_the_executor_fails(self) -> None:
        source = (
            "@router.post('/x')\ndef w():\n"
            "    return registry.knowledge.graph_store.upsert_node()\n"
        )
        found = violations({"m": source}, {"m:w": MUTATION}, set())
        assert any("never builds the executor" in f for f in found)

    def test_a_mutation_claim_building_the_executor_passes(self) -> None:
        source = (
            "@router.post('/x')\ndef w(cmd):\n"
            "    return build_curate_executor(get_registry()).execute(cmd)\n"
        )
        assert violations({"m": source}, {"m:w": MUTATION}, set()) == []
        helper = (
            "def run(cmd):\n    return build_curate_executor(get_registry())\n\n"
            "@router.post('/x')\ndef w(cmd):\n    return run(cmd)\n"
        )
        assert violations({"m": helper}, {"m:w": MUTATION}, set()) == []

    @pytest.mark.parametrize(
        ("shape", "body", "expect"),
        [
            (
                "executor_then_document_read",
                (
                    "    build_curate_executor(get_registry()).execute(cmd)\n"
                    "    return registry.knowledge.document_store.get('a')\n"
                ),
                "claims mutation but touches ['document_store']",
            ),
            (
                "executor_then_registry_to_another_helper",
                (
                    "    build_curate_executor(get_registry()).execute(cmd)\n"
                    "    return dump_everything(get_registry())\n"
                ),
                "claims mutation but hands the registry to a helper",
            ),
            (
                "executor_in_a_dead_branch",
                (
                    "    if False:\n        build_curate_executor(get_registry())\n"
                    "    return registry.knowledge.vector_store.query([0.0])\n"
                ),
                "claims mutation but touches ['vector_store']",
            ),
            (
                "registry_wrapped_inside_the_executor_call",
                "    return build_curate_executor(dump(get_registry())).execute(cmd)\n",
                "claims mutation but hands the registry to a helper",
            ),
            (
                "getattr_string_beside_the_executor",
                (
                    "    build_curate_executor(get_registry()).execute(cmd)\n"
                    "    return getattr(registry.knowledge, 'vector_store').get('a')\n"
                ),
                "claims mutation but touches ['vector_store']",
            ),
        ],
    )
    def test_a_mutation_claim_keeps_the_free_checks(
        self, shape: str, body: str, expect: str
    ) -> None:
        source = f"@router.post('/x')\ndef w(cmd):\n{body}"
        found = violations({"m": source}, {"m:w": MUTATION}, set())
        assert any(expect in f for f in found), (shape, found)

    def test_the_executor_call_alone_is_the_mutation_exemption(self) -> None:
        source = (
            "@router.post('/x')\ndef w(cmd):\n"
            "    executor = build_curate_executor(get_registry())\n"
            "    return executor.execute(cmd)\n"
        )
        assert violations({"m": source}, {"m:w": MUTATION}, set()) == []
        keyword = (
            "@router.post('/x')\ndef w(cmd):\n"
            "    return build_curate_executor(registry=get_registry()).execute(cmd)\n"
        )
        assert violations({"m": keyword}, {"m:w": MUTATION}, set()) == []

    def test_the_same_handoff_is_not_free_but_is_a_mutation(self) -> None:
        source = (
            "@router.post('/x')\ndef w(cmd):\n"
            "    return build_curate_executor(get_registry()).execute(cmd)\n"
        )
        assert violations({"m": source}, {"m:w": FREE}, set())

    def test_an_admin_claim_outside_an_admin_module_fails(self) -> None:
        assert violations(
            {"ingest": self.READS},
            {"ingest:reads": ADMIN},
            _admin_mounted(self.ADMIN_APP),
        )
        assert (
            violations(
                {"admin": self.READS},
                {"admin:reads": ADMIN},
                _admin_mounted(self.ADMIN_APP),
            )
            == []
        )

    def test_a_stale_roster_entry_fails(self) -> None:
        assert "m:gone: rostered but no such route" in violations(
            {}, {"m:gone": FREE}, set()
        )


# ---------------------------------------------------------------------------
# What a ``public`` caller (an ``external`` key: scopes ``read,external``) meets
# ---------------------------------------------------------------------------
#
# ``external`` satisfies ``read`` and nothing above it, so a route is reachable
# by it exactly when its mount and its decorator ask for no scope above ``read``.
# Every such route must be refused outright (``NOT_FOR_PUBLIC_CALLERS``), or be
# listed below as one that answers, which the rule then requires to be ``gated``
# above (the clearance gate is what narrows its answer to public documents). A
# route cannot default open: a new read route that is neither fails. Routes
# behind ingest, mutate or admin need nothing here, and a route on a mount with
# no auth at all must be listed as open and say why.

NOT_FOR_PUBLIC = "NOT_FOR_PUBLIC_CALLERS"

#: Read-scoped routes that answer an external key, each gated by clearance.
EXTERNAL_ANSWERS = frozenset(
    {
        "retrieve:search",  # GET /search
        "retrieve:assemble_pack",  # POST /packs
        "retrieve:assemble_sectioned_pack",  # POST /packs/sectioned
        "explore:list_documents",  # GET /documents
        "explore:get_document",  # GET /documents/{doc_id}
    }
)

#: Routes on a mount with no auth dependency: the same for every caller.
#: ``readyz`` narrows its own body for an external key (``test_health`` and
#: ``test_external_key_routes`` pin that); the others carry no store content.
EXTERNAL_OPEN = frozenset(
    {
        "version:api_version",  # GET /api/version
        "health:healthz",  # GET /healthz
        "health:readyz",  # GET /readyz
    }
)

_SCOPE_BY_AUTH = {
    "read_auth": "read",
    "ingest_auth": "ingest",
    "mutate_auth": "mutate",
    "admin_auth": "admin",
}

_DECORATOR_SCOPES = (
    ("SCOPE_ADMIN", "admin"),
    ("_ADMIN_ONLY", "admin"),
    ("SCOPE_MUTATE", "mutate"),
    ("SCOPE_INGEST", "ingest"),
)


def _mounts(app_source: str) -> dict[str, str | None]:
    """Module -> the name of the auth list its router is mounted with, if any."""
    mounts: dict[str, str | None] = {}
    for node in ast.walk(ast.parse(app_source)):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "include_router"
            and node.args
        ):
            continue
        target = node.args[0]
        if not (
            isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
        ):
            continue
        mounts[target.value.id] = next(
            (
                k.value.id
                for k in node.keywords
                if k.arg == "dependencies" and isinstance(k.value, ast.Name)
            ),
            None,
        )
    return mounts


def _dependency_text(decorators: list[ast.Call]) -> str:
    return " ".join(
        ast.unparse(k.value)
        for d in decorators
        for k in d.keywords
        if k.arg == "dependencies"
    )


def external_violations(  # noqa: PLR0912
    sources: dict[str, str],
    app_source: str,
    roster: dict[str, str],
    answers: frozenset[str],
    open_routes: frozenset[str],
) -> list[str]:
    mounts = _mounts(app_source)
    found: list[str] = []
    seen: set[str] = set()
    for module, source in sources.items():
        for name, fn in _module_functions(source).items():
            decorators = _route_decorators(fn)
            if not decorators:
                continue
            key = f"{module}:{name}"
            seen.add(key)
            mount = mounts.get(module)
            if mount is None:
                if key not in open_routes:
                    found.append(f"{key}: no auth mount, not listed as open")
                continue
            if mount not in _SCOPE_BY_AUTH:
                found.append(
                    f"{key}: mounted behind {mount!r}, which is not understood"
                )
                continue
            text = _dependency_text(decorators)
            required = {_SCOPE_BY_AUTH[mount]} | {
                scope for token, scope in _DECORATOR_SCOPES if token in text
            }
            if required - {"read"}:
                if key in answers or key in open_routes:
                    found.append(
                        f"{key}: listed for an external key but needs "
                        f"{sorted(required)}"
                    )
                continue
            if key in open_routes:
                found.append(f"{key}: listed as open but is behind read auth")
            refused = NOT_FOR_PUBLIC in text
            if key in answers:
                if refused:
                    found.append(
                        f"{key}: listed as answering but carries {NOT_FOR_PUBLIC}"
                    )
                if roster.get(key) != GATED:
                    found.append(
                        f"{key}: answers an external key but is not rostered gated"
                    )
            elif not refused:
                found.append(
                    f"{key}: reachable by an external key, neither refused "
                    "nor listed as answering"
                )
    found.extend(
        f"{key}: listed for an external key but no such route"
        for key in sorted((answers | open_routes) - seen)
    )
    return found


def test_no_route_defaults_open_to_an_external_key() -> None:
    sources = _sources()
    app_source = APP.read_text(encoding="utf-8")
    # Hand read: read-scoped routes in the five read-mounted modules that are not
    # behind a stronger decorator scope: 5 answering, 14 refused (retrieve 8,
    # explore 1, observations 2, policies 2, classify_rules 1), plus 3 on
    # unauthenticated mounts.
    assert_hand_read_floor(
        len(EXTERNAL_ANSWERS) + len(EXTERNAL_OPEN),
        8,
        subject="routes listed as answering or open to an external key",
        hint="a route was dropped from the lists without a person re-reading them",
    )
    refused = sum(
        1
        for source in sources.values()
        for fn in _module_functions(source).values()
        if NOT_FOR_PUBLIC in _dependency_text(_route_decorators(fn))
    )
    assert_hand_read_floor(
        refused,
        14,
        subject="routes refused to an external key",
        hint="a refusal was removed from a route, or the dependency spelling changed",
    )
    found = external_violations(
        sources, app_source, ROSTER, EXTERNAL_ANSWERS, EXTERNAL_OPEN
    )
    assert found == [], "\n".join(found)


class TestTheExternalRuleCatchesADefectiveSubject:
    APP = (
        "app.include_router(m.router, dependencies=read_auth)\n"
        "app.include_router(a.router, dependencies=admin_auth)\n"
        "app.include_router(v.router)\n"
    )
    REFUSED = (
        "@router.get('/x', dependencies=NOT_FOR_PUBLIC_CALLERS)\n"
        "def refused():\n    return 1\n"
    )
    OPEN_ROUTE = "@router.get('/y')\ndef pinged():\n    return 1\n"

    def _run(
        self,
        module_source: str,
        *,
        answers: frozenset[str] = frozenset(),
        roster: dict[str, str] | None = None,
        extra: dict[str, str] | None = None,
        open_routes: frozenset[str] = frozenset({"v:pinged"}),
    ) -> list[str]:
        sources = {"m": module_source, "v": self.OPEN_ROUTE, **(extra or {})}
        return external_violations(
            sources, self.APP, roster or {}, answers, open_routes
        )

    def test_the_clean_subject_passes(self) -> None:
        assert self._run(self.REFUSED) == []

    def test_a_new_read_route_that_is_not_refused_fails(self) -> None:
        source = self.REFUSED + "@router.get('/z')\ndef sneaky():\n    return 1\n"
        expected = (
            "m:sneaky: reachable by an external key, neither refused "
            "nor listed as answering"
        )
        assert self._run(source) == [expected]

    def test_a_refusal_removed_from_a_route_fails(self) -> None:
        source = self.REFUSED.replace(", dependencies=NOT_FOR_PUBLIC_CALLERS", "")
        assert any("neither refused" in v for v in self._run(source))

    def test_an_answering_route_must_be_rostered_gated(self) -> None:
        source = "@router.get('/z')\ndef answers():\n    return 1\n"
        found = self._run(source, answers=frozenset({"m:answers"}), roster={})
        assert found == ["m:answers: answers an external key but is not rostered gated"]
        assert (
            self._run(
                source, answers=frozenset({"m:answers"}), roster={"m:answers": GATED}
            )
            == []
        )

    def test_a_route_cannot_both_answer_and_refuse(self) -> None:
        found = self._run(
            self.REFUSED, answers=frozenset({"m:refused"}), roster={"m:refused": GATED}
        )
        assert any("answering but carries" in v for v in found)

    def test_an_admin_route_needs_no_listing(self) -> None:
        assert (
            self._run(
                self.REFUSED,
                extra={"a": "@router.get('/a')\ndef adm():\n    return 1\n"},
            )
            == []
        )

    def test_a_route_level_admin_dependency_on_a_read_mount_needs_no_listing(
        self,
    ) -> None:
        source = self.REFUSED + (
            "@router.get('/q', dependencies=[Depends(require_scope(SCOPE_ADMIN))])\n"
            "def closed():\n    return 1\n"
        )
        assert self._run(source) == []

    def test_an_unauthenticated_route_must_be_listed_as_open(self) -> None:
        assert self._run(self.REFUSED, open_routes=frozenset()) == [
            "v:pinged: no auth mount, not listed as open"
        ]

    def test_a_stale_listing_fails(self) -> None:
        found = self._run(self.REFUSED, answers=frozenset({"m:gone"}))
        assert found == ["m:gone: listed for an external key but no such route"]

    def test_a_mount_behind_an_unknown_auth_list_fails(self) -> None:
        app = "app.include_router(m.router, dependencies=custom_auth)\n"
        found = external_violations(
            {"m": self.REFUSED}, app, {}, frozenset(), frozenset()
        )
        assert any("not understood" in v for v in found)
