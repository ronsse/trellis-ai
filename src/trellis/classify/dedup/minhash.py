"""MinHash / LSH deterministic fuzzy-duplicate detection.

Provides a lightweight, LLM-free dedup stage that catches fuzzy duplicates
(typos, casing, punctuation variations) for the ``save_memory`` path and
document ingestion.  Sits between exact-match alias dedup and a future
LLM-based resolution fallback.

Design:
- **MinHash signatures** are computed per document using character-level
  shingles (default k=3) and a configurable number of hash permutations
  (default 128).
- **LSH banding** groups signatures into bands for fast approximate
  nearest-neighbor lookup — only items that share at least one identical
  band are compared, giving sub-linear lookup cost.
- **Jaccard threshold** (default 0.85) governs the match sensitivity.
- **Entropy filter** skips very short or generic content to avoid
  false positives on trivial strings.

Reference: Graphiti's ``utils/maintenance/dedup_helpers.py`` (3-stage
dedup pipeline: exact → MinHash/LSH → LLM).  We adopt the middle stage.
"""

from __future__ import annotations

import hashlib
import struct
from typing import Any

import structlog

logger = structlog.get_logger()

#: Large prime for hash mixing (Mersenne prime 2^61 - 1).
_MERSENNE_PRIME = (1 << 61) - 1
_MAX_HASH = (1 << 32) - 1


def _char_shingles(text: str, k: int = 3) -> set[str]:
    """Extract character-level k-shingles from normalised text."""
    text = text.lower().strip()
    if len(text) < k:
        return {text} if text else set()
    return {text[i : i + k] for i in range(len(text) - k + 1)}


def _hash_shingle(shingle: str) -> int:
    """Deterministic 32-bit hash of a shingle string."""
    result: int = struct.unpack(
        "<I",
        hashlib.md5(shingle.encode(), usedforsecurity=False).digest()[:4],
    )[0]
    return result


class MinHashSignature:
    """A MinHash signature for a single document."""

    __slots__ = ("bands", "doc_id", "values")

    def __init__(
        self,
        doc_id: str,
        values: tuple[int, ...],
        num_bands: int,
    ) -> None:
        self.doc_id = doc_id
        self.values = values
        # Pre-compute band hashes for LSH lookup
        rows_per_band = len(values) // num_bands
        self.bands: tuple[int, ...] = tuple(
            hash(values[i * rows_per_band : (i + 1) * rows_per_band])
            for i in range(num_bands)
        )


class MinHashIndex:
    """In-memory MinHash/LSH index for fuzzy duplicate detection.

    Usage::

        index = MinHashIndex()
        index.add("doc1", "The quick brown fox jumps over the lazy dog")
        matches = index.query("The quikc brown fox jumps over the lazy dog")
        # matches == [("doc1", 0.92)]  # (doc_id, estimated_jaccard)

    Args:
        num_perm: Number of hash permutations (higher = more accurate, more RAM).
        num_bands: Number of LSH bands. ``num_perm`` must be divisible by this.
            More bands = higher recall (catches more fuzzy matches) at the cost
            of more false-positive candidates to verify.
        threshold: Minimum estimated Jaccard similarity for a match.
        shingle_size: Character n-gram size for shingling.
        min_shingles: Documents with fewer shingles than this are skipped
            (entropy filter — avoids false positives on trivial content).
    """

    def __init__(
        self,
        *,
        num_perm: int = 128,
        num_bands: int = 16,
        threshold: float = 0.85,
        shingle_size: int = 3,
        min_shingles: int = 5,
    ) -> None:
        if num_perm % num_bands != 0:
            msg = f"num_perm ({num_perm}) must be divisible by num_bands ({num_bands})"
            raise ValueError(msg)
        self._num_perm = num_perm
        self._num_bands = num_bands
        self._threshold = threshold
        self._shingle_size = shingle_size
        self._min_shingles = min_shingles

        # Generate stable random coefficients for MinHash permutations
        # Using deterministic seeds for reproducibility
        self._a: tuple[int, ...] = tuple(
            (i * 6364136223846793005 + 1442695040888963407) % _MERSENNE_PRIME
            for i in range(1, num_perm + 1)
        )
        self._b: tuple[int, ...] = tuple(
            (i * 1442695040888963407 + 6364136223846793005) % _MERSENNE_PRIME
            for i in range(1, num_perm + 1)
        )

        # Storage
        self._signatures: dict[str, MinHashSignature] = {}
        # LSH buckets: band_index -> band_hash -> set of doc_ids
        self._buckets: list[dict[int, set[str]]] = [{} for _ in range(num_bands)]

    @property
    def size(self) -> int:
        """Number of documents in the index."""
        return len(self._signatures)

    def _compute_signature(self, shingles: set[str]) -> tuple[int, ...]:
        """Compute MinHash signature from a set of shingles."""
        hashes = [_hash_shingle(s) for s in shingles]
        sig: list[int] = []
        for i in range(self._num_perm):
            min_val = _MAX_HASH
            a_i = self._a[i]
            b_i = self._b[i]
            for h in hashes:
                val = ((a_i * h + b_i) % _MERSENNE_PRIME) & _MAX_HASH
                min_val = min(min_val, val)
            sig.append(min_val)
        return tuple(sig)

    @staticmethod
    def _estimate_jaccard(sig_a: tuple[int, ...], sig_b: tuple[int, ...]) -> float:
        """Estimate Jaccard similarity from two MinHash signatures."""
        matching = sum(1 for a, b in zip(sig_a, sig_b, strict=True) if a == b)
        return matching / len(sig_a)

    def add(self, doc_id: str, content: str) -> bool:
        """Add a document to the index.

        Returns ``True`` if the document was added, ``False`` if it was
        skipped due to the entropy filter (too short/generic).
        """
        shingles = _char_shingles(content, self._shingle_size)
        if len(shingles) < self._min_shingles:
            logger.debug(
                "minhash_skip_low_entropy",
                doc_id=doc_id,
                shingle_count=len(shingles),
            )
            return False

        sig_values = self._compute_signature(shingles)
        sig = MinHashSignature(doc_id, sig_values, self._num_bands)
        self._signatures[doc_id] = sig

        # Insert into LSH buckets
        for band_idx, band_hash in enumerate(sig.bands):
            bucket = self._buckets[band_idx]
            if band_hash not in bucket:
                bucket[band_hash] = set()
            bucket[band_hash].add(doc_id)

        return True

    def remove(self, doc_id: str) -> bool:
        """Remove a document from the index. Returns ``True`` if it existed."""
        sig = self._signatures.pop(doc_id, None)
        if sig is None:
            return False
        for band_idx, band_hash in enumerate(sig.bands):
            bucket = self._buckets[band_idx].get(band_hash)
            if bucket is not None:
                bucket.discard(doc_id)
                if not bucket:
                    del self._buckets[band_idx][band_hash]
        return True

    def query(
        self, content: str, *, exclude_ids: set[str] | None = None
    ) -> list[tuple[str, float]]:
        """Find fuzzy duplicates of the given content.

        Returns a list of ``(doc_id, estimated_jaccard)`` pairs sorted
        by similarity descending, filtered to those above ``threshold``.
        """
        shingles = _char_shingles(content, self._shingle_size)
        if len(shingles) < self._min_shingles:
            return []

        sig_values = self._compute_signature(shingles)
        rows_per_band = self._num_perm // self._num_bands

        # LSH candidate retrieval: collect doc_ids sharing any band
        candidates: set[str] = set()
        for band_idx in range(self._num_bands):
            band_hash = hash(
                sig_values[band_idx * rows_per_band : (band_idx + 1) * rows_per_band]
            )
            bucket = self._buckets[band_idx].get(band_hash)
            if bucket:
                candidates |= bucket

        if exclude_ids:
            candidates -= exclude_ids

        # Verify candidates against threshold
        matches: list[tuple[str, float]] = []
        for cid in candidates:
            csig = self._signatures.get(cid)
            if csig is None:
                continue
            jaccard = self._estimate_jaccard(sig_values, csig.values)
            if jaccard >= self._threshold:
                matches.append((cid, jaccard))

        matches.sort(key=lambda x: x[1], reverse=True)
        return matches

    def find_duplicate(
        self, content: str, *, exclude_ids: set[str] | None = None
    ) -> tuple[str, float] | None:
        """Return the best matching duplicate, or ``None`` if no match.

        Convenience wrapper around ``query()`` returning only the
        top match above threshold.
        """
        matches = self.query(content, exclude_ids=exclude_ids)
        return matches[0] if matches else None

    def stats(self) -> dict[str, Any]:
        """Return index statistics for diagnostics."""
        non_empty_buckets = sum(
            sum(1 for s in band.values() if s) for band in self._buckets
        )
        return {
            "documents": self.size,
            "num_perm": self._num_perm,
            "num_bands": self._num_bands,
            "threshold": self._threshold,
            "shingle_size": self._shingle_size,
            "non_empty_buckets": non_empty_buckets,
        }
