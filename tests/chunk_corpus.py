"""Shared seeders for chunk-visibility tests (#396).

Four suites assert the same rule from different layers — the store
contract, the REST route, the CLI, and the SDK — and each needs a corpus
shaped like what ``trellis ingest corpus`` writes: a parent document plus
the ``<parent>#chunk-N`` slices cut from it. Kept in one module because
:func:`seed_chunk_favouring` encodes a non-obvious ranking argument that
was wrong once already, and three copies of an argument is three places
for it to drift — the defect #396 itself is about.

Imported cross-directory the way ``tests/structlog_isolation.py`` and
``tests/integration/_live_server.py`` already are (``pythonpath`` in
``pyproject.toml`` puts the repo root on the path).
"""

from __future__ import annotations

from typing import Any


def seed_chunked(
    store: Any,
    *,
    parents: int = 3,
    per_parent: int = 3,
    term: str = "distinctive",
    id_prefix: str = "corpus:notes",
) -> list[str]:
    """Seed *parents* documents, each followed by *per_parent* chunk slices.

    Every document matches *term*, so a search over the corpus returns
    parents and chunks together. Returns the parent ids.

    ``id_prefix`` mirrors the production id shape
    ``corpus:<source_system>:<id>``.

    **CLI callers must pass ``id_prefix="corpus:obsidian"``.** The default
    ``corpus:notes`` is fine over HTTP and in-process, but CLI commands print
    ``--format json`` through ``console.print``, and Rich rewrites the
    ``:notes:`` in the middle of a doc id to an emoji (#403). The default is
    kept as ``notes`` rather than sidestepped because that is the id shape
    the REST and store tests already assert on, and a fixture that quietly
    avoids a live defect teaches the next reader nothing.
    """
    parent_ids = []
    for p in range(parents):
        parent_id = f"{id_prefix}:doc{p}"
        store.put(parent_id, f"parent {p} {term} body text")
        parent_ids.append(parent_id)
        for c in range(per_parent):
            store.put(
                f"{parent_id}#chunk-{c}",
                f"parent {p} {term} body text slice {c}",
                {"parent_doc_id": parent_id, "chunk_index": c},
            )
    return parent_ids


def seed_chunk_favouring(
    store: Any,
    *,
    parents: int = 25,
    term: str = "distinctive",
    id_prefix: str = "corpus:notes",
) -> list[str]:
    """Seed a corpus whose *chunks outrank their parents* on *term*.

    The ranking is the whole point, and it is why this is not
    :func:`seed_chunked` with different numbers. ``search`` applies
    ``LIMIT`` after ordering by relevance, so a post-hoc filter over the
    result set is only distinguishable from a pushdown into the query when
    the top-N *contains chunks*. Under :func:`seed_chunked` the chunks are
    strictly longer than their parents and carry the term once, so SQLite's
    ``bm25`` (which penalises length) ranks every parent above every chunk:
    a 20-row page over 25 parents is all parents, a page filter has nothing
    to remove, and a test written on that fixture passes against the very
    implementation it is meant to reject. That is not hypothetical — it is
    what the first version of
    ``test_excluding_chunks_still_fills_the_search_limit`` did. On Postgres
    the same fixture merely *ties* rather than ordering (``ts_rank`` at the
    default normalization ignores length), which is worse: the outcome is
    then whatever order the planner happens to return.

    Here the term appears three times in a short chunk and once in a long
    parent, which puts chunks first on term frequency (Postgres
    ``ts_rank``) and on frequency *and* brevity (SQLite ``bm25``). Callers
    should still assert the precondition — that the unfiltered top-N really
    is all chunks — rather than trusting this docstring.

    Returns the parent ids.
    """
    parent_ids = []
    for p in range(parents):
        parent_id = f"{id_prefix}:doc{p}"
        filler = " ".join(f"filler{p}x{i}" for i in range(40))
        store.put(parent_id, f"{term} parent {p} {filler}")
        parent_ids.append(parent_id)
        for c in range(3):
            store.put(
                f"{parent_id}#chunk-{c}",
                f"{term} {term} {term}",
                {"parent_doc_id": parent_id, "chunk_index": c},
            )
    return parent_ids
