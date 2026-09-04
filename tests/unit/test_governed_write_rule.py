"""Ratchet for direct document/vector writes awaiting governance (#360).

Derive the exceptions from the AST and require exact agreement with a
temporary prose roster. New direct writes fail; removals force the roster and
hand-read count to shrink.
"""

from __future__ import annotations

import ast
import io
import tokenize
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from tests.ast_rules import (
    assert_hand_read_floor,
    calls_named,
    iter_modules,
    render_evasion_corpus,
)

# Module+operation keys survive line movement. The exact site count below
# prevents a new call from inheriting an existing module-level exemption.
GOVERNED_WRITE_EXEMPTIONS: dict[tuple[str, str], str] = {
    ("trellis/classify/feedback.py", "document.put"): (
        "Metadata-only noise tagging mirrors content tags through "
        "sync_vector_metadata; later governance must remove this exemption."
    ),
    ("trellis/classify/refresh.py", "document.put"): (
        "Metadata-only classification refresh mirrors the vector snapshot; "
        "later governance must remove this exemption."
    ),
    ("trellis/core/derived_metadata.py", "document.put"): (
        "Shared metadata-only rewrite seam; the PR3 design must govern or "
        "transactionally mirror it."
    ),
    ("trellis/core/vector_metadata.py", "vector.upsert"): (
        "Metadata-only vector mirror repairs snapshot divergence; the PR3 "
        "design must replace this direct write."
    ),
    ("trellis/ingest_corpus/sync.py", "document.put"): (
        "Bursty corpus writes await the batched audit emission required by "
        "decision-ledger T-3."
    ),
    ("trellis/ingest_corpus/sync.py", "document.delete"): (
        "Bursty corpus deletion awaits governed batching; no governed "
        "document delete exists yet."
    ),
    ("trellis/ingest_corpus/sync.py", "vector.delete"): (
        "Corpus cleanup mirrors document deletion directly and awaits the "
        "same governed batch boundary."
    ),
    ("trellis/mcp/reconcile.py", "document.put"): (
        "Metadata-only supersession stamping is not vector-mirrored today "
        "and is reserved for the PR3 design."
    ),
    ("trellis/mcp/server.py", "document.put"): (
        "Two memory creates route in PR2; the metadata-only stale downgrade "
        "remains for the PR3 design."
    ),
    ("trellis/mutate/evidence.py", "document.put"): (
        "This helper is not a governed handler; PR2 routes it through the "
        "core evidence.ingest operation."
    ),
    ("trellis/retrieve/embed_ingest_hook.py", "vector.upsert"): (
        "Fail-soft embedding is the vector seam PR2's governed handler will "
        "call; later work must clear the direct-write roster."
    ),
    ("trellis_api/routes/curate.py", "document.put"): (
        "The agent-facing document endpoint routes through evidence.ingest in PR2."
    ),
    ("trellis_api/routes/ingest.py", "document.put"): (
        "The agent-facing evidence endpoint routes through evidence.ingest in PR2."
    ),
    ("trellis_api/routes/ingest.py", "vector.upsert"): (
        "The standalone vector endpoint remains direct until later issue "
        "#360 work defines its governed operation."
    ),
    ("trellis_cli/demo.py", "document.put"): (
        "Demo fixture seeding writes synthetic documents directly and must "
        "eventually use the governed batch path."
    ),
    ("trellis_cli/ingest.py", "document.put"): (
        "CLI evidence and dbt-description ingestion are bursty paths that "
        "await governed batching."
    ),
    ("trellis_workers/session_capture/reconcile_pass.py", "document.put"): (
        "Metadata-only claim withdrawal awaits the PR3 governance and mirror design."
    ),
    ("trellis_workers/trace_embed/handler.py", "document.put"): (
        "The worker-local governed handler is consolidated into the core "
        "evidence.ingest handler in PR2."
    ),
    ("trellis_workers/trace_embed/handler.py", "vector.upsert"): (
        "The worker's strict embedding write is consolidated into the core "
        "evidence.ingest handler in PR2."
    ),
}

# Write-shaped calls whose receiver cannot be resolved to the document/vector
# planes. These are proven other store types. A new unknown receiver fails
# instead of silently escaping the governed-write rule.
UNCLASSIFIED_WRITE_EXEMPTIONS: dict[tuple[str, str], str] = {
    ("trellis/learning/tuners/promotion.py", "put"): (
        "ParameterStore snapshot promotion is outside the document and "
        "vector planes policed by issue #360."
    ),
    ("trellis/learning/tuners/rollback.py", "put"): (
        "ParameterStore rollback persistence is outside the document and "
        "vector planes policed by issue #360."
    ),
    ("trellis/retrieve/effectiveness.py", "put"): (
        "AdvisoryStore whole-file updates are outside the document and "
        "vector planes policed by issue #360."
    ),
    ("trellis_cli/analyze.py", "put"): (
        "These receivers are ParameterStore implementations, outside the "
        "document and vector planes."
    ),
    ("trellis_cli/classify.py", "put"): (
        "These receivers are ParameterStore implementations, outside the "
        "document and vector planes."
    ),
}

# Re-read on origin/main 04deb47 (2026-09-04) with git grep over the
# document_store/doc_store/vector_store/vstore receiver names and all four
# write methods, excluding store backends. It finds 23 non-handler sites:
# 18 document and 5 vector. The AST also resolves the get_document_store()
# alias in trellis_cli/ingest.py, making the complete non-handler inventory 24.
_HAND_READ_SITE_COUNT = 24
_HAND_READ_DOCUMENT_COUNT = 19
_HAND_READ_VECTOR_COUNT = 5
_WRITE_METHODS = {"put", "delete", "upsert", "upsert_many"}


@dataclass(frozen=True)
class WriteSite:
    module: str
    lineno: int
    kind: str


@dataclass(frozen=True)
class UnscannableWrite:
    module: str
    lineno: int
    expression: str


@dataclass(frozen=True)
class UnclassifiedWrite:
    module: str
    lineno: int
    method: str
    expression: str


def _src_root() -> Path:
    return Path(__file__).resolve().parents[2] / "src"


def _dotted_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return None


def _store_plane(receiver: ast.AST, aliases: dict[str, str]) -> str | None:
    if isinstance(receiver, ast.Name) and receiver.id in aliases:
        return aliases[receiver.id]
    if isinstance(receiver, ast.Call):
        target = _dotted_name(receiver.func)
        if target and target.endswith("get_document_store"):
            return "document"
        if target and target.endswith("get_vector_store"):
            return "vector"
    name = _dotted_name(receiver)
    if name and name.endswith(("document_store", "doc_store")):
        return "document"
    if name and name.endswith(("vector_store", "vstore")):
        return "vector"
    return None


Scope = ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda


def _scope_owner(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> Scope:
    current = node
    while current in parents:
        current = parents[current]
        if isinstance(
            current, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
        ):
            return current
    message = "AST node has no module scope"
    raise AssertionError(message)


def _scope_aliases(
    tree: ast.Module, parents: dict[ast.AST, ast.AST]
) -> dict[int, dict[str, str]]:
    """Resolve simple local aliases of document/vector stores."""
    assignments: dict[int, list[tuple[str, ast.AST]]] = defaultdict(list)
    owners: list[Scope] = [tree]
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            owners.append(node)
        if isinstance(node, ast.Assign):
            owner = _scope_owner(node, parents)
            assignments[id(owner)].extend(
                (target.id, node.value)
                for target in node.targets
                if isinstance(target, ast.Name)
            )
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.value is not None
        ):
            owner = _scope_owner(node, parents)
            assignments[id(owner)].append((node.target.id, node.value))

    resolved: dict[int, dict[str, str]] = {}
    for owner in owners:
        aliases: dict[str, str] = {}
        changed = True
        while changed:
            before = dict(aliases)
            for target, value in assignments[id(owner)]:
                plane = _store_plane(value, aliases)
                if plane is not None:
                    aliases[target] = plane
            changed = aliases != before
        resolved[id(owner)] = aliases
    return resolved


def _is_exempt_path(relative: Path) -> bool:
    return relative.parts[:2] == ("trellis", "stores") or (
        relative.as_posix() == "trellis/mutate/handlers.py"
    )


def _scan_direct_writes(
    root: Path,
) -> tuple[list[WriteSite], list[UnscannableWrite], list[UnclassifiedWrite]]:
    found: list[WriteSite] = []
    unscannable: list[UnscannableWrite] = []
    unclassified: list[UnclassifiedWrite] = []
    for path, tree in iter_modules(root):
        relative = path.relative_to(root)
        if _is_exempt_path(relative):
            continue
        parents = {
            child: parent
            for parent in ast.walk(tree)
            for child in ast.iter_child_nodes(parent)
        }
        aliases_by_scope = _scope_aliases(tree, parents)
        getattr_calls = set(calls_named("getattr", tree))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            aliases = aliases_by_scope[id(_scope_owner(node, parents))]
            if not isinstance(node.func, ast.Attribute):
                if node in getattr_calls and len(node.args) >= 2:
                    method = node.args[1]
                    if (
                        isinstance(method, ast.Constant)
                        and isinstance(method.value, str)
                        and _store_plane(node.args[0], aliases) is not None
                        and method.value in _WRITE_METHODS
                    ):
                        unscannable.append(
                            UnscannableWrite(
                                relative.as_posix(), node.lineno, ast.unparse(node)
                            )
                        )
                continue
            plane = _store_plane(node.func.value, aliases)
            receiver_name = _dotted_name(node.func.value) or ""
            possibly_a_store = node.func.attr != "delete" or any(
                marker in receiver_name.casefold()
                for marker in ("store", "document", "vector", "doc")
            )
            if plane is None and node.func.attr in _WRITE_METHODS and possibly_a_store:
                unclassified.append(
                    UnclassifiedWrite(
                        relative.as_posix(),
                        node.func.lineno,
                        node.func.attr,
                        ast.unparse(node.func),
                    )
                )
                continue
            allowed = (
                {"put", "delete"}
                if plane == "document"
                else {"upsert", "upsert_many", "delete"}
            )
            if plane is not None and node.func.attr in allowed:
                found.append(
                    WriteSite(
                        relative.as_posix(),
                        node.func.lineno,
                        f"{plane}.{node.func.attr}",
                    )
                )
    return (
        sorted(found, key=lambda site: (site.module, site.lineno, site.kind)),
        sorted(
            unscannable,
            key=lambda site: (site.module, site.lineno, site.expression),
        ),
        sorted(
            unclassified,
            key=lambda site: (site.module, site.lineno, site.expression),
        ),
    )


def direct_write_sites(root: Path | None = None) -> list[WriteSite]:
    return _scan_direct_writes(root or _src_root())[0]


def unscannable_writes(root: Path | None = None) -> list[UnscannableWrite]:
    return _scan_direct_writes(root or _src_root())[1]


def unclassified_writes(root: Path | None = None) -> list[UnclassifiedWrite]:
    return _scan_direct_writes(root or _src_root())[2]


def _ast_method_lines(root: Path) -> set[tuple[str, int, str]]:
    found: set[tuple[str, int, str]] = set()
    for path, tree in iter_modules(root):
        module = path.relative_to(root).as_posix()
        found.update(
            (module, node.func.lineno, node.func.attr)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _WRITE_METHODS
        )
    return found


def _token_method_lines(root: Path) -> set[tuple[str, int, str]]:
    found: set[tuple[str, int, str]] = set()
    ignored = {
        tokenize.ENCODING,
        tokenize.ENDMARKER,
        tokenize.INDENT,
        tokenize.DEDENT,
        tokenize.NEWLINE,
        tokenize.NL,
        tokenize.COMMENT,
    }
    for path in sorted(root.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        tokens = [
            token
            for token in tokenize.generate_tokens(io.StringIO(source).readline)
            if token.type not in ignored
        ]
        for dot, method, opening in zip(tokens, tokens[1:], tokens[2:], strict=False):
            if (
                dot.type == tokenize.OP
                and dot.string == "."
                and method.type == tokenize.NAME
                and method.string in _WRITE_METHODS
                and opening.type == tokenize.OP
                and opening.string == "("
            ):
                found.add(
                    (path.relative_to(root).as_posix(), method.start[0], method.string)
                )
    return found


def test_every_direct_write_is_a_temporary_exemption() -> None:
    sites = direct_write_sites()
    found = {(site.module, site.kind) for site in sites}
    declared = set(GOVERNED_WRITE_EXEMPTIONS)
    assert found == declared, (
        "direct document/vector writes differ from the staging roster: "
        f"unrostered={sorted(found - declared)}; "
        f"stale={sorted(declared - found)}"
    )
    thin = [
        key
        for key, reason in GOVERNED_WRITE_EXEMPTIONS.items()
        if len(reason.split()) < 4
    ]
    assert not thin, f"direct-write exemptions need prose reasons: {thin}"
    assert len(sites) == _HAND_READ_SITE_COUNT, (
        f"found {len(sites)} direct writes, expected the hand-read "
        f"{_HAND_READ_SITE_COUNT}. A new site must not inherit an existing "
        "module/kind exemption; a removed site must shrink this count."
    )


def test_hand_read_plane_counts_have_not_shrunk_silently() -> None:
    sites = direct_write_sites()
    document = sum(site.kind.startswith("document.") for site in sites)
    vector = sum(site.kind.startswith("vector.") for site in sites)
    assert_hand_read_floor(
        document,
        _HAND_READ_DOCUMENT_COUNT,
        subject="direct document write",
        hint="Re-run the recorded git grep and reconcile aliases.",
    )
    assert_hand_read_floor(
        vector,
        _HAND_READ_VECTOR_COUNT,
        subject="direct vector write",
        hint="Re-run the recorded git grep and reconcile aliases.",
    )
    assert (document, vector) == (
        _HAND_READ_DOCUMENT_COUNT,
        _HAND_READ_VECTOR_COUNT,
    )


def test_no_dynamic_store_write_is_hidden_from_the_rule() -> None:
    hidden = unscannable_writes()
    assert not hidden, (
        "getattr-based document/vector writes cannot be classified by the "
        "attribute-call rule; use an ordinary method call or widen the rule:\n  "
        + "\n  ".join(
            f"{site.module}:{site.lineno}: {site.expression}" for site in hidden
        )
    )


def test_unknown_receivers_are_explicitly_classified_as_other_stores() -> None:
    sites = unclassified_writes()
    found = {(site.module, site.method) for site in sites}
    declared = set(UNCLASSIFIED_WRITE_EXEMPTIONS)
    assert found == declared, (
        "write-shaped calls have unresolved receiver types: "
        f"new={sorted(found - declared)}; stale={sorted(declared - found)}"
    )
    thin = [
        key
        for key, reason in UNCLASSIFIED_WRITE_EXEMPTIONS.items()
        if len(reason.split()) < 4
    ]
    assert not thin, f"unclassified write exemptions need prose reasons: {thin}"
    assert len(sites) == 9, (
        f"found {len(sites)} unclassified calls, expected 9 hand-read "
        "ParameterStore/AdvisoryStore calls"
    )


def test_ast_walk_agrees_with_an_independent_token_scan() -> None:
    ast_lines = _ast_method_lines(_src_root())
    token_lines = _token_method_lines(_src_root())
    assert ast_lines == token_lines, (
        "AST and tokenizer write-call inventories diverged: "
        f"AST-only={sorted(ast_lines - token_lines)}; "
        f"token-only={sorted(token_lines - ast_lines)}"
    )


def test_receiver_aliases_chains_keywords_and_getattr_are_visible(
    tmp_path: Path,
) -> None:
    source = """\
def writes(registry):
    ds = registry.knowledge.document_store
    ds.put("doc-1", "body")
    registry.knowledge.document_store.delete(doc_id="doc-2")
    registry.knowledge.vector_store.upsert(
        item_id="doc-1", vector=[1.0], metadata={}
    )
    self._docs.put("doc-unknown", "body")
    getattr(ds, "put")("doc-3", "body")
"""
    (tmp_path / "writer.py").write_text(source, encoding="utf-8")

    assert [(site.lineno, site.kind) for site in direct_write_sites(tmp_path)] == [
        (3, "document.put"),
        (4, "document.delete"),
        (5, "vector.upsert"),
    ]
    assert [
        (site.lineno, site.expression) for site in unscannable_writes(tmp_path)
    ] == [(9, "getattr(ds, 'put')")]
    assert [
        (site.lineno, site.method, site.expression)
        for site in unclassified_writes(tmp_path)
    ] == [(8, "put", "self._docs.put")]


def test_method_scan_walks_shared_evasion_placements(tmp_path: Path) -> None:
    """Exercise discovery and placement shapes maintained by ast_rules."""
    corpus = render_evasion_corpus(
        subject="DocumentWriter",
        wrap="{call}.put('doc-1', 'body')",
    )
    corpus.write(tmp_path)
    found_lines = {line for _module, line, _method in _ast_method_lines(tmp_path)}
    assert set(corpus.lines.values()) <= found_lines
