"""Near-miss warnings for open-string entity / edge types.

``Entity.entity_type`` and ``Edge.edge_kind`` are deliberately open
strings (see CLAUDE.md and ``docs/design/adr-graph-ontology.md``):
domain-specific integrations pass their own values verbatim and the
storage layer accepts them. Closing the type set would break those
integrations.

That openness has a cost: typos slip through silently. Writing
``"dbt-model"`` when the canonical alias is ``"dbt_model"`` produces
a node that never buckets with the rest of the dbt corpus, and the
divergence is invisible at write time. This module catches the common
shape — separator drift (``_`` ↔ ``-``), case drift, and one-character
typos — by emitting a ``structlog`` warning at construction time.

The warning is informational only: the value is preserved verbatim and
the model still validates. Callers can act on the warning if they
choose; analytics workers downstream can grep for the event key to
find typo hot-spots.
"""

from __future__ import annotations

from typing import Final

import structlog

from trellis.schemas.enums import EdgeKind, EntityType
from trellis.schemas.well_known import (
    CANONICAL_EDGE_KINDS,
    CANONICAL_ENTITY_TYPES,
    EDGE_KIND_ALIASES,
    ENTITY_TYPE_ALIASES,
)

logger = structlog.get_logger(__name__)

# Build the "known" sets once at module import. These are the values
# that pass through silently — exact membership = no warning.
_KNOWN_ENTITY_TYPES: Final[frozenset[str]] = frozenset(
    {
        *CANONICAL_ENTITY_TYPES,
        *ENTITY_TYPE_ALIASES.keys(),
        *(e.value for e in EntityType),
    }
)

_KNOWN_EDGE_KINDS: Final[frozenset[str]] = frozenset(
    {
        *CANONICAL_EDGE_KINDS,
        *EDGE_KIND_ALIASES.keys(),
        *(e.value for e in EdgeKind),
    }
)


def _levenshtein(a: str, a_len: int, b: str, b_len: int) -> int:
    """Iterative two-row Levenshtein distance.

    Internal helper kept tiny — we only call it on short identifier
    strings (well-known names) so the O(n*m) cost is negligible. Inlined
    rather than pulled in as a dependency so ``trellis`` stays
    dependency-free at the schema layer.
    """
    if a_len == 0:
        return b_len
    if b_len == 0:
        return a_len
    prev = list(range(b_len + 1))
    curr = [0] * (b_len + 1)
    for i in range(1, a_len + 1):
        curr[0] = i
        for j in range(1, b_len + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            curr[j] = min(
                prev[j] + 1,  # deletion
                curr[j - 1] + 1,  # insertion
                prev[j - 1] + cost,  # substitution
            )
        prev, curr = curr, prev
    return prev[b_len]


def _normalise(value: str) -> str:
    """Lowercase + collapse ``_`` and ``-`` to a single canonical form."""
    return value.lower().replace("-", "_")


def _closest_match(value: str, known: frozenset[str]) -> str | None:
    """Return the closest known value to *value*, or ``None``.

    Two cheap passes before falling back to Levenshtein:

    1. Normalise (lowercase, ``-`` → ``_``) and check exact match — this
       catches the dominant typo class (``dbt-model`` ↔ ``dbt_model``,
       ``Person`` capitalised wrong, etc.) without distance maths.
    2. Otherwise, scan known values and return the lexicographically
       smallest one within Levenshtein distance ≤ 1 of either the raw
       value or its normalised form.
    """
    norm = _normalise(value)
    for candidate in known:
        if _normalise(candidate) == norm and candidate != value:
            return candidate

    # Levenshtein pass — keep both raw and normalised comparisons so we
    # catch ``dbtModel`` (case-only drift from ``dbt_model`` after
    # normalisation) *and* ``Persn`` (one-char typo on canonical form).
    best: str | None = None
    value_len = len(value)
    norm_len = len(norm)
    for candidate in sorted(known):
        cand_len = len(candidate)
        if abs(cand_len - value_len) > 1 and abs(cand_len - norm_len) > 1:
            continue
        if candidate == value:
            continue
        d_raw = _levenshtein(value, value_len, candidate, cand_len)
        if d_raw <= 1:
            best = candidate
            break
        d_norm = _levenshtein(norm, norm_len, _normalise(candidate), cand_len)
        if d_norm <= 1:
            best = candidate
            break
    return best


def warn_if_near_miss_entity_type(value: str) -> None:
    """Log a warning if *value* looks like a typo of a well-known type.

    Silent for exact matches (canonical, alias, or legacy enum value)
    and for values with no near-miss in the well-known set (open-string
    behaviour preserved).
    """
    if value in _KNOWN_ENTITY_TYPES:
        return
    suggestion = _closest_match(value, _KNOWN_ENTITY_TYPES)
    if suggestion is None:
        return
    logger.warning(
        "entity_type.suspicious_input",
        value=value,
        suggestion=suggestion,
        message=(
            f"entity_type {value!r} looks like a typo of well-known "
            f"{suggestion!r}; value preserved (open-string contract)"
        ),
    )


def warn_if_near_miss_edge_kind(value: str) -> None:
    """Log a warning if *value* looks like a typo of a well-known edge kind.

    Same contract as :func:`warn_if_near_miss_entity_type`.
    """
    if value in _KNOWN_EDGE_KINDS:
        return
    suggestion = _closest_match(value, _KNOWN_EDGE_KINDS)
    if suggestion is None:
        return
    logger.warning(
        "edge_kind.suspicious_input",
        value=value,
        suggestion=suggestion,
        message=(
            f"edge_kind {value!r} looks like a typo of well-known "
            f"{suggestion!r}; value preserved (open-string contract)"
        ),
    )
