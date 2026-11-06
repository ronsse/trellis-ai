"""Every path that sends stored content to an embedder goes through the egress gate.

``trellis.llm.egress`` answers "may this content reach this route?" for chat and
generate routes, and, since the embedder became a route too, for the embedder: a
document labelled above ``internal`` is not embedded unless ``embeddings:`` says
``local: true``. ``test_llm_egress_rule.py`` polices the model senders; this rule
polices the embedder, derived off ``src/`` by AST on every run:

A. **The sink takes a gate.** ``build_vector_row`` is the one function that hands
   stored content to the embedder. Each call to it passes an ``egress=`` keyword
   that is not a literal ``None``, and sits in a rostered function, in a module
   that imports ``trellis.llm.egress``.
B. **The gate is built from the embedder's own declaration.** A function that
   builds a gate for the embedder does so with ``EgressGate.for_embedder`` or
   ``for_embedder_lazy``, which read ``embeddings.local``; the hand-read roster
   of those functions must match the tree in both directions. A gate built any
   other way is the other rule's business (it forbids ``EgressGate(..., local=True)``
   in ``src/``).
C. **Callers of the hook are rostered.** ``run_embed_on_ingest`` builds the gate
   itself, so a caller needs no gate of its own, but a new caller is a new place
   stored content leaves for the embedder and gets a read: an unrostered caller,
   or a roster entry the tree no longer has, fails.
D. **Nothing else calls the embedder.** A call to ``embedding_fn`` (the sink's
   parameter) or ``self._embedding_fn`` is either the sink itself or a query
   embedding. A query embedding embeds the caller's intent at retrieval time, not
   stored content, and is not gated (see ``operations.md``); those three sites are
   listed so a fourth one is a read, not a silent addition. The *exempt* roster for
   stored content is empty and stays empty.

The floors are hand-read off ``src/`` (see ``assert_hand_read_floor``), never taken
from the scan they guard, and the predicates are run against a deliberately
defective subject below.

**This rule is a tripwire, not the guard.** It sees the common shapes of a new
embed site: a direct ``build_vector_row(...)`` without a gate, a new
``run_embed_on_ingest`` caller, an embedder called under its usual names. The
guard is the behaviour test beside each sender (``test_embed_egress_senders.py``):
a labelled document, an embedder not declared local, and a recording embedder that
must see nothing. Known evasions, none of them caught: an embedder called under
another name (``vec = fn(text)`` after ``fn = registry.embedding_fn``), a sink
passed as an argument, ``egress=gate`` where the gate was built open elsewhere.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.ast_rules import assert_hand_read_floor, iter_modules

_SRC = Path(__file__).resolve().parents[2] / "src"

_EGRESS_MODULE = "trellis/llm/egress.py"
_EGRESS_IMPORT = "trellis.llm.egress"

_SINK = "build_vector_row"
_HOOK = "run_embed_on_ingest"
_GATE_BUILDERS = frozenset({"for_embedder", "for_embedder_lazy"})
_EMBEDDER_NAMES = frozenset({"embedding_fn", "_embedding_fn"})

#: Functions that call the sink, hand-read. ``_embed_doc`` takes the gate that
#: ``run_reindex_vectors`` built, as a parameter named ``egress``.
_SINK_CALLERS = frozenset(
    {
        ("trellis/retrieve/embed_ingest_hook.py", "run_embed_on_ingest"),
        ("trellis/mutate/handlers/evidence_handler.py", "_embed_strict"),
        ("trellis_cli/admin_reindex_vectors.py", "_embed_doc"),
    }
)

#: Functions that build an embedder gate, hand-read.
_GATE_BUILDING_FUNCTIONS = frozenset(
    {
        ("trellis/retrieve/embed_ingest_hook.py", "run_embed_on_ingest"),
        ("trellis/mutate/handlers/evidence_handler.py", "_embed_strict"),
        ("trellis_cli/admin_reindex_vectors.py", "run_reindex_vectors"),
        ("trellis_workers/trace_embed/handler.py", "_judge"),
        ("trellis_workers/trace_embed/worker.py", "run_trace_embed_pass"),
    }
)

#: Callers of the hook, hand-read. None builds a gate: the hook does.
_HOOK_CALLERS = frozenset(
    {
        ("trellis/ingest_corpus/sync.py", "_apply_record"),
        ("trellis/ingest_corpus/sync.py", "_write_chunks"),
        ("trellis/mcp/tools/memory.py", "_emit_memory_stored_and_enrich"),
        ("trellis/mutate/handlers/evidence_handler.py", "handle"),
        ("trellis/mutate/handlers/precedent.py", "_write_precedent_document"),
        ("trellis/mutate/handlers/redaction.py", "_reembed_scrubbed_precedent"),
        ("trellis_cli/ingest.py", "_index_dbt_descriptions"),
    }
)

#: The embedder calls that are not the sink: a caller's intent embedded at
#: retrieval time. Not stored content, so not gated.
_QUERY_EMBEDS = frozenset(
    {
        ("trellis/retrieve/semantic_seeds.py", "extract"),
        ("trellis/retrieve/strategies.py", "search_result"),
        ("trellis/retrieve/strategies.py", "public_result"),
    }
)

#: Stored-content embed sites that are exempt from the gate. Empty, and a test
#: pins it empty.
_EXEMPT: dict[tuple[str, str], str] = {}


@dataclass(frozen=True)
class Site:
    """One call: the file, the innermost function around it, and the node."""

    module: str
    function: str
    node: ast.Call

    @property
    def key(self) -> tuple[str, str]:
        return (self.module, self.function)

    def describe(self) -> str:
        snippet = ast.unparse(self.node).replace("\n", " ")[:80]
        return f"{self.module}::{self.function}:{self.node.lineno}: {snippet}"


def _callee(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _calls(tree: ast.AST, owner: str = "<module>") -> Iterator[tuple[str, ast.Call]]:
    for child in ast.iter_child_nodes(tree):
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
            yield from _calls(child, child.name)
            continue
        if isinstance(child, ast.Call):
            yield owner, child
        yield from _calls(child, owner)


def sites(root: Path, names: frozenset[str] | set[str]) -> list[Site]:
    """Every call in *root* to one of *names*, outside ``egress.py``'s own helpers."""
    found: list[Site] = []
    for path, tree in iter_modules(root):
        module = path.relative_to(root).as_posix()
        for owner, call in _calls(tree):
            if _callee(call) in names:
                found.append(Site(module, owner, call))
    return found


def _imports_egress(tree: ast.Module) -> bool:
    return any(
        isinstance(node, ast.ImportFrom) and node.module == _EGRESS_IMPORT
        for node in ast.walk(tree)
    )


def _function_has_param(tree: ast.Module, name: str, param: str) -> bool:
    return any(
        isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name == name
        and any(arg.arg == param for arg in node.args.args + node.args.kwonlyargs)
        for node in ast.walk(tree)
    )


def sink_violations(root: Path, roster: frozenset[tuple[str, str]]) -> list[str]:
    """Rule A: each ``build_vector_row`` call passes a gate, from a rostered place."""
    problems: list[str] = []
    seen: set[tuple[str, str]] = set()
    trees = {
        path.relative_to(root).as_posix(): tree for path, tree in iter_modules(root)
    }
    for site in sites(root, {_SINK}):
        seen.add(site.key)
        keywords = {kw.arg: kw.value for kw in site.node.keywords}
        gate = keywords.get("egress")
        if gate is None and not any(kw.arg is None for kw in site.node.keywords):
            problems.append(f"{site.describe()} passes no egress= gate")
        elif isinstance(gate, ast.Constant) and gate.value is None:
            problems.append(f"{site.describe()} passes egress=None")
        if site.key not in roster and site.key not in _EXEMPT:
            problems.append(f"{site.describe()} is not in the sink roster")
        tree = trees[site.module]
        if not _imports_egress(tree):
            problems.append(f"{site.module} calls the sink but never imports egress")
    problems.extend(
        f"{module}::{function} is rostered but no longer calls the sink"
        for module, function in sorted(roster - seen)
    )
    return problems


def gate_builder_violations(
    root: Path, roster: frozenset[tuple[str, str]]
) -> list[str]:
    """Rule B: the functions that build an embedder gate are the rostered ones."""
    found = {
        site.key
        for site in sites(root, _GATE_BUILDERS)
        if site.module != _EGRESS_MODULE
    }
    problems = [
        f"{module}::{function} builds an embedder gate and is not rostered"
        for module, function in sorted(found - roster)
    ]
    problems.extend(
        f"{module}::{function} is rostered but builds no embedder gate"
        for module, function in sorted(roster - found)
    )
    return problems


def sink_caller_gate_violations(
    root: Path,
    sink_callers: frozenset[tuple[str, str]],
    builders: frozenset[tuple[str, str]],
) -> list[str]:
    """A sink caller builds the gate itself or takes one as an ``egress`` param."""
    trees = {
        path.relative_to(root).as_posix(): tree for path, tree in iter_modules(root)
    }
    problems = []
    for module, function in sorted(sink_callers):
        tree = trees.get(module)
        if tree is None:
            continue
        if (module, function) in builders:
            continue
        if not _function_has_param(tree, function, "egress"):
            problems.append(
                f"{module}::{function} calls the sink with a gate it neither "
                "builds nor takes as an egress parameter"
            )
    return problems


def hook_caller_violations(
    root: Path, roster: frozenset[tuple[str, str]]
) -> tuple[int, list[str]]:
    """Rule C: the callers of ``run_embed_on_ingest`` are exactly the rostered ones."""
    found = {site.key for site in sites(root, {_HOOK})}
    problems = [
        f"{module}::{function} calls {_HOOK} and is not in the caller roster"
        for module, function in sorted(found - roster)
    ]
    problems.extend(
        f"{module}::{function} is rostered but no longer calls {_HOOK}"
        for module, function in sorted(roster - found)
    )
    return len(found), problems


def embedder_call_violations(
    root: Path, sink_module: str, queries: frozenset[tuple[str, str]]
) -> tuple[int, list[str]]:
    """Rule D: an embedder call is the sink's own, or a rostered query embedding."""
    problems: list[str] = []
    found = sites(root, _EMBEDDER_NAMES)
    keys: set[tuple[str, str]] = set()
    for site in found:
        keys.add(site.key)
        if site.key == (sink_module, _SINK) or site.key in queries:
            continue
        problems.append(f"{site.describe()} calls the embedder and is not rostered")
    problems.extend(
        f"{module}::{function} is rostered as a query embedding but no longer calls it"
        for module, function in sorted(queries - keys)
    )
    return len(found), problems


# --- the real tree ------------------------------------------------------------


def test_every_sink_call_passes_a_gate() -> None:
    sink_sites = sites(_SRC, {_SINK})
    assert_hand_read_floor(
        len(sink_sites),
        3,
        subject="build_vector_row call",
        hint="hook, evidence handler strict mode, reindex-vectors",
    )
    assert sink_violations(_SRC, _SINK_CALLERS) == []


def test_every_sink_caller_has_a_gate_to_pass() -> None:
    assert (
        sink_caller_gate_violations(_SRC, _SINK_CALLERS, _GATE_BUILDING_FUNCTIONS) == []
    )


def test_the_embedder_gate_builders_are_the_rostered_ones() -> None:
    builders = {
        site.key
        for site in sites(_SRC, _GATE_BUILDERS)
        if site.module != _EGRESS_MODULE
    }
    assert_hand_read_floor(
        len(builders), 5, subject="embedder gate builder", hint="see the roster"
    )
    assert gate_builder_violations(_SRC, _GATE_BUILDING_FUNCTIONS) == []


def test_every_hook_caller_is_rostered() -> None:
    count, problems = hook_caller_violations(_SRC, _HOOK_CALLERS)
    assert_hand_read_floor(
        count, 7, subject="run_embed_on_ingest caller", hint="see the roster"
    )
    assert problems == []


def test_nothing_else_calls_the_embedder() -> None:
    count, problems = embedder_call_violations(
        _SRC, "trellis/retrieve/embed_ingest_hook.py", _QUERY_EMBEDS
    )
    assert_hand_read_floor(
        count, 4, subject="embedder call", hint="the sink and three query sites"
    )
    assert problems == []


def test_the_exempt_roster_for_stored_content_is_empty() -> None:
    assert _EXEMPT == {}


# --- a deliberately defective subject ----------------------------------------

_CLEAN_SINK = """
from trellis.llm.egress import EgressGate

def embed_one(registry, doc):
    gate = EgressGate.for_embedder(registry, registry.documents)
    return build_vector_row(doc.id, doc.text, doc.meta, registry.fn, egress=gate)

def embed_with(doc, egress):
    return build_vector_row(doc.id, doc.text, doc.meta, doc.fn, egress=egress)
"""

_UNGATED_SINK = """
from trellis.llm.egress import EgressGate

def sneaky(doc):
    return build_vector_row(doc.id, doc.text, doc.meta, doc.fn)
"""

_NONE_GATE = """
from trellis.llm.egress import EgressGate

def sneaky(doc):
    return build_vector_row(doc.id, doc.text, doc.meta, doc.fn, egress=None)
"""

_NO_EGRESS_IMPORT = """
def sneaky(doc, gate):
    return build_vector_row(doc.id, doc.text, doc.meta, doc.fn, egress=gate)
"""

_GATELESS_CALLER = """
from trellis.llm.egress import EgressGate

def sneaky(doc, gate_by_another_name):
    return build_vector_row(
        doc.id, doc.text, doc.meta, doc.fn, egress=gate_by_another_name
    )
"""


def _corpus(tmp_path: Path, **modules: str) -> Path:
    root = tmp_path / "src"
    for name, source in modules.items():
        path = root / f"{name}.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    return root


def test_a_clean_subject_passes_every_rule(tmp_path: Path) -> None:
    root = _corpus(tmp_path, job=_CLEAN_SINK)
    sink_callers = frozenset({("job.py", "embed_one"), ("job.py", "embed_with")})
    builders = frozenset({("job.py", "embed_one")})
    assert sink_violations(root, sink_callers) == []
    assert gate_builder_violations(root, builders) == []
    assert sink_caller_gate_violations(root, sink_callers, builders) == []


def test_a_sink_call_without_a_gate_is_reported(tmp_path: Path) -> None:
    root = _corpus(tmp_path, job=_UNGATED_SINK)
    problems = sink_violations(root, frozenset({("job.py", "sneaky")}))
    assert len(problems) == 1
    assert "passes no egress= gate" in problems[0]


def test_a_sink_call_with_a_literal_none_gate_is_reported(tmp_path: Path) -> None:
    root = _corpus(tmp_path, job=_NONE_GATE)
    problems = sink_violations(root, frozenset({("job.py", "sneaky")}))
    assert len(problems) == 1
    assert "egress=None" in problems[0]


def test_an_unrostered_sink_call_is_reported(tmp_path: Path) -> None:
    root = _corpus(tmp_path, job=_CLEAN_SINK)
    problems = sink_violations(root, frozenset())
    assert len(problems) == 2
    assert all("not in the sink roster" in problem for problem in problems)


def test_a_sink_module_that_never_imports_egress_is_reported(tmp_path: Path) -> None:
    root = _corpus(tmp_path, job=_NO_EGRESS_IMPORT)
    problems = sink_violations(root, frozenset({("job.py", "sneaky")}))
    assert len(problems) == 1
    assert "never imports egress" in problems[0]


def test_a_stale_sink_roster_entry_is_reported(tmp_path: Path) -> None:
    root = _corpus(tmp_path, job=_CLEAN_SINK)
    sink_callers = frozenset(
        {("job.py", "embed_one"), ("job.py", "embed_with"), ("job.py", "gone")}
    )
    problems = sink_violations(root, sink_callers)
    assert len(problems) == 1
    assert "no longer calls the sink" in problems[0]


def test_a_sink_caller_with_no_gate_of_its_own_is_reported(tmp_path: Path) -> None:
    root = _corpus(tmp_path, job=_GATELESS_CALLER)
    sink_callers = frozenset({("job.py", "sneaky")})
    problems = sink_caller_gate_violations(root, sink_callers, frozenset())
    assert len(problems) == 1
    assert "neither builds nor takes" in problems[0]


def test_an_unrostered_gate_builder_is_reported(tmp_path: Path) -> None:
    root = _corpus(tmp_path, job=_CLEAN_SINK)
    problems = gate_builder_violations(root, frozenset())
    assert len(problems) == 1
    assert "is not rostered" in problems[0]


def test_a_stale_gate_builder_entry_is_reported(tmp_path: Path) -> None:
    root = _corpus(tmp_path, job=_CLEAN_SINK)
    problems = gate_builder_violations(
        root, frozenset({("job.py", "embed_one"), ("job.py", "gone")})
    )
    assert len(problems) == 1
    assert "builds no embedder gate" in problems[0]


def test_an_unrostered_hook_caller_is_reported(tmp_path: Path) -> None:
    root = _corpus(
        tmp_path,
        job="def sneaky(registry, doc):\n    run_embed_on_ingest(registry, doc)\n",
    )
    count, problems = hook_caller_violations(root, frozenset())
    assert count == 1
    assert len(problems) == 1
    assert "sneaky" in problems[0]


def test_a_stale_hook_caller_entry_is_reported(tmp_path: Path) -> None:
    root = _corpus(tmp_path, job="def other():\n    return 1\n")
    count, problems = hook_caller_violations(root, frozenset({("job.py", "gone")}))
    assert count == 0
    assert "no longer calls" in problems[0]


def test_an_embedder_called_outside_the_sink_is_reported(tmp_path: Path) -> None:
    root = _corpus(
        tmp_path,
        job="def sneaky(self, doc):\n    return self._embedding_fn(doc.text)\n",
    )
    count, problems = embedder_call_violations(root, "sink.py", frozenset())
    assert count == 1
    assert "calls the embedder and is not rostered" in problems[0]


def test_a_rostered_query_embedding_is_not_reported(tmp_path: Path) -> None:
    root = _corpus(
        tmp_path,
        job="def search(self, query):\n    return self._embedding_fn(query)\n",
    )
    count, problems = embedder_call_violations(
        root, "sink.py", frozenset({("job.py", "search")})
    )
    assert count == 1
    assert problems == []


def test_a_call_that_is_not_an_embedder_is_not_counted(tmp_path: Path) -> None:
    root = _corpus(tmp_path, job="def f(x):\n    return str(x) + embedding(x)\n")
    count, problems = embedder_call_violations(root, "sink.py", frozenset())
    assert count == 0
    assert problems == []


def test_a_floor_above_the_population_fails() -> None:
    with pytest.raises(AssertionError, match="below the hand-read floor"):
        assert_hand_read_floor(2, 3, subject="build_vector_row call")
