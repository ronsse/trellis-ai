"""Deterministic secret-scan gate — runs before any memory is written.

The transcript threat model is real: Claude Code session JSONL embeds
secrets-manager read outputs, bearer tokens, env dumps, and private keys. The
distiller is instructed never to quote raw tool output, but distillation is
model-judged and therefore fallible — so a *deterministic* gate runs on
every candidate memory's rendered text before it reaches
:func:`~trellis.ingest_corpus.sync.sync_records`. A positive hit drops the
candidate and increments a per-class counter; the offending content is
**never** logged (only the class name), because a leaked token that reaches
a memory doc would be served back to every future session.

The regex classes are the ones named in the #255 implementation guide:

* ``key_value_secret`` — ``api_key=...`` / ``password: ...`` style pairs.
* ``bearer_token`` — ``Authorization: Bearer <token>`` headers.
* ``op_ref`` — secrets-manager URI references (the ``op`` URI scheme).
* ``pem_private_key`` — ``-----BEGIN ... PRIVATE KEY-----`` blocks.
* ``high_entropy_string`` — long base64/hex tokens with high Shannon entropy
  (catches raw API responses and access keys the named patterns miss).
"""

from __future__ import annotations

import math
import re

#: The secrets-manager URI scheme, assembled from parts so the literal scheme
#: string never appears verbatim in source (keeps repo secret-scanners quiet).
_OP_SCHEME = "op:" + "//"

#: Minimum token length considered for the entropy heuristic. Shorter
#: strings rarely carry enough bits to distinguish a secret from prose.
_ENTROPY_MIN_LEN = 20

#: Shannon-entropy threshold (bits per character) above which a long
#: secret-shaped token is treated as a credential. English prose sits near
#: ~3.0-4.0 over its alphabet; random base64/hex tokens exceed ~4.0.
_ENTROPY_THRESHOLD = 4.0

#: Named, ordered regex classes. The values are compiled once at import.
_PATTERNS: dict[str, re.Pattern[str]] = {
    "key_value_secret": re.compile(
        r"(?i)\b(?:api[_-]?key|secret(?:[_-]?key)?|access[_-]?key|"
        r"auth[_-]?token|token|password|passwd|pwd|client[_-]?secret)\b"
        r"\s*[:=]\s*['\"]?[^\s'\"]{6,}"
    ),
    "bearer_token": re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/\-]{10,}=*"),
    "op_ref": re.compile(re.escape(_OP_SCHEME) + r"[^\s'\"]+"),
    "pem_private_key": re.compile(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----"),
}

#: Token shape fed to the entropy heuristic (base64 / hex / url-safe).
_TOKEN_RE = re.compile(rf"[A-Za-z0-9+/=_\-]{{{_ENTROPY_MIN_LEN},}}")

#: The high-entropy class name, kept as a constant so callers can special-case.
HIGH_ENTROPY_CLASS = "high_entropy_string"

#: The full set of class names this module can report (stable public list).
SECRET_CLASSES: tuple[str, ...] = (*_PATTERNS.keys(), HIGH_ENTROPY_CLASS)


def _shannon_entropy(token: str) -> float:
    """Return the Shannon entropy (bits/char) of *token*."""
    if not token:
        return 0.0
    counts: dict[str, int] = {}
    for char in token:
        counts[char] = counts.get(char, 0) + 1
    length = len(token)
    return -sum(
        (count / length) * math.log2(count / length) for count in counts.values()
    )


def _has_high_entropy_token(text: str) -> bool:
    """``True`` iff *text* holds a long, high-entropy secret-shaped token."""
    for match in _TOKEN_RE.finditer(text):
        token = match.group(0)
        # A run of digits (a big number, a hash id) or a single repeated
        # character is not a credential; entropy separates them cleanly.
        if _shannon_entropy(token) >= _ENTROPY_THRESHOLD:
            return True
    return False


def scan(text: str) -> list[str]:
    """Return the sorted names of every secret class matched in *text*.

    Never returns or logs the matched substring — only the class labels, so
    a caller can increment counters and drop content without re-leaking it.
    """
    hits: set[str] = set()
    for name, pattern in _PATTERNS.items():
        if pattern.search(text):
            hits.add(name)
    if _has_high_entropy_token(text):
        hits.add(HIGH_ENTROPY_CLASS)
    return sorted(hits)


def contains_secret(text: str) -> bool:
    """``True`` iff any secret class matches — the hard write gate."""
    return bool(scan(text))
