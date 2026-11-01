"""Synthetic stores for the public-only differential tests.

Three worlds, all holding the same public documents:

* ``base`` -- only those.
* ``gated`` -- also documents labelled ``public`` that a pack withholds anyway
  (archived, noise-tagged, superseded). A direct read serves them to a public
  caller, as it would to any caller, so they are public documents there.
* ``full`` -- ``gated`` plus documents a public caller may not read at all
  (unlabelled, internal, confidential, restricted, tagged), and enough bulk to
  outrank the public ones on every axis. Every one names the query words.

A pack, whatever the key, must answer the same from all three, apart from ids
drawn per build and clocks, because what an ``external`` key receives must not
depend on anything it may not read. A direct read must answer the same from
``gated`` and ``full``.

The documents are written in words with no meaning outside the tests. A
deterministic bag-of-words embedder stands in for a real one, so the semantic
axis runs without a network.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import TYPE_CHECKING, Any

from tests.unit.retrieve.test_sensitivity import label
from trellis.ingest_corpus.models import chunk_doc_id
from trellis.retrieve.embed_ingest_hook import build_vector_row

if TYPE_CHECKING:
    from collections.abc import Iterable

    from trellis.stores.registry import StoreRegistry

QUERY = "failover runbook"

#: ``TRELLIS_EMBEDDING_FN`` value for :func:`embed`.
EMBED_FN_PATH = "tests.unit.retrieve.public_corpus.embed"

#: Words that appear only in a document labelled ``public`` that a pack withholds
#: (a direct read may serve it).
GATED_WORDS = ("archivedword", "noiseword", "supersededword")

#: Words that appear only in a document a public caller may not read at all.
UNREADABLE_WORDS = (
    "unlabelledword",
    "internalword",
    "confidentialword",
    "restrictedword",
    "taggedword",
    "bulkword",
    "graphword",
)

#: Every word that appears only in a document a pack must not serve.
HIDDEN_WORDS = (*UNREADABLE_WORDS, *GATED_WORDS)

#: Text the vector row of ``pub-stale`` carries and its stored row no longer
#: does: the stored text was cleaned and the label set to ``public``, and the
#: embedding was never refreshed.
STALE_WORD = "staleoldword"

#: Values planted under every metadata key a store's writers put on a document,
#: other than the allowlist and the keys a strategy reads to score a row
#: (``auto_importance`` and its stamp, ``domain``, ``lifecycle``, the label): a
#: sentinel must not change which documents are candidates. None may reach a
#: public caller; the scoring keys are covered by the allowlist unit test.
PLANTED_METADATA: dict[str, Any] = {
    "entity_ids": ["entsentinel-1"],
    "entity_id": "entsentinel-2",
    "entity_name": "entnamesentinel",
    "entity_type": "enttypesentinel",
    "source_trace_id": "tracesentinel-1",
    "trace_id": "tracesentinel-2",
    "document_ids": ["docptrsentinel-1"],
    "related_doc_ids": ["docptrsentinel-2"],
    "parent_doc_id": "docptrsentinel-3",
    "evidence_ref": "docptrsentinel-4",
    "source_path": "svc/sentinelpath.md",
    "source_item_id": "docptrsentinel-5",
    "owner": "ownersentinel",
    "author": "authorsentinel",
    "agent_id": "agentsentinel",
    "session_id": "sessionsentinel",
    "tags": ["tagsentinel"],
    "namespace": "namespacesentinel",
    "supersedes_doc_id": "docptrsentinel-6",
    "content_hash": "hashsentinel",
    "scope": "scopesentinel",
    "retrieval_affinity": ["affinitysentinel"],
    "content_tags": {"domain": ["ops"], "scope": "scopetagsentinel"},
    "source_system": "sourcesystemsentinel",
    "source_origin": "sourceoriginsentinel",
    "classified_by": "classifiedbysentinel",
    "applied_by": "appliedbysentinel",
    "changed_by": "changedbysentinel",
    "updates_doc_id": "docptrsentinel-7",
}

PLANTED_VALUES = (
    "entsentinel-1",
    "entsentinel-2",
    "entnamesentinel",
    "enttypesentinel",
    "tracesentinel-1",
    "tracesentinel-2",
    "docptrsentinel-1",
    "docptrsentinel-2",
    "docptrsentinel-3",
    "docptrsentinel-4",
    "docptrsentinel-5",
    "docptrsentinel-6",
    "sentinelpath",
    "ownersentinel",
    "authorsentinel",
    "agentsentinel",
    "sessionsentinel",
    "tagsentinel",
    "namespacesentinel",
    "hashsentinel",
    "scopesentinel",
    "affinitysentinel",
    "scopetagsentinel",
    "docptrsentinel-7",
    "sourcesystemsentinel",
    "sourceoriginsentinel",
    "classifiedbysentinel",
    "appliedbysentinel",
    "changedbysentinel",
)

PUBLIC_IDS = ("pub-drain", "pub-promote", "pub-verify", "pub-stale", "pub-parent")


def leaked(blob: str, needles: Iterable[str]) -> list[str]:
    """The needles found in ``blob``; assert this is empty.

    Assert on this rather than ``needle not in blob``: on a failure pytest diffs
    the whole blob against the needle line by line, which takes minutes on a set
    of captured responses and buries the one name that leaked.
    """
    return [needle for needle in needles if needle in blob]


def embed(text: str) -> list[float]:
    """A deterministic bag-of-words embedding: 64 buckets, unit length."""
    vec = [0.0] * 64
    for word in re.findall(r"[a-z0-9]+", (text or "").lower()):
        vec[int(hashlib.sha256(word.encode()).hexdigest(), 16) % 64] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def _embed_row(registry: StoreRegistry, doc_id: str, text: str, meta: dict) -> None:
    row = build_vector_row(doc_id, text, meta, embed)
    registry.knowledge.vector_store.upsert(
        row["item_id"], row["vector"], row["metadata"]
    )


def _put(
    registry: StoreRegistry,
    doc_id: str,
    text: str,
    meta: dict[str, Any],
    *,
    vector_text: str | None = None,
) -> None:
    registry.knowledge.document_store.put(doc_id, text, meta)
    _embed_row(registry, doc_id, vector_text if vector_text is not None else text, meta)


def seed_public(registry: StoreRegistry) -> None:
    """The documents every world holds. All are labelled ``public``."""
    pub = label("public")
    for doc_id, text, title in (
        (
            "pub-drain",
            "failover runbook: drain the write queue before promoting the replica. ",
            "Drain procedure",
        ),
        (
            "pub-promote",
            "failover runbook: promote the standby and confirm the replication lag. ",
            "Promote procedure",
        ),
        (
            "pub-verify",
            "runbook for verifying a failover: check the write queue and replica lag. ",
            "Verify procedure",
        ),
    ):
        _put(
            registry,
            doc_id,
            text * 3,
            {
                **PLANTED_METADATA,
                "title": title,
                "document_form": "runbook",
                **pub,
            },
        )
    # Cleaned and relabelled after it was embedded: the vector row still holds
    # the earlier text and the earlier metadata.
    _put(
        registry,
        "pub-stale",
        "failover runbook: cleaned text that names nothing sensitive. " * 3,
        {"title": "Cleaned note", **pub},
        vector_text=f"failover runbook {STALE_WORD} internal draft " * 3,
    )
    # A public document with chunk rows: the parent is served, no chunk is.
    registry.knowledge.document_store.put(
        "pub-parent",
        "failover runbook: parent of a chunked document. " * 3,
        {"title": "Chunked", **pub},
    )
    _embed_row(
        registry,
        "pub-parent",
        "failover runbook: parent of a chunked document. " * 3,
        {"title": "Chunked", **pub},
    )
    chunk = chunk_doc_id("pub-parent", 0)
    registry.knowledge.document_store.put(
        chunk,
        "failover runbook chunkonlyword: one chunk of the parent. " * 3,
        {"parent_doc_id": "pub-parent", **pub},
    )
    _embed_row(
        registry,
        chunk,
        "failover runbook chunkonlyword: one chunk of the parent. " * 3,
        {"parent_doc_id": "pub-parent", **pub},
    )


GATED_IDS = ("h-archived", "h-noise", "h-old")


def seed_gated(registry: StoreRegistry) -> None:
    """Documents labelled ``public`` that every pack gate withholds.

    The sharpest oracle for a count: the label alone would have served them.
    """
    pub = label("public")
    for doc_id, word, meta in (
        (
            "h-archived",
            "archivedword",
            {"title": "Archived", **pub, "lifecycle": {"state": "archived"}},
        ),
        (
            "h-noise",
            "noiseword",
            {"title": "Noise", **pub, "content_tags": {"signal_quality": "noise"}},
        ),
        (
            "h-old",
            "supersededword",
            {
                "title": "Old",
                **pub,
                "lifecycle": {"state": "superseded", "superseded_by": "pub-drain"},
            },
        ),
    ):
        _put(registry, doc_id, f"failover runbook {word} " * 4, meta)


def seed_unreadable(registry: StoreRegistry, *, bulk: int = 30) -> None:
    """Documents a public caller may not read, all naming the query words.

    ``bulk`` unlabelled documents repeat the words more often than any public
    one does, so they outrank the public documents on both axes and fill a
    strategy's first window.
    """
    for doc_id, word, meta in (
        ("h-unlabelled", "unlabelledword", {"title": "Unlabelled"}),
        ("h-internal", "internalword", {"title": "Internal", **label("internal")}),
        (
            "h-confidential",
            "confidentialword",
            {"title": "Confidential", **label("confidential")},
        ),
        (
            "h-restricted",
            "restrictedword",
            {"title": "Restricted", **label("restricted")},
        ),
        (
            "h-tagged",
            "taggedword",
            {"title": "Tagged", "content_tags": {"domain": ["ops"]}},
        ),
    ):
        _put(registry, doc_id, f"failover runbook {word} " * 4, meta)
    for i in range(bulk):
        _put(
            registry,
            f"h-bulk-{i:02d}",
            "failover runbook failover runbook failover runbook bulkword " * 3,
            {"title": f"Bulk {i}"},
        )
    registry.knowledge.graph_store.upsert_node(
        node_id="ent-hidden",
        node_type="entity",
        properties={"name": "failover runbook graphword"},
    )


WORLDS = ("base", "gated", "full")


def build_world(registry: StoreRegistry, kind: str) -> None:
    """Seed ``registry`` as the ``base``, ``gated`` or ``full`` world."""
    assert kind in WORLDS, kind
    seed_public(registry)
    if kind in ("gated", "full"):
        seed_gated(registry)
    if kind == "full":
        seed_unreadable(registry)


#: Fields that differ between two identical runs: ids drawn per build and
#: clocks. Masked before comparing; nothing else is.
_MASKED_KEY = re.compile(
    r"^(pack_id|event_id|key_id)$|_at$|^timestamp$|duration_ms|latency|elapsed"
)
_ULID = re.compile(r"\b[0-9A-HJKMNP-TV-Z]{26}\b")


def mask(value: Any) -> Any:
    """``value`` with per-build ids and clocks replaced, for comparison."""
    if isinstance(value, dict):
        return {
            k: ("<masked>" if _MASKED_KEY.search(k) else mask(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [mask(v) for v in value]
    if isinstance(value, str):
        return _ULID.sub("<id>", value)
    return value


def differences(a: Any, b: Any, path: str = "") -> list[str]:
    """Paths at which two captures differ, so a failure names the field."""
    if isinstance(a, dict) and isinstance(b, dict):
        return [
            d
            for k in sorted(a.keys() | b.keys())
            for d in differences(a.get(k), b.get(k), f"{path}/{k}")
        ]
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        return [
            d
            for i, (x, y) in enumerate(zip(a, b, strict=True))
            for d in differences(x, y, f"{path}[{i}]")
        ]
    return [] if a == b else [f"{path}: {a!r} != {b!r}"[:300]]
