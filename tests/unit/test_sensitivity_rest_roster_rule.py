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
  store counts as naming it. Known limits: a name bound to the registry on
  another line (``k = registry.knowledge; helper(k)``) is not followed, and
  ``gated`` shows a clearance call is reached, not that its result is applied;
  the producers' own tests carry that.
* ``admin-only``: the router is mounted behind ``admin_auth`` in ``app.py``, or
  the decorator carries an admin dependency. Admin clears ``restricted``.
* ``mutation``: the handler builds the governed ``MutationExecutor``
  (``build_curate_executor``), and its writes go through it. It keeps every
  check ``no-document-content`` makes (no document or vector store named, no
  registry handed to any other helper); the one exemption is the arguments of
  the ``build_curate_executor(...)`` call itself, which has to be given the
  registry. The executor has no clearance rule on main yet: the mutation-side
  rule is a separate PR (#1027), and until it lands this claim is only a
  statement that the route reads no document content itself, not that the
  executor withholds one. A later rule can tighten it once the executor does.
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
#: executor has no clearance rule on main (PR #1027 adds one), so this names
#: the one call whose registry argument a ``mutation`` route is excused for.
EXECUTOR_CALL = "build_curate_executor"

GATE_CALLS = frozenset(
    {"request_clearance", "refuse_document_overwrite", "stamp_request_sensitivity"}
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

#: ``GET /classify/candidates`` mines keywords from every document, so it shows
#: derived text a below-clearance key should not see. Left out of the gate for
#: now, like graph entity names; remove the entry when it is gated.
KNOWN_GAPS = frozenset(
    {"classify_rules:list_classify_candidates", "ingest:upsert_vectors"}
)

ROSTER: dict[str, str] = {
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
    "classify_rules:list_classify_candidates": LEFT_OUT,  # GET /classify/candidates
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
    "curate:record_feedback": MUTATION,  # POST /feedback
    "curate:pack_feedback": FREE,  # POST /packs/{pack_id}/feedback
    "explore:list_documents": GATED,  # GET /documents
    "explore:get_document": GATED,  # GET /documents/{doc_id:path}
    "explore:list_events": ADMIN,  # GET /events
    "explore:get_event": ADMIN,  # GET /events/{event_id}
    "explore:list_packs": ADMIN,  # GET /packs
    "explore:get_pack": ADMIN,  # GET /packs/{pack_id}
    "explore:get_node_history": FREE,  # GET /graph/history
    "extract:submit_drafts": MUTATION,  # POST /extract/drafts
    "health:healthz": FREE,  # GET /healthz
    "health:readyz": FREE,  # GET /readyz
    "ingest:ingest_trace": FREE,  # POST /traces
    "ingest:ingest_evidence": GATED,  # POST /evidence
    "ingest:upsert_vectors": LEFT_OUT,  # POST /vectors
    "ingest:ingest_bulk": FREE,  # POST /ingest/bulk
    "mutations:execute_batch": MUTATION,  # POST /commands/batch
    "observations:record_observation": MUTATION,  # POST /observations
    "observations:list_observations": FREE,  # GET /observations
    "observations:record_measurement": MUTATION,  # POST /measurements
    "observations:list_measurements": FREE,  # GET /measurements
    "policies:list_policies": FREE,  # GET /policies
    "policies:get_policy": FREE,  # GET /policies/{policy_id}
    "policies:create_policy": ADMIN,  # POST /policies
    "policies:delete_policy": ADMIN,  # DELETE /policies/{policy_id}
    "retrieve:search": GATED,  # GET /search
    "retrieve:assemble_pack": GATED,  # POST /packs
    "retrieve:assemble_sectioned_pack": GATED,  # POST /packs/sectioned
    "retrieve:search_entities": FREE,  # GET /graph/search
    "retrieve:search_entity_facets": FREE,  # GET /graph/search/facets
    "retrieve:get_entity": FREE,  # GET /entities/{entity_id:path}
    "retrieve:list_traces": FREE,  # GET /traces
    "retrieve:list_trace_domains": FREE,  # GET /traces/domains
    "retrieve:get_trace": GATED,  # GET /traces/{trace_id}
    "retrieve:list_precedents": FREE,  # GET /precedents
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
_REGISTRY_ROOTS = frozenset({"registry", "knowledge"})


def _chain(expr: ast.expr) -> tuple[ast.expr, list[str]]:
    """The root of an attribute/subscript chain and the attributes along it."""
    attrs: list[str] = []
    while isinstance(expr, ast.Attribute | ast.Subscript):
        if isinstance(expr, ast.Attribute):
            attrs.append(expr.attr)
        expr = expr.value
    return expr, attrs[::-1]


def _is_registry_handoff(expr: ast.expr) -> bool:
    """Whether ``expr`` is the registry, or its whole knowledge bundle.

    ``registry``, ``knowledge``, ``registry.knowledge``, ``get_registry()`` and
    ``get_registry().knowledge``: whatever receives one can reach a document
    store without naming it. ``registry.knowledge.graph_store`` is not one;
    it hands over a single non-document store. A name bound to the registry
    on another line is not followed.
    """
    root, attrs = _chain(expr)
    if (
        isinstance(root, ast.Call)
        and isinstance(root.func, ast.Name)
        and root.func.id == "get_registry"
    ):
        return attrs in ([], ["knowledge"])
    if isinstance(root, ast.Name) and root.id in _REGISTRY_ROOTS:
        return attrs == [] or (root.id == "registry" and attrs == ["knowledge"])
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
                _is_registry_handoff(a)
                for n in nodes
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
    # Hand read: 76 ``@router.<verb>`` decorators across the modules, on 75
    # handler functions (one handler carries two routes).
    assert_hand_read_floor(
        len(route_names(sources)),
        75,
        subject="REST route handlers",
        hint="a route module was skipped, or the decorator spelling changed",
    )
    found = violations(sources, ROSTER, _admin_mounted(APP.read_text(encoding="utf-8")))
    assert found == [], "\n".join(found)


def test_the_known_gaps_only_shrink() -> None:
    assert {k for k, v in ROSTER.items() if v == LEFT_OUT} == KNOWN_GAPS


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
