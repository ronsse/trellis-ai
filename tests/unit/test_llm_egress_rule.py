"""Every path that sends stored content to an LLM goes through the egress predicate.

``trellis.llm.egress`` answers "may this content reach this route?": content
labelled above ``internal`` reaches a route only if the route declares itself
local. The predicate is only as good as its coverage, and coverage is the part
that rots: the next job that sends a document to a model will be written by
someone who has never read that module, and it will work.

So the senders are derived off ``src/`` by AST on every run, not listed:

A. **Inner senders.** Every ``<client>.generate(..., messages=...)`` in
   ``src/`` is a place content leaves for a model. Each is either *gated* (a
   hand-written list of the entry points that reach it, which must exist and
   must call the predicate) or *exempt* (with a written reason). A sender
   found in the tree and in neither list fails, and so does a list entry the
   tree no longer has, so the roster cannot drift from the code in either
   direction.
B. **Entry points call the predicate.** Each gated entry point calls
   ``may_reach_route``, ``EgressGate.permits`` or ``EgressGate.permits_metadata``,
   directly or through one named helper that does.
C. **Callers of the senders are rostered.** A call to a function that reaches
   an inner sender (``judge_reconcile``, ``run_memory_extraction``,
   ``EnrichmentService``, ``LLMExtractor``, ``LLMFacetClassifier`` and the
   classifier builder) must sit inside a roster
   entry point or a declared construct-only helper, so a new caller does not
   slip past A by reusing a sender it never reads.
D. **The gate is opened only by a route's declaration.** ``EgressGate(...)``
   is constructed in ``src/`` only with a literal ``local=False``; the sole way
   to a gate that admits everything is ``EgressGate.for_route``, which reads
   the route's own declaration.

The floors are hand-read off ``src/`` (see ``assert_hand_read_floor``), never
taken from the scan they guard, and the rule's predicates are run against a
deliberately defective subject below.

**This rule is a tripwire, not the guard.** It catches the common shapes of a
new sender or caller: ``.generate(messages=...)`` or ``.generate(**kwargs)``, a
literal ``getattr(client, "generate")``, a sink called under an import alias, a
gate built under an alias. The guard is the behaviour test beside each sender:
a labelled document, a route not declared local, a recording fake that must see
nothing. An exempt function is one that needs a re-read when what it sends
changes; the roster does not notice that it did.

Known evasions, none of them caught, each pinned in
``test_the_known_evasions_are_named_not_caught`` so this list stays true:
``gen = client.generate`` and ``functools.partial`` hide the call; an
attribute write such as ``gate.local = True`` opens a gate after construction;
a sender or service passed in as an argument is built where this scan does not
look; an entry that calls ``gate.permits(...)`` and ignores the result, or
checks one path of several, still counts as passing. A call spelled
``getattr(client, "gen" + "erate")`` is invisible too; what bounds it is that
every provider client is built through ``StoreRegistry.build_llm_client``, which
``test_llm_consumer_rule.py`` polices.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.ast_rules import (
    assert_hand_read_floor,
    construction_names,
    iter_modules,
    name_of,
)

_SRC = Path(__file__).resolve().parents[2] / "src"

#: Calls that answer "may this content reach this route?".
_PREDICATE_NAMES = frozenset({"may_reach_route", "permits", "permits_metadata"})

#: The module that defines the predicate; it may build gates however it likes.
_EGRESS_MODULE = "trellis/llm/egress.py"


@dataclass(frozen=True)
class Entry:
    """A function that hands stored content to an inner sender.

    ``via`` names one helper the entry calls and that itself calls the
    predicate, for the two entry points whose check sits one hop away.
    """

    module: str
    function: str
    via: str | None = None


_SERVER_EXTRACTION = Entry("trellis/mcp/server.py", "_emit_memory_stored_and_enrich")
_SYNC_EXTRACTION = Entry("trellis/ingest_corpus/sync.py", "_apply_record")
_ENRICH_BATCH = Entry("trellis_cli/worker.py", "_run_batch_enrichment")
_SHADOW_ITEM = Entry("trellis/classify/shadow.py", "shadow_classify_item")
_SERVER_RECONCILE = Entry(
    "trellis/mcp/server.py",
    "_compute_reconcile_outcome",
    via="_reconcile_egress_permits",
)
_CAPTURE_RECONCILE = Entry(
    "trellis_workers/session_capture/reconcile_pass.py",
    "adjudicate",
    via="_load_existing",
)

#: Inner sender -> the entry points that reach it. Hand-written.
_GATED: dict[tuple[str, str], tuple[Entry, ...]] = {
    ("trellis/extract/llm.py", "extract"): (_SERVER_EXTRACTION, _SYNC_EXTRACTION),
    ("trellis_workers/enrichment/service.py", "enrich"): (_ENRICH_BATCH, _SHADOW_ITEM),
    ("trellis/mcp/reconcile.py", "judge_reconcile"): (
        _SERVER_RECONCILE,
        _CAPTURE_RECONCILE,
    ),
}

#: Inner senders that do not send a stored, labelled document, each with why.
_EXEMPT: dict[tuple[str, str], str] = {
    ("trellis_workers/session_capture/distill.py", "distill_session"): (
        "sends the session transcript digest, which is not a stored document "
        "and carries no label of its own; a labelled document served to a "
        "session can reach a transcript, be captured unlabelled and become an "
        "enrichment candidate, and nothing here follows that path"
    ),
    ("trellis_workers/learning/miner.py", "_ask_llm"): (
        "sends traces, which carry no sensitivity label in this slice"
    ),
}

#: Functions that reach an inner sender; a call to one must sit in a roster entry.
_SINK_NAMES = frozenset(
    {
        "judge_reconcile",
        "run_memory_extraction",
        "EnrichmentService",
        "build_llm_facet_classifier",
        "LLMExtractor",
        "LLMFacetClassifier",
    }
)

#: Callers of a sink that build but never send, each with why.
_CONSTRUCT_ONLY: dict[tuple[str, str], str] = {
    ("trellis_cli/classify.py", "_require_llm_facet_classifier"): (
        "builds the classifier; its only sender is shadow_classify_item, "
        "which is a roster entry point and passes the gate"
    ),
    ("trellis/extract/save_memory.py", "build_save_memory_extractor"): (
        "builds the extractor and sends nothing; its only sender is "
        "LLMExtractor.extract, which the rostered entry points reach "
        "only after the predicate"
    ),
    ("trellis/classify/classifiers/llm.py", "build_llm_facet_classifier"): (
        "builds the classifier and sends nothing; its only sender is the "
        "classifier's enrich call, reached from shadow_classify_item "
        "after the predicate"
    ),
}

#: Hand-read off ``src/``: extract, enrich, judge_reconcile, distill, _ask_llm.
_SENDER_FLOOR = 5
#: Hand-read: two extraction sites, enrich batch, shadow item, two reconcile.
_ENTRY_FLOOR = 6
#: Hand-read: judge_reconcile x2, run_memory_extraction x2, EnrichmentService x2,
#: build_llm_facet_classifier x1, LLMExtractor x1, LLMFacetClassifier x1.
_SINK_CALL_FLOOR = 9


def _calls_with_scope(
    node: ast.AST, scope: str = "<module>"
) -> Iterator[tuple[ast.Call, str]]:
    """Every call under *node* with the name of its innermost enclosing function."""
    for child in ast.iter_child_nodes(node):
        inner = (
            child.name
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef)
            else scope
        )
        if isinstance(child, ast.Call):
            yield child, scope
        yield from _calls_with_scope(child, inner)


def _is_sender_call(call: ast.Call) -> bool:
    """A call into the LLM client protocol.

    ``<anything>.generate(..., messages=...)``, the same with ``**kwargs``
    (which may carry ``messages``), or a literal ``getattr(x, "generate")``.
    """
    if isinstance(call.func, ast.Attribute) and call.func.attr == "generate":
        return any(keyword.arg in {"messages", None} for keyword in call.keywords)
    return (
        name_of(call.func) == "getattr"
        and len(call.args) >= 2
        and isinstance(call.args[1], ast.Constant)
        and call.args[1].value == "generate"
    )


def _relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def inner_senders(root: Path) -> dict[tuple[str, str], list[int]]:
    """``{(module, function): [lines]}`` for every inner sender under *root*."""
    found: dict[tuple[str, str], list[int]] = {}
    for path, tree in iter_modules(root):
        for call, scope in _calls_with_scope(tree):
            if _is_sender_call(call):
                key = (_relative(root, path), scope)
                found.setdefault(key, []).append(call.lineno)
    return found


def _functions(tree: ast.Module, name: str) -> list[ast.AST]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name == name
    ]


def _calls_any(nodes: list[ast.AST], names: frozenset[str] | set[str]) -> bool:
    return any(
        isinstance(inner, ast.Call) and name_of(inner.func) in names
        for node in nodes
        for inner in ast.walk(node)
    )


def entry_violations(root: Path, entries: set[Entry]) -> list[str]:
    """Entry points that do not pass the predicate, as readable lines."""
    trees = {_relative(root, path): tree for path, tree in iter_modules(root)}
    problems: list[str] = []
    for entry in sorted(entries, key=lambda e: (e.module, e.function)):
        where = f"{entry.module}::{entry.function}"
        tree = trees.get(entry.module)
        functions = _functions(tree, entry.function) if tree is not None else []
        if not functions:
            problems.append(f"{where}: no such function; the roster is stale")
        elif entry.via is None:
            if not _calls_any(functions, _PREDICATE_NAMES):
                problems.append(f"{where}: sends content and never calls the predicate")
        else:
            helper = _functions(tree, entry.via) if tree is not None else []
            if not _calls_any(functions, {entry.via}):
                problems.append(f"{where}: does not call its gate helper {entry.via}")
            elif not _calls_any(helper, _PREDICATE_NAMES):
                problems.append(
                    f"{where}: helper {entry.via} never calls the predicate"
                )
    return problems


def roster_violations(
    root: Path,
    gated: Mapping[tuple[str, str], tuple[Entry, ...]],
    exempt: Mapping[tuple[str, str], str],
) -> list[str]:
    """Inner senders the roster misses, and roster entries the tree no longer has."""
    found = set(inner_senders(root))
    listed = set(gated) | set(exempt)
    problems = [
        f"{module}::{function}: sends content to a model and is in neither the "
        f"gated nor the exempt roster"
        for module, function in sorted(found - listed)
    ]
    problems += [
        f"{module}::{function}: rostered but no longer sends; drop it"
        for module, function in sorted(listed - found)
    ]
    return problems


def sink_caller_violations(
    root: Path, entries: set[Entry], construct_only: Mapping[tuple[str, str], str]
) -> tuple[int, list[str]]:
    """``(sink calls seen, callers that are not entry points or construct-only)``."""
    allowed = {(e.module, e.function) for e in entries} | set(construct_only)
    seen = 0
    problems: list[str] = []
    for path, tree in iter_modules(root):
        module = _relative(root, path)
        spellings = {
            spelling: sink
            for sink in _SINK_NAMES
            for spelling in construction_names(sink, tree)
        }
        for call, scope in _calls_with_scope(tree):
            sink = spellings.get(name_of(call.func) or "")
            if sink is not None:
                seen += 1
                if (module, scope) not in allowed:
                    problems.append(
                        f"{module}:{call.lineno}: {scope} calls "
                        f"{sink} and is not a rostered entry point"
                    )
    return seen, problems


def gate_opening_violations(root: Path) -> list[str]:
    """``EgressGate(...)`` built outside egress.py without a literal ``local=False``."""
    problems: list[str] = []
    for path, tree in iter_modules(root):
        module = _relative(root, path)
        if module == _EGRESS_MODULE:
            continue
        gate_names = construction_names("EgressGate", tree)
        for call, _scope in _calls_with_scope(tree):
            if name_of(call.func) not in gate_names:
                continue
            local = [k.value for k in call.keywords if k.arg == "local"]
            closed = (
                len(local) == 1
                and isinstance(local[0], ast.Constant)
                and local[0].value is False
            )
            if not closed:
                problems.append(
                    f"{module}:{call.lineno}: EgressGate built without a literal "
                    f"local=False; use EgressGate.for_route"
                )
    return problems


def _all_entries() -> set[Entry]:
    return {entry for entries in _GATED.values() for entry in entries}


# ---------------------------------------------------------------------------
# The live tree
# ---------------------------------------------------------------------------


def test_the_roster_matches_the_senders_in_the_tree() -> None:
    assert roster_violations(_SRC, _GATED, _EXEMPT) == []


def test_every_entry_point_passes_through_the_predicate() -> None:
    assert entry_violations(_SRC, _all_entries()) == []


def test_every_caller_of_a_sender_is_a_rostered_entry_point() -> None:
    seen, problems = sink_caller_violations(_SRC, _all_entries(), _CONSTRUCT_ONLY)
    assert problems == []
    assert_hand_read_floor(seen, _SINK_CALL_FLOOR, subject="sink call")


def test_the_gate_is_opened_only_by_a_route_declaration() -> None:
    assert gate_opening_violations(_SRC) == []


def test_the_scan_still_finds_what_a_person_counted() -> None:
    assert_hand_read_floor(
        len(inner_senders(_SRC)),
        _SENDER_FLOOR,
        subject="inner LLM sender",
        hint="A narrowed scan would let every rule above pass with nothing to check.",
    )
    assert_hand_read_floor(len(_all_entries()), _ENTRY_FLOOR, subject="entry point")


def test_every_exemption_is_written_down() -> None:
    for reasons in (_EXEMPT, _CONSTRUCT_ONLY):
        for key, reason in reasons.items():
            assert len(reason.split()) >= 8, f"{key} needs a written reason"


# ---------------------------------------------------------------------------
# A deliberately defective subject
# ---------------------------------------------------------------------------

_CLEAN = """
def may_reach_route(label, *, local):
    return local


def gated_entry(client, gate, doc):
    if not gate.permits(doc["doc_id"], doc.get("metadata")):
        return None
    return client.generate(messages=[doc["content"]])
"""

_UNROSTERED_SENDER = """
def brand_new_job(client, doc):
    return client.generate(messages=[doc["content"]], temperature=0.0)
"""

_ENTRY_WITHOUT_PREDICATE = """
def gated_entry(client, gate, doc):
    return client.generate(messages=[doc["content"]])
"""

_HELPER_WITHOUT_PREDICATE = """
def helper(doc):
    return True


def gated_entry(client, doc):
    helper(doc)
    return client.generate(messages=[doc["content"]])
"""

_OPENED_GATE = """
def build(store, flag):
    open_gate = EgressGate(store, local=True)
    undecided = EgressGate(store, local=flag)
    positional = EgressGate(store)
    closed = EgressGate(store, local=False)
    return open_gate, undecided, positional, closed
"""

_DECOY = """
def not_a_model_call(generator, days):
    return generator.generate(days=days)
"""


def _corpus(tmp_path: Path, **modules: str) -> Path:
    root = tmp_path / "src"
    for name, source in modules.items():
        path = root / f"{name}.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    return root


def test_a_clean_subject_passes_every_rule(tmp_path: Path) -> None:
    root = _corpus(tmp_path, job=_CLEAN, decoy=_DECOY)
    entry = Entry("job.py", "gated_entry")
    gated = {("job.py", "gated_entry"): (entry,)}
    assert roster_violations(root, gated, {}) == []
    assert entry_violations(root, {entry}) == []
    assert gate_opening_violations(root) == []


def test_a_sender_in_neither_roster_is_reported(tmp_path: Path) -> None:
    root = _corpus(tmp_path, job=_UNROSTERED_SENDER)
    problems = roster_violations(root, {}, {})
    assert len(problems) == 1
    assert "job.py::brand_new_job" in problems[0]


def test_a_stale_roster_entry_is_reported(tmp_path: Path) -> None:
    root = _corpus(tmp_path, decoy=_DECOY)
    problems = roster_violations(root, {("job.py", "gone"): ()}, {})
    assert len(problems) == 1
    assert "no longer sends" in problems[0]


def test_an_entry_point_that_never_calls_the_predicate_is_reported(
    tmp_path: Path,
) -> None:
    root = _corpus(tmp_path, job=_ENTRY_WITHOUT_PREDICATE)
    problems = entry_violations(root, {Entry("job.py", "gated_entry")})
    assert len(problems) == 1
    assert "never calls the predicate" in problems[0]


def test_a_missing_entry_point_is_reported(tmp_path: Path) -> None:
    root = _corpus(tmp_path, job=_CLEAN)
    problems = entry_violations(root, {Entry("job.py", "renamed_away")})
    assert len(problems) == 1
    assert "roster is stale" in problems[0]


def test_a_gate_helper_that_does_not_call_the_predicate_is_reported(
    tmp_path: Path,
) -> None:
    root = _corpus(tmp_path, job=_HELPER_WITHOUT_PREDICATE)
    problems = entry_violations(root, {Entry("job.py", "gated_entry", via="helper")})
    assert len(problems) == 1
    assert "helper helper never calls the predicate" in problems[0]


def test_an_unrostered_caller_of_a_sender_is_reported(tmp_path: Path) -> None:
    root = _corpus(
        tmp_path,
        job="def sneaky(registry, doc):\n    judge_reconcile(registry, doc)\n",
    )
    seen, problems = sink_caller_violations(root, set(), {})
    assert seen == 1
    assert len(problems) == 1
    assert "sneaky" in problems[0]


def test_a_gate_that_is_not_literally_closed_is_reported(tmp_path: Path) -> None:
    root = _corpus(tmp_path, job=_OPENED_GATE)
    problems = gate_opening_violations(root)
    assert [problem.split(":")[1] for problem in problems] == ["3", "4", "5"]


def test_the_defining_module_may_build_gates_freely(tmp_path: Path) -> None:
    root = _corpus(tmp_path)
    egress = root / _EGRESS_MODULE
    egress.parent.mkdir(parents=True)
    egress.write_text("gate = EgressGate(store, local=True)\n", encoding="utf-8")
    assert gate_opening_violations(root) == []


def test_a_call_that_is_not_a_model_call_is_not_a_sender(tmp_path: Path) -> None:
    root = _corpus(tmp_path, decoy=_DECOY)
    assert inner_senders(root) == {}


_ALIASED_SINKS = {
    "judge_reconcile": (
        "from trellis.mcp.reconcile import judge_reconcile as jr\n"
        "def sneaky(registry, doc):\n    jr(registry, doc)\n"
    ),
    "EnrichmentService": (
        "from trellis_workers.enrichment.service import EnrichmentService as ES\n"
        "def sneaky(registry):\n    return ES(registry)\n"
    ),
    "LLMExtractor": "def sneaky(registry):\n    return LLMExtractor(registry)\n",
    "LLMFacetClassifier": (
        "def sneaky(registry):\n    return LLMFacetClassifier(registry)\n"
    ),
}


@pytest.mark.parametrize("sink", sorted(_ALIASED_SINKS))
def test_a_sink_is_found_under_an_alias_and_for_the_new_sinks(
    tmp_path: Path, sink: str
) -> None:
    root = _corpus(tmp_path, job=_ALIASED_SINKS[sink])
    seen, problems = sink_caller_violations(root, set(), {})
    assert seen == 1
    assert len(problems) == 1
    assert sink in problems[0]
    assert "sneaky" in problems[0]


def test_a_gate_built_under_an_alias_is_still_checked(tmp_path: Path) -> None:
    source = (
        "from trellis.llm.egress import EgressGate as G\n"
        "def build(store):\n    return G(store, local=True)\n"
    )
    problems = gate_opening_violations(_corpus(tmp_path, job=source))
    assert len(problems) == 1
    assert "local=False" in problems[0]


def test_a_keyword_expansion_on_generate_is_a_sender(tmp_path: Path) -> None:
    source = "def job(client, kwargs):\n    return client.generate(**kwargs)\n"
    assert inner_senders(_corpus(tmp_path, job=source)) == {("job.py", "job"): [2]}


def test_a_literal_getattr_of_generate_is_a_sender(tmp_path: Path) -> None:
    source = (
        "def job(client, doc):\n"
        '    send = getattr(client, "generate")\n'
        "    return send(messages=[doc])\n"
    )
    assert inner_senders(_corpus(tmp_path, job=source)) == {("job.py", "job"): [2]}


def test_the_known_evasions_are_named_not_caught(tmp_path: Path) -> None:
    """Each shape the module docstring lists as uncaught still walks past the rule.

    If one of these starts failing, the rule got stronger: update the docstring.
    """
    hidden_calls = (
        "def job(client, doc):\n"
        "    gen = client.generate\n"
        "    return gen(messages=[doc])\n"
        "def job2(client, doc):\n"
        "    return functools.partial(client.generate, messages=[doc])()\n"
    )
    assert inner_senders(_corpus(tmp_path, a=hidden_calls)) == {}

    opened_later = (
        "def build(store):\n"
        "    gate = EgressGate(store, local=False)\n"
        "    gate.local = True\n"
        "    return gate\n"
    )
    assert gate_opening_violations(_corpus(tmp_path, b=opened_later)) == []

    discarded = (
        "def gated_entry(client, gate, doc):\n"
        '    gate.permits(doc["doc_id"], None)\n'
        '    return client.generate(messages=[doc["content"]])\n'
    )
    root = _corpus(tmp_path, c=discarded)
    assert entry_violations(root, {Entry("c.py", "gated_entry")}) == []

    passed_in = "def job(service, doc):\n    return service.enrich(doc)\n"
    assert sink_caller_violations(_corpus(tmp_path, d=passed_in), set(), {}) == (
        0,
        [],
    )
