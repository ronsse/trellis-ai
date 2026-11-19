"""Seed helpers for the near-duplicates tests: invented texts and hand-set vectors.

Every pair lives in its own two coordinates of a 12-dimensional space, so a
pair's cosine is exactly the one written here and every cross-pair cosine is 0.
The texts are invented. ``LEX_B`` differs from ``LEX_A`` by one word, so the
shipped MinHash predicate (character trigrams, Jaccard 0.85) catches it;
``PARA_B`` says what ``PARA_A`` says in other words, so MinHash does not.
"""

from __future__ import annotations

import math
from typing import Any

from trellis.retrieve.lifecycle import SUPERSEDED_STATE
from trellis.retrieve.noise import NOISE_SIGNAL_QUALITY, SIGNAL_QUALITY_FACET
from trellis.schemas.classification import LIFECYCLE_KEY

DIMS = 12

PARA_A = (
    "To rotate the signing key, generate a fresh pair, publish the public half "
    "to the key registry, wait one hour for caches to expire, then retire the "
    "old private half."
)
PARA_B = (
    "Key rotation procedure: create new keys, upload the public one so the "
    "registry lists it, allow sixty minutes so cached copies lapse, and only "
    "afterwards revoke the previous secret."
)
LEX_A = (
    "The nightly export job writes compressed archives to the cold bucket and "
    "deletes local copies older than fourteen days once the upload is verified."
)
LEX_B = LEX_A.replace("local copies", "local copy")
MID_A = (
    "Release trains leave every second Tuesday; a change that misses the cut "
    "waits for the next train unless the release captain grants an exception."
)
MID_B = (
    "We ship on alternate Tuesdays. Anything not merged by the cutoff rides the "
    "following departure, barring a waiver from whoever is running that release."
)
OTHER = (
    "Garden notes: the tomatoes need staking by midsummer and the herb bed "
    "wants a thinner layer of mulch than last year."
)


def unit(axis: int, cosine: float = 1.0) -> list[float]:
    """A unit vector at angle ``acos(cosine)`` from axis ``axis`` toward ``axis+1``."""
    vec = [0.0] * DIMS
    vec[axis] = cosine
    vec[axis + 1] = math.sqrt(max(0.0, 1.0 - cosine * cosine))
    return vec


def put(
    stores: Any,
    doc_id: str,
    content: str,
    vector: list[float] | None,
    metadata: dict[str, Any] | None = None,
) -> None:
    stores.knowledge.document_store.put(doc_id, content, metadata or {})
    if vector is not None:
        stores.knowledge.vector_store.upsert(doc_id, vector, {})


ARCHIVED = {LIFECYCLE_KEY: {"state": "archived"}}
SUPERSEDED = {LIFECYCLE_KEY: {"state": SUPERSEDED_STATE, "superseded_by": "kb:mid-a"}}
NOISE = {"content_tags": {SIGNAL_QUALITY_FACET: NOISE_SIGNAL_QUALITY}}


def seed_main(stores: Any) -> None:
    """The fixture most tests read.

    * ``kb:para-a`` / ``kb:para-b``: cosine 0.97, reworded -> residue at 0.90 and 0.95.
    * ``mem:lex-a`` / ``mem:lex-b``: cosine 0.97, one word apart -> lexical.
    * ``kb:mid-a`` / ``kb:mid-b``: cosine 0.92, reworded -> residue at 0.90 only.
    * ``arch``, ``sup``, ``noisy``: each a copy of a residue member's vector
      with other text, so leaving any of them in adds residue pairs.
    * ``bare``: no embedding. ``kb:para-a#chunk-0``: a chunk with para-a's vector.
    """
    put(stores, "kb:para-a", PARA_A, unit(0), {"source_system": "notes"})
    put(stores, "kb:para-b", PARA_B, unit(0, 0.97), {"source_system": "notes"})
    put(stores, "mem:lex-a", LEX_A, unit(2))
    put(stores, "mem:lex-b", LEX_B, unit(2, 0.97))
    put(stores, "kb:mid-a", MID_A, unit(4))
    put(stores, "kb:mid-b", MID_B, unit(4, 0.92))
    put(stores, "arch", OTHER + " archived", unit(0), ARCHIVED)
    put(stores, "sup", OTHER + " superseded", unit(4), SUPERSEDED)
    put(stores, "noisy", OTHER + " noise", unit(0, 0.97), NOISE)
    put(stores, "bare", OTHER, None)
    put(
        stores,
        "kb:para-a#chunk-0",
        PARA_A,
        unit(0),
        {"parent_doc_id": "kb:para-a", "chunk_index": 0},
    )
