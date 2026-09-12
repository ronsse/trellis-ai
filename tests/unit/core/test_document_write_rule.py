"""Every document-plane write in ``src/`` goes through the seam.

:mod:`trellis.core.document_write` exists because the guarantee it replaces
was *"every writer calls the right mirror helper"*, and there were **two**
helpers with disjoint key sets — so a writer had to pick, and #337 and #338
are what picking wrong looks like in production. A seam only moves the
failure if nothing can reach the store around it, so this is the rule the
seam's own docstring names:

    no ``document_store.put(...)`` anywhere in ``src/`` outside
    ``trellis/core/document_write.py`` and ``trellis/stores/``.

**Collection is deliberately wider than the offence.** Every call whose
trailing name is ``put`` is collected — ``ParameterStore``'s and
``AdvisoryStore``'s included — and the *classification* is what narrows.
That order is not stylistic. Four scans written for this change resolved the
receiver first (attribute chain, then local assignment, then parameter
annotation) and each one under-reported: the last of them found **one** site
where the tree held **four**, missing ``trellis_cli/demo.py``'s two loop
writes and ``trellis_cli/ingest.py``'s keyword-spelled one. That is #457's
shape exactly — a scanner narrowing quietly while every guard divides by its
own output — which is why the receiver is not consulted at all.

**What separates a document ``put`` from the other stores' is the call's own
shape, not a roster of receiver names.** ``ParameterStore.put(params)`` and
``AdvisoryStore.put(advisory)`` take one record; ``DocumentStore.put`` takes
``(doc_id, content, metadata, *, preserve_updated_at)``. A name roster rots
the first time someone renames a local; arity does not.

**The known false positive is ``BlobStore.put``, and it is left as one.**
``put(key, data, metadata=None, *, expires_at=None)`` is structurally
indistinguishable from a document write at the AST — same arity, same
``metadata`` keyword — so only an explicit blob keyword separates them.
``src/`` writes **zero** ``BlobStore.put`` calls today (measured 2026-09-12),
so nothing is exempted for a caller that does not exist; a future one that
passes neither ``key=``/``data=``/``expires_at=`` will trip this rule. That
is the cheap direction to be wrong in — over-collection costs a keyword at
the call site, under-collection costs an unmirrored vector row — and it is
recorded here rather than pre-emptively exempted, because an exemption for a
population of zero is a blind spot nobody will revisit.
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests.ast_rules import (
    CallSite,
    assert_hand_read_floor,
    assert_scan_is_not_vacuous,
    calls_to_any,
    construction_names,
    iter_modules,
)

ROOT = Path(__file__).parents[3]
SRC = ROOT / "src"

#: The seam, and the backends it is a seam over. A store implementation
#: delegating to another store's ``put`` is the layer this rule is about,
#: not a caller sneaking past it.
ALLOWED_SEAM = "trellis/core/document_write.py"
ALLOWED_PREFIX = "trellis/stores/"

#: Keywords only ``DocumentStore.put`` takes.
DOCUMENT_ONLY_KEYWORDS = frozenset({"doc_id", "content", "preserve_updated_at"})
#: Keywords only ``BlobStore.put`` takes. ``metadata`` is shared by both and
#: is deliberately absent from either set.
BLOB_ONLY_KEYWORDS = frozenset({"key", "data", "expires_at"})

#: Hand-read off ``src/`` on 2026-09-12: one document write (the seam), two
#: ``ParameterStore`` writes under ``learning/tuners/``, three
#: ``AdvisoryStore`` writes in ``retrieve/effectiveness.py``, and four
#: ``ParameterStore`` writes across ``trellis_cli/``. Deleting a call site
#: means re-counting and re-writing this number — that is the cost of a
#: floor a scan is not allowed to compute for itself (#466).
HAND_READ_PUT_CALLS = 10

#: And exactly one of those ten is a document write. A floor of one is the
#: rule's whole point rather than a symptom of a scan that stopped matching,
#: so it is asserted with the reason the helper requires.
HAND_READ_DOCUMENT_PUTS = 1
SOLE_SITE_REASON = (
    "The seam is the only document write by construction: this rule is what "
    "makes that true, so a second site is the violation and not a floor to "
    "be raised. The guard against a silently-narrowing scan is the "
    "ten-call collection floor above plus the synthetic corpus below, both "
    "of which shrink when discovery does."
)


def _is_document_put(node: ast.Call) -> bool:
    """Does this ``put`` call write a document row?

    Order matters. A splat is unprovable and therefore policed, the same
    judgement ``generate_call_sites`` makes about ``**kwargs``; then the
    keywords only one store takes; then arity, which separates the
    single-record stores from the document and blob ones.
    """
    keywords = {keyword.arg for keyword in node.keywords}
    if None in keywords or any(isinstance(arg, ast.Starred) for arg in node.args):
        return True
    if keywords & DOCUMENT_ONLY_KEYWORDS:
        return True
    if keywords & BLOB_ONLY_KEYWORDS:
        return False
    return len(node.args) >= 2


def _put_call_sites(root: Path) -> list[CallSite]:
    """Every ``put(...)`` under *root*, however the target is bound."""
    sites: list[CallSite] = []
    for path, tree in iter_modules(root):
        names = construction_names("put", tree)
        sites.extend(
            CallSite(path=path, node=node) for node in calls_to_any(names, tree)
        )
    return sites


def _document_put_sites(root: Path) -> list[CallSite]:
    return [site for site in _put_call_sites(root) if _is_document_put(site.node)]


def _document_put_lines(root: Path) -> set[int]:
    return {site.lineno for site in _document_put_sites(root)}


def _relative(site: CallSite, root: Path) -> str:
    return str(site.path.relative_to(root))


def _violations(root: Path) -> list[str]:
    return [
        site.describe(root)
        for site in _document_put_sites(root)
        if _relative(site, root) != ALLOWED_SEAM
        and not _relative(site, root).startswith(ALLOWED_PREFIX)
    ]


def test_no_document_store_put_outside_the_seam() -> None:
    sites = _document_put_sites(SRC)
    assert_hand_read_floor(
        len(sites),
        HAND_READ_DOCUMENT_PUTS,
        subject="document-store put",
        sole_site_reason=SOLE_SITE_REASON,
    )
    assert _violations(SRC) == [], (
        "a document row is written outside trellis.core.document_write, so "
        "its vector row is mirrored only if that caller remembered to — the "
        "convention #337 and #338 are instances of. Call put_document(...) "
        "instead; it mirrors the bag it just wrote, and a content write "
        "still re-embeds after it."
    )


def test_put_collection_is_wider_than_the_offence() -> None:
    """The population the rule classifies, floored independently of it.

    The document-put floor above cannot fall below one while the seam
    exists, so it cannot notice file discovery narrowing. This can.
    """
    assert_hand_read_floor(
        len(_put_call_sites(SRC)),
        HAND_READ_PUT_CALLS,
        subject="`put(...)` call",
        hint="A put call site was deleted, or module discovery narrowed.",
    )


def test_single_record_store_puts_are_not_document_writes() -> None:
    """The negative control: over-collection is cheap, but not free.

    ``ParameterStore`` and ``AdvisoryStore`` writes live outside ``stores/``
    and would be violations if the classifier widened to every ``put``.
    """
    classified = {
        _relative(site, SRC): _is_document_put(site.node)
        for site in _put_call_sites(SRC)
    }
    assert classified.get("trellis/learning/tuners/promotion.py") is False
    assert classified.get("trellis/retrieve/effectiveness.py") is False
    assert classified.get("trellis_cli/analyze.py") is False
    assert classified.get("trellis_cli/classify.py") is False
    assert classified.get(ALLOWED_SEAM) is True


def test_document_put_scan_is_not_vacuous(tmp_path: Path) -> None:
    assert_scan_is_not_vacuous(
        _document_put_lines,
        subject="put",
        tmp_path=tmp_path,
        live_population=len(_document_put_sites(SRC)),
        floor=HAND_READ_DOCUMENT_PUTS,
        args="_doc_id, _content, _metadata",
        kwarg="content",
        sole_site_reason=SOLE_SITE_REASON,
        exempt={
            "partial_binding": (
                "A partial-bound method has no statically resolvable target name."
            ),
            "cross_module_subclass": (
                "Per-module name resolution cannot follow a binding defined elsewhere."
            ),
        },
    )


def test_rule_rejects_a_write_that_reaches_the_store_directly(
    tmp_path: Path,
) -> None:
    """Proved on a synthetic tree, not by asserting today's empty list.

    ``no_metadata.py`` is the two-positional-argument spelling, and it is
    here because ``metadata`` defaults to ``None`` on ``DocumentStore.put``
    — so a writer with no metadata to set produces a *legal* document write
    the arity floor has to catch at two rather than three. Without it both
    ``>= 3`` and ``== 3`` pass this whole file (measured), and the spelling
    that evades the rule is the one a caller reaches for by accident.
    """
    package = tmp_path / "trellis" / "mutate"
    package.mkdir(parents=True)
    (package / "positional.py").write_text(
        "def run(store, doc_id, content, metadata):\n"
        "    store.put(doc_id, content, metadata)\n",
        encoding="utf-8",
    )
    (package / "no_metadata.py").write_text(
        "def run(store, doc_id, content):\n    store.put(doc_id, content)\n",
        encoding="utf-8",
    )
    (package / "keyword.py").write_text(
        "def run(store, evidence):\n"
        "    store.put(doc_id=evidence.id, content=evidence.body, metadata={})\n",
        encoding="utf-8",
    )
    (package / "splat.py").write_text(
        "def run(store, **kwargs):\n    store.put(**kwargs)\n",
        encoding="utf-8",
    )
    (package / "single_record.py").write_text(
        "def run(advisory_store, advisory):\n    advisory_store.put(advisory)\n",
        encoding="utf-8",
    )
    (package / "blob.py").write_text(
        "def run(blob_store, key, data):\n"
        "    blob_store.put(key, data, {}, expires_at=None)\n",
        encoding="utf-8",
    )

    reported = {Path(v.split(":")[0]).name for v in _violations(tmp_path)}
    assert reported == {
        "positional.py",
        "no_metadata.py",
        "keyword.py",
        "splat.py",
    }


def test_the_seam_and_the_backends_are_the_only_allowed_locations(
    tmp_path: Path,
) -> None:
    """The location half, which the live tree cannot exercise on its own.

    ``sibling.py`` is what makes the exemption *narrow* rather than merely
    present: it is the seam's own package, and widening
    :data:`ALLOWED_SEAM` to ``trellis/core/`` passes every other assertion
    here (measured). The exemption is one file because the seam is one
    file; a neighbour that reaches the store directly is as much a
    violation as one in ``mutate/``.
    """
    seam = tmp_path / "trellis" / "core"
    seam.mkdir(parents=True)
    (seam / "document_write.py").write_text(
        "def put_document(document_store, doc_id, content, metadata):\n"
        "    document_store.put(doc_id, content, metadata)\n",
        encoding="utf-8",
    )
    (seam / "sibling.py").write_text(
        "def run(document_store, doc_id, content, metadata):\n"
        "    document_store.put(doc_id, content, metadata)\n",
        encoding="utf-8",
    )
    backend = tmp_path / "trellis" / "stores" / "sqlite"
    backend.mkdir(parents=True)
    (backend / "document.py").write_text(
        "def delegate(inner, doc_id, content, metadata):\n"
        "    inner.put(doc_id, content, metadata)\n",
        encoding="utf-8",
    )

    assert len(_document_put_sites(tmp_path)) == 3
    reported = {Path(v.split(":")[0]).name for v in _violations(tmp_path)}
    assert reported == {"sibling.py"}
