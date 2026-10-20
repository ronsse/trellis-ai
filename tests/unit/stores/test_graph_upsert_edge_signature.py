"""``GraphStore.upsert_edge`` declares what every shipped backend accepts.

The five provenance keywords are typed edge columns, and each backend that
writes them took them as keyword-only parameters while the abstract method
did not declare them, so a caller holding a ``GraphStore`` could not pass
one without a type error and a plugin author reading the ABC would not
know to accept them.

The check reads the backends' source rather than importing them: the
Postgres module needs a driver that a minimal install lacks, and an
import-gated check would silently skip exactly the backend most likely to
drift.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.ast_rules import iter_modules
from trellis.stores.base.edge_provenance import EDGE_PROVENANCE_FIELDS

_STORES = Path(__file__).resolve().parents[3] / "src" / "trellis" / "stores"

#: Read by hand from ``src/trellis/stores/*/graph.py``, not derived from
#: the scan below: the backends that define ``upsert_edge`` themselves
#: (Neo4j and ArcadeDB inherit the Bolt one). A scan that found fewer
#: would otherwise shrink the parametrisation with it.
_HAND_READ_BACKENDS = frozenset(
    {"SQLiteGraphStore", "PostgresGraphStore", "BoltOpenCypherGraphStore"}
)


def _upsert_edge_arguments(cls: ast.ClassDef) -> ast.arguments | None:
    for node in cls.body:
        if isinstance(node, ast.FunctionDef) and node.name == "upsert_edge":
            return node.args
    return None


def _base_names(cls: ast.ClassDef) -> set[str]:
    return {b.id for b in cls.bases if isinstance(b, ast.Name)}


def _signatures() -> tuple[ast.arguments, dict[str, ast.arguments]]:
    abstract: ast.arguments | None = None
    backends: dict[str, ast.arguments] = {}
    for path, tree in iter_modules(_STORES):
        for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
            args = _upsert_edge_arguments(cls)
            if args is None:
                continue
            if cls.name == "GraphStore" and path.parent.name == "base":
                abstract = args
            elif any(name.endswith("GraphStore") for name in _base_names(cls)):
                backends[cls.name] = args
    assert abstract is not None, "GraphStore.upsert_edge not found"
    return abstract, backends


_ABSTRACT, _BACKENDS = _signatures()


def test_the_scan_found_every_hand_read_backend() -> None:
    assert set(_BACKENDS) >= _HAND_READ_BACKENDS


def test_abstract_declares_the_five_provenance_keywords_keyword_only() -> None:
    keyword_only = [a.arg for a in _ABSTRACT.kwonlyargs]
    assert keyword_only == ["commit", *EDGE_PROVENANCE_FIELDS]
    # Defaults: ``None`` for every provenance keyword, so a caller that
    # names none writes an edge with no provenance, as before.
    defaults = [ast.unparse(d) if d else None for d in _ABSTRACT.kw_defaults]
    assert defaults == ["True", *["None"] * len(EDGE_PROVENANCE_FIELDS)]


@pytest.mark.parametrize("backend", sorted(_HAND_READ_BACKENDS))
def test_backend_signature_matches_the_abstract(backend: str) -> None:
    assert ast.unparse(_BACKENDS[backend]) == ast.unparse(_ABSTRACT)
